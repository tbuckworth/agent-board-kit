"""Small authenticated SSH transport. Message data is JSON on stdin, never shell code."""
import json
import re
import subprocess
import uuid

from . import config

PROTOCOL = 1
# A request's bytes, either way: room for the files of a Mail-tab reply (mailbox.MAX_ANSWER_FILE_BYTES) or an agent's
# attachments (outbound.MAX_IMAGES_BYTES) once base64 has grown them by a third, and the text around them.
MAX_REQUEST = 40 * 1024 * 1024
TOO_LARGE = "request exceeds %d MiB" % (MAX_REQUEST >> 20)


class RemoteError(RuntimeError):
    pass


def identity(data):
    if not isinstance(data, dict) or data.get("cli") not in config.CLIS:
        raise ValueError("origin must identify Claude or Codex")
    sid = data.get("id", "")
    if str(uuid.UUID(sid)) != sid:
        raise ValueError("origin must use a full canonical session UUID")
    for key in ("host", "name", "cwd"):
        if not isinstance(data.get(key), str) or len(data[key]) > 4096:
            raise ValueError("invalid origin " + key)
    return {k: data[k] for k in ("host", "cli", "id", "name", "cwd")}


def call(cfg, host, payload, timeout=None):
    """Send payload to host (a config remote_hosts name) over SSH, where config remote_command answers it, and return
    the response's result. Raises RemoteError when host is no configured peer, SSH fails or times out, or the answer
    is not valid or is an error."""
    alias = cfg["remote_hosts"].get(host)
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", alias):
        raise RemoteError("host %r has no configured SSH peer" % host)
    request = dict(payload, protocol=PROTOCOL, source_host=cfg["host_id"], destination_host=host)
    wire = json.dumps(request)
    if len(wire.encode()) > MAX_REQUEST:
        raise RemoteError(TOO_LARGE)
    cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
           "-o", "ConnectionAttempts=1", "-o", "StrictHostKeyChecking=yes",
           "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
           alias, cfg["remote_command"]]
    try:
        r = subprocess.run(cmd, input=wire, capture_output=True, text=True,
                           timeout=timeout or cfg["remote_timeout"])
    except subprocess.TimeoutExpired:
        raise RemoteError("%s: SSH operation timed out; outcome may be unknown; no automatic retry" % host)
    except OSError as e:
        raise RemoteError("%s: SSH could not start: %s" % (host, e))
    if r.returncode:
        raise RemoteError("%s: SSH failed (%d): %s" % (host, r.returncode, r.stderr.strip()[:500]))
    try:
        response = json.loads(r.stdout)
        if response.get("protocol") != PROTOCOL:
            raise ValueError("protocol mismatch")
    except (ValueError, AttributeError):
        raise RemoteError("%s: invalid RPC response; outcome may be unknown; no automatic retry" % host)
    if not response.get("ok"):
        raise RemoteError("%s: %s" % (host, response.get("error", "RPC failed")))
    return response["result"]
