"""Headless `claude -p` runs, shared by the orchestrator, the pre-send reviewer and the reMarkable preflight.

claude_command() builds the command: JSON output and no MCP server, on the caller's model and effort with its own
options. logins() lists the ways to run it, in order: each Claude login in usage.order that is set up, Codex where
the caller has a Codex command and the order puts it, then an API key from Bitwarden Secrets Manager. Every child
runs with accounts.claude_env, so none inherits the session or the login this process runs in. run() runs one of
them and reads the CLI's JSON output (result()).
"""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from . import accounts, registry, usage
from .config import STATE_DIR

EMPTY_MCP = STATE_DIR / "no-mcp.json"
BWS_PATHS = ("~/.local/bin/bws", "/usr/local/bin/bws")   # where bws may be when it is not on PATH
API_KEY = "api-key"   # what logins() calls the API-key way to run, recorded as the login a run used


def claude_command(model, effort, extra):
    """`claude -p --output-format json` with no MCP server (--strict-mcp-config on EMPTY_MCP, written on first use),
    `--model model` and `--effort effort` unless None (the CLI's default), then the caller's `extra` options.
    Raises RuntimeError when there is no claude binary."""
    cl = registry.claude_bin()
    if not cl:
        raise RuntimeError("claude binary not found")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not EMPTY_MCP.exists():
        EMPTY_MCP.write_text('{"mcpServers": {}}\n')
    cmd = [cl, "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    return cmd + ["--mcp-config", str(EMPTY_MCP), "--strict-mcp-config"] + list(extra)


def result(out):
    """The result object in `claude -p --output-format json` output: the object itself, or the item whose type is
    "result" when the CLI prints a list. {} when `out` holds no JSON or no result."""
    try:
        j = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        return {}
    if isinstance(j, list):
        j = next((x for x in j if isinstance(x, dict) and x.get("type") == "result"), {})
    return j if isinstance(j, dict) else {}


def run(cmd, prompt, cwd, env, timeout, parse):
    """Run `cmd` in `cwd` with `prompt` on stdin. Returns (output, error): what `parse` (result, or a parser for
    another CLI's output with the same fields) reads from stdout, and "" or what went wrong: the error result,
    stderr or exit status of a failed run, or the output of a run that exited 0 without a result. Raises
    subprocess.TimeoutExpired."""
    r = subprocess.run(cmd, cwd=str(cwd), input=prompt, capture_output=True, text=True, timeout=timeout, env=env)
    out = r.stdout.strip()
    j = parse(out)
    if r.returncode != 0 or j.get("is_error"):
        return j, (j.get("result") or r.stderr or out or "exit %d" % r.returncode).strip()
    if not j:   # nothing to read: the CLI never ran the prompt, or its output is cut or garbled
        return j, "exit 0 without a result: %s" % ((r.stderr or out).strip() or "no output")
    return j, ""


def logins(cfg, cmd, base, codex):
    """(name, command, env) for each way to run `cmd`, a claude command, in order: each Claude login in usage.order
    that is set up (never a manual one: usage.ranked), on its own config dir, with the options that login takes
    (accounts.login_flags); `codex` (a command, or None to leave Codex out) where the order puts Codex; then the API
    key (named API_KEY) when api_key_available. Each env is `base` through accounts.claude_env. The API-key entry
    reads the Bitwarden token and project-id files."""
    s, out = usage.settings(cfg), []
    for t in usage.ranked(s):
        cli, name = usage.parse_target(t, s)
        acct = accounts.account(s, name) if cli != usage.CODEX else {}
        if cli == usage.CODEX:
            if codex:
                out.append(("codex", codex, accounts.claude_env({}, base, s)))
        elif accounts.account_ready(acct)[0]:
            d = accounts.expand(acct.get("config_dir"))
            out.append((name, cmd[:1] + accounts.login_flags(s, d, cmd[1:]), accounts.claude_env(acct, base, s)))
    if api_key_available(cfg):
        env = accounts.claude_env({}, base, s)
        env["BWS_ACCESS_TOKEN"] = _path(cfg["bws_token_file"]).read_text().strip()   # in memory, passed to bws only
        out.append((API_KEY, api_key_command(cfg, cmd), env))
    return out


def _path(p):
    return Path(os.path.expanduser(p))


def api_key_available(cfg):
    """The API key needs a secret name (orchestrator_fallback_secret), bws, its token file and its project-id file
    (desktop only)."""
    return bool(cfg["orchestrator_fallback_secret"] and registry.find_bin("bws", BWS_PATHS)
                and _path(cfg["bws_token_file"]).is_file() and _path(cfg["bws_project_file"]).is_file())


def api_key_command(cfg, claude_cmd):
    """Wrap the claude command so it runs with ANTHROPIC_API_KEY taken from Bitwarden Secrets Manager. Reads the
    project-id file. `bws run` injects every project secret into the child environment under its stored name and
    runs its arguments, joined by spaces, as one `sh -c` script. So the script is passed whole, with the claude
    command shell-quoted, and maps the stored name onto ANTHROPIC_API_KEY *inside that child*: the value never
    passes through this process, its logs, or a file."""
    proj = _path(cfg["bws_project_file"]).read_text().strip()
    secret = cfg["orchestrator_fallback_secret"]
    if not re.fullmatch(r"[A-Za-z_]\w*", secret):
        raise ValueError("orchestrator_fallback_secret must be a variable name")
    return [registry.find_bin("bws", BWS_PATHS), "run", "--project-id", proj, "--",
            'ANTHROPIC_API_KEY="$%s" exec %s' % (secret, shlex.join(claude_cmd))]
