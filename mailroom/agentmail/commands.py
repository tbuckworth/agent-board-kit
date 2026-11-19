"""Running a command and reading a command line, for several tools: a run that reports a command that could not
start or timed out as a failed one instead of raising, the error for a command that had to succeed, the one tmux
helper, whether a branch or a tmux session exists, which folder is a repo's main checkout, and an option's value
in an argv."""
import os
import subprocess
from pathlib import Path

TMUX_SECONDS = 15   # a tmux command answers at once unless its server is stuck


class CommandError(Exception):
    """A command that had to succeed failed or could not start; the message names it and gives its error."""


def run(cmd, timeout, cwd=None):
    """Run cmd (its items made str) with stdin closed and text output captured. A command that cannot start or
    times out comes back as a failed CompletedProcess (returncode 1, the error as stderr), so the caller checks
    returncode either way."""
    try:
        return subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def checked(r, what, error):
    """r, a command's CompletedProcess, if the command exited 0; otherwise raise error (an exception class: the
    caller's own) saying `what` failed, with its exit code and the end of its error output."""
    if r.returncode != 0:
        raise error("%s failed (exit %s): %s" % (what, r.returncode, (r.stderr or r.stdout).strip()[-600:]))
    return r


def tmux(*args, check):
    """Run one tmux command (run, TMUX_SECONDS at most). check: raise CommandError if it fails; otherwise return
    the result whatever it is, for a caller that reads it (a pane or session that is gone gives a failed one)."""
    r = run(["tmux"] + list(args), TMUX_SECONDS)
    return checked(r, "tmux " + " ".join(map(str, args[:3])), CommandError) if check else r


def tmux_has(name):
    """Whether a tmux session has exactly this name (tmux's "=NAME": not a prefix match)."""
    return bool(name) and tmux("has-session", "-t", "=" + name, check=False).returncode == 0


def branch_exists(repo, branch):
    """Whether the repo at repo has a local branch of this name."""
    return run(["git", "-C", repo, "show-ref", "--verify", "--quiet", "refs/heads/" + branch],
               timeout=30).returncode == 0


def common_dir(path):
    """The git common dir of the repo at path (the main checkout's .git, which its linked worktrees share), as an
    absolute path with symlinks resolved; None if path is not in a git repo or git cannot run there."""
    r = run(["git", "-C", path, "rev-parse", "--git-common-dir"], timeout=15)
    out = r.stdout.strip()
    return os.path.realpath(os.path.join(str(path), out)) if r.returncode == 0 and out else None


def repo_root(path):
    """The main checkout of the repo at path, the same for each of its linked worktrees: the folder holding its
    common dir. Outside a git repo, path itself, resolved."""
    common = common_dir(path)
    return str(Path(common).parent) if common else str(Path(path).resolve())


def flag_value(argv, *names):
    """The value of the first of these options in argv, given as `NAME VALUE` or `NAME=VALUE`, or None."""
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None
