"""Keep the agents' CLIs current: `codex update` and `claude update` (agent-cli-update, run by agent-cli-update.timer),
then fast mode's trial of each (`agent-mail fast-trial CLI --if-untested`).

Both CLIs also update themselves, but late, and until Codex installs a release it knows of, every interactive
Codex start opens on its "Update available" menu. Both install a new version beside the old one and switch a
symlink, so updating while agents run is safe. `codex update` reuses a release it already has, so a run with
nothing new costs one metadata request.

A CLI that is not installed (Codex is optional in the kit) is logged and skipped, not counted as a failure, and gets
no trial; a run fails when an installed CLI's update or trial fails, or when neither is installed.

The trials run here because a new version is what needs one, and the mailroom's fast mode presses no keys on a
version until one has passed; what a trial does is in README.md, "CLI updates", and the mailroom's README, "Fast
mode". They run after both updates, and even after a failed one, since the version that runs may be untested.
"""
import os
import re
import subprocess

from . import sources

LOG = sources.BOARD_STATE / "cli-update.log"
CLIS = (("codex", ["update"]), ("claude", ["update"]))
VERSION = re.compile(r"\d+\.\d+\.\d+")
# A trial takes some 15 s, and bounds each of its own waits, to a few minutes in all. Past TRIAL_SECONDS it gets a
# SIGTERM (`timeout`), on which it ends its tmux session and the CLI in it; one still running TRIAL_KILL_SECONDS later
# is killed. systemd/agent-cli-update.service's TimeoutStartSec is sized from these, UPDATE_SECONDS and VERSION_SECONDS:
# change it with them.
TRIAL_SECONDS = 600
TRIAL_KILL_SECONDS = 30
UPDATE_SECONDS = 600    # one `codex update` or `claude update`
VERSION_SECONDS = 30    # one `--version`, before and after each update


def version(exe, run):
    try:
        r = run([exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=VERSION_SECONDS)
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
        r = run([exe] + args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=UPDATE_SECONDS,
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


def trial_one(name, run=subprocess.run, find=sources.find_bin):
    """(log line, ok) for fast mode's trial of CLI name's installed version (`agent-mail fast-trial NAME
    --if-untested`): agent-mail's own line of outcome, or why there is none."""
    exe = find("agent-mail")
    if not exe:
        return "fast-trial %s: agent-mail is not installed" % name, False
    cmd = ["timeout", "--kill-after=%d" % TRIAL_KILL_SECONDS, str(TRIAL_SECONDS), exe, "fast-trial", name,
           "--if-untested"]
    try:
        r = run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=TRIAL_SECONDS + 2 * TRIAL_KILL_SECONDS)   # past `timeout`'s own kill: it is stuck
    except (OSError, subprocess.TimeoutExpired) as e:
        return "fast-trial %s: failed (%s)" % (name, e), False
    if r.returncode in (124, 137):       # `timeout` stopped it: SIGTERM, or SIGKILL after it
        return "fast-trial %s: stopped after %d s" % (name, TRIAL_SECONDS), False
    said = (r.stdout or "").strip().splitlines()
    if said:
        return said[-1][:600], r.returncode == 0
    said = (r.stderr or "").strip().splitlines()
    return "fast-trial %s: failed (exit %d): %s" % (name, r.returncode, said[-1][:300] if said else "no output"), False


def run_all(run=subprocess.run, find=sources.find_bin, log=None):
    """Update every installed CLI, then try fast mode's keys on each installed one (trial_one), even one whose update
    failed, since the version it runs may be untested; append a line each to the log. True when every installed one's
    update and trial succeeded and at least one is installed."""
    log = log or LOG
    results = []
    for name, args in CLIS:
        line, good = update_one(name, args, run, find)
        results.append(good)
        print(sources.append_log(log, line), flush=True)
    installed = [name for (name, _), good in zip(CLIS, results) if good is not None]
    ok = bool(installed) and all(r for r in results if r is not None)
    for name in installed:
        line, good = trial_one(name, run, find)
        ok = ok and good
        print(sources.append_log(log, line), flush=True)
    return ok
