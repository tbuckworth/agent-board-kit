"""Delivering a message to a Claude Code or Codex session, for agent-send: the "[from ...]" line that says who sent
it, the live routes (Claude's inbox socket, `codex queue`) and resuming an offline session (on the best Claude login,
inside its memory cap). README.md, section "agent-send", describes each route."""
import json
import os
import socket
import subprocess
import time

from . import claude_session, config, failover, launch, memcap, registry
from .trust import claude_json_for, ensure_trust
from .registry import all_sessions, claude_bin, codex_bin, short

LOG_DIR = config.HOME / ".local" / "state" / "agent-send"


def wrap(body, sender):
    """Prefix the body with who sent it. The socket path strips sender identity
    from external senders, and Codex queue has none, so say it in the text. If the
    sender is a live session, say how to reply; if not, say there is no reply address
    so the receiver does not waste a SendMessage on it."""
    bare = sender.split(":", 1)[1] if sender.startswith(("claude:", "codex:")) else sender
    live = [s for s in all_sessions() if s["live"] and s["name"] == bare]
    if live:
        return "[from %s via agent-send; reply with: agent-send %s '...']\n%s" % (sender, bare, body)
    return "[from %s via agent-send; not a session, no reply address]\n%s" % (sender, body)


def send_claude_socket(sess, text):
    """Write text to a live Claude session's inbox socket and return what it answers within half a second ("" when
    it says nothing). Raises OSError when the socket cannot be reached or fails, the answer's read included."""
    payload = json.dumps({"type": "user", "message": {"role": "user", "content": text}})
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(sess["socket"])
        tok = os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN")
        if tok and os.environ.get("CLAUDE_CODE_SESSION_ID") == sess["id"]:
            s.sendall((json.dumps({"type": "auth", "token": tok}) + "\n").encode())
        s.sendall((payload + "\n").encode())
        s.settimeout(0.5)
        try:
            ack = s.recv(4096).decode(errors="replace").strip()
        except socket.timeout:
            ack = ""
    finally:
        s.close()
    return ack


def queue_codex(sess, text):
    cx = codex_bin()
    if not cx:
        raise RuntimeError("codex binary not found")
    return launch.queue_codex(cx, sess["id"], text)


def agent_caps():
    """(scope properties, None), or (None, why not): a bad size in config must not block a resume."""
    try:
        return memcap.limits(config.load()), None
    except (ValueError, OSError) as e:
        return None, "uncapped: %s" % e


def spawn_trust(cwd, config_dir):
    """The trust writer (agentmail/trust.py) for the login a resume takes the conversation to (its .claude.json)."""
    return ensure_trust(cwd, claude_json_for(config_dir))


def resume_place(sess, dry_run=False):
    """(config_dir, note): the best login by usage, with the newest copy of the conversation there
    (agentmail/failover.place_resume, which says what it raises when no login can be chosen or the copy fails)."""
    return failover.place_resume(sess, config.load(), trust=spawn_trust, dry_run=dry_run)


def folder_gone(sess):
    """Why offline session sess cannot be resumed, its recorded folder being gone, or None. A session is never
    resumed in another folder (the home folder, say) instead."""
    if sess["cwd"] and not os.path.isdir(sess["cwd"]):
        return "original working folder no longer exists: %s; nothing started" % sess["cwd"]
    return None


def resume_claude(sess, text, dangerous, accepted):
    """Resume an offline Claude session with text as `claude --resume --bg` on the best login, then cap its memory.
    Calls accepted() once the resume has started, before the cap. Returns (ok, what happened)."""
    gone = folder_gone(sess)
    if gone:   # agent-send exits 5 before this; the guard stays so no caller starts the CLI in the wrong folder
        return False, gone
    cl = claude_bin()
    if not cl:
        raise RuntimeError("claude binary not found")
    config_dir, where = resume_place(sess)
    # under that login, in its folder; flags only where that login's daemon has not run it (else it starts a copy)
    ok, out = claude_session.bg_resume(cl, sess, ["--dangerously-skip-permissions"] if dangerous else [], config_dir, text)
    if where:
        out = where + "; " + out
    if ok:
        accepted()
        props, why = agent_caps()
        def live_pid():  # the daemon may hand the resume to a spare: the registry knows which process took it
            return next((s["pid"] for s in registry.live_on(config_dir) if s["id"] == sess["id"]), None)
        out += "; memory: " + (why or memcap.cap_bg_session(sess["id"], sess["name"] or short(sess["id"]), props,
                                                            session_pid=live_pid))
    return ok, out


def resume_codex(sess, text, dangerous, accepted):
    """Resume an offline Codex thread with text as a detached `codex exec resume` logging to LOG_DIR, then cap its
    memory. Calls accepted() once it has started, before the cap. Returns (ok, what happened)."""
    gone = folder_gone(sess)
    if gone:   # agent-send exits 5 before this; the guard stays so no caller starts the CLI in the wrong folder
        return False, gone
    cx = codex_bin()
    if not cx:
        raise RuntimeError("codex binary not found")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / ("codex-%s-%d.log" % (short(sess["id"]), int(time.time())))
    cmd = [cx, "exec", "--skip-git-repo-check"]
    if dangerous:
        cmd += ["-s", "danger-full-access"]
    cmd += ["resume", sess["id"], text]
    cwd = sess["cwd"] if sess["cwd"] and os.path.isdir(sess["cwd"]) else None
    with open(log, "ab") as fh:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                             cwd=cwd, start_new_session=True)
    accepted()
    props, why = agent_caps()
    if why is None:
        name = sess["name"] or short(sess["id"])
        why = memcap.adopt(p.pid, name, props, "agent %s (resumed, codex exec)" % name)[1]
    return True, "detached codex exec resume; log %s; memory: %s" % (log, why)
