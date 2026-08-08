"""
Authentication and session handling.
--------------------------------------
Honest note on Streamlit's security model: Streamlit re-runs the whole
script on every interaction within a server-side session keyed by a browser
connection -- there are no traditional server sessions/cookies you manage
yourself, and st.session_state is per-browser-tab and is cleared if the
tab/server restarts. This module builds real, working login/RBAC/timeout on
top of that model, but it is NOT equivalent to a production auth system
(e.g. OAuth/SSO, HTTPS-enforced cookies, a dedicated auth service). Treat
this as "solid for a single-instance internal tool," not "bank-grade."
"""

import time
import streamlit as st
import database as db

SESSION_TIMEOUT_SECONDS = 30 * 60  # 30 minutes of inactivity


def is_logged_in():
    return st.session_state.get("user") is not None


def current_user():
    return st.session_state.get("user")


def current_role():
    user = current_user()
    return user["role"] if user else None


def has_role(*allowed_roles):
    return current_role() in allowed_roles


def login(username_or_email, password):
    """Returns (success: bool, message: str)."""
    user = db.get_user_by_username(username_or_email) or db.get_user_by_email(username_or_email)
    if not user:
        db.log_action(username_or_email, "login", status="failed - unknown user")
        return False, "Invalid username/email or password."
    if not user["is_active"]:
        db.log_action(user["username"], "login", status="failed - account disabled")
        return False, "This account has been disabled. Contact an administrator."
    if not db.verify_password(password, user["salt"], user["password_hash"]):
        db.log_action(user["username"], "login", status="failed - wrong password")
        return False, "Invalid username/email or password."

    st.session_state["user"] = {"id": user["id"], "username": user["username"],
                                 "email": user["email"], "role": user["role"]}
    st.session_state["last_activity"] = time.time()
    db.update_last_login(user["username"])
    db.log_action(user["username"], "login", status="success")
    return True, "Logged in successfully."


def logout():
    user = current_user()
    if user:
        db.log_action(user["username"], "logout", status="success")
    for key in ("user", "last_activity"):
        st.session_state.pop(key, None)


def touch_session():
    """Call on every page render while logged in -- refreshes the activity
    timestamp and enforces the idle timeout. Honors a per-session extended
    timeout if 'Remember Me' was checked at login (stored in session_state,
    not a real persistent cookie -- see module docstring)."""
    if not is_logged_in():
        return
    timeout = st.session_state.get("session_timeout_seconds", SESSION_TIMEOUT_SECONDS)
    last = st.session_state.get("last_activity", time.time())
    if time.time() - last > timeout:
        username = current_user()["username"]
        logout()
        db.log_action(username, "session_timeout", status="auto-logout")
        st.warning("Your session timed out due to inactivity. Please log in again.")
        st.rerun()
    st.session_state["last_activity"] = time.time()


def change_password(current_password, new_password):
    """Returns (success: bool, message: str)."""
    user_record = db.get_user_by_username(current_user()["username"])
    if not db.verify_password(current_password, user_record["salt"], user_record["password_hash"]):
        return False, "Current password is incorrect."
    if len(new_password) < 8:
        return False, "New password must be at least 8 characters."
    db.reset_password(user_record["id"], new_password)
    db.log_action(current_user()["username"], "change_password", status="success")
    return True, "Password changed successfully."
