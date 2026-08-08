# Invoice Compliance Checker Backend Process

A Python tool that reads vendor invoices — text PDFs, scanned/photographed invoices, and Excel files — and flags trade-compliance risks automatically: missing required fields, arithmetic errors, and an HS-6 tariff code suggestion per line item (via real GRI-based reasoning, not just a keyword search). Built to run on both desktop Python and Pydroid on Android.

Built as a companion project to the [Part Number Verification Tool](#), extending the same data-reconciliation approach from static lists to real invoice documents.

## Supported input formats

| Format | Requires AI provider? | Notes |
|---|---|---|
| Text-based PDF (`.pdf`) | No | Has a real text layer — read directly, free |
| Excel (`.xlsx`, `.xls`) | No | Read directly via `openpyxl`/`xlrd`, free |
| Scanned PDF (`.pdf`, no text layer) | **Yes** (`g` or `a`) | No embedded text to read — needs vision |
| Photo/scan (`.jpg`, `.jpeg`, `.png`) | **Yes** (`g` or `a`) | Same reason — images have no text layer |

The script auto-detects a scanned PDF (one with no extractable text or table) and tells you plainly if it needs an AI mode rather than silently failing or producing garbage output.

**Why scanned/image invoices need an AI provider specifically:** reading a scanned document normally means OCR, and the standard tool for that (`pytesseract`) needs a compiled Tesseract binary — which can't be installed via `pip` on Android, the same category of problem that ruled out `pdfplumber` earlier in this project. Rather than add an incompatible dependency, scanned PDFs and photos are sent directly to Claude's or Gemini's vision capability, which reads the image and extracts the invoice structure as JSON in one call — no OCR library needed at all.

## Summary view with drill-down

When checking multiple files, results are shown as a compact summary table first — filename, PASS/FAIL/INCOMPLETE, and a one-line reason — before any full detail:

```
========================================================
 SUMMARY
========================================================
  1) [OK] invoice_clean.pdf -- PASS
       Reason: All checks passed
  2) [X ] invoice_issues.pdf -- FAIL
       Reason: Math error in 1 line; Grand total mismatch
  3) [X ] invoice_missing_header.pdf -- FAIL
       Reason: Missing: Supplier Address, Payment Terms
--------------------------------------------------------
  Total: 3   Pass: 1   Fail: 2   Incomplete: 0
========================================================

View full details for which file(s)? (numbers comma-separated, 'all', or Enter for none):
```

Type specific numbers (e.g. `2,3`) to drill into just those files' full reports, `all` for everything, or Enter to stop at the summary. On the command line, add `--summary-only` to get just the table with no prompt (useful for scripting/piping).

## Grand total is independently recalculated, not just trusted

The grand total check does **not** simply sum whatever `total_price` each line states and compare that to the invoice's declared grand total — that would let a systematically wrong invoice pass silently, since a line with an incorrect stated total still gets counted at its (wrong) face value, and if the invoice's own declared grand total was built from that same wrong number, the two would "agree" despite both being incorrect.

Instead, every line's total is recalculated from `Qty x Unit Price` from scratch, and *that* recalculated sum is what's compared against the invoice's declared grand total. If an invoice is internally consistent (its stated line totals really do sum to its declared grand total) but that figure is still wrong once recalculated, the tool flags this specifically:

```
GRAND TOTAL
--------------------------------------------------------
  Stated on invoice      : 145.0
  Recalculated (qty x price): 141.0
  [X] vs recalculated: MISMATCH
  ** Invoice is internally consistent (its own line totals sum to
     the declared 145.0) but that figure is
     still wrong once qty x price is recalculated per line. **
```

## What it checks

1. **Line-item table fields** — Unit Price and Total Price must appear as recognizable columns.
2. **Line-level math validation** — `Qty x Unit Price` must equal the stated line `Total Price` (within rounding tolerance).
3. **Invoice-level reconciliation** — the sum of all line totals must match the invoice's stated grand total.
4. **Document-level fields** — Order Number, Supplier Address, Payment Terms, and Date must be present somewhere in the invoice.
5. **HS-6 classification** *(optional)* — three modes, see below.
6. **Custom mandatory fields** *(optional)* — any additional field a compliance specialist wants checked (Country of Origin, Incoterm, Currency, Certificate of Origin reference, etc.) that isn't one of the 4 standard ones above — name it, and it gets checked for presence (and its value extracted) the same way.

## Two check types

**Invoice Compliance Check** (the default) — everything above: field presence, math validation, grand total reconciliation, optional HS-6 classification, optional custom fields layered on top.

**Supporting Document Audit** — a separate, lighter mode that skips all invoice-specific logic (math, grand total, HS-6, the standard 4 fields) entirely and just checks whatever mandatory fields you name, for documents that aren't invoice-shaped (packing lists, certificates of origin, bills of lading, etc.) but still need a fast presence/absence check. Output is a simple per-field OK/NOT FOUND list and an overall PASS/FAIL — no invoice math involved at all.

Both modes support all four input formats. Both can use AI (Gemini/Claude) to check fields against already-extracted text, or fold the field check into the same single AI-vision call already used for scanned/image files — no extra API cost for that. Without an AI provider, custom/audit fields fall back to a generic `"Field name: value"` regex — works for straightforward label:value documents, less reliable if the field is phrased differently than you typed it.

## Three HS-6 lookup modes, and why they all exist

**`u` — USITC keyword search (free, no API key).** Queries USITC's public HTS API directly and scores results by word overlap against the part description. This is fast and free, but it's fundamentally limited: it's just text matching against legal tariff language, which often shares vocabulary across raw materials, parts, and finished goods. Testing surfaced a real example of this — "Bearing, Ball 6203-2RS" matched *"Of ball-bearing steel"* (the raw steel alloy, Chapter 72) instead of the finished bearing product (Chapter 84, the actually-correct code), because both words in the short query happened to appear in that unrelated tariff line, at 100% word-overlap "confidence." Short, few-word descriptions are especially vulnerable to this, so results are flagged **"LOW RELIABILITY"** whenever the match rests on very few overlapping words, regardless of the percentage shown.

**`g` — AI classification via Google Gemini (free, sign in with a Google account only).** Same GRI reasoning workflow as `a` below, but calls Google's Gemini API through Google AI Studio, which has a genuine no-cost tier: no credit card, no expiration, generous daily request limit. This is the recommended default for a portfolio/demo project — full AI reasoning quality at zero cost.

**`a` — AI classification via Claude API (needs an API key, paid after a small trial credit).** Sends the part description to Claude with the same explicit GRI classification workflow:
1. Identify product type, primary function, material(s), end use, industry, and whether it's a raw material, component, or finished product
2. Determine essential character
3. Apply the HS General Rules for Interpretation (GRIs) and Section/Chapter Notes
4. Compare candidate headings across chapters 01–97 rather than just keyword-matching
5. Select the most specific valid 6-digit code, and explicitly reject Chapter 98/99 codes
6. If the description is genuinely ambiguous, say so rather than guessing

This is what correctly distinguishes "finished ball bearing" from "raw bearing steel" in testing. In both `g` and `a` modes, the USITC keyword result is still shown alongside as a secondary, free/official-source reference point — useful for cross-checking, but the AI result is the primary one.

**All modes produce suggestions for human review, not a legal classification.** None of them attempt to determine export-control status (ECCN/license requirement), which needs a proper technical review, not a keyword or AI text match.

It also intentionally stops at 6 digits regardless of mode. HS-6 is standardized across 200+ countries, but each country appends its own extension: 10-digit HTS for US imports, 8-digit HSN for India (used for both GST and customs), 10-digit TARIC for the EU, and so on — these do **not** match each other beyond the shared 6 digits. The remainder is shown dashed out, e.g. `848210 (---- fill remainder for destination)`.

## Web UI (optional)

A Streamlit-based web application -- login/RBAC, dashboard, report history, batch processing -- is available in [webapp/](./webapp/), built on top of this exact same backend (no logic duplicated). See [webapp/README_WEBAPP.md](./webapp/README_WEBAPP.md) for setup and an honest account of what's fully tested versus what still needs a first live run.

## Setup

For exact typing formats at every prompt (paths, mode letters, field name syntax, detail-view selection) plus a full worked example, see [INPUT_FORMAT_GUIDE.md](./INPUT_FORMAT_GUIDE.md). For what an invoice document itself should look like for the free text-based path to read it reliably (recognized labels, table headers, a verified working example), see [SAMPLE_INVOICE_FORMAT.md](./SAMPLE_INVOICE_FORMAT.md).

### Desktop / terminal
```bash
pip install -r requirements.txt

python invoice_compliance_checker.py samples/invoice_clean.pdf
python invoice_compliance_checker.py samples/invoice_issues.pdf --hts-lookup     # USITC only
python invoice_compliance_checker.py samples/invoice_issues.pdf --gemini         # AI (free) + USITC reference
python invoice_compliance_checker.py samples/invoice_issues.pdf --ai-classify    # AI (Claude, paid) + USITC reference
python invoice_compliance_checker.py samples/invoice_excel_clean.xlsx            # Excel invoice, no AI needed
python invoice_compliance_checker.py scanned_invoice.pdf --gemini                # scanned PDF, AI required
python invoice_compliance_checker.py invoice_photo.jpg --gemini                  # photo/scan, AI required
python invoice_compliance_checker.py ./samples/ --gemini                        # batch mode, any mix of formats

# custom fields on top of the standard invoice check
python invoice_compliance_checker.py samples/invoice_clean.pdf --gemini --fields="Country of Origin,Incoterm"

# Supporting Document Audit mode -- bypasses invoice math/HS-6, only checks named fields
python invoice_compliance_checker.py packing_list.pdf --audit --fields="PO Number,Net Weight,Package Count"
```

### Pydroid (Android)
Just tap **Run**. The script auto-installs `pypdf` and `requests` on first run (into the same interpreter it's running in, avoiding the common Pydroid issue where the Pip menu and Run button use different environments), then prompts interactively:

```
What kind of check is this?
  1 = Invoice Compliance Check (fields, math, grand total, HS-6 classification)
  2 = Supporting Document Audit (skip invoice math/HS-6 -- just verify fields you name)
Choose (1/2): 1
Enter path to document (PDF/JPG/PNG/Excel, or a folder): samples/invoice_clean.pdf
HS-6 lookup options:
  n  = none
  u  = USITC keyword search only (free, no key, lower accuracy)
  g  = AI classification via Google Gemini (FREE, sign in with Google account)
  a  = AI classification via Claude API (paid after trial credit, higher accuracy)
Choose (n/u/g/a): g
Check any additional custom mandatory fields too? (y/N): y
Enter field names to check, comma-separated (e.g. Country of Origin, Incoterm): Country of Origin
```

Use the **full path** to the file if it's not sitting in the same folder Pydroid runs from (check via your file manager: long-press the file → Details/Properties).

### Getting a Gemini API key (for `g` mode — recommended, free)
1. Go to [aistudio.google.com](https://aistudio.google.com) and sign in with any Google account — no credit card needed
2. Click **Get API key** → **Create API key** → copy it
3. Free tier: no expiration, no cost, rate-limited to a generous daily request count — more than enough for testing/demoing this tool
4. The script will ask for the key the first time you use `g` mode, and offers to save it locally (in `.gemini_api_key`, already excluded via `.gitignore` — never commit this file)

### Getting an Anthropic API key (for `a` mode — optional, paid)
1. Go to [console.anthropic.com](https://console.anthropic.com) and sign up / log in
2. **API Keys** (left sidebar) → **Create Key** → copy it (starts with `sk-ant-...`)
3. New accounts typically get a small free trial credit; after that it costs a fraction of a cent per line item classified
4. The script will ask for the key the first time you use `a` mode, and offers to save it locally (in `.anthropic_api_key`, already excluded via `.gitignore` — never commit this file)

## Sample output

Real output from an actual run (via `g` / Gemini), not simulated:

```
========================================================
 INVOICE: invoice_clean.pdf
========================================================

DOCUMENT DETAILS
--------------------------------------------------------
  [OK] Order Number    : PO-55210
  [OK] Supplier Address: 42 Industrial Estate Road,
                         Coimbatore, Tamil Nadu 641021
  [OK] Payment Terms   : Net 30 days
  [OK] Date            : 15 Jan 2026

LINE-ITEM TABLE FIELDS
--------------------------------------------------------
  [OK] Unit Price      : present
  [OK] Total Price     : present

LINE ITEMS (3)
--------------------------------------------------------
  1) Bearing, Ball 6203-2RS
     Part No   : PN-1001
     Qty/Price : 10.0 x 4.5
     Total     : 45.0   [OK]

     -- AI classification --
     HS-6      : 848210 (verify remainder for destination)
     Official  : Ball bearings
     Reason    : The product is a standard deep groove
                 ball bearing with rubber seals
                 (6203-2RS). In accordance with GRI 1,
                 it is specifically classified under
                 heading 8482 for ball or roller
                 bearings, and subheading 8482.10 for
                 ball bearings.
     Confidence: High (~98%)

     -- USITC reference (secondary) --
     HS-6      : 722540  (~100% word-overlap)
     Match     : Of ball-bearing steel
     ** LOW RELIABILITY -- short description, treat as unverified **
--------------------------------------------------------
  3) Bracket, Steel Mounting L-Type
     Part No   : PN-1003
     Qty/Price : 5.0 x 8.0
     Total     : 40.0   [OK]

     -- AI classification --
     HS-6      : 830250 (verify remainder for destination)
     Official  : Base metal hat-racks, hat-pegs,
                 brackets and similar fixtures
     Reason    : The product is an L-type mounting
                 bracket made of steel (base metal). In
                 accordance with GRI 1, base metal
                 brackets and similar fixtures are
                 specifically provided for under heading
                 8302, subheading 8302.50.
     Confidence: High (~90%)

     -- USITC reference (secondary) --
     HS-6      : 990215  (~67% word-overlap)
     Match     : Fire escape ladders no taller than 4.3
                 m when fully extended, tested to
                 support 510.3 k...
--------------------------------------------------------

GRAND TOTAL
--------------------------------------------------------
  Stated on invoice   : 115.0
  Computed from lines : 115.0
  [OK] matches

========================================================
 RESULT: PASS
========================================================
```

Notice the disagreement pattern holding up in practice: the AI result reasons its way to *"Ball bearings"* (8482.10) and *"Base metal brackets and similar fixtures"* (8302.50) — both defensible, specific classifications — while USITC's keyword search independently latched onto *"Of ball-bearing steel"* and a completely unrelated *"Fire escape ladders"* provision, both flagged for it. That gap is the tool being honest about which source to trust.

It also correctly declines to guess on a genuinely ambiguous item rather than making something up:

```
  2) Seal Kit, Hydraulic
     -- AI classification --
     AI result : AI needs more info: What is the
                 material composition of the seals in
                 the kit (e.g., all vulcanized rubber,
                 all plastic/PTFE, or an assortment of
                 seals made of different/dissimilar
                 materials)?
```

"Hydraulic Seal Kit" genuinely doesn't specify the seal material, and material determines the correct heading — so it asks rather than assumes.

## How it works

- **Text PDFs:** extracted using `pypdf` (pure Python — no compiled dependencies, so it installs cleanly on Android/Pydroid, unlike libraries that depend on `pypdfium2`, which has no prebuilt Android wheel). `pypdf` emits each table cell as its own line, so the parser locates the header row (a run of 2+ recognized column labels) and groups the lines that follow into rows of that same width — no rigid template required.
- **Scanned PDFs are auto-detected**: if a PDF yields (close to) no text or no recognizable table, it's treated as scanned and routed to AI vision instead of silently failing.
- **Excel files** are read via `openpyxl` (`.xlsx`) or `xlrd` (`.xls`) — both pure Python. The same header-alias matching used for PDFs is applied to the sheet's actual columns, and label/value cell pairs (e.g. `"Order Number"` | `"PO-12345"` in adjacent cells) are joined into `"Label: value"` text for the same document-field regex used elsewhere.
- **Scanned PDFs and images** are sent as base64 directly to Claude's or Gemini's vision capability with a single prompt asking for the full invoice structure (fields + line items) as JSON — one API call extracts everything, no separate OCR step.
- Recognizes common header variants (e.g. "Amount" or "Line Total" for Total Price, "Item" for Description) via an alias-matching system, regardless of source format.
- Document-level fields (Order Number, Supplier Address, Payment Terms, Date) are matched via labeled-field regex against the invoice's full text (PDF/Excel) or extracted directly by the AI (scanned/image).
- USITC lookup calls `https://hts.usitc.gov/reststop/search?keyword=...` (official, free, no API key), scores every returned candidate by word overlap against the query, and flags low-confidence/low-word-count matches explicitly.
- AI classification (and AI vision extraction) calls the Anthropic Messages API or Google's Gemini API directly via `requests` (no SDK dependency for either — the official `anthropic` SDK depends on Rust-based packages that don't build on Android) with a system prompt encoding the GRI classification workflow, and parses the structured JSON response. Both providers share the exact same prompts and response formats.
- Applies a small rounding tolerance (0.02) to math checks to avoid false positives from currency rounding.

## Tech stack

Python 3, [pypdf](https://pypdf.readthedocs.io/), [openpyxl](https://openpyxl.readthedocs.io/), [xlrd](https://xlrd.readthedocs.io/), [requests](https://requests.readthedocs.io/), [USITC HTS REST API](https://www.usitc.gov/data/index.htm), [Anthropic Messages API](https://docs.claude.com/en/api/messages), [Gemini API](https://ai.google.dev/gemini-api/docs)

## Possible extensions

- OCR support for scanned (image-based) invoices via `pytesseract`
- Export findings to CSV/Excel for audit trail
- Web UI (Streamlit) for drag-and-drop invoice review
- Country-specific suffix lookup (e.g. India's HSN schedule) alongside the HS-6, once a destination country is known

## License

All rights reserved — see [LICENSE](./LICENSE). Free to view and run for personal, educational, or evaluation purposes; no commercial use, redistribution, or modification without prior written permission from the author.

## Author

V Sathish Kumar — Sourcing Process Analyst, trade compliance & procurement systems (Oracle R12, Oracle GTM)
[LinkedIn](https://linkedin.com/in/sathishkumarv024) · [Portfolio](https://about.me/vsathishkumar)
