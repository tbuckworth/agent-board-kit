"""Reading the process tree from /proc: a process's command line, its parent, its children and descendants."""
from pathlib import Path


def cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _stat_ppid(stat):
    """The parent pid in a /proc/PID/stat line: the second field after the command name, which is in
    parentheses and may itself hold spaces and ")"."""
    return int(stat.rsplit(")", 1)[1].split()[1])


def ppid(pid):
    """The parent pid of one process, or None if it is gone."""
    try:
        return _stat_ppid(Path("/proc/%d/stat" % pid).read_text())
    except (OSError, IndexError, ValueError):
        return None


def parents():
    """{pid: ppid} for every process we can see."""
    out = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            out[int(d.name)] = _stat_ppid((d / "stat").read_text())
        except (OSError, IndexError, ValueError):
            continue
    return out


def children(pid, ppids=None):
    """The direct children of pid, lowest pid first; ppids: a parents() map to read instead of /proc."""
    ppids = parents() if ppids is None else ppids
    return sorted(p for p, pp in ppids.items() if pp == pid)


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
