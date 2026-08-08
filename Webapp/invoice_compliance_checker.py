"""
Invoice Compliance Checker
---------------------------
Reads invoices in multiple formats and checks two categories of required fields:

  Table-level (checked per line item, with math validation):
    - Unit Price
    - Total Price   (validated as Qty x Unit Price = Total Price per line,
                     and sum of lines = invoice grand total)

  Document-level (checked for presence anywhere in the invoice):
    - Order Number
    - Supplier Address
    - Payment Terms
    - Date

Supported input formats:
    - Text-based PDF     (.pdf with a real text layer)      -- free, no AI needed
    - Excel               (.xlsx / .xls)                     -- free, no AI needed
    - Scanned/image PDF   (.pdf with no extractable text)    -- REQUIRES Gemini or Claude mode
    - Photos/scans        (.jpg / .jpeg / .png)              -- REQUIRES Gemini or Claude mode

Scanned PDFs and images have no embedded text, so they need OCR. Rather than a
local OCR library (pytesseract needs a compiled Tesseract binary that can't be
installed via pip on Android -- same class of problem as pdfplumber's pypdfium2
dependency), this script sends the scanned page/image directly to Claude's or
Gemini's vision capability and asks it to extract the invoice structure as JSON.
That reuses infrastructure already in this script and needs no new incompatible
dependency, at the cost of requiring an AI provider for those two formats.

Optionally (--hts-lookup / --gemini / --ai-classify), each line's Part
Description is also classified for an HS-6 tariff code suggestion -- see the
README for the three modes and their tradeoffs.

Usage:
    python invoice_compliance_checker.py path/to/invoice.pdf
    python invoice_compliance_checker.py path/to/invoice.xlsx
    python invoice_compliance_checker.py path/to/scanned_invoice.pdf --gemini
    python invoice_compliance_checker.py path/to/invoice_photo.jpg --gemini
    python invoice_compliance_checker.py path/to/folder_of_invoices/ --gemini

Requires: pypdf, requests, openpyxl, xlrd (all pure-Python-friendly -- install
cleanly on Android/Pydroid, unlike libraries needing compiled/native extensions)
    pip install pypdf requests openpyxl xlrd --break-system-packages
    (On Pydroid: this script will attempt to auto-install these itself if missing)

Note on the PDF engine: this originally used pdfplumber, but pdfplumber hard-depends
on pypdfium2, which has no prebuilt Android wheel and cannot be built on-device
(needs git + a C toolchain). pypdf is pure Python and installs cleanly everywhere,
including Pydroid.
"""

import sys
import os
import re
import base64
import json
import subprocess


def _ensure_package(module_name, pip_name=None):
    """Import a module, installing it via pip first if missing.
    Uses sys.executable so the install always targets the SAME interpreter
    that is running this script -- this avoids the common Pydroid issue
    where the Pip menu installs into a different environment than 'Run'."""
    pip_name = pip_name or module_name
    try:
        return __import__(module_name)
    except ImportError:
        print(f"'{module_name}' not found -- installing it now, please wait...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
        return __import__(module_name)


# Table columns that MUST be present, with math validation applied
REQUIRED_TABLE_FIELDS = ["unit_price", "total_price"]

# Document-wide fields that MUST be present somewhere in the invoice (presence only)
REQUIRED_DOCUMENT_FIELDS = ["order_number", "supplier_address", "payment_terms", "invoice_date"]

DOCUMENT_FIELD_LABELS = {
    "order_number": "Order Number",
    "supplier_address": "Supplier Address",
    "payment_terms": "Payment Terms",
    "invoice_date": "Date",
}

# Regex patterns used to detect each document-level field in the invoice's full text.
# Each pattern should capture the value in group(1) if found.
DOCUMENT_FIELD_PATTERNS = {
    "order_number": r"(?:Order\s*(?:No\.?|Number|#)|P\.?O\.?\s*(?:No\.?|Number|#))\s*[:\-]?\s*(\S+)",
    "payment_terms": r"Payment\s*Terms?\s*[:\-]?\s*(.+)",
    "invoice_date": r"(?:Invoice\s*Date|Date)\s*[:\-]?\s*([A-Za-z0-9,/\-\. ]+?)(?:\n|$)",
    "supplier_address": r"(?:Supplier|Vendor)(?:'s)?\s*Address\s*[:\-]?\s*(.+)",
}

# Header keywords we accept as aliases for each line-item table column
FIELD_ALIASES = {
    "part_number": ["part number", "part no", "part #", "sku", "item code"],
    "part_description": ["part description", "description", "item", "part name"],
    "quantity": ["qty", "quantity"],
    "unit_price": ["unit price", "price", "rate"],
    "total_price": ["total price", "total amount", "amount", "line total", "total"],
}

TOLERANCE = 0.02  # allowed rounding difference in currency amounts

USITC_SEARCH_URL = "https://hts.usitc.gov/reststop/search"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-5"
_HTS_CACHE = {}  # avoid repeat lookups for the same description within one run
_AI_CACHE = {}

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


def get_anthropic_api_key():
    """Look for an API key in the environment, then a local cache file next to
    this script, then prompt for it interactively (personal-use convenience --
    NOT recommended for shared/production environments)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key

    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".anthropic_api_key")
    if os.path.isfile(key_file):
        with open(key_file) as f:
            saved = f.read().strip()
            if saved:
                return saved

    key = input("Enter your Anthropic API key (from console.anthropic.com): ").strip()
    if key:
        save = input("Save this key locally for next time? (y/N): ").strip().lower()
        if save in ("y", "yes"):
            with open(key_file, "w") as f:
                f.write(key)
            print(f"Saved to {key_file} -- this file is plain text, keep it private "
                  f"and don't upload it to GitHub (it's already in .gitignore).")
    return key


def classify_hts_ai(description, api_key):
    """Ask Claude to classify a part description using the full GRI workflow.
    Returns a dict with keys: hs6, official_description, reason,
    confidence_label, confidence_percent, insufficient_info,
    clarifying_question, error."""
    import json
    requests = _ensure_package("requests")

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


GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-flash-latest"  # Google-maintained alias, auto-points to their current Flash release
_GEMINI_CACHE = {}


def get_gemini_api_key():
    """Look for a Google AI Studio (Gemini) API key in the environment, then a
    local cache file, then prompt interactively. This is the free-tier option:
    sign in with just a Google account at aistudio.google.com, no credit card,
    no expiration -- unlike Anthropic's paid-after-trial-credit API."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key

    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gemini_api_key")
    if os.path.isfile(key_file):
        with open(key_file) as f:
            saved = f.read().strip()
            if saved:
                return saved

    print("Get a free key at aistudio.google.com -> sign in with Google -> 'Get API key'")
    key = input("Enter your Gemini API key: ").strip()
    if key:
        save = input("Save this key locally for next time? (y/N): ").strip().lower()
        if save in ("y", "yes"):
            with open(key_file, "w") as f:
                f.write(key)
            print(f"Saved to {key_file} -- this file is plain text, keep it private "
                  f"and don't upload it to GitHub (it's already in .gitignore).")
    return key


def classify_hts_gemini(description, api_key):
    """Ask Gemini (free tier) to classify a part description using the same
    GRI workflow as the Claude path. Returns the same dict shape as
    classify_hts_ai: hs6, official_description, reason, confidence_label,
    confidence_percent, insufficient_info, clarifying_question, error."""
    import json
    requests = _ensure_package("requests")

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


def match_field_alias(line):
    """Check whether a single text line matches one of our known column
    header aliases (e.g. 'Unit Price', 'Qty'). Returns the canonical field
    name, or None. Checked most-specific-first so 'Total Price' isn't
    caught by the generic 'price' alias meant for Unit Price."""
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
    """Find the invoice's line-item table in a flat list of text lines
    (pypdf emits each table cell as its own line) by locating a run of
    consecutive header-alias lines, then grouping the lines that follow
    into rows of that same width. Returns (header_fields, rows) where
    rows is a list of dicts keyed by canonical field name."""
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
        if len(run) >= 2:  # a real header needs at least 2 recognizable columns
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
            break  # reached the totals section, not another data row
        rows.append(dict(zip(header_fields, chunk)))
        idx += ncols

    return header_fields, rows


def check_document_fields(text):
    """Search the invoice's full text for each required document-level field.
    Returns dict of field -> matched value (or None if not found)."""
    found = {}
    for field, pattern in DOCUMENT_FIELD_PATTERNS.items():
        match = re.search(pattern, text, re.IGNORECASE)
        found[field] = match.group(1).strip() if match else None
    return found


# Common filler words to ignore when scoring how well a candidate HTS
# description matches the part description (these carry little meaning).
_STOPWORDS = {
    "of", "and", "or", "the", "a", "an", "in", "for", "to", "other",
    "with", "not", "by", "which", "type", "part", "parts", "thereof",
}

MIN_MATCH_CONFIDENCE = 0.34  # require at least ~1/3 of the description's words to overlap


def _tokenize(text):
    words = re.findall(r"[a-z]+", (text or "").lower())
    tokens = set()
    for w in words:
        if w in _STOPWORDS or len(w) <= 2:
            continue
        # Naive plural stripping so 'seals' matches 'seal', 'gaskets' matches
        # 'gasket', etc. -- meaningfully improves recall for word-overlap scoring.
        tokens.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return tokens


def lookup_hts(description):
    """Query the official USITC HTS API by keyword (the part description).

    USITC's API returns matches in ascending HTS-code order, NOT ranked by
    relevance -- so simply taking the first result is unreliable (e.g. a
    query for 'Seal Kit' can return an entry about actual seals, the marine
    mammal, because chapter 01 sorts first and happens to contain the word
    'seal'). To compensate, every returned candidate is scored by word
    overlap against the query, and only the best-scoring candidate is used
    -- and only if it clears a minimum confidence bar. Below that bar, no
    code is suggested at all, since a wrong suggestion is worse than none.

    Returns a dict with keys: htsno, match_description, confidence, error.
    Results are cached per description for this run."""
    requests = _ensure_package("requests")

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
                continue  # category header row with no actual code, skip
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
                # A high RATIO from very few words is not trustworthy -- e.g. a
                # 2-word query hitting both words in an unrelated tariff line is
                # common (shared vocabulary between raw materials, parts, and
                # finished goods). Flag it regardless of the percentage shown.
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
    """Split a full HTS number into the globally standardized HS-6 portion
    and the country-specific remainder. Returns (hs6, suffix_placeholder)."""
    digits = re.sub(r"\D", "", htsno or "")
    if len(digits) < 6:
        return None, None
    hs6 = f"{digits[0:4]}.{digits[4:6]}"
    remainder_len = len(digits) - 6
    # Show remainder as dashes grouped in pairs, matching typical XX.XX suffix formatting
    dash_groups = ["--"] * ((remainder_len + 1) // 2)
    suffix_placeholder = ".".join(dash_groups) if dash_groups else ""
    return hs6, suffix_placeholder


def clean_number(value):
    """Convert '$1,234.50' or '1234.50' style strings to float. Returns None if not parseable."""
    if value is None:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def find_grand_total(text):
    """Search free text for a 'Grand Total' style figure outside the table."""
    match = re.search(r"grand\s*total\s*[:\-]?\s*\$?([\d,]+\.?\d*)", text, re.IGNORECASE)
    if match:
        return clean_number(match.group(1))
    return None


# ---------------------------------------------------------------------------
# Format loaders. Each one returns a dict with keys:
#   full_text    -- flattened text for document-field regex matching
#   table_rows   -- list of dicts keyed by canonical field names
#                   (part_number, part_description, quantity, unit_price, total_price)
#   grand_total  -- resolved number or None
#   error        -- set instead of the above if loading failed
# ---------------------------------------------------------------------------

def load_pdf_text(path):
    """Try to read a PDF as a text-based document. If it comes back with
    (close to) no text or no recognizable line-item table, that's a strong
    signal it's a scanned/image-based PDF -- flagged via 'likely_scanned'
    so the caller can fall back to AI vision instead."""
    pypdf = _ensure_package("pypdf")
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
    """Turn one spreadsheet row into text for document-field regex matching.
    Excel invoices commonly put a label and its value in two adjacent cells
    (e.g. 'Order Number' | 'PO-12345') rather than one 'Label: value' cell,
    so a 2-cell row is joined as 'Label: value' to match that pattern. Labels
    that already end in ':' or '-' in the sheet have it stripped first, so we
    don't end up with a broken 'Label:: value' double separator."""
    non_empty = [str(c) for c in row if c is not None and str(c).strip() != ""]
    if len(non_empty) == 2:
        label = non_empty[0].rstrip(":-").strip()
        return f"{label}: {non_empty[1]}"
    return " ".join(non_empty)


def extract_excel_line_items(rows):
    """Find the header row (2+ recognized column labels, same alias matching
    used for PDF tables) in a 2D grid of cell values, then group the rows
    that follow into line items by actual column position."""
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
    """Read an .xlsx (via openpyxl) or legacy .xls (via xlrd) invoice."""
    ext = path.lower().rsplit(".", 1)[-1]
    rows = []
    if ext == "xlsx":
        openpyxl = _ensure_package("openpyxl")
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        rows = [[cell for cell in row] for row in ws.iter_rows(values_only=True)]
    elif ext == "xls":
        xlrd = _ensure_package("xlrd")
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


def _post_with_retry(post_fn, max_retries=2, base_delay=3):
    """Call post_fn() (a no-arg lambda performing a requests.post), retrying
    with backoff if the response is a 429 (rate limited). Free-tier API keys
    (especially Gemini's) can hit per-minute request caps when processing
    several files back to back -- this smooths over transient rate limits
    instead of failing the whole file on the first hit."""
    import time
    resp = None
    for attempt in range(max_retries + 1):
        resp = post_fn()
        if resp.status_code != 429:
            return resp
        if attempt < max_retries:
            wait = base_delay * (attempt + 1)
            print(f"  (rate limited, waiting {wait}s and retrying...)")
            time.sleep(wait)
    return resp


def ask_ai_json(prompt, provider, api_key):
    """Generic helper: send a plain text prompt to Gemini or Claude and parse
    a JSON object back. Used for custom mandatory-field checks against text
    that's already been extracted (no image/document upload needed)."""
    requests = _ensure_package("requests")
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
        else:  # anthropic
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


def check_custom_fields_regex(full_text, custom_fields):
    """Free fallback: generic 'Field name: value' style regex per custom
    field, used when no AI provider is available. Less reliable than the AI
    path for fields phrased differently than entered, but works with zero
    cost/setup for straightforward label:value documents."""
    results = {}
    for field in custom_fields:
        pattern = re.escape(field.strip()) + r"\s*[:\-]?\s*(.+)"
        match = re.search(pattern, full_text, re.IGNORECASE)
        results[field] = match.group(1).strip() if match else None
    return results


def check_custom_fields_ai_text(full_text, custom_fields, provider, api_key):
    """AI-based presence + value check against already-extracted text (for
    text PDFs and Excel) -- one call covers every requested field.

    IMPORTANT: on failure (rate limit, bad key, etc.) this returns an EMPTY
    dict, not {field: None for every field} -- a dict entry with value None
    means 'checked, and confirmed not present'; no entry at all means
    'the check itself never ran'. Conflating those two would make an API
    failure look identical to a genuinely missing field, which is
    dangerously misleading for a compliance tool."""
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
    """Build the AI vision extraction prompt, optionally including a request
    to also check a list of user-specified custom mandatory fields in the
    same single call (rather than a separate pass)."""
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
    """Send a scanned PDF or photo/image directly to Claude's or Gemini's
    vision capability and ask it to extract the full invoice structure as
    JSON in one call (fields + line items together, plus any custom fields
    requested) -- this is the fallback for formats with no text layer to
    read locally."""
    requests = _ensure_package("requests")
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
        else:  # anthropic
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


def run_audit(path, custom_fields, ai_provider=None, api_key=None):
    """Supporting Document Audit mode: bypasses all invoice-specific checks
    (math, grand total, HS-6, the standard 4 document fields) entirely, and
    just verifies presence of whatever mandatory fields the user names --
    for non-invoice supporting documents (packing lists, certificates of
    origin, bills of lading, etc.) where the invoice-shaped checks don't
    apply but you still need a fast presence/absence audit."""
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

    if ext == "pdf":
        pdf_data = load_pdf_text(path)
        if pdf_data["likely_scanned"]:
            if not ai_provider:
                report["status"] = "FAIL"
                report["load_error"] = ("Scanned PDF -- needs Gemini ('g') or Claude ('a') to read.")
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
            report["load_error"] = "Image files need Gemini ('g') or Claude ('a') to read."
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


def print_audit_report(report):
    W = 56
    print("=" * W)
    print(f" DOCUMENT AUDIT: {report['file']}")
    print("=" * W)

    if report.get("load_error") and not report["fields_checked"]:
        _print_wrapped("\n  Could not process: ", report["load_error"], W)
        print()
        print("=" * W)
        print(f" RESULT: {report['status']}")
        print("=" * W)
        return

    print("\nMANDATORY FIELDS CHECKED")
    print("-" * W)
    for field in report.get("fields_requested", report["fields_checked"].keys()):
        if field in report["unverified_fields"]:
            print(f"  [? ] {field}: COULD NOT VERIFY")
        elif field in report["missing_fields"]:
            print(f"  [X ] {field}: NOT FOUND")
        else:
            _print_wrapped(f"  [OK] {field}: ", report["fields_checked"].get(field), W)
    if report.get("load_error"):
        _print_wrapped("\n  Note: ", report["load_error"], W)

    print()
    print("=" * W)
    print(f" RESULT: {report['status']}")
    print("=" * W)


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
    full_text_for_custom_check = None  # set for text/excel paths; None means vision already handled it

    if ext == "pdf":
        pdf_data = load_pdf_text(path)
        if pdf_data["likely_scanned"]:
            if not ai_provider:
                report["status"] = "FAIL"
                report["load_error"] = (
                    "This looks like a scanned/image-based PDF with no extractable text. "
                    "Re-run with Gemini ('g') or Claude ('a') mode to process it -- plain "
                    "text extraction can't read scanned documents."
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
                "re-run with Gemini ('g') or Claude ('a') mode."
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

    # Custom fields for text/Excel sources (vision sources already handled above,
    # folded into the same extraction call rather than a second pass)
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
                # Not even a key in the results dict -- the check itself never
                # ran (e.g. rate limit), NOT a confirmed absence. Keep these
                # separate so an API hiccup can't look like a compliance failure.
                report["unverified_custom_fields"].add(f)

    # ---- From here on, processing is identical regardless of source format ----
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

        if hts_lookup or ai_provider:  # USITC shown as a secondary, free/official-source reference
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
            # IMPORTANT: sum the RECALCULATED value (qty x unit_price), not the
            # invoice's own stated total_price. If we summed stated values, a
            # line with a wrong total_price would still get counted at its
            # wrong value -- and if the invoice's declared grand total was
            # itself built from that same wrong number, the mismatch check
            # would falsely say everything matches. Recalculating from scratch
            # is the only way to catch a grand total that's wrong but
            # internally "consistent" with its own bad line total.
            running_total_recalculated += expected
            running_total_as_stated += total_price
        elif total_price is not None:
            # Can't independently verify this line (missing qty or unit price)
            # -- fall back to its stated total, but flag it as unverified so
            # the grand total figure it feeds into isn't presented as fully checked.
            running_total_recalculated += total_price
            running_total_as_stated += total_price
            report["unverified_line_totals"].append(desc)

        report["line_items"].append(line_result)

    report["grand_total_recalculated"] = round(running_total_recalculated, 2)
    report["grand_total_sum_of_stated_lines"] = round(running_total_as_stated, 2)
    report["grand_total_computed"] = report["grand_total_recalculated"]  # kept for backward compatibility
    if report["grand_total_stated"] is not None:
        diff = round(abs(report["grand_total_stated"] - report["grand_total_recalculated"]), 2)
        report["grand_total_mismatch"] = diff > TOLERANCE

    if (report["missing_table_fields"] or report["missing_document_fields"]
            or report["line_math_errors"] or report["grand_total_mismatch"]
            or report["missing_custom_fields"]):
        report["status"] = "FAIL"
    elif report["unverified_custom_fields"]:
        # Nothing is confirmed wrong, but not everything could be checked either
        # (e.g. rate limit) -- this is neither a clean PASS nor a real FAIL.
        report["status"] = "INCOMPLETE"

    return report


import textwrap


def _print_wrapped(prefix, text, width):
    """Print `prefix + text`, wrapping continuation lines to align under the
    prefix (so long text -- addresses, tariff descriptions, AI reasoning --
    doesn't blow past the screen width). `prefix` should already include any
    marker/label/colon, e.g. '     Official  : '."""
    cont_indent = " " * len(prefix)
    avail = max(15, width - len(prefix))
    wrapped = textwrap.wrap(str(text), width=avail) or [""]
    print(prefix + wrapped[0])
    for line in wrapped[1:]:
        print(cont_indent + line)


def _truncate(text, max_len):
    text = str(text)
    return text if len(text) <= max_len else text[:max_len - 3].rstrip() + "..."


def print_report(report):
    W = 56  # line width for separators, phone-screen friendly
    ok_mark, bad_mark = "OK", "X "

    print("=" * W)
    print(f" INVOICE: {report['file']}")
    print("=" * W)

    if report.get("load_error"):
        _print_wrapped("\n  Could not process: ", report["load_error"], W)
        print()
        print("=" * W)
        print(f" RESULT: {report['status']}")
        print("=" * W)
        return

    if report.get("extraction_method"):
        print(f" (read via: {report['extraction_method']})")

    # ---- Document details ----
    print("\nDOCUMENT DETAILS")
    print("-" * W)
    for field in REQUIRED_DOCUMENT_FIELDS:
        label = DOCUMENT_FIELD_LABELS[field]
        if field in report["missing_document_fields"]:
            print(f"  [{bad_mark}] {label:<16}: MISSING")
        else:
            value = report["document_fields_found"].get(field)
            _print_wrapped(f"  [{ok_mark}] {label:<16}: ", value, W)

    # ---- Custom mandatory fields (user-specified, optional) ----
    if report.get("custom_fields_requested"):
        print("\nCUSTOM MANDATORY FIELDS")
        print("-" * W)
        for field in report["custom_fields_requested"]:
            if field in report["unverified_custom_fields"]:
                print(f"  [? ] {field:<16}: COULD NOT VERIFY")
            elif field in report["missing_custom_fields"]:
                print(f"  [{bad_mark}] {field:<16}: NOT FOUND")
            else:
                _print_wrapped(f"  [{ok_mark}] {field:<16}: ", report["custom_fields_found"].get(field), W)
        if report.get("custom_fields_error"):
            _print_wrapped("  Note: ", report["custom_fields_error"], W)

    # ---- Line-item table presence ----
    print("\nLINE-ITEM TABLE FIELDS")
    print("-" * W)
    for field in REQUIRED_TABLE_FIELDS:
        label = field.replace("_", " ").title()
        mark = bad_mark if field in report["missing_table_fields"] else ok_mark
        status = "MISSING" if field in report["missing_table_fields"] else "present"
        print(f"  [{mark}] {label:<16}: {status}")

    # ---- Each line item as its own block ----
    print(f"\nLINE ITEMS ({len(report['line_items'])})")
    for n, item in enumerate(report["line_items"], start=1):
        print("-" * W)
        for line in textwrap.wrap(f"{n}) {item['description']}", width=W):
            print(f"  {line}")
        if item.get("part_number"):
            print(f"     Part No   : {item['part_number']}")
        print(f"     Qty/Price : {item['qty']} x {item['unit_price']}")
        math_display = {True: "OK", False: "MISMATCH", None: "n/a"}[item["math_ok"]]
        print(f"     Total     : {item['total_price']}   [{math_display}]")

        # AI (GRI-reasoned) classification -- the primary result when available
        if item.get("ai_hs6"):
            print()
            print("     -- AI classification --")
            print(f"     HS-6      : {item['ai_hs6'].replace('.', '')} (verify remainder for destination)")
            _print_wrapped("     Official  : ", item["ai_official_description"], W)
            _print_wrapped("     Reason    : ", item["ai_reason"], W)
            print(f"     Confidence: {item['ai_confidence_label']} (~{item['ai_confidence_percent']}%)")
        elif item.get("ai_error"):
            print()
            _print_wrapped("     AI result : ", item["ai_error"], W)

        # USITC keyword match -- shown as a secondary, free/official-source reference
        if item.get("hs6"):
            hs6_digits = item["hs6"].replace(".", "")
            print()
            print("     -- USITC reference (secondary) --")
            print(f"     HS-6      : {hs6_digits}  (~{item['hts_confidence']}% word-overlap)")
            _print_wrapped("     Match     : ", _truncate(item["hts_match_description"], 90), W)
            if item.get("hts_low_reliability"):
                print("     ** LOW RELIABILITY -- short description, treat as unverified **")
        elif item.get("hts_error"):
            print()
            _print_wrapped("     USITC ref.: ", _truncate(item["hts_error"], 90), W)

    print("-" * W)

    # ---- Math errors, called out separately for visibility ----
    if report["line_math_errors"]:
        print("\nMATH ERRORS")
        print("-" * W)
        for err in report["line_math_errors"]:
            print(f"  - {err['description']}")
            print(f"    expected {err['expected_total']}, stated {err['stated_total']} "
                  f"(diff {err['diff']})")

    # ---- Grand total reconciliation ----
    print("\nGRAND TOTAL")
    print("-" * W)
    print(f"  Stated on invoice      : {report['grand_total_stated']}")
    print(f"  Recalculated (qty x price): {report['grand_total_recalculated']}")
    if report["grand_total_stated"] is not None:
        mark = bad_mark if report["grand_total_mismatch"] else ok_mark
        note = "MISMATCH" if report["grand_total_mismatch"] else "matches"
        print(f"  [{mark}] vs recalculated: {note}")

        # Flag the specific case that matters most: the invoice's own numbers
        # agree with each other but are collectively wrong -- i.e. simply
        # summing whatever total_price each line states would have looked
        # fine, and only independent recalculation catches it.
        stated_sum = report["grand_total_sum_of_stated_lines"]
        if stated_sum is not None:
            stated_sum_matches_declared = abs(report["grand_total_stated"] - stated_sum) <= TOLERANCE
            if stated_sum_matches_declared and report["grand_total_mismatch"]:
                print(f"  ** Invoice is internally consistent (its own line totals sum to")
                print(f"     the declared {report['grand_total_stated']}) but that figure is")
                print(f"     still wrong once qty x price is recalculated per line. **")
    else:
        print("  [?] Could not locate a stated grand total in the text")

    if report["unverified_line_totals"]:
        print(f"  Note: {len(report['unverified_line_totals'])} line(s) had missing qty/price "
              f"and couldn't be independently recalculated -- their stated total was used as-is.")

    # ---- Final verdict, unmissable ----
    print()
    print("=" * W)
    print(f" RESULT: {report['status']}")
    print("=" * W)
    print()


def _parse_field_list(raw):
    return [f.strip() for f in raw.split(",") if f.strip()]


def summarize_invoice_reason(report):
    """One-line reason for an invoice-mode report's status, for the summary table."""
    if report.get("load_error"):
        return _truncate(report["load_error"], 70)
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
    """One-line reason for an audit-mode report's status, for the summary table."""
    if report.get("load_error") and not report["fields_checked"]:
        return _truncate(report["load_error"], 70)
    if report["status"] == "PASS":
        return "All fields present"
    reasons = []
    if report["missing_fields"]:
        reasons.append(f"Missing: {', '.join(sorted(report['missing_fields']))}")
    if report["unverified_fields"]:
        reasons.append(f"Could not verify: {', '.join(sorted(report['unverified_fields']))}")
    return "; ".join(reasons) if reasons else "Unknown"


def print_summary_table(results):
    """results: list of (filename, status, reason) tuples, in processing order."""
    W = 56
    status_mark = {"PASS": "OK", "FAIL": "X ", "INCOMPLETE": "? "}
    print("=" * W)
    print(" SUMMARY")
    print("=" * W)
    for i, (fname, status, reason) in enumerate(results, start=1):
        mark = status_mark.get(status, "? ")
        print(f"  {i}) [{mark}] {fname} -- {status}")
        _print_wrapped("       Reason: ", reason, W)
    print("-" * W)
    n_pass = sum(1 for _, s, _ in results if s == "PASS")
    n_fail = sum(1 for _, s, _ in results if s == "FAIL")
    n_inc = sum(1 for _, s, _ in results if s == "INCOMPLETE")
    print(f"  Total: {len(results)}   Pass: {n_pass}   Fail: {n_fail}   Incomplete: {n_inc}")
    print("=" * W)


def _prompt_detail_selection(filenames):
    """Ask which summary rows to view in full detail. Accepts either the row
    number (1-indexed) or the filename itself (exact, partial, or with a full
    path typed in -- matched against the basename). Returns a set of
    1-indexed selections, or empty for none. Never silently returns nothing
    just because the input didn't parse -- unmatched entries are reported."""
    n = len(filenames)
    raw = input(f"\nView full details for which file(s)? "
                f"(numbers, filenames, 'all', or Enter for none): ").strip()
    if not raw or raw.lower() in ("n", "none"):
        return set()
    if raw.lower() == "all":
        return set(range(1, n + 1))

    selected = set()
    unmatched = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and 1 <= int(part) <= n:
            selected.add(int(part))
            continue
        # Try matching as a filename: exact basename match first, then
        # substring match, so typing the full path or just part of the
        # name both work.
        typed_name = os.path.basename(part).strip().lower()
        exact = [i for i, f in enumerate(filenames, start=1) if f.lower() == typed_name]
        if exact:
            selected.update(exact)
            continue
        partial = [i for i, f in enumerate(filenames, start=1) if typed_name in f.lower()]
        if partial:
            selected.update(partial)
            continue
        unmatched.append(part)

    if unmatched:
        print(f"  Could not match: {', '.join(unmatched)} -- check the number or filename and try again.")
    return selected


def main():
    api_key = None
    SUPPORTED_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls")
    interactive = len(sys.argv) < 2
    summary_only = False

    if len(sys.argv) < 2:
        # No command-line arguments -- likely launched via Pydroid's "Run" button,
        # which doesn't pass argv. Ask interactively instead.
        print("What kind of check is this?")
        print("  1 = Invoice Compliance Check (fields, math, grand total, HS-6 classification)")
        print("  2 = Supporting Document Audit (skip invoice math/HS-6 -- just verify fields you name)")
        mode = "audit" if input("Choose (1/2): ").strip() == "2" else "invoice"

        path = input("Enter path to document (PDF/JPG/PNG/Excel, or a folder): ").strip().strip('"').strip("'")

        hts_lookup = False
        ai_provider = None
        custom_fields = []

        if mode == "invoice":
            print("HS-6 lookup options:")
            print("  n  = none")
            print("  u  = USITC keyword search only (free, no key, lower accuracy)")
            print("  g  = AI classification via Google Gemini (FREE, sign in with Google account)")
            print("  a  = AI classification via Claude API (paid after trial credit, higher accuracy)")
            print("(note: scanned PDFs and JPG/PNG images REQUIRE 'g' or 'a' -- no free-text option for those)")
            choice = input("Choose (n/u/g/a): ").strip().lower()
            hts_lookup = choice == "u"
            ai_provider = {"a": "anthropic", "g": "gemini"}.get(choice)

            extra = input("Check any additional custom mandatory fields too? (y/N): ").strip().lower()
            if extra in ("y", "yes"):
                raw = input("Enter field names to check, comma-separated (e.g. Country of Origin, Incoterm): ")
                custom_fields = _parse_field_list(raw)
        else:
            print("Which AI provider should read scanned/image files, if any come up?")
            print("  n = none (text-based PDF/Excel only)")
            print("  g = Google Gemini (free)")
            print("  a = Claude API (paid)")
            ai_choice = input("Choose (n/g/a): ").strip().lower()
            ai_provider = {"a": "anthropic", "g": "gemini"}.get(ai_choice)
            raw = input("Enter mandatory field names to check, comma-separated: ")
            custom_fields = _parse_field_list(raw)
            if not custom_fields:
                print("No fields entered -- nothing to check.")
                sys.exit(1)
    else:
        args = sys.argv[1:]
        mode = "audit" if "--audit" in args else "invoice"
        hts_lookup = "--hts-lookup" in args
        ai_provider = "anthropic" if "--ai-classify" in args else ("gemini" if "--gemini" in args else None)
        summary_only = "--summary-only" in args
        custom_fields = []
        for a in args:
            if a.startswith("--fields="):
                custom_fields = _parse_field_list(a[len("--fields="):])
        positional = [a for a in args if not a.startswith("--")]
        path = positional[0]
        if mode == "audit" and not custom_fields:
            print("Audit mode needs --fields=\"Field One, Field Two\" naming what to check.")
            sys.exit(1)

    if ai_provider == "anthropic":
        api_key = get_anthropic_api_key()
    elif ai_provider == "gemini":
        api_key = get_gemini_api_key()

    if ai_provider and not api_key:
        print("No API key provided -- falling back to free options only "
              "(won't work for scanned/image files).")
        ai_provider = None
        if mode == "invoice":
            hts_lookup = True

    invoice_files = []
    if os.path.isdir(path):
        invoice_files = [os.path.join(path, f) for f in sorted(os.listdir(path))
                          if f.lower().endswith(SUPPORTED_EXTS)]
    elif os.path.isfile(path) and path.lower().endswith(SUPPORTED_EXTS):
        invoice_files = [path]
    else:
        print(f"Could not find a supported file or folder at: {path}")
        print(f"Supported types: {', '.join(SUPPORTED_EXTS)}")
        sys.exit(1)

    reports = []  # list of (path, report) in order
    for doc_path in invoice_files:
        if mode == "audit":
            report = run_audit(doc_path, custom_fields, ai_provider=ai_provider, api_key=api_key)
        else:
            report = check_invoice(doc_path, hts_lookup=hts_lookup, ai_provider=ai_provider,
                                    api_key=api_key, custom_fields=custom_fields)
        reports.append((doc_path, report))

    summarize = summarize_audit_reason if mode == "audit" else summarize_invoice_reason
    summary_rows = [(os.path.basename(p), r["status"], summarize(r)) for p, r in reports]
    print_summary_table(summary_rows)

    if summary_only:
        return  # CLI --summary-only: skip full details entirely

    if interactive:
        selected = _prompt_detail_selection([os.path.basename(p) for p, _ in reports])
    else:
        selected = set(range(1, len(reports) + 1))  # CLI default: show all details after summary, unchanged behavior

    for i, (doc_path, report) in enumerate(reports, start=1):
        if i not in selected:
            continue
        print()
        if mode == "audit":
            print_audit_report(report)
        else:
            print_report(report)


if __name__ == "__main__":
    main()
