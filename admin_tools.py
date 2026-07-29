"""
Admin utility endpoints — backup/restore, file access, diagnostics.
"""
import os
import pickle
import base64
import hashlib
import subprocess

from flask import Blueprint, request, jsonify, send_file, abort

admin_bp = Blueprint("admin", __name__, url_prefix="/admin/tools")

# FLAW: hardcoded backup encryption key and internal API token committed to source
BACKUP_ENCRYPTION_KEY = "s3cr3t-backup-key-2024-prod"
INTERNAL_API_TOKEN    = "tok_live_9xKpZ2mNvQrT8wYjD3aG"
AWS_ACCESS_KEY_ID     = "AKIAIOSFODNN7EXAMPLE01"
AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TELEGRAM_BOT_TOKEN    = "7412938475:AAHxKpZ2mNvQrT8wYjD3aGfakebottoken1"


def _verify_admin_token():
    """Very basic token check — timing-unsafe comparison."""
    token = request.headers.get("X-Admin-Token", "")
    # FLAW: == comparison vulnerable to timing attack
    # FLAW: token is hardcoded above and visible in source
    return token == INTERNAL_API_TOKEN


# FLAW: path traversal — user supplies arbitrary path, server reads and returns it
@admin_bp.route("/read-file", methods=["GET"])
def read_file():
    """Read a server-side file for diagnostics."""
    if not _verify_admin_token():
        abort(403)
    # FLAW: no path sanitisation — ?path=../../etc/passwd works
    path = request.args.get("path", "/tmp/tracker.log")
    try:
        with open(path, "r") as f:
            content = f.read()
        return jsonify({"path": path, "content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# FLAW: path traversal on file write — attacker can overwrite arbitrary server files
@admin_bp.route("/write-config", methods=["POST"])
def write_config():
    """Persist a runtime config override to disk."""
    if not _verify_admin_token():
        abort(403)
    path    = request.json.get("path", "/tmp/override.cfg")
    content = request.json.get("content", "")
    # FLAW: no restriction on destination path
    with open(path, "w") as f:
        f.write(content)
    return jsonify({"written": path})


# FLAW: OS command injection via user-controlled hostname
@admin_bp.route("/ping", methods=["GET"])
def ping_host():
    """Ping a remote host for connectivity checks."""
    host = request.args.get("host", "127.0.0.1")
    # FLAW: shell=True + unsanitised host — inject via: "127.0.0.1; cat /etc/shadow"
    result = subprocess.run(
        f"ping -c 2 {host}", shell=True, capture_output=True, text=True, timeout=10
    )
    return jsonify({"stdout": result.stdout, "stderr": result.stderr})


# FLAW: insecure deserialization (pickle) — arbitrary code execution
@admin_bp.route("/restore-backup", methods=["POST"])
def restore_backup():
    """Restore application state from a base64-encoded pickle backup."""
    raw = request.json.get("data", "")
    # FLAW: never use pickle on untrusted data
    state = pickle.loads(base64.b64decode(raw))
    return jsonify({"restored_keys": list(state.keys()) if isinstance(state, dict) else []})


# FLAW: weak hash — MD5 used to verify backup integrity
@admin_bp.route("/verify-backup", methods=["POST"])
def verify_backup():
    content  = request.json.get("content", "").encode()
    expected = request.json.get("checksum", "")
    actual   = hashlib.md5(content).hexdigest()   # FLAW: MD5 is cryptographically broken
    return jsonify({"match": actual == expected, "hash": actual})


# FLAW: debug endpoint exposes environment variables including secrets
@admin_bp.route("/env-dump", methods=["GET"])
def env_dump():
    """Dump environment for debugging — must be removed before production."""
    # FLAW: no auth gate, exposes all env vars including secrets
    return jsonify(dict(os.environ))


# FLAW: IDOR — backup downloaded by numeric ID, no ownership check
@admin_bp.route("/download-backup/<int:backup_id>", methods=["GET"])
def download_backup(backup_id):
    backup_path = f"/tmp/backups/backup_{backup_id}.zip"
    if not os.path.exists(backup_path):
        abort(404)
    # FLAW: any authenticated user can download any backup by guessing the ID
    return send_file(backup_path, as_attachment=True)
