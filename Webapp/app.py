"""
Compliance Auditing Platform -- Streamlit web UI
---------------------------------------------------
This is a UI layer on top of invoice_compliance_checker.py (the CLI tool's
backend, reused unmodified via backend_bridge.py) plus a local SQLite
database for user accounts, report history, and an audit log.

SCOPE NOTE: this is a genuine, working single-instance application suitable
for local/internal use or a single Streamlit Cloud deployment. It is not a
substitute for a real multi-tenant production system -- see README_WEBAPP.md
for exactly what that distinction means and what would need to change for a
true production deployment (a server-based database, a real auth provider,
HTTPS, etc.). Nothing here pretends otherwise.
"""

import os
import time
import json
import uuid
import datetime

import streamlit as st
import pandas as pd

import auth
import database as db
import backend_bridge as bridge
import invoice_compliance_checker as icc
from theme import inject_css, status_badge_html

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(APP_DIR, "uploaded_documents")
os.makedirs(UPLOAD_DIR, exist_ok=True)

SUPPORTED_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls")

SUPPORTING_DOC_TYPES = {
    "Packing List": ["PO Number", "Package Count", "Net Weight", "Gross Weight"],
    "Bill of Lading": ["Shipper", "Consignee", "Vessel Name", "Port of Loading", "Port of Discharge"],
    "Certificate of Origin": ["Exporter", "Importer", "Country of Origin", "Certificate Number", "Signature", "Date"],
    "Insurance Certificate": ["Policy Number", "Insured Value", "Coverage Type", "Date"],
    "Inspection Certificate": ["Inspection Date", "Inspector Name", "Result", "Certificate Number"],
    "Purchase Order": ["PO Number", "Buyer", "Seller", "Date", "Payment Terms"],
    "Delivery Note": ["Delivery Number", "Recipient", "Date", "Item Count"],
    "Letter of Credit": ["LC Number", "Issuing Bank", "Beneficiary", "Expiry Date", "Amount"],
    "Export Declaration": ["Declaration Number", "Exporter", "HS Code", "Destination Country"],
    "Import Declaration": ["Declaration Number", "Importer", "HS Code", "Country of Origin"],
    "Other": [],
}


st.set_page_config(page_title="Compliance Auditing Platform", page_icon="\U0001F4CB",
                    layout="wide", initial_sidebar_state="expanded")

db.init_db()

if "dark_mode" not in st.session_state:
    st.session_state["dark_mode"] = False
inject_css(dark_mode=st.session_state["dark_mode"])


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def save_uploaded_file(uploaded_file):
    """Persist a Streamlit UploadedFile to disk (needed since the backend
    works on file paths, not in-memory objects) and return the path."""
    ext = os.path.splitext(uploaded_file.name)[1]
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(UPLOAD_DIR, unique_name)
    with open(dest, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return dest


def metric_card(label, value, help_text=None):
    st.markdown(f"""
        <div class="metric-card">
            <div style="font-size:0.85rem; color:#64748b; font-weight:600;">{label}</div>
            <div style="font-size:1.8rem; font-weight:700; margin-top:0.2rem;">{value}</div>
        </div>
    """, unsafe_allow_html=True)


def fmt_ts(ts):
    if ts is None:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def get_ai_key_for(provider):
    """Look up an API key already entered this session, or None."""
    if provider == "gemini":
        return st.session_state.get("gemini_api_key")
    if provider == "anthropic":
        return st.session_state.get("anthropic_api_key")
    return None


def icc_summarize_invoice(report):
    return icc.summarize_invoice_reason(report)


def icc_summarize_audit(report):
    return icc.summarize_audit_reason(report)


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------

def render_login_page():
    col1, col2, col3 = st.columns([1, 1.2, 1])
    with col2:
        st.markdown("<div style='text-align:center; font-size:2.5rem;'>\U0001F4CB</div>", unsafe_allow_html=True)
        st.markdown("<div class='app-header' style='text-align:center;'>Compliance Auditing Platform</div>",
                    unsafe_allow_html=True)
        st.markdown("<div class='app-subheader' style='text-align:center;'>Sign in to continue</div>",
                    unsafe_allow_html=True)

        if "show_password" not in st.session_state:
            st.session_state["show_password"] = False
        if "auth_view" not in st.session_state:
            st.session_state["auth_view"] = "login"

        if st.session_state["auth_view"] == "login":
            with st.form("login_form"):
                username = st.text_input("Username or Email")
                password = st.text_input("Password", type="password" if not st.session_state["show_password"] else "default")
                show_pw = st.checkbox("Show password", value=st.session_state["show_password"])
                remember = st.checkbox("Remember me (extends session timeout)")
                submitted = st.form_submit_button("Log In", use_container_width=True, type="primary")

                if show_pw != st.session_state["show_password"]:
                    st.session_state["show_password"] = show_pw
                    st.rerun()

                if submitted:
                    if not username or not password:
                        st.error("Enter both username/email and password.")
                    else:
                        success, message = auth.login(username, password)
                        if success:
                            st.session_state["session_timeout_seconds"] = (4 * 60 * 60) if remember else auth.SESSION_TIMEOUT_SECONDS
                            st.session_state["last_activity"] = time.time()
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)

            fcol1, fcol2 = st.columns(2)
            with fcol1:
                if st.button("Forgot password?", use_container_width=True):
                    st.session_state["auth_view"] = "forgot"
                    st.rerun()
            with fcol2:
                if st.button("Create an account", use_container_width=True):
                    st.session_state["auth_view"] = "signup"
                    st.rerun()
            st.caption("Default admin login: `admin` / `admin123` (change this immediately)")

        elif st.session_state["auth_view"] == "forgot":
            st.info("Self-service password reset requires an email provider, which isn't wired up in "
                    "this demo deployment. In this build, an **Administrator** resets your password from "
                    "User Management. Contact your administrator, or if you are the administrator, log in "
                    "with the default admin account to reset your own password from Settings.")
            if st.button("Back to login"):
                st.session_state["auth_view"] = "login"
                st.rerun()

        elif st.session_state["auth_view"] == "signup":
            st.markdown("##### Create your account")
            st.caption("New accounts start with **Viewer** access (view, search, and download reports only). "
                      "An administrator can grant Compliance Auditor or Administrator access afterward from "
                      "User Management -- self-signup deliberately can't grant upload/processing/admin rights.")
            with st.form("signup_form"):
                su_username = st.text_input("Choose a username")
                su_email = st.text_input("Email address")
                su_password = st.text_input("Choose a password", type="password")
                su_confirm = st.text_input("Confirm password", type="password")
                su_submitted = st.form_submit_button("Create Account", use_container_width=True, type="primary")

                if su_submitted:
                    if not su_username or not su_email or not su_password:
                        st.error("All fields are required.")
                    elif "@" not in su_email or "." not in su_email.split("@")[-1]:
                        st.error("Enter a valid email address.")
                    elif len(su_password) < 8:
                        st.error("Password must be at least 8 characters.")
                    elif su_password != su_confirm:
                        st.error("Passwords don't match.")
                    elif db.get_user_by_username(su_username):
                        st.error("That username is already taken.")
                    elif db.get_user_by_email(su_email):
                        st.error("An account with that email already exists.")
                    else:
                        db.create_user(su_username, su_email, su_password, "Viewer")
                        db.log_action(su_username, "signup", su_email)
                        st.success(f"Account created for {su_username}. You can log in now.")
                        st.session_state["auth_view"] = "login"
                        time.sleep(1.2)
                        st.rerun()

            if st.button("Back to login", key="signup_back"):
                st.session_state["auth_view"] = "login"
                st.rerun()


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

NAV_ITEMS = [
    ("Dashboard", "\U0001F4CA", ("Administrator", "Compliance Auditor", "Viewer")),
    ("Invoice Compliance", "\U0001F4C4", ("Administrator", "Compliance Auditor")),
    ("Supporting Document Audit", "\U0001F4C1", ("Administrator", "Compliance Auditor")),
    ("Batch Processing", "\U0001F4E6", ("Administrator", "Compliance Auditor")),
    ("Search Reports", "\U0001F50D", ("Administrator", "Compliance Auditor", "Viewer")),
    ("PASS Reports", "\u2705", ("Administrator", "Compliance Auditor", "Viewer")),
    ("FAIL Reports", "\u274C", ("Administrator", "Compliance Auditor", "Viewer")),
    ("INCOMPLETE Reports", "\u2753", ("Administrator", "Compliance Auditor", "Viewer")),
    ("Analytics", "\U0001F4C8", ("Administrator", "Compliance Auditor", "Viewer")),
    ("Audit Trail", "\U0001F4DC", ("Administrator",)),
    ("User Management", "\U0001F465", ("Administrator",)),
    ("Settings", "\u2699\uFE0F", ("Administrator", "Compliance Auditor", "Viewer")),
]


def render_sidebar():
    user = auth.current_user()
    with st.sidebar:
        st.markdown(f"### \U0001F4CB Compliance Platform")
        st.markdown(f"**{user['username']}**  \n{user['role']}")
        st.session_state["dark_mode"] = st.toggle("Dark mode", value=st.session_state["dark_mode"])
        st.divider()

        if "page" not in st.session_state:
            st.session_state["page"] = "Dashboard"

        for label, icon, allowed_roles in NAV_ITEMS:
            if user["role"] not in allowed_roles:
                continue
            if st.button(f"{icon}  {label}", key=f"nav_{label}", use_container_width=True):
                st.session_state["page"] = label
                st.rerun()

        st.divider()
        if st.button("\U0001F6AA  Logout", use_container_width=True):
            auth.logout()
            st.rerun()

    return st.session_state["page"]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def page_dashboard():
    st.markdown("<div class='app-header'>Dashboard</div>", unsafe_allow_html=True)
    st.markdown("<div class='app-subheader'>Compliance processing overview</div>", unsafe_allow_html=True)

    stats = db.dashboard_stats()
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Documents Today", stats["today"])
    with c2:
        metric_card("Total Documents", stats["total"])
    with c3:
        rate = round(100 * stats["pass"] / stats["total"], 1) if stats["total"] else 0
        metric_card("Compliance Rate", f"{rate}%")
    with c4:
        metric_card("Active Users (7d)", stats["active_users_7d"])

    c5, c6, c7, c8 = st.columns(4)
    with c5:
        metric_card("PASS", stats["pass"])
    with c6:
        metric_card("FAIL", stats["fail"])
    with c7:
        metric_card("INCOMPLETE", stats["incomplete"])
    with c8:
        avg = f"{stats['avg_processing_time']}s" if stats["avg_processing_time"] else "-"
        metric_card("Avg. Processing Time", avg)

    st.markdown("&nbsp;", unsafe_allow_html=True)
    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("Daily Processing Volume (14 days)")
        volume = db.daily_volume(14)
        if volume:
            df = pd.DataFrame({"Date": list(volume.keys()), "Documents": list(volume.values())}).set_index("Date")
            st.bar_chart(df)
        else:
            st.caption("No documents processed yet.")

    with col_b:
        st.subheader("PASS vs FAIL vs INCOMPLETE")
        if stats["total"]:
            df2 = pd.DataFrame({"Status": ["PASS", "FAIL", "INCOMPLETE"],
                                 "Count": [stats["pass"], stats["fail"], stats["incomplete"]]}).set_index("Status")
            st.bar_chart(df2)
        else:
            st.caption("No documents processed yet.")

    st.subheader("Recent Activity")
    recent = db.search_reports(limit=10)
    if recent:
        df3 = pd.DataFrame([{
            "File": r["filename"], "Status": r["status"], "Type": r["doc_type"],
            "Processed By": r["processed_by"], "When": fmt_ts(r["processed_at"]),
        } for r in recent])
        st.dataframe(df3, use_container_width=True, hide_index=True)
    else:
        st.caption("Nothing processed yet -- try Invoice Compliance or Supporting Document Audit from the sidebar.")


# ---------------------------------------------------------------------------
# Invoice Compliance module
# ---------------------------------------------------------------------------

def render_hs_mode_picker(key_prefix=""):
    """Shared HS-6 mode selector + API key capture. Returns (hts_lookup, ai_provider, api_key)."""
    mode = st.radio(
        "HS-6 Classification",
        ["None", "USITC keyword search (free)", "Google Gemini (free, AI)", "Claude API (paid, AI)"],
        key=f"{key_prefix}_hs_mode",
        help="Scanned PDFs and image files (JPG/PNG) require Gemini or Claude -- there's no free-text option for those.",
    )
    hts_lookup = mode.startswith("USITC")
    ai_provider = "gemini" if "Gemini" in mode else ("anthropic" if "Claude" in mode else None)
    api_key = None
    if ai_provider:
        existing = get_ai_key_for(ai_provider)
        api_key = st.text_input(
            f"{'Gemini' if ai_provider == 'gemini' else 'Anthropic'} API key",
            value=existing or "", type="password", key=f"{key_prefix}_{ai_provider}_key",
            help="Stored only for this browser session, never written to disk.",
        )
        if api_key:
            st.session_state[f"{ai_provider}_api_key"] = api_key
    return hts_lookup, ai_provider, api_key


def page_invoice_compliance():
    st.markdown("<div class='app-header'>Invoice Compliance</div>", unsafe_allow_html=True)
    st.markdown("<div class='app-subheader'>Upload invoices to check required fields, math, grand total, "
                "and optionally HS-6 classification.</div>", unsafe_allow_html=True)

    uploaded_files = st.file_uploader(
        "\U0001F4E4 Upload invoice(s) -- drag & drop or browse. Supports PDF, scanned PDF, JPG, PNG, XLS, XLSX",
        type=["pdf", "jpg", "jpeg", "png", "xlsx", "xls"],
        accept_multiple_files=True,
    )

    with st.expander("Processing options", expanded=True):
        hts_lookup, ai_provider, api_key = render_hs_mode_picker(key_prefix="invoice")
        use_custom = st.checkbox("Check additional custom mandatory fields")
        custom_fields = []
        if use_custom:
            raw = st.text_input("Field names, comma-separated (e.g. Country of Origin, Incoterm)")
            custom_fields = [f.strip() for f in raw.split(",") if f.strip()]

    if uploaded_files and st.button("Run Compliance Check", type="primary"):
        if ai_provider and not api_key:
            st.error(f"Enter your {ai_provider} API key above, or choose a mode that doesn't need one.")
            return

        results = []
        for uf in uploaded_files:
            with st.status(f"Processing {uf.name}...", expanded=True) as status_box:
                status_box.write("Saving upload...")
                saved_path = save_uploaded_file(uf)
                db.log_action(auth.current_user()["username"], "upload", uf.name)

                step_desc = "Reading document"
                if ai_provider:
                    step_desc += f" (AI vision fallback available via {ai_provider} if scanned)"
                status_box.write(step_desc + "...")

                status_box.write("Validating required fields, line-item math, and grand total"
                                  + (", classifying HS-6 codes" if (hts_lookup or ai_provider) else "") + "...")
                report, elapsed = bridge.process_invoice(saved_path, hts_lookup=hts_lookup,
                                                           ai_provider=ai_provider, api_key=api_key,
                                                           custom_fields=custom_fields)
                report["_ui_processing_options"] = {"hts_lookup": hts_lookup, "ai_provider": ai_provider,
                                                      "custom_fields": custom_fields}

                summary = bridge.extract_summary_fields(report)
                report_id = db.save_report(
                    filename=uf.name, doc_type="Invoice", status=report["status"],
                    processed_by=auth.current_user()["username"], report_dict=report,
                    supplier=summary["supplier"], order_number=summary["order_number"],
                    hs_codes=summary["hs_codes"], processing_time_seconds=elapsed,
                    file_path=saved_path,
                )
                db.log_action(auth.current_user()["username"], "process", f"{uf.name} -> {report['status']}")
                status_box.update(label=f"{uf.name} -- {report['status']}", state="complete")
                results.append((uf.name, report_id, report))

        st.toast(f"Processed {len(results)} document(s)", icon="\u2705")
        st.subheader("Results")
        for fname, report_id, report in results:
            render_invoice_report(report, report_id, expanded=(len(results) == 1))


def render_invoice_report(report, report_id=None, expanded=True):
    """Shared, detailed renderer for an invoice-mode report dict -- used
    right after processing and from the Report Viewer."""
    st.markdown(f"#### {report['file']}  {status_badge_html(report['status'])}", unsafe_allow_html=True)

    if report.get("load_error"):
        st.error(report["load_error"])
        return

    synopsis = icc_summarize_invoice(report)
    st.markdown(f"<div class='quick-synopsis'>{synopsis}</div>", unsafe_allow_html=True)

    if report.get("extraction_method"):
        st.caption(f"Read via: {report['extraction_method']}")

    with st.expander("Document Fields", expanded=expanded):
        for field, label in [("order_number", "Order Number"), ("supplier_address", "Supplier Address"),
                              ("payment_terms", "Payment Terms"), ("invoice_date", "Date")]:
            value = report["document_fields_found"].get(field)
            if field in report["missing_document_fields"]:
                st.markdown(f"- \u274C **{label}**: MISSING")
            else:
                st.markdown(f"- \u2705 **{label}**: {value}")

    if report.get("custom_fields_requested"):
        with st.expander("Custom Mandatory Fields", expanded=expanded):
            for field in report["custom_fields_requested"]:
                if field in report.get("unverified_custom_fields", set()):
                    st.markdown(f"- \u2753 **{field}**: COULD NOT VERIFY")
                elif field in report["missing_custom_fields"]:
                    st.markdown(f"- \u274C **{field}**: NOT FOUND")
                else:
                    st.markdown(f"- \u2705 **{field}**: {report['custom_fields_found'].get(field)}")
            if report.get("custom_fields_error"):
                st.caption(f"Note: {report['custom_fields_error']}")

    with st.expander(f"Line Items ({len(report['line_items'])})", expanded=expanded):
        for item in report["line_items"]:
            math_flag = {"True": "\u2705", "False": "\u274C", "None": "\u2796"}[str(item["math_ok"])]
            st.markdown(f"**{item['description']}** (P/N: {item.get('part_number', '-')}) "
                        f"{math_flag}  \nQty {item['qty']} x {item['unit_price']} = {item['total_price']}")
            if item.get("ai_hs6"):
                st.caption(f"AI HS-6: {item['ai_hs6']} -- {item['ai_official_description']} "
                          f"({item['ai_confidence_label']}, ~{item['ai_confidence_percent']}%)")
                st.caption(f"Reason: {item['ai_reason']}")
            elif item.get("ai_error"):
                st.caption(f"AI HS-6: {item['ai_error']}")
            if item.get("hs6"):
                reliability = " (LOW RELIABILITY)" if item.get("hts_low_reliability") else ""
                st.caption(f"USITC ref: {item['hs6'].replace('.', '')} -- "
                          f"{item['hts_match_description']}{reliability}")
            st.divider()

    if report["line_math_errors"]:
        with st.expander("Math Errors", expanded=True):
            for err in report["line_math_errors"]:
                st.markdown(f"- **{err['description']}**: expected {err['expected_total']}, "
                           f"stated {err['stated_total']} (diff {err['diff']})")

    with st.expander("Grand Total Validation", expanded=expanded):
        st.markdown(f"- Stated on invoice: **{report['grand_total_stated']}**")
        st.markdown(f"- Recalculated (qty x price): **{report['grand_total_recalculated']}**")
        if report["grand_total_mismatch"]:
            st.error("Mismatch between stated and recalculated grand total.")
        elif report["grand_total_stated"] is not None:
            st.success("Matches.")

    cols = st.columns(3)
    with cols[0]:
        st.download_button("Download JSON", data=json.dumps(report, indent=2, default=str),
                          file_name=f"{report['file']}_report.json", mime="application/json",
                          key=f"dl_json_{report_id}_{report['file']}")
    with cols[1]:
        csv_rows = pd.DataFrame(report["line_items"])
        st.download_button("Download Line Items CSV", data=csv_rows.to_csv(index=False),
                          file_name=f"{report['file']}_lines.csv", mime="text/csv",
                          key=f"dl_csv_{report_id}_{report['file']}")


# ---------------------------------------------------------------------------
# Supporting Document Audit module
# ---------------------------------------------------------------------------

def page_supporting_document_audit():
    st.markdown("<div class='app-header'>Supporting Document Audit</div>", unsafe_allow_html=True)
    st.markdown("<div class='app-subheader'>Verify mandatory fields on non-invoice supporting documents "
                "-- no invoice math or HS-6 involved.</div>", unsafe_allow_html=True)

    doc_type = st.selectbox("Document Type", list(SUPPORTING_DOC_TYPES.keys()))
    default_fields = SUPPORTING_DOC_TYPES[doc_type]

    uploaded_file = st.file_uploader("\U0001F4E4 Upload document -- drag & drop or browse. Supports PDF, scanned PDF, JPG, PNG, XLS, XLSX",
                                     type=["pdf", "jpg", "jpeg", "png", "xlsx", "xls"])

    st.markdown(f"**Mandatory fields for {doc_type}** (edit as needed):")
    fields_raw = st.text_area("Comma-separated field names", value=", ".join(default_fields), height=80)
    custom_fields = [f.strip() for f in fields_raw.split(",") if f.strip()]

    ai_provider = None
    api_key = None
    ai_choice = st.radio("AI provider for scanned/image files (optional)",
                         ["None (text-based files only)", "Google Gemini (free)", "Claude API (paid)"],
                         key="audit_ai_choice")
    if "Gemini" in ai_choice:
        ai_provider = "gemini"
    elif "Claude" in ai_choice:
        ai_provider = "anthropic"
    if ai_provider:
        existing = get_ai_key_for(ai_provider)
        api_key = st.text_input(f"{'Gemini' if ai_provider == 'gemini' else 'Anthropic'} API key",
                                value=existing or "", type="password", key=f"audit_{ai_provider}_key")
        if api_key:
            st.session_state[f"{ai_provider}_api_key"] = api_key

    if uploaded_file and custom_fields and st.button("Run Document Audit", type="primary"):
        if ai_provider and not api_key:
            st.error(f"Enter your {ai_provider} API key above, or choose 'None'.")
            return

        with st.status(f"Auditing {uploaded_file.name}...", expanded=True) as status_box:
            saved_path = save_uploaded_file(uploaded_file)
            db.log_action(auth.current_user()["username"], "upload", uploaded_file.name)
            status_box.write(f"Checking {len(custom_fields)} mandatory field(s)...")
            report, elapsed = bridge.process_audit(saved_path, custom_fields, ai_provider=ai_provider, api_key=api_key)

            report_id = db.save_report(
                filename=uploaded_file.name, doc_type=doc_type, status=report["status"],
                processed_by=auth.current_user()["username"], report_dict=report,
                processing_time_seconds=elapsed, file_path=saved_path,
            )
            db.log_action(auth.current_user()["username"], "process", f"{uploaded_file.name} -> {report['status']}")
            status_box.update(label=f"{uploaded_file.name} -- {report['status']}", state="complete")

        st.toast("Audit complete", icon="\u2705")
        render_audit_report(report, report_id)


def render_audit_report(report, report_id=None):
    st.markdown(f"#### {report['file']}  {status_badge_html(report['status'])}", unsafe_allow_html=True)
    if report.get("load_error") and not report["fields_checked"]:
        st.error(report["load_error"])
        return
    st.markdown(f"<div class='quick-synopsis'>{icc_summarize_audit(report)}</div>", unsafe_allow_html=True)
    for field in report.get("fields_requested", report["fields_checked"].keys()):
        if field in report.get("unverified_fields", set()):
            st.markdown(f"- \u2753 **{field}**: COULD NOT VERIFY")
        elif field in report["missing_fields"]:
            st.markdown(f"- \u274C **{field}**: NOT FOUND")
        else:
            st.markdown(f"- \u2705 **{field}**: {report['fields_checked'].get(field)}")
    if report.get("load_error"):
        st.caption(f"Note: {report['load_error']}")
    st.download_button("Download JSON", data=json.dumps(report, indent=2, default=str),
                      file_name=f"{report['file']}_audit.json", mime="application/json",
                      key=f"dl_audit_json_{report_id}_{report['file']}")


# ---------------------------------------------------------------------------
# Batch Processing -- summary table first, then drill down (mirrors the CLI)
# ---------------------------------------------------------------------------

def page_batch_processing():
    st.markdown("<div class='app-header'>Batch Processing</div>", unsafe_allow_html=True)
    st.markdown("<div class='app-subheader'>Process many invoices at once -- results shown as a summary "
                "table first, with drill-down into any file's full detail.</div>", unsafe_allow_html=True)

    uploaded_files = st.file_uploader(
        "\U0001F4E4 Upload multiple invoices -- drag & drop or browse. Supports PDF, scanned PDF, JPG, PNG, XLS, XLSX",
        type=["pdf", "jpg", "jpeg", "png", "xlsx", "xls"], accept_multiple_files=True, key="batch_uploader",
    )
    with st.expander("Processing options", expanded=True):
        hts_lookup, ai_provider, api_key = render_hs_mode_picker(key_prefix="batch")

    if uploaded_files and st.button(f"Process {len(uploaded_files)} File(s)", type="primary"):
        if ai_provider and not api_key:
            st.error(f"Enter your {ai_provider} API key above, or choose a mode that doesn't need one.")
            return

        progress = st.progress(0, text="Starting batch...")
        batch_results = []
        for i, uf in enumerate(uploaded_files):
            progress.progress((i) / len(uploaded_files), text=f"Processing {uf.name} ({i+1}/{len(uploaded_files)})...")
            saved_path = save_uploaded_file(uf)
            report, elapsed = bridge.process_invoice(saved_path, hts_lookup=hts_lookup,
                                                       ai_provider=ai_provider, api_key=api_key)
            report["_ui_processing_options"] = {"hts_lookup": hts_lookup, "ai_provider": ai_provider,
                                                  "custom_fields": []}
            summary = bridge.extract_summary_fields(report)
            report_id = db.save_report(
                filename=uf.name, doc_type="Invoice", status=report["status"],
                processed_by=auth.current_user()["username"], report_dict=report,
                supplier=summary["supplier"], order_number=summary["order_number"],
                hs_codes=summary["hs_codes"], processing_time_seconds=elapsed, file_path=saved_path,
            )
            batch_results.append((report_id, report))
        progress.progress(1.0, text="Batch complete.")
        db.log_action(auth.current_user()["username"], "batch_process", f"{len(uploaded_files)} file(s)")
        st.session_state["batch_results"] = batch_results
        st.toast(f"Processed {len(batch_results)} document(s)", icon="\u2705")

    if st.session_state.get("batch_results"):
        batch_results = st.session_state["batch_results"]
        st.subheader("Summary")
        n_pass = sum(1 for _, r in batch_results if r["status"] == "PASS")
        n_fail = sum(1 for _, r in batch_results if r["status"] == "FAIL")
        n_inc = sum(1 for _, r in batch_results if r["status"] == "INCOMPLETE")
        c1, c2, c3 = st.columns(3)
        c1.metric("PASS", n_pass)
        c2.metric("FAIL", n_fail)
        c3.metric("INCOMPLETE", n_inc)

        df = pd.DataFrame([{
            "File": r["file"], "Status": r["status"],
            "Reason": icc_summarize_invoice(r),
        } for _, r in batch_results])
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.subheader("View detail for a file")
        options = {f"{r['file']} ({r['status']})": (rid, r) for rid, r in batch_results}
        pick = st.selectbox("Select a file", ["-- none --"] + list(options.keys()))
        if pick != "-- none --":
            rid, r = options[pick]
            render_invoice_report(r, rid, expanded=True)


# ---------------------------------------------------------------------------
# Search Reports / PASS / FAIL / INCOMPLETE
# ---------------------------------------------------------------------------

def page_search_reports(status_filter=None, page_title="Search Reports"):
    st.markdown(f"<div class='app-header'>{page_title}</div>", unsafe_allow_html=True)

    with st.expander("Filters", expanded=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            filename_q = st.text_input("Filename contains")
            supplier_q = st.text_input("Supplier contains")
        with c2:
            # When status_filter is passed in (PASS/FAIL/INCOMPLETE Reports pages), the `or`
            # short-circuits and the selectbox is never rendered -- those pages intentionally
            # don't let you override the fixed status filter.
            status_q = status_filter or st.selectbox("Status", ["Any", "PASS", "FAIL", "INCOMPLETE"])
            doc_type_q = st.selectbox("Document Type", ["Any", "Invoice"] + list(SUPPORTING_DOC_TYPES.keys()))
        with c3:
            quick = st.selectbox("Quick date filter", ["All time", "Today", "Yesterday", "Last 7 days", "Last month"])
        my_reports_only = st.checkbox(f"Show only reports processed by me ({auth.current_user()['username']})")

    date_from = None
    now = time.time()
    if quick == "Today":
        date_from = now - (now % 86400)
    elif quick == "Yesterday":
        date_from = now - (now % 86400) - 86400
    elif quick == "Last 7 days":
        date_from = now - 7 * 86400
    elif quick == "Last month":
        date_from = now - 30 * 86400

    results = db.search_reports(
        status=None if (status_q in (None, "Any")) else status_q,
        supplier=supplier_q or None, filename=filename_q or None,
        doc_type=None if doc_type_q == "Any" else doc_type_q, date_from=date_from,
        processed_by=auth.current_user()["username"] if my_reports_only else None,
    )

    st.caption(f"{len(results)} result(s)")
    if not results:
        st.info("No matching reports.")
        return

    df = pd.DataFrame([{
        "ID": r["id"], "File": r["filename"], "Type": r["doc_type"], "Status": r["status"],
        "Supplier": r["supplier"] or "-", "Processed By": r["processed_by"], "When": fmt_ts(r["processed_at"]),
    } for r in results])
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(f"Downloads below reflect your logged-in account ({auth.current_user()['email']}) -- "
              f"every report you process is tied to the account you signed up with.")

    bulk_col1, bulk_col2 = st.columns(2)
    with bulk_col1:
        st.download_button("Bulk Export CSV", data=df.to_csv(index=False),
                          file_name="reports_export.csv", mime="text/csv")
    with bulk_col2:
        st.download_button("Bulk Export JSON",
                          data=json.dumps([json.loads(r["report_json"]) for r in results], indent=2, default=str),
                          file_name="reports_export.json", mime="application/json")

    st.subheader("View a report")
    pick_id = st.selectbox("Select by ID", ["-- none --"] + [str(r["id"]) for r in results])
    if pick_id != "-- none --":
        render_report_viewer(int(pick_id))


# ---------------------------------------------------------------------------
# Report Viewer (detail view, with original document preview + reprocess)
# ---------------------------------------------------------------------------

def render_report_viewer(report_id):
    row = db.get_report(report_id)
    if not row:
        st.error("Report not found.")
        return
    report = json.loads(row["report_json"])

    st.divider()
    st.markdown(f"### Report #{report_id}: {row['filename']}")
    meta_cols = st.columns(4)
    meta_cols[0].markdown(f"**Type**  \n{row['doc_type']}")
    meta_cols[1].markdown(f"**Processed by**  \n{row['processed_by']}")
    meta_cols[2].markdown(f"**When**  \n{fmt_ts(row['processed_at'])}")
    meta_cols[3].markdown(f"**Processing time**  \n{row['processing_time_seconds']}s"
                          if row["processing_time_seconds"] else "**Processing time**  \n-")

    if row.get("file_path") and os.path.isfile(row["file_path"]):
        with st.expander("Original document"):
            ext = os.path.splitext(row["file_path"])[1].lower()
            if ext in (".jpg", ".jpeg", ".png"):
                st.image(row["file_path"])
            elif ext == ".pdf":
                with open(row["file_path"], "rb") as f:
                    st.download_button("Download original PDF to view", data=f.read(),
                                      file_name=row["filename"], key=f"dl_orig_{report_id}")
                st.caption("Inline PDF preview isn't embedded here to keep this app dependency-light -- "
                          "download to view, or open directly from the file if running locally.")
            else:
                with open(row["file_path"], "rb") as f:
                    st.download_button("Download original file", data=f.read(),
                                      file_name=row["filename"], key=f"dl_orig2_{report_id}")

    if row["doc_type"] == "Invoice" or "line_items" in report:
        render_invoice_report(report, report_id, expanded=False)
    else:
        render_audit_report(report, report_id)

    action_cols = st.columns(3)
    with action_cols[0]:
        if auth.has_role("Administrator", "Compliance Auditor") and row["status"] == "FAIL":
            if row.get("file_path") and os.path.isfile(row["file_path"]):
                opts = report.get("_ui_processing_options", {})
                reprocess_provider = opts.get("ai_provider")
                reprocess_key = None
                if reprocess_provider:
                    existing = get_ai_key_for(reprocess_provider)
                    reprocess_key = st.text_input(
                        f"{reprocess_provider} API key (needed to reprocess with the same "
                        f"settings originally used -- keys are never stored)",
                        value=existing or "", type="password", key=f"reprocess_key_{report_id}")
                can_reprocess = (not reprocess_provider) or reprocess_key
                if st.button("Reprocess", key=f"reprocess_{report_id}", disabled=not can_reprocess, type="primary"):
                    if "line_items" in report:
                        new_report, elapsed = bridge.process_invoice(
                            row["file_path"], hts_lookup=opts.get("hts_lookup", False),
                            ai_provider=reprocess_provider, api_key=reprocess_key,
                            custom_fields=opts.get("custom_fields", []))
                    else:
                        fields = report.get("fields_requested", list(report.get("fields_checked", {}).keys()))
                        new_report, elapsed = bridge.process_audit(row["file_path"], fields,
                                                                     ai_provider=reprocess_provider,
                                                                     api_key=reprocess_key)
                    db.log_action(auth.current_user()["username"], "reprocess", row["filename"])
                    if "line_items" in new_report:
                        summary = bridge.extract_summary_fields(new_report)
                        new_report["_ui_processing_options"] = opts
                        db.save_report(filename=row["filename"], doc_type=row["doc_type"],
                                       status=new_report["status"], processed_by=auth.current_user()["username"],
                                       report_dict=new_report, supplier=summary["supplier"],
                                       order_number=summary["order_number"], hs_codes=summary["hs_codes"],
                                       processing_time_seconds=elapsed, file_path=row["file_path"])
                    else:
                        db.save_report(filename=row["filename"], doc_type=row["doc_type"],
                                       status=new_report["status"], processed_by=auth.current_user()["username"],
                                       report_dict=new_report, processing_time_seconds=elapsed,
                                       file_path=row["file_path"])
                    st.toast(f"Reprocessed -- new status: {new_report['status']} "
                            f"(saved as a new report entry, original kept for history)")
                    st.rerun()
    with action_cols[1]:
        if auth.has_role("Administrator") and st.button("Delete Report", key=f"delete_{report_id}"):
            db.delete_report(report_id)
            db.log_action(auth.current_user()["username"], "delete_report", row["filename"])
            st.toast("Report deleted")
            st.rerun()


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def page_analytics():
    st.markdown("<div class='app-header'>Analytics</div>", unsafe_allow_html=True)

    all_reports = db.search_reports(limit=5000)
    if not all_reports:
        st.info("No data yet.")
        return

    df = pd.DataFrame([{
        "Status": r["status"], "Supplier": r["supplier"] or "Unknown", "Type": r["doc_type"],
        "When": datetime.datetime.fromtimestamp(r["processed_at"]),
        "ProcessingTime": r["processing_time_seconds"],
        "HSCodes": json.loads(r["hs_codes"]) if r["hs_codes"] else [],
    } for r in all_reports])

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Invoice Volume by Month")
        df["Month"] = df["When"].dt.strftime("%Y-%m")
        st.bar_chart(df.groupby("Month").size())

        st.subheader("Supplier Analysis (top 10)")
        top_suppliers = df["Supplier"].value_counts().head(10)
        st.bar_chart(top_suppliers)

    with col2:
        st.subheader("Processing Time Trend")
        time_df = df.dropna(subset=["ProcessingTime"]).sort_values("When")
        if not time_df.empty:
            st.line_chart(time_df.set_index("When")["ProcessingTime"])
        else:
            st.caption("No timing data yet.")

        st.subheader("HS Code Distribution (top 10)")
        all_codes = [c for codes in df["HSCodes"] for c in codes]
        if all_codes:
            code_counts = pd.Series(all_codes).value_counts().head(10)
            st.bar_chart(code_counts)
        else:
            st.caption("No HS-6 classifications recorded yet.")

    st.subheader("Missing Field Frequency")
    field_misses = {}
    for r in all_reports:
        rep = json.loads(r["report_json"])
        for f in rep.get("missing_document_fields", []) or []:
            field_misses[f] = field_misses.get(f, 0) + 1
        for f in rep.get("missing_custom_fields", []) or []:
            field_misses[f] = field_misses.get(f, 0) + 1
        for f in rep.get("missing_fields", []) or []:
            field_misses[f] = field_misses.get(f, 0) + 1
    if field_misses:
        st.bar_chart(pd.Series(field_misses))
    else:
        st.caption("No missing fields recorded yet -- good sign.")


# ---------------------------------------------------------------------------
# Audit Trail (Administrator only)
# ---------------------------------------------------------------------------

def page_audit_trail():
    st.markdown("<div class='app-header'>Audit Trail</div>", unsafe_allow_html=True)
    st.markdown("<div class='app-subheader'>Immutable log of login, upload, processing, download, and "
                "administrative actions.</div>", unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        user_filter = st.text_input("Filter by username")
    with c2:
        action_filter = st.selectbox("Filter by action", ["Any", "signup", "login", "logout", "upload", "process",
                                                            "batch_process", "reprocess", "delete_report",
                                                            "session_timeout", "change_password",
                                                            "create_user", "update_user"])
    logs = db.get_audit_log(username=user_filter or None,
                            action=None if action_filter == "Any" else action_filter)
    if not logs:
        st.info("No audit log entries match.")
        return
    df = pd.DataFrame([{
        "Timestamp": fmt_ts(l["timestamp"]), "User": l["username"], "Action": l["action"],
        "Details": l["details"] or "-", "Status": l["status"],
    } for l in logs])
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption("Note: this log does not capture client IP address -- Streamlit's execution model doesn't "
              "reliably expose the originating request IP to application code without a reverse-proxy "
              "setup outside this app's scope. Everything else in the spec's audit list is captured.")


# ---------------------------------------------------------------------------
# User Management (Administrator only)
# ---------------------------------------------------------------------------

def page_user_management():
    st.markdown("<div class='app-header'>User Management</div>", unsafe_allow_html=True)

    tab1, tab2 = st.tabs(["All Users", "Create User"])

    with tab1:
        users = db.list_users()
        df = pd.DataFrame([{
            "ID": u["id"], "Username": u["username"], "Email": u["email"], "Role": u["role"],
            "Active": "Yes" if u["is_active"] else "No", "Last Login": fmt_ts(u["last_login"]),
        } for u in users])
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.subheader("Manage a user")
        pick = st.selectbox("Select user", ["-- none --"] + [u["username"] for u in users])
        if pick != "-- none --":
            u = next(x for x in users if x["username"] == pick)
            c1, c2, c3 = st.columns(3)
            with c1:
                new_role = st.selectbox("Role", db.ROLES, index=db.ROLES.index(u["role"]), key=f"role_{u['id']}")
                if new_role != u["role"] and st.button("Update Role", key=f"updaterole_{u['id']}"):
                    db.set_user_role(u["id"], new_role)
                    db.log_action(auth.current_user()["username"], "update_user", f"{u['username']} role -> {new_role}")
                    st.toast("Role updated")
                    st.rerun()
            with c2:
                label = "Disable" if u["is_active"] else "Enable"
                if st.button(f"{label} Account", key=f"toggle_{u['id']}"):
                    db.set_user_active(u["id"], not u["is_active"])
                    db.log_action(auth.current_user()["username"], "update_user",
                                 f"{u['username']} {'disabled' if u['is_active'] else 'enabled'}")
                    st.toast(f"Account {label.lower()}d")
                    st.rerun()
            with c3:
                new_pw = st.text_input("Reset password to", type="password", key=f"resetpw_{u['id']}")
                if st.button("Reset Password", key=f"doresetpw_{u['id']}") and new_pw:
                    if len(new_pw) < 8:
                        st.error("Password must be at least 8 characters.")
                    else:
                        db.reset_password(u["id"], new_pw)
                        db.log_action(auth.current_user()["username"], "update_user", f"{u['username']} password reset")
                        st.toast("Password reset")

    with tab2:
        with st.form("create_user_form"):
            new_username = st.text_input("Username")
            new_email = st.text_input("Email")
            new_password = st.text_input("Temporary password", type="password")
            new_role = st.selectbox("Role", db.ROLES)
            if st.form_submit_button("Create User", type="primary"):
                if not new_username or not new_email or len(new_password) < 8:
                    st.error("Username, email, and an 8+ character password are all required.")
                elif db.get_user_by_username(new_username):
                    st.error("That username already exists.")
                else:
                    db.create_user(new_username, new_email, new_password, new_role)
                    db.log_action(auth.current_user()["username"], "create_user", new_username)
                    st.success(f"User '{new_username}' created.")
                    st.rerun()


# ---------------------------------------------------------------------------
# Settings (Administrator only) + My Account (all users)
# ---------------------------------------------------------------------------

def page_settings():
    st.markdown("<div class='app-header'>Settings</div>", unsafe_allow_html=True)

    st.subheader("My Account")
    with st.form("change_pw_form"):
        current_pw = st.text_input("Current password", type="password")
        new_pw = st.text_input("New password", type="password")
        if st.form_submit_button("Change Password", type="primary"):
            success, msg = auth.change_password(current_pw, new_pw)
            (st.success if success else st.error)(msg)

    if auth.has_role("Administrator"):
        st.divider()
        st.subheader("Application Settings")
        st.caption("System-wide settings. Currently informational -- wiring these into enforced behavior "
                  "(e.g. an org-wide default HS mode) is a natural next increment.")
        st.selectbox("Default HS-6 mode for new checks", ["None", "USITC", "Gemini", "Claude"])
        st.number_input("Session timeout (minutes)", min_value=5, max_value=240, value=30)
        st.info("These controls are placeholders for a future settings-persistence pass -- see README_WEBAPP.md.")


# ---------------------------------------------------------------------------
# Main routing
# ---------------------------------------------------------------------------

def main():
    if not auth.is_logged_in():
        render_login_page()
        return

    auth.touch_session()
    if not auth.is_logged_in():  # touch_session() may have just logged us out (timeout)
        return

    page = render_sidebar()

    if page == "Dashboard":
        page_dashboard()
    elif page == "Invoice Compliance":
        page_invoice_compliance()
    elif page == "Supporting Document Audit":
        page_supporting_document_audit()
    elif page == "Batch Processing":
        page_batch_processing()
    elif page == "Search Reports":
        page_search_reports()
    elif page == "PASS Reports":
        page_search_reports(status_filter="PASS", page_title="PASS Reports")
    elif page == "FAIL Reports":
        page_search_reports(status_filter="FAIL", page_title="FAIL Reports")
    elif page == "INCOMPLETE Reports":
        page_search_reports(status_filter="INCOMPLETE", page_title="INCOMPLETE Reports")
    elif page == "Analytics":
        page_analytics()
    elif page == "Audit Trail":
        if auth.has_role("Administrator"):
            page_audit_trail()
        else:
            st.error("Administrator access required.")
    elif page == "User Management":
        if auth.has_role("Administrator"):
            page_user_management()
        else:
            st.error("Administrator access required.")
    elif page == "Settings":
        page_settings()


if __name__ == "__main__":
    main()






