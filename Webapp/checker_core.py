"""
checker_core.py
----------------
Pure logic layer for the Invoice Compliance Checker, ported from the
original CLI script (invoice_compliance_checker.py). No print()/input()
calls live here -- every function takes explicit arguments and returns
plain dict/list data so it can be driven by the Streamlit UI (or any
other front end, or tests) instead of a terminal.

Supported input formats:
    - Text-based PDF     (.pdf with a real text layer)
    - Excel               (.xlsx / .xls)
    - Scanned/image PDF   (.pdf with no extractable text) -- needs AI vision
    - Photos/scans        (.jpg / .jpeg / .png)            -- needs AI vision

Two check modes:
    - Invoice Compliance Check  (check_invoice): field presence, per-line
      math validation, grand-total reconciliation, optional HS-6 tariff
      classification (USITC keyword search and/or AI/GRI reasoning).
    - Supporting Document Audit (run_audit): presence-only check against a
      user-supplied list of mandatory fields, for non-invoice documents.
"""

import os
import re
import json
import base64
import time

import requests
import pypdf
import openpyxl

try:
    import xlrd
except ImportError:
    xlrd = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_TABLE_FIELDS = ["unit_price", "total_price"]
REQUIRED_DOCUMENT_FIELDS = ["order_number", "supplier_address", "payment_terms", "invoice_date"]

DOCUMENT_FIELD_LABELS = {
    "order_number": "Order Number",
    "supplier_address": "Supplier Address",
    "payment_terms": "Payment Terms",
    "invoice_date": "Date",
}

DOCUMENT_FIELD_PATTERNS = {
    "order_number": r"(?:Order\s*(?:No\.?|Number|#)|P\.?O\.?\s*(?:No\.?|Number|#))\s*[:\-]?\s*(\S+)",
    "payment_terms": r"Payment\s*Terms?\s*[:\-]?\s*(.+)",
    "invoice_date": r"(?:Invoice\s*Date|Date)\s*[:\-]?\s*([A-Za-z0-9,/\-\. ]+?)(?:\n|$)",
    "supplier_address": r"(?:Supplier|Vendor)(?:'s)?\s*Address\s*[:\-]?\s*(.+)",
}

FIELD_ALIASES = {
    "part_number": ["part number", "part no", "part #", "sku", "item code"],
    "part_description": ["part description", "description", "item", "part name"],
    "quantity": ["qty", "quantity"],
    "unit_price": ["unit price", "price", "rate"],
    "total_price": ["total price", "total amount", "amount", "line total", "total"],
}

TOLERANCE = 0.02

USITC_SEARCH_URL = "https://hts.usitc.gov/reststop/search"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-5"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-flash-latest"

SUPPORTED_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls")

GRI_SYSTEM_PROMPT = """You are an expert HS (Harmonized System) classification assistant.

Before assigning an HS code, follow this workflow:
1. Read the complete product description.
2. Identify: product type; primary function; material(s); end use; industry;
   and whether it is a raw material, a component/part, or a finished product.
3. Determine the product's essential character.
4. Apply the HS General Rules for Interpretation (GRIs) and Section/Chapter Notes.
5. Search and compare the most relevant standard HS headings (01-97), not just keyword matches.
6. Select the most specific valid 6-digit HS code that best describes the product.
7. Verify the HS description matches the product. Reject unrelated or Chapter 98/99 codes.
8. If the description is ambiguous or insufficient, do not guess -- say so instead of
   picking arbitrarily, and specify what information is missing.

Respond with ONLY a single JSON object, no markdown formatting, no commentary before or
after, with exactly these keys:
{
  "hs6": "6-digit code as XXXX.XX, or null if insufficient information",
  "official_description": "the official HS heading/subheading text",
  "reason": "1-3 sentence justification referencing essential character and the relevant GRI/chapter note",
  "confidence_label": "High, Medium, or Low",
  "confidence_percent": integer from 0 to 100,
  "insufficient_info": true or false,
  "clarifying_question": "only set if insufficient_info is true, else null"
}"""

_STOPWORDS = {
    "of", "and", "or", "the", "a", "an", "in", "for", "to", "other",
    "with", "not", "by", "which", "type", "part", "parts", "thereof",
}
MIN_MATCH_CONFIDENCE = 0.34

_HTS_CACHE = {}
_AI_CACHE = {}
_GEMINI_CACHE = {}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def clean_number(value):
    if value is None:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def truncate(text, max_len):
    text = str(text)
    return text if len(text) <= max_len else text[:max_len - 3].rstrip() + "..."


def _tokenize(text):
    words = re.findall(r"[a-z]+", (text or "").lower())
    tokens = set()
    for w in words:
        if w in _STOPWORDS or len(w) <= 2:
            continue
        tokens.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return tokens


def _post_with_retry(post_fn, max_retries=2, base_delay=3):
    resp = None
    for attempt in range(max_retries + 1):
        resp = post_fn()
        if resp.status_code != 429:
            return resp
        if attempt < max_retries:
            time.sleep(base_delay * (attempt + 1))
    return resp


def _parse_field_list(raw):
    return [f.strip() for f in raw.split(",") if f.strip()]


# ---------------------------------------------------------------------------
# Field / table detection
# ---------------------------------------------------------------------------

def match_field_alias(line):
    if not line:
        return None
    line_lower = line.strip().lower()
    field_order = ["total_price", "quantity", "unit_price", "part_number", "part_description"]
    for field in field_order:
        if field == "unit_price" and "total" in line_lower:
            continue
        if any(alias == line_lower or alias in line_lower for alias in FIELD_ALIASES[field]):
            return field
    return None


def extract_line_items(lines):
    header_start = header_fields = header_end = None
    for i in range(len(lines)):
        first = match_field_alias(lines[i])
        if not first:
            continue
        run = []
        j = i
        while j < len(lines) and match_field_alias(lines[j]):
            run.append(match_field_alias(lines[j]))
            j += 1
        if len(run) >= 2:
            header_start, header_fields, header_end = i, run, j
            break

    if header_start is None:
        return [], []

    ncols = len(header_fields)
    rows = []
    idx = header_end
    while idx + ncols <= len(lines):
        chunk = lines[idx:idx + ncols]
        if any("grand total" in c.lower() or "subtotal" in c.lower() for c in chunk):
            break
        rows.append(dict(zip(header_fields, chunk)))
        idx += ncols

    return header_fields, rows


def check_document_fields(text):
    found = {}
    for field, pattern in DOCUMENT_FIELD_PATTERNS.items():
        match = re.search(pattern, text, re.IGNORECASE)
        found[field] = match.group(1).strip() if match else None
    return found


def find_grand_total(text):
    match = re.search(r"grand\s*total\s*[:\-]?\s*\$?([\d,]+\.?\d*)", text, re.IGNORECASE)
    if match:
        return clean_number(match.group(1))
    return None


# ---------------------------------------------------------------------------
# File loaders (operate on file-like paths already saved to disk)
# ---------------------------------------------------------------------------

def load_pdf_text(path):
    reader = pypdf.PdfReader(path)
    full_text = "\n".join((page.extract_text() or "") for page in reader.pages)
    lines = [ln.strip() for ln in full_text.split("\n") if ln.strip()]
    header_fields, table_rows = extract_line_items(lines)
    likely_scanned = len(full_text.strip()) < 30 or not table_rows
    return {
        "full_text": full_text,
        "table_rows": table_rows,
        "grand_total": find_grand_total(full_text),
        "likely_scanned": likely_scanned,
    }


def _excel_row_to_text(row):
    non_empty = [str(c) for c in row if c is not None and str(c).strip() != ""]
    if len(non_empty) == 2:
        label = non_empty[0].rstrip(":-").strip()
        return f"{label}: {non_empty[1]}"
    return " ".join(non_empty)


def extract_excel_line_items(rows):
    header_map = {}
    header_row_index = None
    for i, row in enumerate(rows):
        col_map = {}
        for idx, cell in enumerate(row):
            field = match_field_alias(str(cell)) if cell is not None else None
            if field:
                col_map[idx] = field
        if len(col_map) >= 2:
            header_map = col_map
            header_row_index = i
            break

    if header_row_index is None:
        return []

    table_rows = []
    for row in rows[header_row_index + 1:]:
        row_text = " ".join(str(c) for c in row if c is not None).lower()
        if "grand total" in row_text or "subtotal" in row_text:
            break
        item = {field: row[idx] for idx, field in header_map.items()
                 if idx < len(row) and row[idx] is not None}
        if item:
            table_rows.append(item)
    return table_rows


def load_excel(path):
    ext = path.lower().rsplit(".", 1)[-1]
    rows = []
    if ext == "xlsx":
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        rows = [[cell for cell in row] for row in ws.iter_rows(values_only=True)]
    elif ext == "xls":
        if xlrd is None:
            return {"error": "xlrd is not installed -- cannot read legacy .xls files"}
        book = xlrd.open_workbook(path)
        sheet = book.sheet_by_index(0)
        rows = [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]
    else:
        return {"error": f"Unsupported spreadsheet type: .{ext}"}

    full_text = "\n".join(_excel_row_to_text(r) for r in rows)
    return {
        "full_text": full_text,
        "table_rows": extract_excel_line_items(rows),
        "grand_total": find_grand_total(full_text),
        "likely_scanned": False,
    }


# ---------------------------------------------------------------------------
# HTS / HS-6 lookups
# ---------------------------------------------------------------------------

def lookup_hts(description):
    if not description:
        return {"error": "No part description to search"}

    key = description.strip().lower()
    if key in _HTS_CACHE:
        return _HTS_CACHE[key]

    result = {"htsno": None, "match_description": None, "confidence": None,
               "low_reliability": False, "error": None}
    query_tokens = _tokenize(description)

    try:
        resp = requests.get(USITC_SEARCH_URL, params={"keyword": description}, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        candidates = data if isinstance(data, list) else []

        scored = []
        for item in candidates:
            htsno = (item.get("htsno") or "").strip()
            if not re.search(r"\d{4}\.\d{2}", htsno):
                continue
            desc_text = (item.get("description") or "").strip()
            cand_tokens = _tokenize(desc_text)
            if not query_tokens or not cand_tokens:
                continue
            overlap_count = len(query_tokens & cand_tokens)
            overlap_ratio = overlap_count / len(query_tokens)
            scored.append((overlap_ratio, overlap_count, htsno, desc_text))

        if scored:
            scored.sort(key=lambda t: t[0], reverse=True)
            best_ratio, best_count, best_htsno, best_desc = scored[0]
            if best_ratio >= MIN_MATCH_CONFIDENCE:
                result["htsno"] = best_htsno
                result["match_description"] = best_desc
                result["confidence"] = round(best_ratio * 100)
                result["low_reliability"] = len(query_tokens) < 3 or best_count < 2
            else:
                result["error"] = (
                    f"No confident match (best guess was only {round(best_ratio*100)}% "
                    f"word overlap) -- classify this one manually"
                )
        else:
            result["error"] = "No matching HTS entry found for this description"
    except Exception as exc:
        result["error"] = f"HTS lookup failed ({exc})"

    _HTS_CACHE[key] = result
    return result


def split_hs6(htsno):
    digits = re.sub(r"\D", "", htsno or "")
    if len(digits) < 6:
        return None, None
    hs6 = f"{digits[0:4]}.{digits[4:6]}"
    remainder_len = len(digits) - 6
    dash_groups = ["--"] * ((remainder_len + 1) // 2)
    suffix_placeholder = ".".join(dash_groups) if dash_groups else ""
    return hs6, suffix_placeholder


def classify_hts_ai(description, api_key):
    if not description:
        return {"error": "No part description to classify"}

    key = description.strip().lower()
    if key in _AI_CACHE:
        return _AI_CACHE[key]

    result = {
        "hs6": None, "official_description": None, "reason": None,
        "confidence_label": None, "confidence_percent": None,
        "insufficient_info": False, "clarifying_question": None, "error": None,
    }

    try:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 500,
            "system": GRI_SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": f"Classify this product for HS purposes:\n\n{description}\n\n"
                           f"Respond with ONLY the JSON object described in your instructions.",
            }],
        }
        resp = _post_with_retry(lambda: requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=30))
        if resp.status_code == 401:
            result["error"] = "Invalid API key (401 Unauthorized) -- check the key and try again"
            _AI_CACHE[key] = result
            return result
        if resp.status_code == 429:
            result["error"] = "Claude API rate limit hit (429) -- still limited after retries, try again shortly"
            _AI_CACHE[key] = result
            return result
        resp.raise_for_status()
        data = resp.json()
        text = "".join(block.get("text", "") for block in data.get("content", [])
                        if block.get("type") == "text")
        cleaned = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(cleaned)
        result.update({k: parsed.get(k) for k in result if k in parsed})
    except Exception as exc:
        result["error"] = f"AI classification failed ({exc})"

    _AI_CACHE[key] = result
    return result


def classify_hts_gemini(description, api_key):
    if not description:
        return {"error": "No part description to classify"}

    key = description.strip().lower()
    if key in _GEMINI_CACHE:
        return _GEMINI_CACHE[key]

    result = {
        "hs6": None, "official_description": None, "reason": None,
        "confidence_label": None, "confidence_percent": None,
        "insufficient_info": False, "clarifying_question": None, "error": None,
    }

    try:
        url = GEMINI_API_URL.format(model=GEMINI_MODEL)
        body = {
            "system_instruction": {"parts": [{"text": GRI_SYSTEM_PROMPT}]},
            "contents": [{"parts": [{
                "text": f"Classify this product for HS purposes:\n\n{description}\n\n"
                        f"Respond with ONLY the JSON object described in your instructions.",
            }]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        resp = _post_with_retry(lambda: requests.post(url, params={"key": api_key}, json=body, timeout=30))
        if resp.status_code in (400, 403):
            result["error"] = f"Invalid or unauthorized API key ({resp.status_code}) -- check the key and try again"
            _GEMINI_CACHE[key] = result
            return result
        if resp.status_code == 429:
            result["error"] = "Gemini rate limit hit (429) -- still limited after retries, try again shortly"
            _GEMINI_CACHE[key] = result
            return result
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(cleaned)
        result.update({k: parsed.get(k) for k in result if k in parsed})
    except Exception as exc:
        result["error"] = f"AI classification failed ({exc})"

    _GEMINI_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# Custom mandatory field checks (audit mode / extra invoice fields)
# ---------------------------------------------------------------------------

def check_custom_fields_regex(full_text, custom_fields):
    results = {}
    for field in custom_fields:
        pattern = re.escape(field.strip()) + r"\s*[:\-]?\s*(.+)"
        match = re.search(pattern, full_text, re.IGNORECASE)
        results[field] = match.group(1).strip() if match else None
    return results


def ask_ai_json(prompt, provider, api_key):
    try:
        if provider == "gemini":
            url = GEMINI_API_URL.format(model=GEMINI_MODEL)
            body = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            }
            resp = _post_with_retry(lambda: requests.post(url, params={"key": api_key}, json=body, timeout=30))
            if resp.status_code in (400, 403):
                return {"error": f"Invalid or unauthorized Gemini API key ({resp.status_code})"}
            if resp.status_code == 429:
                return {"error": "Gemini rate limit hit (429) -- still limited after retries, try again shortly"}
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        else:
            headers = {
                "x-api-key": api_key, "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {"model": ANTHROPIC_MODEL, "max_tokens": 800,
                    "messages": [{"role": "user", "content": prompt}]}
            resp = _post_with_retry(lambda: requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=30))
            if resp.status_code == 401:
                return {"error": "Invalid Anthropic API key (401 Unauthorized)"}
            if resp.status_code == 429:
                return {"error": "Claude API rate limit hit (429) -- still limited after retries, try again shortly"}
            resp.raise_for_status()
            data = resp.json()
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        cleaned = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return json.loads(cleaned)
    except Exception as exc:
        return {"error": f"AI request failed ({exc})"}


def check_custom_fields_ai_text(full_text, custom_fields, provider, api_key):
    fields_list = "\n".join(f"- {f}" for f in custom_fields)
    prompt = (
        f"Given this document text, check whether each listed field/detail is present, "
        f"and if so extract its value.\n\nDocument text:\n{full_text[:8000]}\n\n"
        f"Fields to check:\n{fields_list}\n\n"
        f"Return ONLY a JSON object where each key is EXACTLY one of the field names "
        f"listed above, and the value is the extracted text (as a string) if present, "
        f"or null if that field is not present anywhere in the document."
    )
    result = ask_ai_json(prompt, provider, api_key)
    if result.get("error"):
        return {}, result["error"]
    return {f: result.get(f) for f in custom_fields}, None


def build_vision_prompt(custom_fields=None):
    prompt = """Extract structured data from this invoice.

Return ONLY a JSON object (no markdown, no commentary) with exactly these keys:
{
  "order_number": "value or null",
  "supplier_address": "value or null",
  "payment_terms": "value or null",
  "invoice_date": "value or null",
  "grand_total": number or null,
  "line_items": [
    {"part_number": "value or null", "part_description": "value or null",
     "quantity": number or null, "unit_price": number or null, "total_price": number or null}
  ]"""
    if custom_fields:
        fields_json = ",\n    ".join(f'"{f}": "value or null"' for f in custom_fields)
        prompt += f''',
  "custom_fields": {{
    {fields_json}
  }}'''
    prompt += """
}

Use null for anything not visibly present. Do not guess or invent values that
aren't actually shown in the document."""
    return prompt


def extract_invoice_via_ai_vision(path, provider, api_key, custom_fields=None):
    ext = path.lower().rsplit(".", 1)[-1]
    mime = {
        "pdf": "application/pdf", "jpg": "image/jpeg",
        "jpeg": "image/jpeg", "png": "image/png",
    }.get(ext, "application/octet-stream")
    prompt = build_vision_prompt(custom_fields)

    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    try:
        if provider == "gemini":
            url = GEMINI_API_URL.format(model=GEMINI_MODEL)
            body = {
                "contents": [{"parts": [
                    {"inline_data": {"mime_type": mime, "data": b64}},
                    {"text": prompt},
                ]}],
                "generationConfig": {"responseMimeType": "application/json"},
            }
            resp = _post_with_retry(lambda: requests.post(url, params={"key": api_key}, json=body, timeout=60))
            if resp.status_code in (400, 403):
                return {"error": f"Invalid or unauthorized Gemini API key ({resp.status_code})"}
            if resp.status_code == 429:
                return {"error": "Gemini rate limit hit (429) -- still limited after retries, try again shortly"}
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        else:
            block_type = "document" if mime == "application/pdf" else "image"
            headers = {
                "x-api-key": api_key, "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": ANTHROPIC_MODEL,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": [
                    {"type": block_type, "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": prompt},
                ]}],
            }
            resp = _post_with_retry(lambda: requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=60))
            if resp.status_code == 401:
                return {"error": "Invalid Anthropic API key (401 Unauthorized)"}
            if resp.status_code == 429:
                return {"error": "Claude API rate limit hit (429) -- still limited after retries, try again shortly"}
            resp.raise_for_status()
            data = resp.json()
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

        cleaned = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return json.loads(cleaned)
    except Exception as exc:
        return {"error": f"AI vision extraction failed ({exc})"}


# ---------------------------------------------------------------------------
# Supporting Document Audit mode
# ---------------------------------------------------------------------------

def run_audit(path, custom_fields, ai_provider=None, api_key=None):
    report = {
        "file": os.path.basename(path),
        "fields_requested": list(custom_fields),
        "fields_checked": {},
        "missing_fields": set(),
        "unverified_fields": set(),
        "load_error": None,
        "status": "PASS",
    }

    ext = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    full_text = None
    vision_custom = None
    fields_found = {}

    if ext == "pdf":
        pdf_data = load_pdf_text(path)
        if pdf_data["likely_scanned"]:
            if not ai_provider:
                report["status"] = "FAIL"
                report["load_error"] = "Scanned PDF -- needs Gemini or Claude AI mode to read."
                return report
            vision = extract_invoice_via_ai_vision(path, ai_provider, api_key, custom_fields=custom_fields)
            if vision.get("error"):
                report["status"] = "FAIL"
                report["load_error"] = vision["error"]
                return report
            vision_custom = vision.get("custom_fields") or {}
        else:
            full_text = pdf_data["full_text"]
    elif ext in ("jpg", "jpeg", "png"):
        if not ai_provider:
            report["status"] = "FAIL"
            report["load_error"] = "Image files need Gemini or Claude AI mode to read."
            return report
        vision = extract_invoice_via_ai_vision(path, ai_provider, api_key, custom_fields=custom_fields)
        if vision.get("error"):
            report["status"] = "FAIL"
            report["load_error"] = vision["error"]
            return report
        vision_custom = vision.get("custom_fields") or {}
    elif ext in ("xlsx", "xls"):
        excel_data = load_excel(path)
        if excel_data.get("error"):
            report["status"] = "FAIL"
            report["load_error"] = excel_data["error"]
            return report
        full_text = excel_data["full_text"]
    else:
        report["status"] = "FAIL"
        report["load_error"] = f"Unsupported file type: .{ext or '(none)'}"
        return report

    if vision_custom is not None:
        fields_found = {f: vision_custom.get(f) for f in custom_fields}
    elif ai_provider:
        fields_found, err = check_custom_fields_ai_text(full_text, custom_fields, ai_provider, api_key)
        if err:
            report["load_error"] = f"AI field check issue: {err}"
    else:
        fields_found = check_custom_fields_regex(full_text, custom_fields)

    report["fields_checked"] = fields_found
    report["missing_fields"] = {f for f in custom_fields if f in fields_found and not fields_found[f]}
    report["unverified_fields"] = {f for f in custom_fields if f not in fields_found}
    if report["missing_fields"]:
        report["status"] = "FAIL"
    elif report["unverified_fields"]:
        report["status"] = "INCOMPLETE"
    return report


# ---------------------------------------------------------------------------
# Invoice Compliance Check mode
# ---------------------------------------------------------------------------

def check_invoice(path, hts_lookup=False, ai_provider=None, api_key=None, custom_fields=None):
    custom_fields = custom_fields or []
    report = {
        "file": os.path.basename(path),
        "missing_table_fields": set(),
        "missing_document_fields": set(),
        "document_fields_found": {},
        "line_items": [],
        "line_math_errors": [],
        "unverified_line_totals": [],
        "grand_total_stated": None,
        "grand_total_computed": None,
        "grand_total_recalculated": None,
        "grand_total_sum_of_stated_lines": None,
        "grand_total_mismatch": False,
        "extraction_method": None,
        "load_error": None,
        "custom_fields_found": {},
        "custom_fields_requested": list(custom_fields),
        "missing_custom_fields": set(),
        "unverified_custom_fields": set(),
        "custom_fields_error": None,
        "status": "PASS",
    }

    ext = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    doc_fields = {}
    table_rows = []
    full_text_for_custom_check = None

    if ext == "pdf":
        pdf_data = load_pdf_text(path)
        if pdf_data["likely_scanned"]:
            if not ai_provider:
                report["status"] = "FAIL"
                report["load_error"] = (
                    "This looks like a scanned/image-based PDF with no extractable text. "
                    "Re-run with Gemini or Claude AI mode to process it -- plain text "
                    "extraction can't read scanned documents."
                )
                return report
            vision = extract_invoice_via_ai_vision(path, ai_provider, api_key, custom_fields=custom_fields)
            if vision.get("error"):
                report["status"] = "FAIL"
                report["load_error"] = vision["error"]
                return report
            doc_fields = {f: vision.get(f) for f in REQUIRED_DOCUMENT_FIELDS}
            table_rows = vision.get("line_items") or []
            report["grand_total_stated"] = clean_number(vision.get("grand_total"))
            report["extraction_method"] = "AI vision (scanned PDF)"
            if custom_fields:
                vision_custom = vision.get("custom_fields") or {}
                report["custom_fields_found"] = {f: vision_custom.get(f) for f in custom_fields}
        else:
            doc_fields = check_document_fields(pdf_data["full_text"])
            table_rows = pdf_data["table_rows"]
            report["grand_total_stated"] = pdf_data["grand_total"]
            report["extraction_method"] = "text-based PDF"
            full_text_for_custom_check = pdf_data["full_text"]

    elif ext in ("jpg", "jpeg", "png"):
        if not ai_provider:
            report["status"] = "FAIL"
            report["load_error"] = (
                "Image files have no embedded text and need AI vision to read -- "
                "re-run with Gemini or Claude AI mode."
            )
            return report
        vision = extract_invoice_via_ai_vision(path, ai_provider, api_key, custom_fields=custom_fields)
        if vision.get("error"):
            report["status"] = "FAIL"
            report["load_error"] = vision["error"]
            return report
        doc_fields = {f: vision.get(f) for f in REQUIRED_DOCUMENT_FIELDS}
        table_rows = vision.get("line_items") or []
        report["grand_total_stated"] = clean_number(vision.get("grand_total"))
        report["extraction_method"] = "AI vision (image)"
        if custom_fields:
            vision_custom = vision.get("custom_fields") or {}
            report["custom_fields_found"] = {f: vision_custom.get(f) for f in custom_fields}

    elif ext in ("xlsx", "xls"):
        excel_data = load_excel(path)
        if excel_data.get("error"):
            report["status"] = "FAIL"
            report["load_error"] = excel_data["error"]
            return report
        doc_fields = check_document_fields(excel_data["full_text"])
        table_rows = excel_data["table_rows"]
        report["grand_total_stated"] = excel_data["grand_total"]
        report["extraction_method"] = "Excel"
        full_text_for_custom_check = excel_data["full_text"]

    else:
        report["status"] = "FAIL"
        report["load_error"] = f"Unsupported file type: .{ext or '(none)'}"
        return report

    if custom_fields and full_text_for_custom_check is not None:
        if ai_provider:
            found, err = check_custom_fields_ai_text(full_text_for_custom_check, custom_fields, ai_provider, api_key)
            report["custom_fields_error"] = err
        else:
            found = check_custom_fields_regex(full_text_for_custom_check, custom_fields)
        report["custom_fields_found"] = found

    if custom_fields:
        for f in custom_fields:
            if f in report["custom_fields_found"]:
                if not report["custom_fields_found"][f]:
                    report["missing_custom_fields"].add(f)
            else:
                report["unverified_custom_fields"].add(f)

    report["document_fields_found"] = doc_fields
    for field in REQUIRED_DOCUMENT_FIELDS:
        if not doc_fields.get(field):
            report["missing_document_fields"].add(field)

    for field in REQUIRED_TABLE_FIELDS:
        if not any(row.get(field) is not None for row in table_rows):
            report["missing_table_fields"].add(field)

    if not table_rows:
        report["status"] = "FAIL"
        if not report["missing_table_fields"]:
            report["missing_table_fields"] = set(REQUIRED_TABLE_FIELDS)
        return report

    running_total_recalculated = 0.0
    running_total_as_stated = 0.0
    for item in table_rows:
        desc = item.get("part_description")
        part_number = item.get("part_number")
        qty = clean_number(item.get("quantity"))
        unit_price = clean_number(item.get("unit_price"))
        total_price = clean_number(item.get("total_price"))

        line_result = {
            "description": desc,
            "part_number": part_number,
            "qty": qty,
            "unit_price": unit_price,
            "total_price": total_price,
            "math_ok": None,
            "hs6": None,
            "country_suffix_placeholder": None,
            "hts_match_description": None,
            "hts_confidence": None,
            "hts_low_reliability": False,
            "hts_error": None,
            "ai_hs6": None,
            "ai_official_description": None,
            "ai_reason": None,
            "ai_confidence_label": None,
            "ai_confidence_percent": None,
            "ai_clarifying_question": None,
            "ai_error": None,
        }

        if ai_provider:
            if ai_provider == "gemini":
                ai_result = classify_hts_gemini(desc, api_key)
            else:
                ai_result = classify_hts_ai(desc, api_key)
            if ai_result.get("error"):
                line_result["ai_error"] = ai_result["error"]
            elif ai_result.get("insufficient_info"):
                line_result["ai_error"] = (
                    f"AI needs more info: {ai_result.get('clarifying_question') or 'description is ambiguous'}"
                )
            else:
                line_result["ai_hs6"] = ai_result.get("hs6")
                line_result["ai_official_description"] = ai_result.get("official_description")
                line_result["ai_reason"] = ai_result.get("reason")
                line_result["ai_confidence_label"] = ai_result.get("confidence_label")
                line_result["ai_confidence_percent"] = ai_result.get("confidence_percent")

        if hts_lookup or ai_provider:
            lookup = lookup_hts(desc)
            if lookup.get("error"):
                line_result["hts_error"] = lookup["error"]
            else:
                hs6, suffix = split_hs6(lookup["htsno"])
                line_result["hs6"] = hs6
                line_result["country_suffix_placeholder"] = suffix
                line_result["hts_match_description"] = lookup["match_description"]
                line_result["hts_confidence"] = lookup["confidence"]
                line_result["hts_low_reliability"] = lookup.get("low_reliability", False)

        if qty is not None and unit_price is not None and total_price is not None:
            expected = round(qty * unit_price, 2)
            diff = round(abs(expected - total_price), 2)
            line_result["math_ok"] = diff <= TOLERANCE
            if not line_result["math_ok"]:
                report["line_math_errors"].append({
                    "description": desc,
                    "expected_total": expected,
                    "stated_total": total_price,
                    "diff": diff,
                })
            running_total_recalculated += expected
            running_total_as_stated += total_price
        elif total_price is not None:
            running_total_recalculated += total_price
            running_total_as_stated += total_price
            report["unverified_line_totals"].append(desc)

        report["line_items"].append(line_result)

    report["grand_total_recalculated"] = round(running_total_recalculated, 2)
    report["grand_total_sum_of_stated_lines"] = round(running_total_as_stated, 2)
    report["grand_total_computed"] = report["grand_total_recalculated"]
    if report["grand_total_stated"] is not None:
        diff = round(abs(report["grand_total_stated"] - report["grand_total_recalculated"]), 2)
        report["grand_total_mismatch"] = diff > TOLERANCE

    if (report["missing_table_fields"] or report["missing_document_fields"]
            or report["line_math_errors"] or report["grand_total_mismatch"]
            or report["missing_custom_fields"]):
        report["status"] = "FAIL"
    elif report["unverified_custom_fields"]:
        report["status"] = "INCOMPLETE"

    return report


# ---------------------------------------------------------------------------
# One-line reason summaries (used in tables / lists)
# ---------------------------------------------------------------------------

def summarize_invoice_reason(report):
    if report.get("load_error"):
        return truncate(report["load_error"], 90)
    if report["status"] == "PASS":
        return "All checks passed"
    reasons = []
    if report["missing_document_fields"]:
        labels = [DOCUMENT_FIELD_LABELS[f] for f in report["missing_document_fields"]]
        reasons.append(f"Missing: {', '.join(labels)}")
    if report["missing_table_fields"]:
        reasons.append(f"Missing table field(s): {', '.join(report['missing_table_fields'])}")
    if report["line_math_errors"]:
        n = len(report["line_math_errors"])
        reasons.append(f"Math error in {n} line{'s' if n != 1 else ''}")
    if report["grand_total_mismatch"]:
        reasons.append("Grand total mismatch")
    if report["missing_custom_fields"]:
        reasons.append(f"Missing custom field(s): {', '.join(sorted(report['missing_custom_fields']))}")
    if report["status"] == "INCOMPLETE" and report["unverified_custom_fields"]:
        reasons.append(f"Could not verify: {', '.join(sorted(report['unverified_custom_fields']))}")
    return "; ".join(reasons) if reasons else "Unknown"


def summarize_audit_reason(report):
    if report.get("load_error") and not report["fields_checked"]:
        return truncate(report["load_error"], 90)
    if report["status"] == "PASS":
        return "All fields present"
    reasons = []
    if report["missing_fields"]:
        reasons.append(f"Missing: {', '.join(sorted(report['missing_fields']))}")
    if report["unverified_fields"]:
        reasons.append(f"Could not verify: {', '.join(sorted(report['unverified_fields']))}")
    return "; ".join(reasons) if reasons else "Unknown"
