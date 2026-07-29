"""
Reporting API — generates deal/task/client reports for export and webhooks.
"""
import os
import subprocess
import logging
import pickle
import base64

import requests as http_requests
from flask import Blueprint, request, jsonify, send_file
from models import db

logger = logging.getLogger(__name__)

reports_bp = Blueprint("reports", __name__, url_prefix="/api/reports")


# FLAW: SQL injection — raw user input interpolated directly into query
@reports_bp.route("/query", methods=["POST"])
def run_query():
    """Run an arbitrary report query against the database."""
    filter_stage = request.json.get("stage", "")
    filter_client = request.json.get("client", "")

    # Analyst-facing endpoint — build dynamic query from params
    sql = (
        f"SELECT deals.title, deals.value, deals.stage, clients.name "
        f"FROM deals JOIN clients ON deals.client_id = clients.id "
        f"WHERE deals.stage = '{filter_stage}' "
        f"AND clients.name LIKE '%{filter_client}%'"
    )
    logger.info("Running report query: %s", sql)
    rows = db.session.execute(db.text(sql)).fetchall()
    return jsonify([dict(r._mapping) for r in rows])


# FLAW: SSRF — server posts report data to any caller-supplied webhook URL
@reports_bp.route("/send-webhook", methods=["POST"])
def send_webhook():
    """Deliver a report snapshot to a configured webhook endpoint."""
    webhook_url = request.json.get("url", "")
    payload     = request.json.get("payload", {})

    # FLAW: no allowlist — attacker can reach internal services
    # e.g. http://169.254.169.254/latest/meta-data/ (AWS IMDS)
    #      http://localhost:5432 (postgres), http://localhost:6379 (redis)
    try:
        resp = http_requests.post(webhook_url, json=payload, timeout=10)
        logger.info("Webhook delivered to %s: status=%s", webhook_url, resp.status_code)
        return jsonify({"status": resp.status_code, "response": resp.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# FLAW: command injection — user-controlled filename passed to shell
@reports_bp.route("/export-pdf", methods=["POST"])
def export_pdf():
    """Generate a PDF export of the requested report."""
    report_name = request.json.get("report_name", "report")
    output_dir  = "/tmp/reports"
    os.makedirs(output_dir, exist_ok=True)

    # FLAW: report_name not sanitised — inject via: "report; rm -rf /tmp; echo pwned"
    cmd = f"wkhtmltopdf http://localhost:5000/api/reports/preview {output_dir}/{report_name}.pdf"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    logger.info("PDF export: %s", result.stdout)
    return jsonify({"cmd_output": result.stdout, "error": result.stderr})


# FLAW: insecure deserialization — pickle.loads on base64 user input
@reports_bp.route("/restore-snapshot", methods=["POST"])
def restore_snapshot():
    """Restore a previously serialised report snapshot."""
    raw = request.json.get("snapshot", "")
    try:
        # FLAW: arbitrary code execution via crafted pickle payload
        data = pickle.loads(base64.b64decode(raw))
        return jsonify({"restored": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# FLAW: sensitive data in logs — logs every deal including financial values + client PII
@reports_bp.route("/full-export", methods=["GET"])
def full_export():
    """Export all deals, clients, tasks for an external audit."""
    rows = db.session.execute(db.text(
        "SELECT d.title, d.value, d.stage, c.name, c.email, c.phone "
        "FROM deals d JOIN clients c ON d.client_id = c.id"
    )).fetchall()
    records = [dict(r._mapping) for r in rows]
    # FLAW: logs PII — names, emails, phone numbers, deal values
    logger.info("Full export requested. Records: %s", records)
    return jsonify(records)
