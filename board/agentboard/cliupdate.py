"""Keep the agents' CLIs current: `codex update` and `claude update` (agent-cli-update, run by agent-cli-update.timer).

Both CLIs also update themselves, but late, and until Codex installs a release it knows of, every interactive
Codex start opens on its "Update available" menu. Both install a new version beside the old one and switch a
symlink, so updating while agents run is safe. `codex update` reuses a release it already has, so a run with
nothing new costs one metadata request.

A CLI that is not installed (Codex is optional in the kit) is logged and skipped, not counted as a failure; a run
fails when an installed CLI's update fails, or when neither is installed.
"""
import os
import re
import subprocess

from . import sources

LOG = sources.BOARD_STATE / "cli-update.log"
CLIS = (("codex", ["update"]), ("claude", ["update"]))
VERSION = re.compile(r"\d+\.\d+\.\d+")


def version(exe, run):
    try:
        r = run([exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = VERSION.search(r.stdout or "")
    return m.group(0) if m else None


def update_one(name, args, run=subprocess.run, find=sources.find_bin):
    """(log line, ok) for one CLI; ok is None when it is not installed (skipped)."""
    exe = find(name)
    if not exe:
        return "%s: not installed, skipped" % name, None
    before = version(exe, run)
    try:
        r = run([exe] + args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600,
                env=dict(os.environ, CODEX_NON_INTERACTIVE="1"))   # Codex's installer: no questions
    except (OSError, subprocess.TimeoutExpired) as e:
        return "%s %s: update failed (%s)" % (name, before, e), False
    after = version(exe, run)
    if r.returncode != 0:
        said = ((r.stderr or "").strip() or (r.stdout or "").strip()).splitlines()
        return "%s %s: update failed (exit %d): %s" % (name, before, r.returncode, said[-1][:200] if said else ""), False
    if after is None:                    # exited 0, but what is installed now doesn't run: not a success
        return "%s %s: update failed: `%s --version` gives no version afterwards" % (name, before, name), False
    if before != after:
        return "%s %s -> %s" % (name, before, after), True
    return "%s %s: up to date" % (name, after), True


def run_all(run=subprocess.run, find=sources.find_bin, log=None):
    """Update every installed CLI, append a line each to the log; True when every installed one succeeded and at
    least one is installed."""
    log = log or LOG
    results = []
    for name, args in CLIS:
        line, good = update_one(name, args, run, find)
        results.append(good)
        print(sources.append_log(log, line), flush=True)
    tried = [r for r in results if r is not None]
    return bool(tried) and all(tried)
