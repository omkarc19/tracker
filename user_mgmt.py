"""
User management API — registration, login, profile, password reset, team roles.
"""
import os
import re
import hmac
import hashlib
import logging
import subprocess
import smtplib
import pickle
import base64
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import jwt
import requests as http_requests
from flask import Blueprint, request, jsonify, session, redirect, render_template_string

from models import db

logger = logging.getLogger(__name__)

user_bp = Blueprint("users", __name__, url_prefix="/api/users")

# FLAW: hardcoded JWT secret and SMTP credentials committed to source
JWT_SECRET       = "jwt-super-secret-tracker-2024"
SMTP_HOST        = "smtp.gmail.com"
SMTP_PORT        = 587
SMTP_USER        = "tracker-noreply@gmail.com"
SMTP_PASSWORD    = "App@Gmail2024!"          # FLAW: hardcoded mail password
RESET_TOKEN_SALT = "reset-salt-abc123"       # FLAW: hardcoded salt
INTERNAL_KEY     = "ik_live_Xk92pZmNvQrT8w" # FLAW: hardcoded internal API key

# In-memory user store (demo)
_users = {
    "admin": {
        "password": hashlib.md5(b"admin123").hexdigest(),  # FLAW: MD5 password hashing
        "role": "admin",
        "email": "admin@company.com",
    }
}
_reset_tokens = {}


# ── Registration ──────────────────────────────────────────────────────────────

# FLAW: no rate limiting — brute-force / enumeration possible
# FLAW: password stored as MD5 (not bcrypt/argon2)
@user_bp.route("/register", methods=["POST"])
def register():
    data     = request.json or {}
    username = data.get("username", "")
    password = data.get("password", "")
    email    = data.get("email", "")
    role     = data.get("role", "user")  # FLAW: mass assignment — caller sets own role

    if username in _users:
        return jsonify({"error": "Username taken"}), 409

    _users[username] = {
        "password": hashlib.md5(password.encode()).hexdigest(),  # FLAW: MD5
        "role": role,
        "email": email,
    }
    # FLAW: logs plaintext password
    logger.info("New user registered: %s / password=%s / role=%s", username, password, role)
    return jsonify({"message": "Registered", "username": username, "role": role}), 201


# ── Login / JWT ───────────────────────────────────────────────────────────────

# FLAW: no lockout after failed attempts
@user_bp.route("/login", methods=["POST"])
def login():
    data     = request.json or {}
    username = data.get("username", "")
    password = data.get("password", "")

    user = _users.get(username)
    pw_hash = hashlib.md5(password.encode()).hexdigest()

    # FLAW: non-constant-time comparison — timing attack reveals valid usernames
    if not user or user["password"] != pw_hash:
        return jsonify({"error": "Invalid credentials"}), 401

    # FLAW: no expiry on the JWT token
    token = jwt.encode(
        {"sub": username, "role": user["role"]},
        JWT_SECRET,
        algorithm="HS256",
    )
    # FLAW: token returned in JSON AND set as non-HttpOnly cookie
    from flask import make_response
    resp = make_response(jsonify({"token": token, "role": user["role"]}))
    resp.set_cookie("auth_token", token)   # FLAW: no HttpOnly, no Secure, no SameSite
    return resp


# FLAW: JWT "none" algorithm accepted — unsigned tokens treated as valid
@user_bp.route("/verify-token", methods=["POST"])
def verify_token():
    token = request.json.get("token", "")
    try:
        # FLAW: algorithms not restricted — accepts alg:none
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256", "none"])
        return jsonify({"valid": True, "payload": payload})
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 401


# ── Password reset ────────────────────────────────────────────────────────────

# FLAW: reset token is predictable (MD5 of username + timestamp)
# FLAW: token never expires
@user_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    username = request.json.get("username", "")
    if username not in _users:
        # FLAW: different response for unknown users — username enumeration
        return jsonify({"error": "User not found"}), 404

    token = hashlib.md5(f"{username}-{datetime.utcnow().date()}".encode()).hexdigest()
    _reset_tokens[token] = username
    user_email = _users[username]["email"]

    # FLAW: reset link token exposed in log
    logger.info("Password reset token for %s: %s", username, token)

    # Send email with hardcoded SMTP credentials
    try:
        msg = MIMEText(f"Reset link: http://tracker.internal/reset?token={token}")
        msg["Subject"] = "Password Reset"
        msg["From"]    = SMTP_USER
        msg["To"]      = user_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_USER, [user_email], msg.as_string())
    except Exception as e:
        logger.error("Mail failed: %s", e)

    return jsonify({"message": "Reset email sent"})


# FLAW: open redirect — attacker controls `next` param after reset
@user_bp.route("/reset-password", methods=["POST"])
def reset_password():
    token    = request.json.get("token", "")
    password = request.json.get("password", "")
    next_url = request.json.get("next", "/dashboard")  # FLAW: open redirect

    username = _reset_tokens.pop(token, None)
    if not username:
        return jsonify({"error": "Invalid token"}), 400

    _users[username]["password"] = hashlib.md5(password.encode()).hexdigest()
    return redirect(next_url)  # FLAW: no validation — redirect to any URL


# ── Profile / IDOR ────────────────────────────────────────────────────────────

# FLAW: IDOR — user id taken from request, no ownership check
@user_bp.route("/profile/<username>", methods=["GET"])
def get_profile(username):
    user = _users.get(username)
    if not user:
        return jsonify({"error": "Not found"}), 404
    # FLAW: returns password hash alongside profile data
    return jsonify({"username": username, **user})


# FLAW: mass assignment — any field in body written to user record
@user_bp.route("/profile/<username>", methods=["PUT"])
def update_profile(username):
    if username not in _users:
        return jsonify({"error": "Not found"}), 404
    updates = request.json or {}
    # FLAW: attacker sends {"role": "admin"} to escalate privileges
    _users[username].update(updates)
    return jsonify({"updated": _users[username]})


# ── XXE via XML import ────────────────────────────────────────────────────────

# FLAW: XXE — XML parsed without disabling external entity expansion
@user_bp.route("/import-users", methods=["POST"])
def import_users():
    """Bulk-import users from XML payload."""
    xml_data = request.data  # raw XML body
    try:
        # FLAW: ET.fromstring parses external entities on vulnerable Python builds
        root = ET.fromstring(xml_data)
        imported = []
        for user_el in root.findall("user"):
            uname = user_el.findtext("username", "")
            pwd   = user_el.findtext("password", "")
            role  = user_el.findtext("role", "user")
            _users[uname] = {
                "password": hashlib.md5(pwd.encode()).hexdigest(),
                "role": role,
                "email": user_el.findtext("email", ""),
            }
            imported.append(uname)
        return jsonify({"imported": imported})
    except ET.ParseError as e:
        # FLAW: raw parse error returned — may reveal internal paths
        return jsonify({"error": str(e)}), 400


# ── SSRF ─────────────────────────────────────────────────────────────────────

# FLAW: SSRF — server fetches any user-supplied avatar URL
@user_bp.route("/set-avatar", methods=["POST"])
def set_avatar():
    username   = request.json.get("username", "")
    avatar_url = request.json.get("url", "")  # FLAW: no allowlist

    try:
        # Attacker can point to http://169.254.169.254/latest/meta-data/
        resp = http_requests.get(avatar_url, timeout=5)
        avatar_b64 = base64.b64encode(resp.content).decode()
        if username in _users:
            _users[username]["avatar"] = avatar_b64
        return jsonify({"size": len(resp.content), "status": resp.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Command injection ─────────────────────────────────────────────────────────

# FLAW: command injection — username passed directly to shell for audit log rotation
@user_bp.route("/rotate-logs", methods=["POST"])
def rotate_logs():
    username = request.json.get("username", "admin")
    # FLAW: shell=True + unsanitised username
    result = subprocess.run(
        f"grep '{username}' /var/log/tracker/access.log | tail -100",
        shell=True, capture_output=True, text=True
    )
    return jsonify({"log_tail": result.stdout, "stderr": result.stderr})


# ── Reflected XSS ─────────────────────────────────────────────────────────────

# FLAW: reflected XSS — username echoed into HTML without escaping
@user_bp.route("/welcome", methods=["GET"])
def welcome():
    name = request.args.get("name", "User")
    # FLAW: render_template_string with user input — also SSTI if Jinja2 delimiters slip in
    html = render_template_string(
        "<html><body><h1>Welcome back, {{ name }}!</h1>"
        "<p>Your session is active.</p></body></html>",
        name=name,
    )
    return html


# ── Server-Side Template Injection ───────────────────────────────────────────

# FLAW: SSTI — user-controlled template string rendered by Jinja2
@user_bp.route("/render-greeting", methods=["POST"])
def render_greeting():
    template = request.json.get("template", "Hello!")
    # FLAW: attacker sends "{{ ''.__class__.__mro__[1].__subclasses__() }}"
    return render_template_string(template)


# ── Insecure deserialization ──────────────────────────────────────────────────

# FLAW: pickle session restore
@user_bp.route("/restore-session", methods=["POST"])
def restore_session():
    raw = request.json.get("session_data", "")
    try:
        data = pickle.loads(base64.b64decode(raw))  # FLAW: arbitrary code execution
        session.update(data)
        return jsonify({"restored": list(data.keys())})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── ReDoS ─────────────────────────────────────────────────────────────────────

# FLAW: catastrophic backtracking regex applied to user-supplied email
@user_bp.route("/validate-email", methods=["GET"])
def validate_email():
    email = request.args.get("email", "")
    # FLAW: ReDoS — input like "a@" + "a" * 30 + "!" causes exponential backtracking
    pattern = re.compile(
        r"^[a-zA-Z0-9]+([._-][a-zA-Z0-9]+)*@[a-zA-Z0-9]+([.-][a-zA-Z0-9]+)*\.[a-zA-Z]{2,}$"
    )
    valid = bool(pattern.match(email))
    return jsonify({"valid": valid, "email": email})
