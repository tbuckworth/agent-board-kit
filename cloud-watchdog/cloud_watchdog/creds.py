"""Run a child with exactly one account's credentials, and nothing else.

The watchdog process itself never reads a provider key. Each collector or stop action runs through
childexec.py, which keeps an allowlist of ordinary variables (the names the parent passed), adds the
account's keys under the names the provider's tools expect, and execs the real command. Where the
keys come from is the account's `secrets` backend (config.resolve_account fills in the paths):

  env-file  childexec reads <env_dir>/<provider>-<account>.env itself (KEY=value lines, mode 600).
            With an `env` table, only the mapped names are passed; without one, every key in the file.
  bws       `bws run -- childexec`: Bitwarden Secrets Manager injects the project's secrets, and
            childexec keeps only the ones the `env` table maps (default: the provider's own names),
            dropping the vault token and every other secret.
  environ   the watchdog's own environment, mapped the same way as bws.
  none      no keys at all (Slurm over SSH; a Modal account that uses a ~/.modal.toml profile).

Why the command travels base64-encoded in the environment: `bws run` re-joins the words after `--`
through /bin/sh, which destroys quoting.
"""
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

from . import config, providers, util

BASE_NAMES = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "XDG_RUNTIME_DIR",
              "SSH_AUTH_SOCK", "PYTHONPATH", "TERM")
CHILDEXEC = config.PKG_DIR / "childexec.py"
VAULT_BUSY = re.compile(r"Received error message from server: \[(?:429|503)\b")
BACKENDS = ("env-file", "bws", "environ", "none")


def base_env(extra=None):
    env = {k: os.environ[k] for k in BASE_NAMES if k in os.environ}
    env["PATH"] = ":".join([str(config.HOME / ".local" / "bin"), env.get("PATH", "/usr/local/bin:/usr/bin:/bin")])
    env["PYTHONPATH"] = str(config.REPO_DIR)
    env.setdefault("HOME", str(config.HOME))
    env.update(extra or {})
    return env


def required_env(acct):
    """The variable names this account's provider needs (none for a Modal account using a profile)."""
    if acct.get("provider") == "modal" and acct.get("profile"):
        return []
    return list(providers.ENV.get(acct.get("provider"), ()))


def _file_problem(path):
    try:
        mode = path.stat().st_mode
    except OSError as e:
        return str(e)
    if mode & 0o077:
        return "readable by others: run `chmod 600 %s`" % path
    return None


def plan(acct):
    """(backend, remap, need, problem): what run_as will do for this account. No secret is read."""
    backend = acct.get("secrets") or "env-file"
    need = required_env(acct)
    remap = dict(acct.get("env") or {})
    if backend not in BACKENDS:
        return backend, remap, need, "secrets = %r is not one of %s" % (backend, ", ".join(BACKENDS))
    if backend == "env-file":
        path = Path(acct.get("env_file") or "")
        if not acct.get("env_file") or not path.exists():
            if not need and not remap:
                return "none", remap, need, None
            return backend, remap, need, "no credentials: create %s (chmod 600) with %s, or set this account's " \
                "`secrets`" % (path, ", ".join("%s=..." % n for n in (list(remap.values()) or need)))
        problem = _file_problem(path)
        return backend, remap, need, ("%s: %s" % (path, problem)) if problem else None
    if backend in ("bws", "environ"):
        remap = remap or {n: n for n in need}
        if not remap:
            return "none", remap, need, None
    if backend == "environ":
        missing = [s for s in remap.values() if s not in os.environ]
        return backend, remap, need, ("not in the environment: %s" % ", ".join(missing)) if missing else None
    if backend == "bws":
        tok, pid = config.expand(acct.get("bws_token_file") or ""), config.expand(acct.get("bws_project_file") or "")
        if not (acct.get("bws_token_file") and tok.exists() and pid.exists()):
            return backend, remap, need, "no Bitwarden token/project id at %s / %s" % (tok, pid)
    return backend, remap, need, None


def describe(acct):
    """One line for `cloud-watch accounts`: where the keys come from, and whether that looks usable."""
    backend, remap, need, problem = plan(acct)
    if problem:
        return "PROBLEM: " + problem
    if backend == "none":
        return "no keys"
    if backend == "env-file":
        return "%s (%s)" % (acct["env_file"], ", ".join(sorted(remap)) or "every key in the file")
    return "%s: %s" % (backend, ", ".join("%s<-%s" % (w, s) if w != s else w for w, s in sorted(remap.items())))


def _payload(argv, env, remap, need, env_file=None):
    p = {"argv": argv, "keep": sorted(env), "remap": remap, "need": need}
    if env_file:
        p["env_file"] = str(env_file)
    return base64.b64encode(json.dumps(p).encode()).decode()


def run_as(acct, argv, timeout=300, extra_env=None):
    """Run argv with this account's credentials (acct as resolved by config.load). Returns (code, stdout, stderr)."""
    env = base_env(extra_env)
    backend, remap, need, problem = plan(acct)
    if problem:
        return 1, "", problem
    if backend == "none":
        return util.run(argv, env=env, timeout=timeout)
    child = [sys.executable, str(CHILDEXEC)]
    if backend == "env-file":
        return util.run(child, env=dict(env, CW_CHILD=_payload(argv, env, remap, need, acct["env_file"])),
                        timeout=timeout)
    if backend == "environ":
        child_env = dict(env, CW_CHILD=_payload(argv, env, remap, need))
        child_env.update({s: os.environ[s] for s in remap.values() if s in os.environ})
        return util.run(child, env=child_env, timeout=timeout)
    # bws
    bws_env = dict(env, CW_CHILD=_payload(argv, env, remap, need))
    bws_env["BWS_ACCESS_TOKEN"] = config.expand(acct["bws_token_file"]).read_text().strip()
    cmd = ["bws", "run", "--project-id", config.expand(acct["bws_project_file"]).read_text().strip(), "--",
           " ".join(child)]
    for attempt in range(3):
        code, out, err = util.run(cmd, env=bws_env, timeout=timeout)
        # Bitwarden's API occasionally answers 503/429 when several children start at once. Retry only
        # that: bws's own server-error line, and only when the child never ran (no output). A provider's
        # 429/503 inside the child is reported by collect.py as JSON on stdout and is never retried here.
        if code == 0 or out.strip() or not VAULT_BUSY.search(err):
            return code, out, err
        time.sleep(3 * (attempt + 1))
    return code, out, err
