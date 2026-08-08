"""
Invoice Compliance Checker -- Streamlit Web App
================================================
A web front end over checker_core.py (the same field/math/HS-6 logic as
the original CLI tool), with:

  - Sign up / sign in (SQLite + pbkdf2_hmac, no compiled deps)
  - File upload (PDF / Excel / JPG / PNG, single or multiple)
  - Dropdowns instead of terminal prompts (check type, HS-6/AI provider,
    custom mandatory fields)
  - Neatly presented results (status badges, per-line breakdown, grand
    total reconciliation, HS-6 classification)
  - A saved history of every run, filterable by date range and status
  - Separate downloadable reports for Invoice Compliance runs vs.
    Supporting Document Audit runs

Run with:  streamlit run app.py
"""

import os
import io
import zipfile
import tempfile
from datetime import date, timedelta

import streamlit as st

import auth
import storage
import checker_core as core

# ---------------------------------------------------------------------------
# Page config + light styling
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Invoice Compliance Checker",
    page_icon="\U0001F4CB",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .status-pass {background:#e6f4ea;color:#1e7e34;padding:4px 12px;border-radius:14px;
                  font-weight:600;display:inline-block;font-size:0.85rem;}
    .status-fail {background:#fdecea;color:#c0392b;padding:4px 12px;border-radius:14px;
                  font-weight:600;display:inline-block;font-size:0.85rem;}
    .status-incomplete {background:#fff8e1;color:#a97c00;padding:4px 12px;border-radius:14px;
                  font-weight:600;display:inline-block;font-size:0.85rem;}
    div[data-testid="stMetricValue"] {font-size:1.6rem;}
    .field-ok {color:#1e7e34;}
    .field-bad {color:#c0392b;}
    .block-container {padding-top: 2rem;}
</style>
""", unsafe_allow_html=True)


def status_badge(status):
    cls = {"PASS": "status-pass", "FAIL": "status-fail", "INCOMPLETE": "status-incomplete"}.get(status, "status-fail")
    return f'<span class="{cls}">{status}</span>'


# ---------------------------------------------------------------------------
# DB init (cached connection for the session)
# ---------------------------------------------------------------------------

@st.cache_resource
def get_db():
    conn = auth.get_connection()
    auth.init_auth_tables(conn)
    storage.init_report_tables(conn)
    return conn


conn = get_db()

if "user" not in st.session_state:
    st.session_state.user = None
if "last_results" not in st.session_state:
    st.session_state.last_results = []  # list of (mode, filename, status, reason, report_dict)


# ---------------------------------------------------------------------------
# Auth screens
# ---------------------------------------------------------------------------

def render_auth_screen():
    st.title("\U0001F4CB Invoice Compliance Checker")
    st.caption("Trade-compliance document checking -- fields, math, grand-total reconciliation, HS-6 classification.")

    tab_signin, tab_signup = st.tabs(["Sign In", "Sign Up"])

    with tab_signin:
        with st.form("signin_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign In", use_container_width=True)
            if submitted:
                user = auth.verify_user(conn, username.strip(), password)
                if user:
                    st.session_state.user = user
                    st.rerun()
                else:
                    st.error("Incorrect username or password.")

    with tab_signup:
        with st.form("signup_form"):
            new_username = st.text_input("Choose a username")
            new_email = st.text_input("Email (optional)")
            new_password = st.text_input("Choose a password", type="password")
            confirm_password = st.text_input("Confirm password", type="password")
            submitted = st.form_submit_button("Create Account", use_container_width=True)
            if submitted:
                if new_password != confirm_password:
                    st.error("Passwords don't match.")
                else:
                    ok, msg = auth.create_user(conn, new_username.strip(), new_password, new_email.strip() or None)
                    if ok:
                        st.success(f"{msg} You can sign in now.")
                    else:
                        st.error(msg)


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

def render_sidebar():
    user = st.session_state.user
    st.sidebar.markdown(f"### \U0001F464 {user['username']}")
    st.sidebar.caption(f"Role: {user['role']}")
    page = st.sidebar.radio(
        "Navigate",
        ["Dashboard", "New Check", "Reports & Downloads", "Account"],
        label_visibility="collapsed",
    )
    st.sidebar.divider()
    if st.sidebar.button("Log out", use_container_width=True):
        st.session_state.user = None
        st.session_state.last_results = []
        st.rerun()
    return page


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def render_dashboard():
    user = st.session_state.user
    st.title("Dashboard")

    counts = storage.dashboard_counts(conn, user["id"])
    total = sum(c["n"] for c in counts)
    passes = sum(c["n"] for c in counts if c["status"] == "PASS")
    fails = sum(c["n"] for c in counts if c["status"] == "FAIL")
    incomplete = sum(c["n"] for c in counts if c["status"] == "INCOMPLETE")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total documents checked", total)
    c2.metric("Pass", passes)
    c3.metric("Fail", fails)
    c4.metric("Incomplete", incomplete)

    if total == 0:
        st.info("No checks run yet -- head to **New Check** to upload your first document.")
        return

    st.subheader("Pass / Fail by document type")
    by_mode = {}
    for c in counts:
        by_mode.setdefault(c["mode"], {"PASS": 0, "FAIL": 0, "INCOMPLETE": 0})
        by_mode[c["mode"]][c["status"]] = c["n"]

    chart_data = {
        "Invoice Compliance": by_mode.get("invoice", {"PASS": 0, "FAIL": 0, "INCOMPLETE": 0}),
        "Supporting Document Audit": by_mode.get("audit", {"PASS": 0, "FAIL": 0, "INCOMPLETE": 0}),
    }
    import pandas as pd
    df = pd.DataFrame(chart_data).T
    st.bar_chart(df)

    st.subheader("Most recent checks")
    recent = storage.query_reports(conn, user["id"])[:8]
    for r in recent:
        cols = st.columns([3, 2, 2, 5])
        cols[0].write(r["filename"])
        cols[1].write("Invoice" if r["mode"] == "invoice" else "Audit")
        cols[2].markdown(status_badge(r["status"]), unsafe_allow_html=True)
        cols[3].caption(r["reason"] or "")


# ---------------------------------------------------------------------------
# New Check page
# ---------------------------------------------------------------------------

def render_new_check():
    user = st.session_state.user
    st.title("New Compliance Check")

    check_type = st.selectbox(
        "Check type",
        ["Invoice Compliance Check", "Supporting Document Audit"],
        help="Invoice mode checks Order No., Supplier Address, Payment Terms, Date, "
             "line-item math, and grand-total reconciliation. Audit mode checks only "
             "the fields you name below (for packing lists, COOs, bills of lading, etc.).",
    )
    mode = "invoice" if check_type == "Invoice Compliance Check" else "audit"

    col_a, col_b = st.columns(2)
    with col_a:
        if mode == "invoice":
            hs6_choice = st.selectbox(
                "HS-6 tariff classification",
                ["None", "USITC keyword search (free, no key)", "AI -- Google Gemini (free tier)", "AI -- Claude API"],
            )
        else:
            hs6_choice = "None"
            ai_choice_audit = st.selectbox(
                "AI reader for scanned/image files",
                ["None (text PDF / Excel only)", "Google Gemini (free tier)", "Claude API"],
            )
    with col_b:
        api_key = None
        provider = None
        hts_lookup = False
        if mode == "invoice":
            if hs6_choice.startswith("USITC"):
                hts_lookup = True
            elif "Gemini" in hs6_choice:
                provider = "gemini"
                api_key = st.text_input("Gemini API key", type="password",
                                         help="Free at aistudio.google.com -> Get API key")
            elif "Claude" in hs6_choice:
                provider = "anthropic"
                api_key = st.text_input("Anthropic API key", type="password",
                                         help="From console.anthropic.com")
        else:
            if "Gemini" in ai_choice_audit:
                provider = "gemini"
                api_key = st.text_input("Gemini API key", type="password")
            elif "Claude" in ai_choice_audit:
                provider = "anthropic"
                api_key = st.text_input("Anthropic API key", type="password")

    custom_fields = []
    if mode == "invoice":
        with st.expander("Add custom mandatory fields (optional)"):
            raw = st.text_input("Comma-separated field names", placeholder="Country of Origin, Incoterm")
            custom_fields = core._parse_field_list(raw)
    else:
        raw = st.text_input(
            "Mandatory fields to check (required for Audit mode)",
            placeholder="Country of Origin, Packing List No., Bill of Lading No.",
        )
        custom_fields = core._parse_field_list(raw)

    st.divider()
    uploaded_files = st.file_uploader(
        "Upload document(s)",
        type=["pdf", "xlsx", "xls", "jpg", "jpeg", "png"],
        accept_multiple_files=True,
        help="PDF, Excel, JPG, or PNG. Scanned PDFs and images need Gemini or Claude selected above.",
    )

    run_disabled = not uploaded_files or (mode == "audit" and not custom_fields)
    if mode == "audit" and not custom_fields:
        st.caption("\u26A0\uFE0F Audit mode needs at least one mandatory field name before it can run.")

    if st.button("\u25B6 Run Compliance Check", type="primary", disabled=run_disabled, use_container_width=True):
        st.session_state.last_results = []
        progress = st.progress(0.0, text="Starting...")
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, uf in enumerate(uploaded_files):
                progress.progress(i / len(uploaded_files), text=f"Checking {uf.name}...")
                tmp_path = os.path.join(tmpdir, uf.name)
                with open(tmp_path, "wb") as f:
                    f.write(uf.getbuffer())

                if mode == "invoice":
                    report = core.check_invoice(
                        tmp_path, hts_lookup=hts_lookup, ai_provider=provider,
                        api_key=api_key, custom_fields=custom_fields,
                    )
                    reason = core.summarize_invoice_reason(report)
                else:
                    report = core.run_audit(
                        tmp_path, custom_fields, ai_provider=provider, api_key=api_key,
                    )
                    reason = core.summarize_audit_reason(report)

                storage.save_report(conn, user["id"], mode, uf.name, report["status"], reason, report)
                st.session_state.last_results.append((mode, uf.name, report["status"], reason, report))
            progress.progress(1.0, text="Done.")
        st.rerun()

    if st.session_state.last_results:
        st.divider()
        st.subheader("Results")
        n_pass = sum(1 for r in st.session_state.last_results if r[2] == "PASS")
        n_fail = sum(1 for r in st.session_state.last_results if r[2] == "FAIL")
        n_inc = sum(1 for r in st.session_state.last_results if r[2] == "INCOMPLETE")
        c1, c2, c3 = st.columns(3)
        c1.metric("Pass", n_pass)
        c2.metric("Fail", n_fail)
        c3.metric("Incomplete", n_inc)

        for r_mode, filename, status, reason, report in st.session_state.last_results:
            with st.expander(f"{filename} \u2014 {status}", expanded=(status != "PASS")):
                st.markdown(status_badge(status), unsafe_allow_html=True)
                if report.get("load_error"):
                    st.error(report["load_error"])
                    continue

                if r_mode == "audit":
                    _render_audit_result(report)
                else:
                    _render_invoice_result(report)


def _field_line(label, ok, value):
    icon = "\u2705" if ok else "\u274C"
    css = "field-ok" if ok else "field-bad"
    shown = value if ok else "MISSING"
    st.markdown(f"{icon} **{label}:** <span class='{css}'>{shown}</span>", unsafe_allow_html=True)


def _render_audit_result(report):
    st.markdown("**Mandatory fields checked**")
    fields_checked = report.get("fields_checked", {})
    missing = set(report.get("missing_fields", []))
    unverified = set(report.get("unverified_fields", []))
    for field in report.get("fields_requested", fields_checked.keys()):
        if field in unverified:
            st.markdown(f"\u2753 **{field}:** could not verify")
        elif field in missing:
            _field_line(field, False, None)
        else:
            _field_line(field, True, fields_checked.get(field))
    if report.get("load_error"):
        st.caption(f"Note: {report['load_error']}")


def _render_invoice_result(report):
    if report.get("extraction_method"):
        st.caption(f"Read via: {report['extraction_method']}")

    st.markdown("**Document details**")
    missing_doc = set(report.get("missing_document_fields", []))
    doc_found = report.get("document_fields_found", {})
    for field in core.REQUIRED_DOCUMENT_FIELDS:
        label = core.DOCUMENT_FIELD_LABELS[field]
        _field_line(label, field not in missing_doc, doc_found.get(field))

    if report.get("custom_fields_requested"):
        st.markdown("**Custom mandatory fields**")
        missing_c = set(report.get("missing_custom_fields", []))
        unverified_c = set(report.get("unverified_custom_fields", []))
        cf_found = report.get("custom_fields_found", {})
        for field in report["custom_fields_requested"]:
            if field in unverified_c:
                st.markdown(f"\u2753 **{field}:** could not verify")
            elif field in missing_c:
                _field_line(field, False, None)
            else:
                _field_line(field, True, cf_found.get(field))

    st.markdown("**Line-item table fields**")
    missing_table = set(report.get("missing_table_fields", []))
    for field in core.REQUIRED_TABLE_FIELDS:
        _field_line(field.replace("_", " ").title(), field not in missing_table, "present")

    line_items = report.get("line_items", [])
    st.markdown(f"**Line items ({len(line_items)})**")
    if line_items:
        import pandas as pd
        table_data = []
        for item in line_items:
            table_data.append({
                "Description": item.get("description"),
                "Part No.": item.get("part_number"),
                "Qty": item.get("qty"),
                "Unit Price": item.get("unit_price"),
                "Total": item.get("total_price"),
                "Math": {True: "OK", False: "MISMATCH", None: "n/a"}.get(item.get("math_ok")),
                "AI HS-6": item.get("ai_hs6"),
                "AI Confidence": item.get("ai_confidence_label"),
                "USITC HS-6": item.get("hs6"),
            })
        df = pd.DataFrame(table_data)
        df = df.dropna(axis=1, how="all")
        st.dataframe(df, use_container_width=True, hide_index=True)

        for n, item in enumerate(line_items, start=1):
            if item.get("ai_reason") or item.get("hts_match_description"):
                with st.container(border=True):
                    st.caption(f"Line {n}: {item.get('description')}")
                    if item.get("ai_reason"):
                        st.write(f"**AI reasoning:** {item['ai_reason']}")
                    if item.get("hts_match_description"):
                        st.write(f"**USITC match:** {item['hts_match_description']} "
                                 f"(~{item.get('hts_confidence')}% overlap)"
                                 + (" \u2014 low reliability" if item.get("hts_low_reliability") else ""))

    ai_line_errors = [(n, item["ai_error"]) for n, item in enumerate(line_items, start=1) if item.get("ai_error")]
    if ai_line_errors:
        st.markdown("**AI HS-6 classification issues**")
        for n, err in ai_line_errors:
            st.warning(f"Line {n}: {err}")

    if report.get("line_math_errors"):
        st.markdown("**Math errors**")
        for err in report["line_math_errors"]:
            st.error(f"{err['description']}: expected {err['expected_total']}, "
                      f"stated {err['stated_total']} (diff {err['diff']})")

    st.markdown("**Grand total reconciliation**")
    gc1, gc2, gc3 = st.columns(3)
    gc1.metric("Stated on invoice", report.get("grand_total_stated") if report.get("grand_total_stated") is not None else "\u2014")
    gc2.metric("Recalculated (qty \u00d7 price)", report.get("grand_total_recalculated"))
    if report.get("grand_total_stated") is not None:
        gc3.metric("Match?", "MISMATCH" if report.get("grand_total_mismatch") else "Matches")
    else:
        gc3.metric("Match?", "Not found")

    if report.get("unverified_line_totals"):
        st.caption(f"{len(report['unverified_line_totals'])} line(s) had missing qty/price and "
                    f"couldn't be independently recalculated.")


# ---------------------------------------------------------------------------
# Reports & Downloads page
# ---------------------------------------------------------------------------

def render_reports_page():
    user = st.session_state.user
    st.title("Reports & Downloads")

    st.markdown("Filter your check history, then download **Invoice Compliance** and "
                "**Supporting Document Audit** reports separately.")

    c1, c2, c3 = st.columns(3)
    with c1:
        date_from = st.date_input("From date", value=date.today() - timedelta(days=30))
    with c2:
        date_to = st.date_input("To date", value=date.today())
    with c3:
        status_filter = st.selectbox("Status", ["All", "PASS", "FAIL", "INCOMPLETE"])

    status_arg = None if status_filter == "All" else status_filter

    if date_from > date_to:
        st.error("'From date' must be on or before 'To date'.")
        return

    invoice_rows = storage.query_reports(conn, user["id"], mode="invoice", status=status_arg,
                                          date_from=date_from, date_to=date_to)
    audit_rows = storage.query_reports(conn, user["id"], mode="audit", status=status_arg,
                                        date_from=date_from, date_to=date_to)

    tab_inv, tab_audit = st.tabs([
        f"Invoice Compliance ({len(invoice_rows)})",
        f"Supporting Document Audit ({len(audit_rows)})",
    ])

    with tab_inv:
        _render_report_tab(invoice_rows, "Invoice Compliance", "invoice")

    with tab_audit:
        _render_report_tab(audit_rows, "Supporting Document Audit", "audit")


def _render_report_tab(rows, label, mode):
    if not rows:
        st.info(f"No {label} reports in this date/status range.")
        return

    table_rows = [{
        "Date": r["created_at"][:19].replace("T", " "),
        "File": r["filename"],
        "Status": r["status"],
        "Reason": r["reason"],
    } for r in rows]
    import pandas as pd
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

    dc1, dc2 = st.columns(2)
    with dc1:
        csv_bytes = storage.reports_to_csv_bytes(rows, label)
        st.download_button(
            f"\u2B07 Download {label} summary (CSV)",
            data=csv_bytes,
            file_name=f"{mode}_reports_{date.today().isoformat()}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with dc2:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in rows:
                text = storage.report_to_text(r)
                safe_name = f"{r['id']}_{os.path.splitext(r['filename'])[0]}.txt"
                zf.writestr(safe_name, text)
        st.download_button(
            f"\u2B07 Download {label} details (ZIP of .txt)",
            data=zip_buf.getvalue(),
            file_name=f"{mode}_reports_detailed_{date.today().isoformat()}.zip",
            mime="application/zip",
            use_container_width=True,
        )

    with st.expander("View an individual report"):
        options = {f"{r['filename']} ({r['status']}, {r['created_at'][:10]})": r for r in rows}
        choice = st.selectbox("Select a report", list(options.keys()), key=f"select_{mode}")
        if choice:
            st.code(storage.report_to_text(options[choice]), language=None)


# ---------------------------------------------------------------------------
# Account page
# ---------------------------------------------------------------------------

def render_account_page():
    user = st.session_state.user
    st.title("Account")
    st.write(f"**Username:** {user['username']}")
    st.write(f"**Email:** {user.get('email') or '\u2014'}")
    st.write(f"**Role:** {user['role']}")
    st.write(f"**Member since:** {user['created_at'][:10]}")

    st.divider()
    st.subheader("Change password")
    with st.form("change_pw"):
        old_pw = st.text_input("Current password", type="password")
        new_pw = st.text_input("New password", type="password")
        confirm_pw = st.text_input("Confirm new password", type="password")
        submitted = st.form_submit_button("Update password")
        if submitted:
            if not auth.verify_user(conn, user["username"], old_pw):
                st.error("Current password is incorrect.")
            elif new_pw != confirm_pw:
                st.error("New passwords don't match.")
            else:
                err = auth.validate_password(new_pw)
                if err:
                    st.error(err)
                else:
                    salt, pw_hash = auth._hash_password(new_pw)
                    conn.execute("UPDATE users SET salt=?, password_hash=? WHERE id=?",
                                 (salt, pw_hash, user["id"]))
                    conn.commit()
                    st.success("Password updated.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not st.session_state.user:
        render_auth_screen()
        return

    page = render_sidebar()
    if page == "Dashboard":
        render_dashboard()
    elif page == "New Check":
        render_new_check()
    elif page == "Reports & Downloads":
        render_reports_page()
    elif page == "Account":
        render_account_page()


if __name__ == "__main__":
    main()
