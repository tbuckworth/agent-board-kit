"""The status log: one JSON line per event, written by hooks and by agents themselves.

Events (all carry ts, host, cli, session_id, name, cwd, branch):
  idle    a turn finished                          (Stop hook; no message text, ever)
  start   session started / resumed                (SessionStart hook)
  end     session ended                            (SessionEnd hook)
  quota   a usage-limit notification fired         (Notification hook, quota_* types)
  limit   a turn ended on a plan limit             (StopFailure hook: rate_limit, billing_error, account_on_hold)
  api-error  a turn ended on another API error     (StopFailure hook; text = error and details)
  failover   agent-failover moved a session to another Claude login (account = the new login)
  limit-wait an agent hit its limit and no other login had headroom
  note    an agent chose to record a milestone     (`agent-status note ...`; has text)
  spawn   agent-spawn started a session            (has text = brief path)

Nothing here calls a model. Reading it is the report's job; writing it is free.
"""
import json
import os
import subprocess
import time
from pathlib import Path

from . import registry

STATE_DIR = Path(os.environ.get("AGENT_STATUS_DIR", Path.home() / ".local" / "state" / "agent-status"))
LOG = STATE_DIR / "events.jsonl"
ROTATE_BYTES = 20 * 1024 * 1024


def append(event):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    event = dict(event)
    event.setdefault("ts", time.time())
    event.setdefault("host", registry.HOST)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        if LOG.exists() and LOG.stat().st_size > ROTATE_BYTES:
            os.replace(LOG, LOG.with_suffix(".jsonl.1"))
    except OSError:
        pass
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(line)
    return event


def read(since=None, session_id=None, event=None, limit=None):
    if not LOG.exists():
        return []
    out = []
    with open(LOG, errors="replace") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if since is not None and e.get("ts", 0) < since:
                continue
            if session_id and e.get("session_id") != session_id:
                continue
            if event and e.get("event") != event:
                continue
            out.append(e)
    if limit:
        out = out[-limit:]
    return out


def parse_since(spec, now=None):
    """'24h', '7d', '90m', or a unix ts."""
    now = now or time.time()
    if spec is None:
        return None
    s = str(spec).strip().lower()
    mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s and s[-1] in mult and s[:-1].replace(".", "", 1).isdigit():
        return now - float(s[:-1]) * mult[s[-1]]
    try:
        return float(s)
    except ValueError:
        raise ValueError("bad --since %r (use e.g. 24h, 7d)" % spec)


def _git(cwd, *args, timeout=5):
    try:
        r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def git_facts(cwd):
    if not cwd or not os.path.isdir(cwd):
        return {"branch": "", "dirty": None, "last_commit": "", "repo": ""}
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return {"branch": "", "dirty": None, "last_commit": "", "repo": ""}
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git(cwd, "status", "--porcelain", "--untracked-files=no")
    dirty = len([ln for ln in porcelain.splitlines() if ln.strip()]) if porcelain is not None else None
    last = _git(cwd, "log", "-1", "--format=%h %s (%cr)")
    common = _git(cwd, "rev-parse", "--git-common-dir")
    repo = os.path.basename(os.path.dirname(os.path.abspath(os.path.join(cwd, common)))) if common else os.path.basename(top)
    return {"branch": branch, "dirty": dirty, "last_commit": last[:90], "repo": repo}


def identity(cli=None, session_id=None, cwd=None, name=None):
    """Fill in cli/session_id/name/cwd/branch for an event, from whatever is known."""
    cwd = cwd or os.getcwd()
    sess = None
    if session_id:
        for s in registry.all_sessions(cli):
            if s["id"] == session_id:
                sess = s
                break
    if sess is None and not session_id:
        sess = registry.current_session(cwd)
    if sess:
        cli = cli or sess["cli"]
        session_id = session_id or sess["id"]
        name = name or sess.get("name") or ""
        cwd = sess.get("cwd") or cwd
    facts = git_facts(cwd)
    return {"cli": cli or "?", "session_id": session_id or "", "name": name or "",
            "cwd": cwd, "branch": facts["branch"]}
