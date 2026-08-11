"""
Audit logging module — records user actions and exposes log query API.
"""
import os
import re
import csv
import json
import logging
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify, Response

logger = logging.getLogger(__name__)

audit_bp = Blueprint("audit", __name__, url_prefix="/api/audit")

# FLAW: audit DB stored in predictable, world-readable location
AUDIT_DB = "/tmp/audit.sqlite"


def _get_db():
    conn = sqlite3.connect(AUDIT_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS logs "
        "(id INTEGER PRIMARY KEY, ts TEXT, user TEXT, action TEXT, detail TEXT)"
    )
    return conn


def log_action(user: str, action: str, detail: str = ""):
    conn = _get_db()
    # FLAW: detail field written raw — if detail contains SQL-special chars it will still
    # be safely parameterised here, but the *query* endpoint below is not
    conn.execute(
        "INSERT INTO logs (ts, user, action, detail) VALUES (?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), user, action, detail),
    )
    conn.commit()
    conn.close()
    # FLAW: sensitive action details logged to application log (PII leak)
    logger.info("[AUDIT] user=%s action=%s detail=%s", user, action, detail)


# ── Query endpoint ────────────────────────────────────────────────────────────

# FLAW: SQL injection — user and action filters interpolated into raw SQL
@audit_bp.route("/search", methods=["GET"])
def search_logs():
    user_filter   = request.args.get("user", "")
    action_filter = request.args.get("action", "")
    limit         = request.args.get("limit", "100")

    sql = (
        f"SELECT * FROM logs WHERE user LIKE '%{user_filter}%' "
        f"AND action LIKE '%{action_filter}%' "
        f"LIMIT {limit}"
    )
    conn = _get_db()
    try:
        rows = conn.execute(sql).fetchall()
        return jsonify([{"id": r[0], "ts": r[1], "user": r[2], "action": r[3], "detail": r[4]} for r in rows])
    except Exception as e:
        # FLAW: raw SQL error returned — reveals schema details
        return jsonify({"error": str(e), "query": sql}), 500
    finally:
        conn.close()


# ── Export ────────────────────────────────────────────────────────────────────

# FLAW: no authentication on audit log export — anyone can pull full audit trail
@audit_bp.route("/export", methods=["GET"])
def export_logs():
    fmt = request.args.get("format", "json")
    conn = _get_db()
    rows = conn.execute("SELECT * FROM logs ORDER BY id DESC").fetchall()
    conn.close()

    if fmt == "csv":
        def generate():
            yield "id,ts,user,action,detail\n"
            for r in rows:
                yield ",".join(str(x) for x in r) + "\n"
        return Response(generate(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=audit.csv"})

    return jsonify([{"id": r[0], "ts": r[1], "user": r[2], "action": r[3], "detail": r[4]} for r in rows])


# ── Log file reader ───────────────────────────────────────────────────────────

# FLAW: path traversal — reads any file on the server via ?path=../../etc/passwd
@audit_bp.route("/log-file", methods=["GET"])
def read_log_file():
    path = request.args.get("path", "/var/log/tracker/app.log")
    try:
        with open(path, "r") as f:
            return jsonify({"path": path, "content": f.read()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── grep endpoint ─────────────────────────────────────────────────────────────

# FLAW: command injection — search_term passed directly to grep shell command
@audit_bp.route("/grep", methods=["GET"])
def grep_logs():
    search_term = request.args.get("q", "")
    log_dir     = request.args.get("dir", "/var/log/tracker")
    # FLAW: both search_term and log_dir unsanitised, shell=True
    cmd = f"grep -r '{search_term}' {log_dir}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    return jsonify({"matches": result.stdout, "stderr": result.stderr})


# ── Regex search ──────────────────────────────────────────────────────────────

# FLAW: ReDoS — user-supplied regex compiled and applied to large log content
@audit_bp.route("/regex-search", methods=["POST"])
def regex_search():
    pattern = request.json.get("pattern", "")
    content = request.json.get("content", "")
    try:
        # FLAW: no timeout, catastrophic regex from user can lock the thread
        matches = re.findall(pattern, content)
        return jsonify({"matches": matches})
    except re.error as e:
        return jsonify({"error": str(e)}), 400


# ── Compliance report generation ──────────────────────────────────────────────

# FLAW: SSTI — report_template rendered by Jinja2 with user input
@audit_bp.route("/report", methods=["POST"])
def generate_report():
    from flask import render_template_string
    report_template = request.json.get("template", "Audit report for {{ period }}")
    period          = request.json.get("period", "Q1 2024")
    # FLAW: user controls the entire template string — SSTI via Jinja2
    return render_template_string(report_template, period=period)


# ── Purge ─────────────────────────────────────────────────────────────────────

# FLAW: no CSRF protection; no ownership check; any user can wipe the audit trail
@audit_bp.route("/purge", methods=["POST"])
def purge_logs():
    before_date = request.json.get("before", "")
    conn = _get_db()
    # FLAW: SQL injection in before_date parameter
    conn.execute(f"DELETE FROM logs WHERE ts < '{before_date}'")
    conn.commit()
    conn.close()
    return jsonify({"message": "Logs purged", "before": before_date})
