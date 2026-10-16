"""Memory caps for agents.

Without them, one agent's batch of parallel workers can fill RAM and swap; the kernel then kills small services
one by one, and finally the user systemd manager, which takes every agent and service with it.

Every agent this toolkit starts now runs in its own systemd scope, `agent-<name>.scope`, with MemoryMax and
MemorySwapMax (config agent_memory_max / agent_swap_max). A runaway batch is OOM-killed inside its agent's
scope (the kernel picks the biggest process there, normally a worker), and nothing else is touched.
`systemd-cgtop` shows memory per agent by name.

Agents are moved in just after they start: StartTransientUnit with the agent's top process (as tmux does for
panes), then AttachProcessesToUnit for any descendant still outside. Only the top PID goes in the first call,
because systemd rejects the whole call ("No such process") if any listed PID has exited by then, and a session
starting up forks short-lived children. A cap that cannot be applied never stops an agent from starting: it
runs uncapped and the caller reports why.
It is a no-op where there is no systemd user bus (the Mac, some cron or ssh contexts).

Two shared processes must not live inside a service's cgroup, where `systemctl --user restart` of that service
(the mailroom, by default KillMode) kills them too: the tmux server that hosts every agent's terminal, and
Claude's background daemon behind `claude --resume --bg`. Whoever starts them first decides their cgroup, so
`free_from_service` moves them to a scope of their own, without a cap. Callers free the shared process before
adopting the agent and exclude the agent's tree from it, so the agent's move is always the last one.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MANAGER = ["org.freedesktop.systemd1", "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager"]
UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
UNCAPPED = ("", "off", "none", "infinity", "false")


def parse_bytes(text):
    """'32G', '512M', '2048' (bytes), '0' -> bytes; None, 'off', 'infinity' -> None (no cap)."""
    if text is None or str(text).strip().lower() in UNCAPPED:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?", str(text).strip(), re.I)
    if not m:
        raise ValueError("bad size %r (use e.g. 32G, 512M or off)" % text)
    return int(float(m.group(1)) * UNITS[m.group(2).upper()])


def limits(cfg, memory_max=None, swap_max=None):
    """The scope properties for one agent: per-launch values win over config. {} means a scope with no cap."""
    out = {}
    mem = parse_bytes(memory_max if memory_max is not None else cfg.get("agent_memory_max"))
    swap = parse_bytes(swap_max if swap_max is not None else cfg.get("agent_swap_max"))
    if mem == 0:
        raise ValueError("agent_memory_max 0 would kill every agent at once; use off for no cap")
    if mem is not None:
        out["MemoryMax"] = mem
    if swap is not None:
        out["MemorySwapMax"] = swap
    return out


def describe(props):
    def size(n):
        for unit in "TGMK":
            if n and n % UNITS[unit] == 0:
                return "%d%s" % (n // UNITS[unit], unit)
        return str(n)
    return ", ".join("%s=%s" % (k, size(v)) for k, v in sorted(props.items())) or "no cap"


def bus_env():
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR") and Path("/run/user/%d" % os.getuid()).is_dir():
        env["XDG_RUNTIME_DIR"] = "/run/user/%d" % os.getuid()
    return env


def available():
    if not sys.platform.startswith("linux") or not shutil.which("busctl"):
        return False
    env = bus_env()
    return bool(env.get("DBUS_SESSION_BUS_ADDRESS")) or Path(env.get("XDG_RUNTIME_DIR", "/nonexistent"), "bus").exists()


def call(*args):
    """One systemd manager call on the user bus. Returns (ok, output)."""
    try:
        r = subprocess.run(["busctl", "--user", "call"] + MANAGER + [str(a) for a in args], capture_output=True,
                           text=True, timeout=15, env=bus_env())
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


# ------------------------------------------------------------------ processes
def cgroup(pid):
    try:
        with open("/proc/%d/cgroup" % pid) as f:
            for line in f:
                if line.startswith("0::"):
                    return line[3:].strip()
    except OSError:
        pass
    return None


def cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def parents():
    """{pid: ppid} for every process we can see."""
    out = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            out[int(d.name)] = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
    return out


def tree(root, ppids=None):
    """root and all its descendants."""
    ppids = parents() if ppids is None else ppids
    kids = {}
    for pid, ppid in ppids.items():
        kids.setdefault(ppid, []).append(pid)
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        out.append(pid)
        todo.extend(kids.get(pid, []))
    return out


# ------------------------------------------------------------------ scopes
def unit_name(prefix, name):
    base = re.sub(r"[^A-Za-z0-9:_.-]+", "-", "%s-%s" % (prefix, name)).strip("-.")[:120]
    return base + ".scope"


def start_scope(unit, pids, props, description, call=call):
    """Create a transient scope holding these PIDs. Returns (unit or None, message). A name already taken
    (an earlier agent of the same name still running) gets a time suffix."""
    args = [("PIDs", "au", [len(pids)] + list(pids)), ("Description", "s", [description]),
            ("Delegate", "b", ["true"]),  # lets attach() add a straggler later
            ("CollectMode", "s", ["inactive-or-failed"])]
    args += [(k, "t", [v]) for k, v in sorted(props.items())]
    for attempt in (unit, unit[:-len(".scope")] + "-%d.scope" % int(time.time())):
        flat = []
        for k, sig, vals in args:
            flat += [k, sig] + vals
        ok, out = call("StartTransientUnit", "ssa(sv)a(sa(sv))", attempt, "fail", len(args), *flat, 0)
        if ok:
            return attempt, "%s (%s)" % (attempt, describe(props))
        if "already" not in out:
            return None, "uncapped: StartTransientUnit failed: %s" % out[:200]
    return None, "uncapped: %s" % out[:200]


def wait_moved(pid, unit, timeout=5.0, step=0.05):
    """StartTransientUnit returns once the job is queued; the move happens a moment later. Wait for it, so
    nothing the process forks next is left behind."""
    deadline = time.time() + timeout
    while True:
        cg = cgroup(pid)
        if cg is None or cg.endswith("/" + unit):
            return cg is not None
        if time.time() >= deadline:
            return False
        time.sleep(step)


def attach(unit, pids, call=call):
    """Move these PIDs into unit. systemd refuses the whole call if one has exited meanwhile, so on failure each
    is tried alone; one that is gone is no loss. Returns (ok, message)."""
    pids = [p for p in pids if cgroup(p) is not None]
    if not pids:
        return True, ""
    ok, out = call("AttachProcessesToUnit", "ssau", unit, "", len(pids), *pids)
    if ok:
        return True, out
    failed = []
    for p in pids:
        ok, out = call("AttachProcessesToUnit", "ssau", unit, "", 1, p)
        if not ok and cgroup(p) is not None:   # one that exited meanwhile is no loss
            failed.append("%d: %s" % (p, out))
    return not failed, "; ".join(failed)[:200]


def sweep(unit, root, call=call):
    """Move root's descendants that are not in root's cgroup yet (forked before root moved) into unit."""
    target = cgroup(root)
    if target is None:
        return True, ""
    return attach(unit, [p for p in tree(root) if p != root and cgroup(p) not in (None, target)], call=call)


def adopt(root, name, props, description, settle=0.5):
    """Move an agent's process tree into agent-<name>.scope with these caps. Never raises; returns
    (unit or None, message). Children forked while it moves are swept in after `settle` seconds."""
    if not available():
        return None, "uncapped: no systemd user bus here"
    try:
        if cgroup(root) is None:
            return None, "uncapped: process %d is gone" % root
        unit, msg = start_scope(unit_name("agent", name), [root], props, description)
        if unit and not wait_moved(root, unit):
            return unit, msg + "; not yet moved after 5 s"
        if unit:
            sweep(unit, root)
            if settle:
                time.sleep(settle)
                sweep(unit, root)
        return unit, msg
    except Exception as e:  # noqa: BLE001 - a cap must never stop an agent from starting
        return None, "uncapped: %s: %s" % (type(e).__name__, e)


def borrowed_home(cg):
    """True for a cgroup a shared process must not stay in: a service's (restarting it kills the process) or
    an agent's capped scope (the shared process would count against that agent's cap and die with it)."""
    leaf = (cg or "").rsplit("/", 1)[-1]
    return leaf.endswith(".service") or (leaf.startswith("agent-") and leaf.endswith(".scope"))


def free_from_service(pid, name, description, exclude=()):
    """If pid sits in a service's cgroup or an agent's scope (whoever started it first decided), move it and
    its descendants in that same cgroup to <name>.scope, with no cap. Anything already elsewhere (a terminal,
    its own scope) is left alone, and so is every PID in `exclude`: the agent being adopted, which may not
    have left that cgroup yet."""
    if not available():
        return None, "no systemd user bus here"
    try:
        home = cgroup(pid)
        if not borrowed_home(home):
            return None, "already outside a service or agent scope (%s)" % home
        skip = set(exclude)
        unit, msg = start_scope(name + ".scope", [pid], {},
                                "%s (moved out of %s)" % (description, home.rsplit("/", 1)[-1]))
        if unit:
            wait_moved(pid, unit)
            attach(unit, [p for p in tree(pid) if p != pid and p not in skip and cgroup(p) == home])
        return unit, ("moved out of %s into %s" % (home.rsplit("/", 1)[-1], unit)) if unit else msg
    except Exception as e:  # noqa: BLE001
        return None, "not moved: %s: %s" % (type(e).__name__, e)


# ------------------------------------------------------------------ claude --bg
def is_claude_daemon(pid):
    argv = cmdline(pid)
    return bool(argv) and "claude" in os.path.basename(argv[0]) and argv[1:3] == ["daemon", "run"]


def find_bg_session(session_id, timeout=10.0, step=0.25, session_pid=None):
    """The top process of a `claude --resume ID --bg` session: the daemon's child (a bg-pty-host) hosting it.
    The daemon often hands a resume to a pre-started spare whose command line never names the session, so the
    session registry's pid (`session_pid()`) comes first; a command line resuming ID is the fallback.
    None if neither shows up within `timeout` seconds."""
    deadline = time.time() + timeout
    while True:
        pid = session_pid() if session_pid else None
        if pid and cgroup(pid):
            host = parents().get(pid)
            return host if host and "bg-pty-host" in " ".join(cmdline(host)) else pid
        ppids = parents()
        hits = [p for p in ppids if session_id in cmdline(p) and "--resume" in cmdline(p)]
        tops = [p for p in hits if ppids.get(p) not in hits]
        if tops:
            return next((p for p in tops if is_claude_daemon(ppids.get(p, 0))), tops[0])
        if time.time() >= deadline:
            return None
        time.sleep(step)


def cap_bg_session(session_id, name, props, session_pid=None):
    """After `claude --resume ID --bg`: cap that session, and move the daemon out of the caller's service.
    Returns a one-line summary."""
    if not available():
        return "uncapped: no systemd user bus here"
    root = find_bg_session(session_id, session_pid=session_pid)
    if root is None:
        return "uncapped: background session %s not found" % session_id[:8]
    freed = ""
    daemon = parents().get(root)   # first, so the session's own move below is the last one
    if daemon and is_claude_daemon(daemon):
        moved, how = free_from_service(daemon, "claude-bg-daemon", "Claude background daemon", exclude=tree(root))
        if moved:
            freed = "; daemon " + how
    unit, msg = adopt(root, name, props, "agent %s (resumed, claude --bg)" % name, settle=1.0)
    return msg + freed
