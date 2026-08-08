# Sample Invoice Format

What an invoice actually needs to look like for the **free, text-based path** (`n`/`u` modes, or Excel) to read it reliably. This is a reference, not a strict requirement — AI mode (`g`/`a`) is far more tolerant of variation, since it reasons about the document rather than pattern-matching fixed labels.

## Recognized labels (document-level fields)

These are matched by label text anywhere in the document, case-insensitive. The value must come **after** the label on the same line — see [INPUT_FORMAT_GUIDE.md](./INPUT_FORMAT_GUIDE.md) for why reversed order (value-above-label) breaks the free path and needs AI mode instead.

| Field | Accepted labels |
|---|---|
| Order Number | `Order No.`, `Order Number`, `Order #`, `PO No.`, `PO Number`, `PO #` |
| Supplier Address | `Supplier Address`, `Vendor Address`, `Supplier's Address`, `Vendor's Address` |
| Payment Terms | `Payment Term`, `Payment Terms` |
| Date | `Invoice Date`, `Date` |

## Recognized table column headers (line-item table)

These are matched per-column, case-insensitive, checked most-specific-first so e.g. "Total Price" is never mistaken for "Unit Price":

| Field | Accepted column headers |
|---|---|
| Part Number | `Part Number`, `Part No`, `Part #`, `SKU`, `Item Code` |
| Part Description | `Part Description`, `Description`, `Item`, `Part Name` |
| Quantity | `Qty`, `Quantity` |
| Unit Price | `Unit Price`, `Price`, `Rate` |
| Total Price *(required)* | `Total Price`, `Total Amount`, `Amount`, `Line Total`, `Total` |

Only **Unit Price** and **Total Price** are strictly mandatory columns for the check to run at all — Part Number and Part Description are read and displayed if present, but their absence won't fail the check on their own.

## A working example (real, verified output)

This is the actual text layer of `samples/invoice_clean.pdf`, extracted by the library the tool actually uses — not a mockup:

```
INVOICE #INV-1001
Vendor: Acme Precision Parts Ltd.
Supplier Address: 42 Industrial Estate Road, Coimbatore, Tamil Nadu 641021
Bill To: GE HealthCare Sourcing Dept.
Invoice Date: 15 Jan 2026
Order Number: PO-55210
Payment Terms: Net 30 days

Part No          Part Description                Qty   Unit Price   Total Price
PN-1001           Bearing, Ball 6203-2RS           10    $4.50        $45.00
PN-1002           Gasket, Rubber O-Ring 12mm        25    $1.20        $30.00
PN-1003           Bracket, Steel Mounting L-Type     5    $8.00        $40.00

Grand Total: $115.00
```

This passes every check cleanly: all 4 document fields present and labeled, both required table columns present, every line's `Qty x Unit Price` matches its stated total, and the grand total reconciles against the recalculated sum. See it run for real in [README.md](./README.md#sample-output).

## For Excel (.xlsx / .xls)

Same label/column logic applies, but on an actual spreadsheet grid rather than PDF text:
- **Document fields:** either a label and its value in two adjacent cells in the same row (e.g. cell A1 = `Order Number`, cell B1 = `PO-55210`), or both in one cell as `Order Number: PO-55210`
- **Line-item table:** a real header row using any of the column names above, with data rows immediately following

## What does NOT work well on the free path

- **Scanned/photographed invoices** — no text layer at all; requires `g` or `a` (AI vision) mode, there's no free text-based option for these
- **Value positioned above its label** instead of after it (see [INPUT_FORMAT_GUIDE.md](./INPUT_FORMAT_GUIDE.md) for why) — use AI mode
- **Handwritten signatures** — a visual mark, not text; only AI vision mode can even attempt to detect presence, and it can't verify authenticity
- **Completely custom/unlisted header wording** the table above doesn't cover — the free path won't recognize it as a match; AI mode will still generally understand it from context
