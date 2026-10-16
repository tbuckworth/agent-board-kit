"""Hand a text to a session via agent-send."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import STATE_DIR


BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


def agent_send_bin():
    here = BIN_DIR / "agent-send"
    if here.exists():
        return str(here)
    found = shutil.which("agent-send")
    if found:
        return found
    cand = Path.home() / ".local" / "bin" / "agent-send"
    if cand.exists():
        return str(cand)
    raise RuntimeError("agent-send not found")


def agent_send_cmd():
    """agent-send as a command line. The kit's own copy runs under this interpreter (the kit venv when the
    caller was started through an installed wrapper or a systemd unit), anything else as found."""
    path = agent_send_bin()
    return [sys.executable, path] if Path(path).parent == BIN_DIR else [path]


def registry():
    r = subprocess.run(agent_send_cmd() + ["--list", "--json"], capture_output=True, text=True, timeout=60)
    import json
    return json.loads(r.stdout or "[]")


def _send(cmd, target, text, timeout):
    """Verify a transport receipt before treating a sender exit/timeout as failure.

    agent-send writes acceptance before memory capping or printing. A restart can
    otherwise lose its stdout even after the session has received the message.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    with tempfile.TemporaryDirectory(prefix="delivery-", dir=STATE_DIR) as tmp:
        receipt = Path(tmp) / "accepted.json"
        failure = None
        try:
            r = subprocess.run(cmd + ["--receipt", str(receipt), target], input=text,
                               capture_output=True, text=True, timeout=timeout, env=env)
            rc, out = r.returncode, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as e:
            failure = e
        if failure is not None or rc != 0:
            try:
                accepted = json.loads(receipt.read_text())
            except (OSError, ValueError):
                accepted = {}
            if isinstance(accepted, dict) and accepted.get("target") == target and (
                    accepted.get("sha256") == hashlib.sha256(text.strip().encode()).hexdigest()):
                return 0, "session accepted the message; transport receipt verified after sender interruption"
        if failure is not None:
            raise failure
        return rc, out


def deliver(cli, session_id, text, resume=True, dangerous=True, timeout=240):
    """Returns (exit_code, output). 0 sent/queued/resumed, 2 offline and not resumed, else error."""
    cmd = agent_send_cmd() + ["--raw", "--exact-id", "--cli", cli, "--offline", "resume" if resume else "fail"]
    cmd.append("--dangerous" if dangerous else "--no-dangerous")
    return _send(cmd, session_id, text, timeout)


def deliver_session(session, text, cfg):
    """Known origin only. Remote sessions are never resumed, even if local resume is enabled."""
    from . import remote
    host = session["origin_host"]
    try:
        if host == cfg["host_id"]:
            return deliver(session["cli"], session["session_id"], text,
                           cfg["resume_offline"], cfg["resume_dangerous"])
        # The receiving Codex can answer without guessing its identity.
        import re
        text = re.sub(r"agent-mail reply (\d+)",
                      lambda m: m.group(0) + " --session " + session["session_id"], text, count=1)
        result = remote.call(cfg, host, {"op": "deliver", "cli": session["cli"],
                                       "session_id": session["session_id"], "text": text})
        return result["code"], result["detail"]
    except (remote.RemoteError, OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def deliver_target(target, text, resume=False, dangerous=True, timeout=240):
    """Deliver to a session by name or id, any CLI. Same exit codes as deliver()."""
    cmd = agent_send_cmd() + ["--raw", "--offline", "resume" if resume else "fail"]
    cmd.append("--dangerous" if dangerous else "--no-dangerous")
    return _send(cmd, target, text, timeout)
