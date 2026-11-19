"""Hand a text to a session via agent-send, and the delivery receipt agent-send writes once the session took it."""
import base64
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import jsonfile, registry, replyfiles
from .config import STATE_DIR

AGENT_SEND = Path(__file__).resolve().parent.parent / "bin" / "agent-send"
# agent-send's exit codes for a session that cannot take a message until someone starts it again: not live and not
# resumed (2), no such session (4), its folder removed (5). A Mac answers a delivery with the same codes.
SESSION_GONE = (2, 4, 5)


def agent_send_bin():
    """This checkout's bin/agent-send. Raises RuntimeError when it is missing."""
    if not AGENT_SEND.exists():
        raise RuntimeError("agent-send not found at %s" % AGENT_SEND)
    return str(AGENT_SEND)


def receipt(target, body):
    """What a delivery receipt holds: the target as deliver passed it, and the sha256 of the body stripped."""
    return {"target": target, "sha256": hashlib.sha256(body.strip().encode()).hexdigest()}


def write_receipt(path, target, body):
    """Record at path (a file only its owner can read) that the session accepted body for target: agent-send calls
    this once the session takes the message. Raises OSError."""
    jsonfile.write(path, receipt(target, body), jsonfile.PRIVATE, None)


def receipt_written(path, target, body):
    """Whether path holds the receipt for body to target (False when it is missing or holds anything else)."""
    try:
        return json.loads(Path(path).read_text()) == receipt(target, body)
    except (OSError, ValueError):
        return False


def _send(cmd, target, text, timeout):
    """Verify a transport receipt before treating a sender exit/timeout as failure.

    agent-send writes acceptance before memory capping or printing. A restart can
    otherwise lose its stdout even after the session has received the message.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    with tempfile.TemporaryDirectory(prefix="delivery-", dir=STATE_DIR) as tmp:
        path = Path(tmp) / "accepted.json"
        failure = None
        try:
            r = subprocess.run(cmd + ["--receipt", str(path), target], input=text,
                               capture_output=True, text=True, timeout=timeout, env=env)
            rc, out = r.returncode, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as e:
            failure = e
        if (failure is not None or rc != 0) and receipt_written(path, target, text):
            return 0, "session accepted the message; transport receipt verified after sender interruption"
        if failure is not None:
            raise failure
        return rc, out


def deliver(cli, session_id, text, resume, dangerous, timeout=240):
    """Hand text to session_id through agent-send (a subprocess); an offline session is resumed only when resume,
    and then with permissions bypassed only when dangerous. Returns (exit_code, output): 0 sent/queued/resumed,
    a SESSION_GONE code when the session cannot take it, else error."""
    cmd = [agent_send_bin(), "--raw", "--exact-id", "--cli", cli, "--offline", "resume" if resume else "fail"]
    cmd.append("--dangerous" if dangerous else "--no-dangerous")
    return _send(cmd, session_id, text, timeout)


def deliver_session(session, text, cfg):
    """Known origin only. Remote sessions are never resumed, even if local resume is enabled."""
    host = session["origin_host"]
    if host == cfg["host_id"]:
        try:
            return deliver(session["cli"], session["session_id"], text,
                           cfg["resume_offline"], cfg["resume_dangerous"])
        except (OSError, subprocess.SubprocessError) as e:
            return 1, str(e)
    # The receiving Codex can answer without guessing its identity.
    import re
    text = re.sub(r"agent-mail reply (\d+)",
                  lambda m: m.group(0) + " --session " + session["session_id"], text, count=1)
    return _remote_deliver(cfg, host, {"cli": session["cli"], "session_id": session["session_id"], "text": text})


def deliver_named(agent, text, timeout):
    """Hand text to this machine's session named agent (registry.by_name) through agent-send, never resuming it.
    Returns (exit code, output): 0 sent or queued, 2 not live, 3 several live sessions have that name, 4 none has."""
    sess, code, cands = registry.by_name(registry.all_sessions(), agent)
    if code == 4:
        return 4, "no session is named %s" % agent
    if code == 3:
        return 3, "%d live sessions are named %s" % (len(cands), agent)
    return deliver(sess["cli"], sess["id"], text, resume=False, dangerous=False, timeout=timeout)


def deliver_named_on(cfg, host, agent, text):
    """deliver_named on host: here, or over SSH on another machine (config remote_hosts). Returns (exit code,
    output); 1 when the delivery failed, the transport included."""
    if host == cfg["host_id"]:
        try:
            return deliver_named(agent, text, timeout=60)
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            return 1, str(e)
    return _remote_deliver(cfg, host, {"agent": agent, "text": text})


def place_files(cfg, host, key, files):
    """Put the files of reply key ([(name, bytes)], replyfiles.write's names) on host, for its session to open:
    under this machine's replyfiles.FILES_DIR, or over SSH under host's (rpc "files"), within config
    remote_files_timeout seconds. Returns their absolute paths on host, in order. Raises OSError or ValueError when
    they cannot be written here, remote.RemoteError when host does not write them."""
    if host == cfg["host_id"]:
        return replyfiles.write(replyfiles.FILES_DIR, key, files)
    from . import remote
    paths = remote.call(cfg, host, {"op": "files", "key": key,
                                    "files": [{"name": n, "data": base64.b64encode(d).decode()} for n, d in files]},
                        timeout=cfg["remote_files_timeout"])["paths"]
    if not (isinstance(paths, list) and len(paths) == len(files) and all(isinstance(p, str) for p in paths)):
        raise remote.RemoteError("%s: the files' paths are not understood: %r" % (host, paths))
    return paths


def _remote_deliver(cfg, host, payload):
    """A delivery on another machine (its rpc "deliver"): (exit code, output), 1 when the transport failed."""
    from . import remote
    try:
        result = remote.call(cfg, host, dict(payload, op="deliver"))
        return result["code"], result["detail"]
    except (remote.RemoteError, OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
