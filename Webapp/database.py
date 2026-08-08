"""
Database layer for the compliance auditing platform.
------------------------------------------------------
SQLite, stdlib-only (no ORM dependency) -- deliberately kept dependency-free
given this project's repeated experience with packages that fail to install
in constrained environments (Android/Pydroid earlier, this sandbox's
restricted mirror now). SQLite ships with Python itself.

NOTE ON SCOPE: this is a genuine, working local database suitable for a
single-instance deployment (e.g. Streamlit Community Cloud, an internal
server, or local use) -- it is NOT a multi-instance-safe production database.
For real concurrent multi-user production use, this would need to be swapped
for a proper server-based database (Postgres, etc.). That's a deliberate,
documented simplification, not an oversight -- see README_WEBAPP.md.
"""

import sqlite3
import json
import os
import time
import secrets
import hashlib
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compliance_platform.db")

ROLES = ("Administrator", "Compliance Auditor", "Viewer")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('Administrator','Compliance Auditor','Viewer')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                last_login REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                file_path TEXT,
                doc_type TEXT NOT NULL DEFAULT 'Invoice',
                status TEXT NOT NULL CHECK(status IN ('PASS','FAIL','INCOMPLETE')),
                supplier TEXT,
                invoice_number TEXT,
                order_number TEXT,
                hs_codes TEXT,
                processed_by TEXT NOT NULL,
                processed_at REAL NOT NULL,
                processing_time_seconds REAL,
                report_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                status TEXT NOT NULL DEFAULT 'success'
            )
        """)
        # Seed a default administrator on first run only
        existing = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if existing == 0:
            salt = secrets.token_hex(16)
            pw_hash = _hash_password("admin123", salt)
            conn.execute(
                "INSERT INTO users (username, email, password_hash, salt, role, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                ("admin", "admin@example.com", pw_hash, salt, "Administrator", time.time()),
            )


def _hash_password(password, salt):
    """PBKDF2-HMAC-SHA256, 200k iterations. Pure stdlib (hashlib) -- no
    bcrypt/argon2 dependency, deliberately, to avoid this project's recurring
    'compiled package won't install' problem. This is still a real, slow,
    salted hash -- not reversible, not a security theater placeholder."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000).hex()


def verify_password(password, salt, stored_hash):
    return secrets.compare_digest(_hash_password(password, salt), stored_hash)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username, email, password, role):
    if role not in ROLES:
        raise ValueError(f"Invalid role: {role}")
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (username, email, password_hash, salt, role, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (username, email, pw_hash, salt, role, time.time()),
        )


def get_user_by_username(username):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_user_by_email(email):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def list_users():
    with get_conn() as conn:
        rows = conn.execute("SELECT id, username, email, role, is_active, created_at, last_login "
                             "FROM users ORDER BY username").fetchall()
        return [dict(r) for r in rows]


def set_user_active(user_id, is_active):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if is_active else 0, user_id))


def set_user_role(user_id, role):
    if role not in ROLES:
        raise ValueError(f"Invalid role: {role}")
    with get_conn() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def reset_password(user_id, new_password):
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(new_password, salt)
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                     (pw_hash, salt, user_id))


def update_last_login(username):
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_login = ? WHERE username = ?", (time.time(), username))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def save_report(filename, doc_type, status, processed_by, report_dict,
                 supplier=None, invoice_number=None, order_number=None,
                 hs_codes=None, processing_time_seconds=None, file_path=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO reports
               (filename, file_path, doc_type, status, supplier, invoice_number, order_number,
                hs_codes, processed_by, processed_at, processing_time_seconds, report_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (filename, file_path, doc_type, status, supplier, invoice_number, order_number,
             json.dumps(hs_codes) if hs_codes else None, processed_by, time.time(),
             processing_time_seconds, json.dumps(report_dict)),
        )
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def search_reports(status=None, supplier=None, filename=None, processed_by=None,
                    doc_type=None, date_from=None, date_to=None, limit=500):
    query = "SELECT * FROM reports WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if supplier:
        query += " AND supplier LIKE ?"
        params.append(f"%{supplier}%")
    if filename:
        query += " AND filename LIKE ?"
        params.append(f"%{filename}%")
    if processed_by:
        query += " AND processed_by = ?"
        params.append(processed_by)
    if doc_type:
        query += " AND doc_type = ?"
        params.append(doc_type)
    if date_from:
        query += " AND processed_at >= ?"
        params.append(date_from)
    if date_to:
        query += " AND processed_at <= ?"
        params.append(date_to)
    query += " ORDER BY processed_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_report(report_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return dict(row) if row else None


def delete_report(report_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))


def dashboard_stats():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM reports").fetchone()["c"]
        by_status = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM reports GROUP BY status").fetchall()}
        today_start = time.time() - (time.time() % 86400)
        today = conn.execute("SELECT COUNT(*) AS c FROM reports WHERE processed_at >= ?",
                              (today_start,)).fetchone()["c"]
        avg_time = conn.execute("SELECT AVG(processing_time_seconds) AS a FROM reports "
                                 "WHERE processing_time_seconds IS NOT NULL").fetchone()["a"]
        active_users = conn.execute(
            "SELECT COUNT(DISTINCT processed_by) AS c FROM reports WHERE processed_at >= ?",
            (time.time() - 7 * 86400,)).fetchone()["c"]
        return {
            "total": total,
            "pass": by_status.get("PASS", 0),
            "fail": by_status.get("FAIL", 0),
            "incomplete": by_status.get("INCOMPLETE", 0),
            "today": today,
            "avg_processing_time": round(avg_time, 2) if avg_time else None,
            "active_users_7d": active_users,
        }


def daily_volume(days=14):
    cutoff = time.time() - days * 86400
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT processed_at, status FROM reports WHERE processed_at >= ? ORDER BY processed_at",
            (cutoff,)).fetchall()
    from collections import defaultdict
    import datetime
    counts = defaultdict(int)
    for r in rows:
        day = datetime.datetime.fromtimestamp(r["processed_at"]).strftime("%Y-%m-%d")
        counts[day] += 1
    return counts


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def log_action(username, action, details=None, status="success"):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (timestamp, username, action, details, status) VALUES (?, ?, ?, ?, ?)",
            (time.time(), username, action, details, status),
        )


def get_audit_log(username=None, action=None, limit=500):
    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []
    if username:
        query += " AND username = ?"
        params.append(username)
    if action:
        query += " AND action = ?"
        params.append(action)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
