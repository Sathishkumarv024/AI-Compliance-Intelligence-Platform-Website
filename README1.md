# Compliance Auditing Platform (Web UI)

A Streamlit web application on top of the existing [invoice_compliance_checker.py](./invoice_compliance_checker.py) CLI tool -- same backend logic, reused directly (not reimplemented), with a login system, role-based access control, a SQLite-backed report history, dashboard/analytics, and an audit trail added on top.

## Important: this was built and reviewed without being able to run it live

The sandbox this was built in cannot install `streamlit` (a restricted package mirror -- the same category of limitation that blocked `xlrd` earlier in this project, not a code problem). Everything **not** dependent on Streamlit itself was fully tested against real data:

- `database.py` -- verified directly: user creation, password hashing/verification, report save/search, dashboard stats, audit logging
- `backend_bridge.py` -- verified directly against the real sample invoices (`invoice_clean.pdf`, `invoice_issues.pdf`, `invoice_missing_header.pdf`), confirming it correctly drives the same backend the CLI tool uses
- `app.py` and `auth.py` -- syntax-checked, and manually reviewed line-by-line for the most common real Streamlit bugs (unguarded `session_state` access that would throw `KeyError`, duplicate widget keys, incorrect API call signatures) -- several real issues were caught and fixed this way (see "Known fixes made during review" below)

**What this means practically: run it, and if anything breaks, tell me exactly what happened (the error message, which page) and I'll fix it** -- the same iterative process that got the CLI tool working through the Pydroid issues earlier in this project. Treat this as a strong first pass that needs one real runtime pass with you, not a guaranteed-perfect deliverable.

## Setup

```bash
cd webapp
pip install -r requirements_webapp.txt
streamlit run app.py
```

Opens at `http://localhost:8501`. Default login: **`admin` / `admin123`** -- change this immediately via Settings after first login (User Management page also lets you reset any user's password).

## Architecture

```
webapp/
├── app.py                          # All UI pages + routing
├── auth.py                         # Login, session, RBAC helpers
├── database.py                     # SQLite: users, reports, audit_log
├── backend_bridge.py               # Thin adapter to the CLI's check_invoice()/run_audit()
├── theme.py                        # Custom CSS (cards, status badges, dark mode)
├── invoice_compliance_checker.py   # The actual compliance engine (copied from the CLI tool)
└── requirements_webapp.txt
```

**No compliance logic lives in the web layer.** Every check the app performs (field presence, math validation, grand total recalculation, HS-6 classification, custom fields, AI vision extraction) is the exact same code the command-line tool uses and that's already been tested extensively throughout this project. `app.py` only handles: file upload -> save to disk -> call the same functions the CLI calls -> store the result -> display it.

## What's genuinely complete vs. deliberately simplified

**Fully working:**
- Login, logout, RBAC (Administrator / Compliance Auditor / Viewer), session timeout, change password, user management (create/disable/reset password/assign role)
- **Self-service Sign Up** -- new accounts default to **Viewer** access only (view/search/download, no upload or processing), by design: self-signup deliberately can't grant itself upload, processing, or admin rights. An Administrator promotes a user's role afterward from User Management. This is a standard least-privilege pattern for exactly this reason -- letting signup grant any role would be a real security hole.
- Invoice Compliance and Supporting Document Audit modules, wired to the real backend, all 4 file formats, all 3 HS-6 modes
- Batch Processing with summary-table-first + drill-down, mirroring the CLI's own UX
- Search/filter/PASS/FAIL/INCOMPLETE report views, bulk CSV/JSON export, plus a **"My Reports Only"** filter tied to your logged-in account
- Report Viewer with original document access and a working Reprocess action (that actually saves the new result and re-uses the original processing settings, rather than discarding it)
- Dashboard KPIs and Analytics charts, computed from real stored data
- Audit trail logging real actions (signup, login, upload, process, reprocess, delete, user changes)

**On "download via the sign-up email" specifically:** every report is tied to the authenticated account that processed it (`processed_by`), and the "My Reports Only" filter plus the account email shown on the Search Reports page make that traceable. What is **not** implemented is actually emailing files to that address -- that needs a real SMTP/email provider (e.g. SendGrid, AWS SES) with credentials, which isn't configured here. Downloads work directly in-browser once logged in; say the word if you want real email delivery wired in and can provide an email provider's credentials.

**Deliberately simplified, and why:**
- **Password hashing uses `hashlib.pbkdf2_hmac`, not `bcrypt`.** `bcrypt` is a compiled C-extension package -- exactly the category of dependency that's failed to install in constrained environments throughout this project (Android/Pydroid, this sandbox). PBKDF2 via Python's own `hashlib` is a real, salted, industry-recognized hash with zero extra dependencies.
- **"Remember Me" extends the session timeout for the current browser session; it does not persist login across a closed browser/restarted server.** True persistent login needs real cookies, which Streamlit doesn't manage itself without extra packages. Documented here rather than silently pretending it does more than it does.
- **No client IP address in the audit log.** Streamlit's execution model doesn't reliably expose the originating request IP to application code without a reverse-proxy setup outside this app's scope. Every other audit event the spec asked for is captured.
- **Settings page's system-wide controls are placeholders**, not yet wired to actually change app behavior -- flagged clearly in the UI itself, not hidden.
- **No inline PDF preview** in the Report Viewer (download instead) -- avoids pulling in an additional PDF-rendering JS/Python dependency for something a download button already solves.
- **SQLite, not a server database.** Genuinely fine for single-instance local/internal use; would need to move to Postgres (or similar) for true multi-instance concurrent production use. **If deploying to Streamlit Community Cloud specifically: the free tier's filesystem is ephemeral**, so this local database won't reliably persist across app sleep/restart there -- fine for a demo, not for real long-term history.

## Known fixes made during review (before you even run it)

Caught by careful manual review, not live testing, since live testing wasn't possible here -- listed for transparency:
- Settings page was originally gated to Administrators only, which would have locked every non-admin user out of changing their own password. Fixed: My Account/change password is now reachable by all roles; only the system-wide settings sub-section stays admin-only.
- "Remember Me" originally set an attribute that nothing actually read -- fixed to genuinely extend the session timeout.
- Reprocess originally ran the check again but never saved the new result (looked like it worked, silently discarded the outcome) -- fixed to actually persist it as a new report entry.
- Reprocess originally ignored the original HS-6 mode/custom fields settings, meaning a reprocessed invoice could silently get checked differently than the first time -- fixed to reuse the original settings (re-prompting only for the API key, which is never stored).

## Security note for production use

This is solid for local/internal/demo use. For an actual production deployment handling real compliance data, you'd additionally want: HTTPS enforcement, a real database instead of SQLite, rate limiting on login attempts, a proper secrets manager for API keys instead of session-only storage, and likely a dedicated auth provider (SSO/OAuth) instead of hand-rolled username/password. None of that is implemented here -- said plainly rather than implied by the presence of "password hashing" and "RBAC" in the feature list.
