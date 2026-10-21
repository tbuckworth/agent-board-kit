"""Run the orchestrator: one fresh headless Claude run per event, no long-lived session.

Each event is `claude -p` in the orchestrator directory with a fresh context. The brief in
that directory's CLAUDE.md/AGENTS.md and the memory in STATE.md are all it knows, plus the
event text (which carries the whole email thread when there is one). Runs are serialised with
a lock so two events never edit STATE.md at once. Every run's cost, turns and outcome are
appended to a run log.
"""
import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import registry, statuslog
from .config import DEFAULTS, STATE_DIR

RUNS_LOG = STATE_DIR / "orchestrator-runs.jsonl"
LOCK = STATE_DIR / "orchestrator.lock"
EMPTY_MCP = STATE_DIR / "no-mcp.json"


def _claude():
    cl = registry.claude_bin()
    if not cl:
        raise RuntimeError("claude binary not found")
    return cl


def command(cfg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not EMPTY_MCP.exists():
        EMPTY_MCP.write_text('{"mcpServers": {}}\n')
    cmd = [_claude(), "-p", "--output-format", "json", "--dangerously-skip-permissions",
           "--model", cfg.get("orchestrator_model") or DEFAULTS["orchestrator_model"],
           "--mcp-config", str(EMPTY_MCP), "--strict-mcp-config"]
    eff = cfg.get("orchestrator_effort")
    if eff:
        cmd += ["--effort", eff]
    return cmd


def fallback_available(cfg):
    """The optional API-key fallback: off unless config orchestrator_fallback_secret names a secret, and it
    needs the bws CLI, its access-token file and its project-id file on this machine."""
    if not cfg.get("orchestrator_fallback_secret"):
        return False
    tok = Path(os.path.expanduser(cfg.get("bws_token_file", "~/.config/bws/token")))
    proj = Path(os.path.expanduser(cfg.get("bws_project_file", "~/.config/bws/project-id")))
    return bool(registry.find_bin("bws", ["~/.local/bin/bws", "/usr/local/bin/bws"])) and tok.is_file() and proj.is_file()


def fallback_command(cfg, claude_cmd):
    """Wrap the claude command so it runs with ANTHROPIC_API_KEY taken from Bitwarden Secrets
    Manager (https://bitwarden.com/help/secrets-manager-cli/). `bws run` injects every project secret into the child
    environment under its stored name and runs its arguments, joined by spaces, as one `sh -c` script. So the script is
    passed whole, with the claude command shell-quoted, and maps the stored name onto ANTHROPIC_API_KEY *inside that
    child*: the value never passes through this process, its logs, or a file. (Passed unquoted, the script would be
    only the assignment and claude would never run.)"""
    bws = registry.find_bin("bws", ["~/.local/bin/bws", "/usr/local/bin/bws"])
    proj = Path(os.path.expanduser(cfg.get("bws_project_file", "~/.config/bws/project-id"))).read_text().strip()
    secret = cfg["orchestrator_fallback_secret"]
    if not re.fullmatch(r"[A-Za-z_]\w*", secret):
        raise ValueError("orchestrator_fallback_secret must be a variable name")
    return [bws, "run", "--project-id", proj, "--", 'ANTHROPIC_API_KEY="$%s" exec %s' % (secret, shlex.join(claude_cmd))]


def _fallback_env(cfg, env):
    env = dict(env)
    tok = Path(os.path.expanduser(cfg.get("bws_token_file", "~/.config/bws/token")))
    env["BWS_ACCESS_TOKEN"] = tok.read_text().strip()   # read in memory, passed to bws only
    return env


def looks_like_auth_or_limit(msg):
    import re
    return bool(re.search(r"limit|quota|rate.?limit|credit|overload|401|403|429|529|auth|login|unauthor|billing|out of", msg or "", re.I))


def run_event(text, source, cfg, timeout=None):
    """Returns a dict: ok, cost, turns, duration_s, result (excerpt), error, session_id."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    workdir = Path(os.path.expanduser(cfg.get("orchestrator_dir") or DEFAULTS["orchestrator_dir"]))
    timeout = timeout or int(cfg.get("orchestrator_timeout", 900))
    info = {"ts": time.time(), "source": source, "ok": False, "cost": None, "turns": None,
            "duration_s": None, "result": "", "error": "", "session_id": ""}
    if not workdir.is_dir():
        info["error"] = "orchestrator directory missing: %s" % workdir
        _log(info)
        return info
    env = dict(os.environ)
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN"):
        env.pop(k, None)
    env["AGENT_ORCHESTRATOR"] = "1"   # agent-mail: send/reply as the orchestrator pseudo-session
    info["auth"] = "claude.ai"
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
                    _log(info)
                    return info
                time.sleep(2)
        t1 = time.time()

        def attempt(cmd, run_env):
            try:
                r = subprocess.run(cmd, cwd=str(workdir), input=text, capture_output=True,
                                   text=True, timeout=timeout, env=run_env)
            except subprocess.TimeoutExpired:
                return None, "timed out after %ds" % timeout
            out = r.stdout.strip()
            try:
                j = json.loads(out) if out else {}
            except json.JSONDecodeError:
                j = {}
            if isinstance(j, list):
                j = next((x for x in j if isinstance(x, dict) and x.get("type") == "result"), {})
            err = ""
            if r.returncode != 0 or j.get("is_error"):
                err = (j.get("result") or r.stderr or out or "exit %d" % r.returncode)[:800]
            return j, err

        j, err = attempt(command(cfg), env)
        if err and not err.startswith("timed out") and fallback_available(cfg) and (
                looks_like_auth_or_limit(err) or not (j or {}).get("result")):
            info["first_error"] = err[:300]
            j2, err2 = attempt(fallback_command(cfg, command(cfg)), _fallback_env(cfg, env))
            if j2 is not None:
                j, err, info["auth"] = j2, err2, "api-key-fallback"
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
    _log(info)
    try:
        statuslog.append({"cli": "claude", "session_id": info["session_id"], "name": cfg.get("orchestrator", "orchestrator"),
                          "cwd": str(workdir), "branch": statuslog.git_facts(str(workdir))["branch"],
                          "event": "orchestrator", "text": "%s · %s · $%s · %s turns · %s" % (
                              source, "ok" if info["ok"] else "FAILED", info["cost"], info["turns"], info.get("auth"))})
    except Exception:
        pass
    return info


def _log(info):
    try:
        fd = os.open(RUNS_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(info, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent_runs(limit=20):
    if not RUNS_LOG.exists():
        return []
    rows = []
    for line in RUNS_LOG.read_text(errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows[-limit:]
