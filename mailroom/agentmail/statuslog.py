"""The status log: one JSON line per event, written by hooks, by the tools and by agents themselves.

Events. Each carries ts, host, cli and session_id; all but limit-wait carry name, and most cwd and branch:
  idle    a turn finished                          (Stop hook; no message text, ever)
  start   session started / resumed                (SessionStart hook)
  end     session ended                            (SessionEnd hook)
  quota   a usage-limit notification fired         (Notification hook, quota_* types)
  limit   a turn ended on a plan limit             (StopFailure hook: rate_limit, billing_error, account_on_hold)
  api-error  a turn ended on another API error     (StopFailure hook; text = error and details)
  failover   agent-failover moved a session to another Claude login (account = the new login)
  limit-wait an agent hit its limit and no other login had headroom
  note    an agent chose to record a milestone     (`agent-status note ...`; has text)
  spawn   agent-spawn started a session            (text = brief path, and the failure if any)
  closed  agent-close ended a session              (text = what it did)
  orchestrator  one orchestrator run               (text = source, outcome, cost, turns, the login it ran on)
  switch  agent-switch tried a switch              (text = from -> to and the result)

Nothing here calls a model. Reading it is the report's job; writing it is free. The hook's own failures go to a
small file of their own beside it (record_hook_error), which the report counts.
"""
import os
import time
from pathlib import Path

from . import commands, config, jsonfile, registry

STATE_DIR = Path(os.environ.get("AGENT_STATUS_DIR", config.HOME / ".local" / "state" / "agent-status"))
LOG = STATE_DIR / "events.jsonl"
ROTATE_BYTES = 20 * 1024 * 1024
HOOK_ERRORS = STATE_DIR / "hook-errors.jsonl"   # the agent-status hook's own failures (record_hook_error)
HOOK_ERRORS_BYTES = 64 * 1024                   # only the latest matter: the report counts them


def _append(path, record, rotate_bytes):
    """Append record to the JSON-lines file path in STATE_DIR (owner-only); a file over rotate_bytes is first
    renamed to <path>.1, replacing the one before."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    try:
        if path.exists() and path.stat().st_size > rotate_bytes:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except FileNotFoundError:   # another writer rotated it first
        pass
    jsonfile.append_line(path, record, jsonfile.PRIVATE)


def append(event):
    """Write one event to LOG, with its time and host. Returns the event as written."""
    event = dict(event)
    event.setdefault("ts", time.time())
    event.setdefault("host", registry.HOST)
    _append(LOG, event, ROTATE_BYTES)
    return event


def record_hook_error(cli, hook, error):
    """Write one failure of the agent-status hook (the hook event it ran for, the error) to HOOK_ERRORS, rotated
    past HOOK_ERRORS_BYTES. Raises OSError if it cannot be written."""
    _append(HOOK_ERRORS, {"ts": time.time(), "cli": cli, "hook": hook, "error": error[:500]}, HOOK_ERRORS_BYTES)


def hook_errors(since):
    """How many hook failures HOOK_ERRORS holds from epoch `since` on."""
    return sum(1 for e in jsonfile.read_lines(HOOK_ERRORS) if e.get("ts", 0) >= since)


def read(since=None, session_id=None, event=None, limit=None):
    """The logged events that match, oldest first (the last `limit` of them); a damaged line is skipped and said on
    stderr (jsonfile.read_lines)."""
    out = [e for e in jsonfile.read_lines(LOG)
           if (since is None or e.get("ts", 0) >= since) and (not session_id or e.get("session_id") == session_id)
           and (not event or e.get("event") == event)]
    return out[-limit:] if limit else out


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
    """git's output in cwd, stripped; None if it failed."""
    r = commands.run(["git", "-C", cwd] + list(args), timeout)
    return r.stdout.strip() if r.returncode == 0 else None


def git_facts(cwd):
    """The branch, changed tracked files, last commit and repo name of the checkout at cwd (runs git). Outside a
    repo dirty is None, and inside one it is None too when git status failed: unknown, never 0. A branch or last
    commit git cannot give (a repo with no commits yet) is ""."""
    if not cwd or not os.path.isdir(cwd):
        return {"branch": "", "dirty": None, "last_commit": "", "repo": ""}
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return {"branch": "", "dirty": None, "last_commit": "", "repo": ""}
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""
    porcelain = _git(cwd, "status", "--porcelain", "--untracked-files=no")
    dirty = None if porcelain is None else sum(1 for ln in porcelain.splitlines() if ln.strip())
    last = _git(cwd, "log", "-1", "--format=%h %s (%cr)") or ""
    common = commands.common_dir(cwd)
    repo = os.path.basename(os.path.dirname(common)) if common else os.path.basename(top)
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
