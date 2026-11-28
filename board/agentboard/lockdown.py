"""Keeping the owner's password and the phone page's memory out of other processes' reach. `agent-board
keyring-unlock`, which reads the password, is readied (ready): hardened, and checked to be the system's Python in
isolated mode. `agent-board mail-web`, the phone page's server, which holds session cookies, passkey approvals and mail
in its memory, and which the password passes through when the keyring unlock is on, hardens itself as it starts
(mailweb.serve), and gives the password to keyring-unlock only once that has made itself undumpable (undumpable).
Standard library only, and nothing else from agentboard, so keyring-unlock can run it before anything else loads: the
password may be waiting on its stdin already.
"""
import ctypes
import os
import resource
import sys

PR_SET_DUMPABLE = 4                    # prctl(2)
SYSTEM_PYTHON = "/usr/bin/python3"     # the distribution's, which only root can change; keyring-unlock runs on nothing else
INTERPRETER = (SYSTEM_PYTHON, "-I")    # what keyring-unlock is run on (bin/agent-keyring-unlock; the Mac's command)
COMMAND = " ".join(INTERPRETER)


def harden():
    """Keep this process's memory out of core files and out of the owner's other processes' reach: no core file
    (RLIMIT_CORE 0), and not dumpable (prctl PR_SET_DUMPABLE 0), which also refuses them ptrace, /proc/<pid>/mem and
    /proc/<pid>/fd. Lasts until the process ends or execs. Raises OSError when prctl fails."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, "prctl(PR_SET_DUMPABLE, 0): " + os.strerror(err))


def undumpable(pid):
    """Whether the process `pid` has made itself undumpable (harden, PR_SET_DUMPABLE 0): the kernel then makes its
    /proc files root's, so no other process of the owner's can read its memory or reach its descriptors through
    /proc/<pid>. False once it is gone."""
    try:
        return os.stat("/proc/%d/mem" % pid).st_uid == 0
    except FileNotFoundError:
        return False


def ready():
    """None when this process may read the password: harden() went through, and it is SYSTEM_PYTHON in isolated mode
    (-I), which loads nothing from a user site, PYTHONPATH or the current folder, where agents install things. Else
    why not, for the caller to refuse with. Hardens the process as a side effect; safe to call again."""
    try:
        harden()
    except OSError as e:
        return "this process could not be hardened: %s" % e
    if not (sys.flags.isolated and sys.executable == SYSTEM_PYTHON):
        return ("run it as `agent-keyring-unlock` (or `%s <kit>/board/bin/agent-board keyring-unlock`), so only the "
                "system's Python loads" % COMMAND)
    return None
