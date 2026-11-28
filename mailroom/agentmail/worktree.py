"""A git worktree for a new agent, for agent-spawn and the reMarkable route: fetch origin (retrying, never prompting
for credentials), pick the base for the agent's branch, check the branch out in a new worktree, and remove one
again."""
import os
import signal
import subprocess
import time

from . import commands

# Nothing here has a terminal to answer a credential prompt (the mailroom's daemon, an agent's shell): git must fail
# instead of waiting out the timeout.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
LOCAL_SECONDS = 120   # a git command that does not reach the network


def git(repo, *args, timeout, check):
    """Run git in repo, without a terminal or stdin, and return its CompletedProcess. On timeout its process group
    gets SIGTERM before SIGKILL: git then removes its lock files (a SIGKILLed fetch can leave
    refs/remotes/origin/main.lock behind and fail every later fetch), and the remote helper it started goes with it;
    the result is a failed one (returncode 1, "timed out after Ns"). check: raise commands.CommandError instead of
    returning a failure."""
    p = subprocess.Popen(["git", "-C", str(repo)] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=dict(os.environ, **GIT_ENV), start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        r = subprocess.CompletedProcess(p.args, p.returncode, out, err)
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
                p.communicate(timeout=5)
                break
            except ProcessLookupError:
                break
            except subprocess.TimeoutExpired:
                continue
        r = subprocess.CompletedProcess(p.args, 1, "", "timed out after %ds" % timeout)
    return commands.checked(r, "git " + " ".join(args[:2]), commands.CommandError) if check else r


def fetch_base(repo, bases, tries, timeout, pause):
    """Fetch origin, up to `tries` times `pause` seconds apart, each try limited to `timeout` seconds, then pick the
    base for a new branch: the first of `bases` that names a commit, else the last. Returns (base, warning); warning
    is "" if a fetch worked, and otherwise says the base is as the last good fetch left it and may be behind."""
    why = None
    for k in range(tries):
        if k:
            time.sleep(pause)
        r = git(repo, "fetch", "--quiet", "origin", timeout=timeout, check=False)
        if r.returncode == 0:
            why = None
            break
        why = " ".join((r.stderr or r.stdout).split())[:200] or "exit %d" % r.returncode
    for base in bases:
        r = git(repo, "rev-parse", "--verify", "--quiet", "--short", base + "^{commit}", timeout=LOCAL_SECONDS,
                check=False)
        if r.returncode == 0:
            break
    if why is None:
        return base, ""
    return base, "git fetch origin failed %d time%s (last: %s), so the branch is off %s at %s, which may be behind" % (
        tries, "s" if tries > 1 else "", why, base, r.stdout.strip() or "?")


def add(repo, path, branch, base):
    """Check out branch in a new worktree at path: the branch as it is if it exists, otherwise made from base.
    Worktrees whose folder was deleted by hand are forgotten first (git worktree prune). Raises
    commands.CommandError if git fails; a branch made here is then removed again, with whatever git left at path."""
    git(repo, "worktree", "prune", timeout=LOCAL_SECONDS, check=False)
    if commands.branch_exists(repo, branch):
        git(repo, "worktree", "add", "--quiet", str(path), branch, timeout=LOCAL_SECONDS, check=True)
        return
    try:
        git(repo, "worktree", "add", "--quiet", "-b", branch, str(path), base, timeout=LOCAL_SECONDS, check=True)
    except commands.CommandError:
        drop(repo, path, branch)   # neither the branch nor path was there before, so anything there is this try's
        raise


def drop(repo, path, branch):
    """Remove the worktree at path and its branch. Returns what could not be removed (empty if all went)."""
    left = []
    if path.exists() or str(path) in git(repo, "worktree", "list", "--porcelain", timeout=LOCAL_SECONDS,
                                         check=False).stdout:
        r = git(repo, "worktree", "remove", "--force", str(path), timeout=LOCAL_SECONDS, check=False)
        if r.returncode != 0 and path.exists():
            left.append("worktree %s (%s)" % (path, (r.stderr or r.stdout).strip()[:120]))
    if commands.branch_exists(repo, branch):
        r = git(repo, "branch", "-D", branch, timeout=LOCAL_SECONDS, check=False)
        if r.returncode != 0:
            left.append("branch %s (%s)" % (branch, (r.stderr or r.stdout).strip()[:120]))
    return left
