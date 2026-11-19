"""Run the orchestrator: one fresh headless run per event, no long-lived session.

Each event is one `claude -p` (or `codex exec`) in the orchestrator directory with a fresh context. The brief in
that directory's CLAUDE.md/AGENTS.md and the memory in STATE.md are all it knows, plus the
event text (which carries the whole email thread when there is one). Runs are serialised with
a lock so two events never edit STATE.md at once. Every run's cost, turns and outcome are
appended to a run log.

A login that is out of usage or logged out passes the event to the next one (headless.logins):
each Claude login in usage.order, Codex where that order puts it, then the API key from
Bitwarden.
"""
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

from . import headless, jsonfile, registry, statuslog
from .config import DEFAULTS, STATE_DIR

RUNS_LOG = STATE_DIR / "orchestrator-runs.jsonl"
LOCK = STATE_DIR / "orchestrator.lock"


def command(cfg):
    """The claude command for one event (headless.claude_command): orchestrator_model and orchestrator_effort,
    permission prompts skipped. Raises RuntimeError when there is no claude binary."""
    return headless.claude_command(cfg.get("orchestrator_model") or DEFAULTS["orchestrator_model"],
                                   cfg.get("orchestrator_effort"), ["--dangerously-skip-permissions"])


def codex_command(cfg, workdir):
    """`codex exec` on the event from stdin, as JSON lines. --ignore-user-config leaves out ~/.codex/config.toml's
    MCP servers (a mail server among them, say), plugins and hooks, as the claude run has no MCP; the global and the
    directory's AGENTS.md are still read, and auth still comes from CODEX_HOME."""
    cx = registry.codex_bin()
    if not cx:
        raise RuntimeError("codex binary not found")
    cmd = [cx, "exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "-s", "danger-full-access",
           "-C", str(workdir)] + registry.CODEX_NO_UPDATE_PROMPT
    model = cfg.get("orchestrator_codex_model") or cfg.get("spawn_codex_model")
    if model:
        cmd += ["-m", model]
    eff = cfg.get("orchestrator_codex_effort")
    if eff:
        cmd += ["-c", 'model_reasoning_effort="%s"' % eff]
    return cmd + ["-"]


def codex_result(out):
    """codex exec --json output -> the claude -p result fields: result (its last message), session_id,
    num_turns (commands it ran) and is_error. An "error" item is a warning (an unreadable plugin config);
    an "error" event or turn.failed is the run failing. {} when out holds no JSON event."""
    j = {"result": "", "session_id": "", "num_turns": 0, "is_error": False}
    events = 0
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        events += 1
        item = e.get("item") or {}
        if e.get("type") == "thread.started":
            j["session_id"] = e.get("thread_id", "")
        elif e.get("type") == "item.completed" and item.get("type") == "agent_message":
            j["result"] = item.get("text") or ""
        elif e.get("type") == "item.completed" and item.get("type") == "command_execution":
            j["num_turns"] += 1
        elif e.get("type") in ("error", "turn.failed"):
            j["is_error"] = True
            j["result"] = (e.get("error") or {}).get("message") or e.get("message") or "codex failed"
    return j if events else {}


def looks_like_auth_or_limit(msg):
    import re
    return bool(re.search(r"limit|quota|rate.?limit|credit|overload|401|403|429|529|auth|login|unauthor|billing|out of", msg or "", re.I))


def run_event(text, source, cfg, timeout=None):
    """Run one event (`text`, on stdin) in orchestrator_dir under the orchestrator lock, on the first login that
    takes it (headless.logins: claude, codex or `bws run` subprocesses). Returns a dict: ok, cost, turns,
    duration_s, result (excerpt), error, session_id. Appends the run to the run log, and a run that started to the
    status log; when either write fails it raises RuntimeError naming the run's outcome, after the run."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    workdir = Path(os.path.expanduser(cfg["orchestrator_dir"]))
    timeout = timeout or int(cfg["orchestrator_timeout"])
    info = {"ts": time.time(), "source": source, "ok": False, "cost": None, "turns": None,
            "duration_s": None, "result": "", "error": "", "session_id": ""}
    if not workdir.is_dir():
        info["error"] = "orchestrator directory missing: %s" % workdir
        _record(info, None)
        return info
    base = dict(os.environ, AGENT_ORCHESTRATOR="1")   # agent-mail: send/reply as the orchestrator pseudo-session
    lock = open(LOCK, "a+")
    t0 = time.time()
    try:
        while True:  # serialise: wait for a running event to finish (bounded)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() - t0 > timeout:
                    info["error"] = "another orchestrator run held the lock for %ds" % timeout
                    _record(info, None)
                    return info
                time.sleep(2)
        t1 = time.time()

        def attempt(name, cmd, run_env):
            try:
                j, err = headless.run(cmd, text, workdir, run_env, timeout,
                                      codex_result if name == "codex" else headless.result)
            except subprocess.TimeoutExpired:
                return None, "timed out after %ds" % timeout
            return j, err[:800]

        # The next login runs only when this one failed on usage or login, or returned nothing, within its
        # first turn: never after a timeout or a failure part-way, since that run may have acted already.
        j, err, failed = None, "no way to run claude or codex", []
        codex = codex_command(cfg, workdir) if registry.codex_bin() else None
        for name, cmd, run_env in headless.logins(cfg, command(cfg), base, codex):
            j, err = attempt(name, cmd, run_env)
            info["auth"] = name
            if not err or err.startswith("timed out") or (j.get("num_turns") or 0) > 1 or not (
                    looks_like_auth_or_limit(err) or not j.get("result")):
                break
            failed.append("%s: %s" % (name, err[:200]))
        if failed:
            info["failed"] = "; ".join(failed)
        info["duration_s"] = round(time.time() - t1, 1)
        j = j or {}
        info["cost"] = j.get("total_cost_usd")
        info["turns"] = j.get("num_turns")
        info["session_id"] = j.get("session_id", "")
        info["result"] = (j.get("result") or "")[:2000]
        if err:
            info["error"] = err
        else:
            info["ok"] = True
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass
        lock.close()
    _record(info, {"cli": "codex" if info.get("auth") == "codex" else "claude", "session_id": info["session_id"],
                   "name": cfg["orchestrator"], "cwd": str(workdir),
                   "branch": statuslog.git_facts(str(workdir))["branch"], "event": "orchestrator",
                   "text": _outcome(info)})
    return info


def _outcome(info):
    return "%s · %s · $%s · %s turns · %s" % (
        info["source"], "ok" if info["ok"] else "FAILED", info["cost"], info["turns"], info.get("auth"))


def _record(info, status):
    """Append the run to the run log and `status` (an event, or None) to the status log. The run is over by then,
    so a failed write raises RuntimeError naming its outcome: the caller still learns what the run did."""
    try:
        jsonfile.append_line(RUNS_LOG, info, jsonfile.PRIVATE)
        if status:
            statuslog.append(status)
    except OSError as e:
        raise RuntimeError("orchestrator run (%s%s) not recorded: %s: %s" % (
            _outcome(info), ": " + info["error"][:200] if info["error"] else "", type(e).__name__, e)) from e


def logged_runs():
    """Every run in the run log, oldest first; a damaged line is skipped and said on stderr (jsonfile.read_lines)."""
    return jsonfile.read_lines(RUNS_LOG)
