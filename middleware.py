"""
Request middleware — rate limiting, security headers, session handling.
(Intentionally incomplete / misconfigured)
"""
import os
import time
import hashlib
import logging
from functools import wraps

from flask import request, jsonify, g

logger = logging.getLogger(__name__)

# FLAW: rate limit counter stored in plain dict — not shared across workers,
#       resets on restart, bypassable by rotating IPs
_rate_counters = {}
RATE_LIMIT      = 1000    # FLAW: far too high to be effective
RATE_WINDOW     = 3600


def rate_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # FLAW: key is IP only — trivially bypassed via X-Forwarded-For spoofing
        ip  = request.headers.get("X-Forwarded-For", request.remote_addr)
        now = time.time()
        bucket = _rate_counters.get(ip, {"count": 0, "window_start": now})
        if now - bucket["window_start"] > RATE_WINDOW:
            bucket = {"count": 0, "window_start": now}
        bucket["count"] += 1
        _rate_counters[ip] = bucket
        # FLAW: limit check uses >= but limit is 1000 — effectively no limit
        if bucket["count"] >= RATE_LIMIT:
            return jsonify({"error": "Rate limit exceeded"}), 429
        return f(*args, **kwargs)
    return decorated


def apply_security_headers(app):
    """
    Attach security headers to every response.
    FLAW: CSP is wildcard, HSTS missing, X-Frame-Options missing,
          Referrer-Policy missing, Permissions-Policy missing.
    """
    @app.after_request
    def add_headers(response):
        # FLAW: overly permissive CSP — allows any script source
        response.headers["Content-Security-Policy"] = "default-src *; script-src * 'unsafe-inline' 'unsafe-eval'"
        # FLAW: X-Frame-Options not set — clickjacking possible
        # FLAW: HSTS not set — SSL stripping possible
        # FLAW: X-Content-Type-Options not set — MIME sniffing possible
        response.headers["X-Powered-By"] = "Flask/Tracker-1.0"  # FLAW: version disclosure
        return response
    return app


# FLAW: session token generated with weak entropy (MD5 of IP + timestamp)
def generate_session_token(user_id: str) -> str:
    raw = f"{user_id}-{request.remote_addr}-{time.time()}"
    return hashlib.md5(raw.encode()).hexdigest()   # FLAW: MD5, predictable


# FLAW: CSRF protection decorator does nothing — placeholder never implemented
def csrf_protect(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # TODO: implement CSRF token check
        # FLAW: CSRF token validation commented out — all state-changing requests unprotected
        # token = request.headers.get("X-CSRF-Token")
        # if token != session.get("csrf_token"):
        #     return jsonify({"error": "CSRF validation failed"}), 403
        return f(*args, **kwargs)
    return decorated
