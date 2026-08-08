"""
Backend bridge.
-----------------
Deliberately thin. All actual compliance logic (field checks, math
validation, grand total recalculation, HS-6 classification, AI vision
extraction, custom fields) lives in invoice_compliance_checker.py -- the
same module the command-line tool uses, already tested against real
samples. This file does NOT reimplement any of that; it just adapts the
CLI's check_invoice()/run_audit() functions (which take plain arguments
and return a dict, no input() calls involved) for use from the web UI, and
extracts a few summary fields for the database/dashboard.
"""

import time
import invoice_compliance_checker as icc


def process_invoice(file_path, hts_lookup=False, ai_provider=None, api_key=None, custom_fields=None):
    """Run the same check_invoice() the CLI uses. Returns (report_dict, elapsed_seconds)."""
    start = time.time()
    report = icc.check_invoice(file_path, hts_lookup=hts_lookup, ai_provider=ai_provider,
                                api_key=api_key, custom_fields=custom_fields or [])
    elapsed = round(time.time() - start, 2)
    return report, elapsed


def process_audit(file_path, custom_fields, ai_provider=None, api_key=None):
    """Run the same run_audit() the CLI uses. Returns (report_dict, elapsed_seconds)."""
    start = time.time()
    report = icc.run_audit(file_path, custom_fields, ai_provider=ai_provider, api_key=api_key)
    elapsed = round(time.time() - start, 2)
    return report, elapsed


def extract_summary_fields(report):
    """Pull a few searchable fields out of an invoice report for the reports
    table (supplier, invoice/order number, HS codes found)."""
    supplier = None
    doc_fields = report.get("document_fields_found") or {}
    if doc_fields.get("supplier_address"):
        # Just the first ~60 chars for a compact, searchable label
        supplier = str(doc_fields["supplier_address"])[:60]
    order_number = doc_fields.get("order_number")
    hs_codes = [item["hs6"] for item in report.get("line_items", []) if item.get("hs6")]
    hs_codes += [item["ai_hs6"] for item in report.get("line_items", []) if item.get("ai_hs6")]
    return {
        "supplier": supplier,
        "order_number": order_number,
        "hs_codes": sorted(set(hs_codes)) if hs_codes else None,
    }
