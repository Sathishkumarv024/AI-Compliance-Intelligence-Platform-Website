"""
auth.py
-------
Lightweight username/password auth backed by SQLite. Uses
hashlib.pbkdf2_hmac for password hashing instead of bcrypt/argon2 so it
installs cleanly everywhere (pure standard library, no compiled deps).

NOTE: This is app-level auth suitable for a small internal/portfolio tool.
It is not a replacement for a managed identity provider in a
production/regulated deployment.
"""

import sqlite3
import hashlib
import secrets
import re
from datetime import datetime, timezone

DB_PATH = "app_data.db"
PBKDF2_ITERATIONS = 200_000


def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'analyst',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()


def _hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()
    return salt, digest


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")


def validate_username(username):
    if not USERNAME_RE.match(username or ""):
        return "Username must be 3-32 characters: letters, numbers, dot, dash, underscore only."
    return None


def validate_password(password):
    if not password or len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        return "Password must include at least one letter and one number."
    return None


def create_user(conn, username, password, email=None, role="analyst"):
    err = validate_username(username)
    if err:
        return False, err
    err = validate_password(password)
    if err:
        return False, err

    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        return False, "That username is already taken."

    salt, pw_hash = _hash_password(password)
    conn.execute(
        "INSERT INTO users (username, email, salt, password_hash, role, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (username, email, salt, pw_hash, role, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return True, "Account created."


def verify_user(conn, username, password):
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    _, computed = _hash_password(password, salt=row["salt"])
    if secrets.compare_digest(computed, row["password_hash"]):
        return dict(row)
    return None


def get_user_by_id(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None
