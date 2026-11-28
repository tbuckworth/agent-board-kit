"""Running a command and reading a command line, for several tools: a run that reports a command that could not
start or timed out as a failed one instead of raising, the error for a command that had to succeed, the one tmux
helper and the lock everything that presses keys in a tmux pane holds, whether a branch or a tmux session exists,
which folder is a repo's main checkout, and an option's value in an argv."""
import fcntl
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

TMUX_SECONDS = 15   # a tmux command answers at once unless its server is stuck


class CommandError(Exception):
    """A command that had to succeed failed or could not start; the message names it and gives its error."""


def run(cmd, timeout, cwd=None, env=None, input=None):
    """Run cmd (its items made str) with text output captured, in env (None: this process's), its stdin the text
    input, or closed when None. A command that cannot start or times out comes back as a failed CompletedProcess
    (returncode 1, the error as stderr), so the caller checks returncode either way."""
    try:
        return subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env=env, **({"input": input} if input is not None else {"stdin": subprocess.DEVNULL}))
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def checked(r, what, error):
    """r, a command's CompletedProcess, if the command exited 0; otherwise raise error (an exception class: the
    caller's own) saying `what` failed, with its exit code and the end of its error output."""
    if r.returncode != 0:
        raise error("%s failed (exit %s): %s" % (what, r.returncode, (r.stderr or r.stdout).strip()[-600:]))
    return r


def tmux_socket():
    """The tmux server's socket: config tmux_socket, or tmux's own default for this user ($TMUX_TMPDIR, else /tmp,
    then tmux-UID/default). Never $TMUX, which names the server of whichever pane started this process. Raises
    ValueError when config.json cannot be read, or its tmux_socket is neither null nor a path."""
    sock = config.load()["tmux_socket"]
    if sock is not None and not (isinstance(sock, str) and sock):
        raise ValueError("config tmux_socket must be null or a socket's path, not %r" % (sock,))
    return sock or os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", "tmux-%d" % os.getuid(), "default")


def tmux(*args, check, input=None):
    """Run one tmux command on tmux_socket's server (-S), without $TMUX in its environment (run, TMUX_SECONDS at
    most), its stdin the text input (None: closed). check: raise CommandError if it fails; otherwise return the result
    whatever it is, for a caller that reads it (a pane or session that is gone gives a failed one). A config that
    does not say which server is a failed command too, which says why, and is never run on another server."""
    try:
        sock = tmux_socket()
    except ValueError as e:
        r = subprocess.CompletedProcess(["tmux"] + list(args), 1, "", "tmux not run: %s" % e)
    else:
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        r = run(["tmux", "-S", sock] + list(args), TMUX_SECONDS, env=env, input=input)
    return checked(r, "tmux " + " ".join(map(str, args[:3])), CommandError) if check else r


class LockBusy(CommandError):
    """file_lock found the lock held by another thread or process: at once with wait False, or for as long as it was
    to wait (LOCK_WAIT_SECONDS, or the seconds given)."""


# How long file_lock waits for another holder: far longer than any holder holds a lock (an agent-switch handoff, the
# longest, holds the keys lock through its new agent's start, under 3 minutes), so a wait this long means a holder
# that waits for itself, such as one that runs a command which takes the same lock; that fails loudly instead of
# hanging.
LOCK_WAIT_SECONDS = 600
LOCK_STEP = 0.1        # how often a waiting file_lock tries again


_HELD = threading.local()   # the paths file_lock holds for this thread, each with its depth


@contextmanager
def file_lock(path, wait):
    """Hold an exclusive lock on the file at path (made if missing, its folder too). A thread that holds it already
    holds it again at once (it is released when the outermost hold ends); when another thread or process holds it,
    wait for it up to LOCK_WAIT_SECONDS (wait True), that many seconds (wait a number), or not at all (wait False),
    then raise LockBusy. The wait tries again every LOCK_STEP, so it is not fair: a later waiter may take the lock
    first. flock(2) promises blocked waiters no order either, and a blocking wait with a time limit would need a signal
    (the main thread only) or a thread left blocked in flock, so the polling stays."""
    held = _HELD.__dict__.setdefault("paths", {})
    key = os.path.abspath(path)
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        seconds = LOCK_WAIT_SECONDS if wait is True else wait or 0
        deadline = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy("%s is held by another program%s%s" % (
                        path, " after %g s of waiting for it" % seconds if seconds else "",
                        " (does its holder wait for itself?)" if wait is True else "")) from None
                time.sleep(LOCK_STEP)
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
            fcntl.flock(fh, fcntl.LOCK_UN)


def keys_lock(wait):
    """file_lock(config.KEYS_LOCK, wait) (wait: True, False or seconds): whatever types or presses keys in an agent's
    pane holds it (launch.py's lines, a relaunch from the kill to its line's Enter, fast mode's effort keys, fast
    mode's trial of a new version, agent-send's message or note typed at a Codex agent's prompt), so their keys never
    mix. The board's Resume and post-reboot restore take it too,
    through this function (agent-board's agentboard/relaunch.py)."""
    return file_lock(config.KEYS_LOCK, wait)


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


def without_flags(argv, *names):
    """argv without these options, each given as `NAME VALUE` or `NAME=VALUE` (flag_value's two forms)."""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a in names:
            i += 2
            continue
        if not any(a.startswith(n + "=") for n in names):
            out.append(a)
        i += 1
    return out
