"""
storage.py
----------
Persists every check run (invoice or audit mode) to SQLite so the Reports
page can filter by date range / status / mode and generate separate
downloadable reports for invoices vs. supporting documents.
"""

import json
import csv
import io
from datetime import datetime, timezone


def init_report_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mode TEXT NOT NULL,               -- 'invoice' or 'audit'
            filename TEXT NOT NULL,
            status TEXT NOT NULL,             -- PASS / FAIL / INCOMPLETE
            reason TEXT,
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL,          -- ISO timestamp
            created_date TEXT NOT NULL         -- YYYY-MM-DD, for fast date filtering
        )
    """)
    conn.commit()


def _jsonable(obj):
    """Recursively convert sets (used throughout checker_core) to sorted
    lists so the report dict can be stored as JSON."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    return obj


def save_report(conn, user_id, mode, filename, status, reason, report_dict):
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO reports (user_id, mode, filename, status, reason, report_json, "
        "created_at, created_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, mode, filename, status, reason,
            json.dumps(_jsonable(report_dict)),
            now.isoformat(), now.strftime("%Y-%m-%d"),
        ),
    )
    conn.commit()


def query_reports(conn, user_id, mode=None, status=None, date_from=None, date_to=None):
    """Returns matching rows (most recent first) as a list of dicts."""
    clauses = ["user_id = ?"]
    params = [user_id]
    if mode and mode != "all":
        clauses.append("mode = ?")
        params.append(mode)
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if date_from:
        clauses.append("created_date >= ?")
        params.append(str(date_from))
    if date_to:
        clauses.append("created_date <= ?")
        params.append(str(date_to))

    sql = f"SELECT * FROM reports WHERE {' AND '.join(clauses)} ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def dashboard_counts(conn, user_id):
    rows = conn.execute(
        "SELECT mode, status, COUNT(*) as n FROM reports WHERE user_id = ? GROUP BY mode, status",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def reports_to_csv_bytes(rows, mode_label):
    """Build a flat CSV summary (one row per checked document) for download."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "File", "Type", "Status", "Reason"])
    for r in rows:
        writer.writerow([
            r["created_at"][:19].replace("T", " "),
            r["filename"],
            mode_label,
            r["status"],
            r["reason"] or "",
        ])
    return buf.getvalue().encode("utf-8")


def report_to_text(row):
    """Render one stored report_json back into a CLI-style plain-text report,
    for the detailed per-file .txt export."""
    data = json.loads(row["report_json"])
    W = 60
    lines = []
    lines.append("=" * W)
    header = "INVOICE" if row["mode"] == "invoice" else "DOCUMENT AUDIT"
    lines.append(f" {header}: {row['filename']}")
    lines.append("=" * W)

    if data.get("load_error"):
        lines.append(f"\n  Could not process: {data['load_error']}")
        lines.append("")
        lines.append("=" * W)
        lines.append(f" RESULT: {row['status']}")
        lines.append("=" * W)
        return "\n".join(lines)

    if row["mode"] == "audit":
        lines.append("\nMANDATORY FIELDS CHECKED")
        lines.append("-" * W)
        fields_checked = data.get("fields_checked", {})
        missing = set(data.get("missing_fields", []))
        unverified = set(data.get("unverified_fields", []))
        for field in data.get("fields_requested", fields_checked.keys()):
            if field in unverified:
                lines.append(f"  [? ] {field}: COULD NOT VERIFY")
            elif field in missing:
                lines.append(f"  [X ] {field}: NOT FOUND")
            else:
                lines.append(f"  [OK] {field}: {fields_checked.get(field)}")
    else:
        lines.append("\nDOCUMENT DETAILS")
        lines.append("-" * W)
        labels = {
            "order_number": "Order Number", "supplier_address": "Supplier Address",
            "payment_terms": "Payment Terms", "invoice_date": "Date",
        }
        missing_doc = set(data.get("missing_document_fields", []))
        doc_found = data.get("document_fields_found", {})
        for field, label in labels.items():
            if field in missing_doc:
                lines.append(f"  [X ] {label:<16}: MISSING")
            else:
                lines.append(f"  [OK] {label:<16}: {doc_found.get(field)}")

        line_items = data.get("line_items", [])
        lines.append(f"\nLINE ITEMS ({len(line_items)})")
        for n, item in enumerate(line_items, start=1):
            lines.append("-" * W)
            lines.append(f"  {n}) {item.get('description')}")
            if item.get("part_number"):
                lines.append(f"     Part No   : {item['part_number']}")
            lines.append(f"     Qty/Price : {item.get('qty')} x {item.get('unit_price')}")
            math_display = {True: "OK", False: "MISMATCH", None: "n/a"}.get(item.get("math_ok"))
            lines.append(f"     Total     : {item.get('total_price')}   [{math_display}]")
            if item.get("ai_hs6"):
                lines.append(f"     AI HS-6   : {item['ai_hs6']} ({item.get('ai_confidence_label')})")
            if item.get("hs6"):
                lines.append(f"     USITC HS-6: {item['hs6']} (~{item.get('hts_confidence')}%)")

        lines.append("-" * W)
        lines.append("\nGRAND TOTAL")
        lines.append("-" * W)
        lines.append(f"  Stated       : {data.get('grand_total_stated')}")
        lines.append(f"  Recalculated : {data.get('grand_total_recalculated')}")
        if data.get("grand_total_mismatch"):
            lines.append("  [X ] MISMATCH")
        elif data.get("grand_total_stated") is not None:
            lines.append("  [OK] matches")

    lines.append("")
    lines.append("=" * W)
    lines.append(f" RESULT: {row['status']}")
    lines.append("=" * W)
    return "\n".join(lines)
