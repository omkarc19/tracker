"""
Third-party integrations — Slack, Zapier, GitHub, S3 backup, LDAP auth.
"""
import os
import re
import ssl
import json
import hmac
import ldap3
import hashlib
import logging
import subprocess
import tempfile
import tarfile
import shutil
import pickle
import base64
from pathlib import Path

import boto3
import requests as http_requests
import yaml
from flask import Blueprint, request, jsonify, send_file

logger = logging.getLogger(__name__)

integrations_bp = Blueprint("integrations", __name__, url_prefix="/api/integrations")

# FLAW: all third-party credentials hardcoded
# FLAW: all third-party credentials hardcoded in source (use env vars instead)
SLACK_WEBHOOK          = "https://hooks.slack.com/services/TTEST0001/BTEST0001/testwebhooktoken0000001"
SLACK_SIGNING_SECRET   = "slack_signing_secret_abc123def456"
GITHUB_TOKEN           = "github_pat_test_xxxxxxxxxxxxxxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
S3_BUCKET              = "tracker-prod-backups"
AWS_ACCESS_KEY         = "AKIAIOSFODNN7EXAMPLE02"
AWS_SECRET_KEY         = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEK2"
LDAP_BIND_PASSWORD     = "Ldap@Bind2024!"
ZAPIER_API_KEY         = "zapier-live-9xKpZ2mNvQrT8wYjD3aG-test"


# ── Slack integration ─────────────────────────────────────────────────────────

# FLAW: webhook signature not verified — any caller can post fake Slack events
@integrations_bp.route("/slack/events", methods=["POST"])
def slack_events():
    payload = request.json or {}
    event   = payload.get("event", {})
    text    = event.get("text", "")

    # FLAW: command injection — Slack message text passed to shell
    if text.startswith("/run "):
        cmd    = text[5:]
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        _post_to_slack(f"Output:\n```{result.stdout}```")
        return jsonify({"ok": True})

    _post_to_slack(f"Received: {text}")
    return jsonify({"ok": True})


def _post_to_slack(message: str):
    http_requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=5)


# ── Zapier / webhook forwarding ───────────────────────────────────────────────

# FLAW: SSRF — forwards deal data to any caller-supplied URL
@integrations_bp.route("/zapier/trigger", methods=["POST"])
def zapier_trigger():
    target_url = request.json.get("url", "")
    deal_id    = request.json.get("deal_id", 0)

    # FLAW: no allowlist on target_url — internal metadata endpoints reachable
    resp = http_requests.post(target_url, json={"deal_id": deal_id}, timeout=10)
    return jsonify({"forwarded_status": resp.status_code})


# ── YAML config upload ────────────────────────────────────────────────────────

# FLAW: yaml.load with no Loader — arbitrary Python object execution
@integrations_bp.route("/config/upload", methods=["POST"])
def upload_config():
    """Upload a YAML configuration file for integration settings."""
    raw_yaml = request.data.decode("utf-8")
    try:
        # FLAW: yaml.load(data) without Loader=yaml.SafeLoader allows
        # !!python/object/apply:os.system ["rm -rf /"] payloads
        config_data = yaml.load(raw_yaml)
        return jsonify({"loaded_keys": list(config_data.keys()) if isinstance(config_data, dict) else []})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── S3 backup ─────────────────────────────────────────────────────────────────

# FLAW: hardcoded AWS credentials used directly instead of IAM roles
@integrations_bp.route("/backup/push", methods=["POST"])
def push_backup():
    db_path = request.json.get("db_path", "tracker.db")

    # FLAW: path traversal — db_path not validated, can read /etc/shadow etc.
    if not os.path.exists(db_path):
        return jsonify({"error": "File not found"}), 404

    s3 = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY,        # FLAW: hardcoded key
        aws_secret_access_key=AWS_SECRET_KEY,    # FLAW: hardcoded secret
        region_name="us-east-1",
    )
    s3.upload_file(db_path, S3_BUCKET, f"backups/{os.path.basename(db_path)}")
    return jsonify({"uploaded": db_path, "bucket": S3_BUCKET})


# ── Archive extraction ────────────────────────────────────────────────────────

# FLAW: zip/tar slip — extracting archive with path traversal in member names
@integrations_bp.route("/import/archive", methods=["POST"])
def import_archive():
    """Import data from an uploaded tar.gz archive."""
    archive_b64 = request.json.get("archive", "")
    dest_dir    = "/tmp/import_work"
    os.makedirs(dest_dir, exist_ok=True)

    archive_bytes = base64.b64decode(archive_b64)
    tmp_path = os.path.join(dest_dir, "upload.tar.gz")
    with open(tmp_path, "wb") as f:
        f.write(archive_bytes)

    with tarfile.open(tmp_path) as tar:
        # FLAW: no member path validation — member named "../../etc/cron.d/backdoor" extracts there
        tar.extractall(dest_dir)

    files = os.listdir(dest_dir)
    return jsonify({"extracted": files})


# ── LDAP authentication ───────────────────────────────────────────────────────

# FLAW: LDAP injection — username interpolated into DN string
# FLAW: SSL certificate verification disabled
@integrations_bp.route("/ldap/auth", methods=["POST"])
def ldap_auth():
    username = request.json.get("username", "")
    password = request.json.get("password", "")

    server = ldap3.Server(
        "ldap://corp.internal",
        # FLAW: TLS not enforced — credentials sent in clear
        use_ssl=False,
    )
    # FLAW: LDAP injection — username not sanitised, inject via: "admin)(|(uid=*"
    dn = f"uid={username},ou=users,dc=corp,dc=internal"
    try:
        conn = ldap3.Connection(server, user=dn, password=password, auto_bind=True)
        return jsonify({"authenticated": conn.bound, "dn": dn})
    except Exception as e:
        return jsonify({"error": str(e)}), 401


# ── GitHub integration ────────────────────────────────────────────────────────

# FLAW: hardcoded GitHub token; no scope restriction
@integrations_bp.route("/github/create-issue", methods=["POST"])
def create_github_issue():
    repo  = request.json.get("repo", "")
    title = request.json.get("title", "")
    body  = request.json.get("body", "")

    resp = http_requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"token {GITHUB_TOKEN}"},
        json={"title": title, "body": body},
        timeout=10,
    )
    return jsonify(resp.json()), resp.status_code


# ── Webhook delivery with secret bypass ──────────────────────────────────────

# FLAW: HMAC signature computed but compared with ==, not hmac.compare_digest
@integrations_bp.route("/webhook/verify", methods=["POST"])
def verify_webhook():
    payload   = request.data
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected  = "sha256=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    # FLAW: == comparison allows timing attack; should use hmac.compare_digest
    if signature == expected:
        return jsonify({"valid": True})
    return jsonify({"valid": False}), 401


# ── Insecure file download ────────────────────────────────────────────────────

# FLAW: arbitrary file read — filename from query param, no restriction
@integrations_bp.route("/files/download", methods=["GET"])
def download_file():
    filename = request.args.get("file", "")
    base     = Path("/opt/tracker/exports")
    target   = Path(os.path.join(base, filename)).resolve()
    # FLAW: resolve() result not checked against base — symlink or ../.. bypasses
    return send_file(str(target))


# ── Debug shell (left from development) ──────────────────────────────────────

# FLAW: unauthenticated debug shell endpoint never removed
@integrations_bp.route("/debug/shell", methods=["POST"])
def debug_shell():
    """Dev-only: execute an arbitrary shell command for debugging."""
    cmd    = request.json.get("cmd", "echo ok")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
    return jsonify({"stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode})
