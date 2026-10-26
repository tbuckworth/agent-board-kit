"""Run the orchestrator: one fresh headless Claude run per event, no long-lived session.

Each event is `claude -p` in the orchestrator directory with a fresh context. The brief in
that directory's CLAUDE.md/AGENTS.md and the memory in STATE.md are all it knows, plus the
event text (which carries the whole email thread when there is one). Runs are serialised with
a lock so two events never edit STATE.md at once. Every run's cost, turns and outcome are
appended to a run log.

A login that is out of usage or logged out passes the event to the next one (logins()): each
Claude login in usage.order (say your own, then a work seat), Codex where that order puts it,
then an API key from Bitwarden (orchestrator_fallback_secret), when one is set.
"""
import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import registry, statuslog, usage
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


def codex_command(cfg, workdir):
    """`codex exec` on the event from stdin, as JSON lines. --ignore-user-config leaves out ~/.codex/config.toml's
    MCP servers (his Gmail among them), plugins and hooks, as the claude run has no MCP; the global and the
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
    an "error" event or turn.failed is the run failing."""
    j = {"result": "", "session_id": "", "num_turns": 0, "is_error": False}
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
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
    return j


def logins(cfg, env, workdir):
    """(name, command, env) for each way to run an event, in order: each Claude login in usage.order that is
    set up, Codex where the order puts it, then the API key (fallback_command)."""
    s, out = usage.settings(cfg), []
    for t in usage.ranked(s):
        if t == usage.CODEX:
            if registry.codex_bin():
                out.append(("codex", codex_command(cfg, workdir), env))
            continue
        acct = usage.account(s, t[len("claude:"):])
        if usage.account_ready(acct)[0]:
            out.append((t[len("claude:"):], command(cfg), usage.claude_env(acct, env)))
    if fallback_available(cfg):
        out.append(("api-key", fallback_command(cfg, command(cfg)), _fallback_env(cfg, usage.claude_env({}, env))))
    return out


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

        def attempt(name, cmd, run_env):
            try:
                r = subprocess.run(cmd, cwd=str(workdir), input=text, capture_output=True,
                                   text=True, timeout=timeout, env=run_env)
            except subprocess.TimeoutExpired:
                return None, "timed out after %ds" % timeout
            out = r.stdout.strip()
            if name == "codex":
                j = codex_result(out)
            else:
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

        # The next login runs only when this one failed on usage or login, or returned nothing, within its
        # first turn: never after a timeout or a failure part-way, since that run may have acted already.
        j, err, failed = None, "no way to run claude or codex", []
        for name, cmd, run_env in logins(cfg, env, workdir):
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
    _log(info)
    try:
        statuslog.append({"cli": "codex" if info.get("auth") == "codex" else "claude", "session_id": info["session_id"], "name": cfg.get("orchestrator", "orchestrator"),
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
