"""Memory watch: warn the agents holding the most memory before the machine runs out.

One agent's batch of parallel workers can fill RAM and swap; the kernel then kills small services one by one
(the mailroom among them) and finally the user systemd manager, which ends every agent. A plain message to the
agent that started the batch is usually enough for it to cut its workers. This sends that message automatically:

- warn (default 80% of RAM in use): message the agents that hold most of it;
- urgent (90% of RAM, or swap half full): message them again, urgently, and email the owner the table;
- cap: an agent whose own scope (agent-NAME.scope, see memcap) is at 90% of its MemoryMax is told before
  the cap kills its batch.

"In use" is RAM the kernel cannot reclaim (MemTotal - MemAvailable). Each process's anonymous memory plus swap is
charged to the nearest ancestor that is a Claude or Codex session; the rest is listed by its cgroup unit.
Pure functions here, so the decisions are testable; bin/agent-memwatch does the reading, sending and looping.
"""
import os
from pathlib import Path

DEFAULTS = {
    "warn_pct": 80,             # RAM in use, %: message the top agents
    "urgent_pct": 90,           # RAM in use, %: message again, urgently, and email the owner
    "swap_urgent_pct": 50,      # swap in use, %: counts as urgent (swap thrash is what makes an OOM storm fatal)
    "rearm_pct": 75,            # below this (and swap below half the urgent level) everything re-arms
    "interval_seconds": 15,
    "repeat_minutes": 30,       # per agent and level
    "email_repeat_minutes": 60,
    "cap_warn_pct": 90,         # of an agent scope's own MemoryMax
    "min_agent_gb": 2,          # agents holding less are never messaged
    "max_agents": 3,
    "email": True,
}
GB = 1 << 30


def settings(cfg):
    s = dict(DEFAULTS, **(cfg.get("memwatch") or {}))
    s.setdefault("owner_name", (cfg.get("owner_name") or "").strip() or "the owner")
    return s


# ------------------------------------------------------------------ reading
def meminfo(path="/proc/meminfo"):
    """{field: bytes}"""
    out = {}
    with open(path) as f:
        for line in f:
            k, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[k] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    return out


def usage(mi):
    total, swap_total = mi["MemTotal"], mi.get("SwapTotal", 0)
    used, swap_used = total - mi["MemAvailable"], swap_total - mi.get("SwapFree", 0)
    return {"ram_total": total, "ram_used": used, "ram_pct": 100.0 * used / total,
            "swap_total": swap_total, "swap_used": swap_used,
            "swap_pct": 100.0 * swap_used / swap_total if swap_total else 0.0}


def processes(proc="/proc"):
    """[{pid, ppid, comm, mem (anon + swap bytes), cgroup}] for every process we can read."""
    out = []
    for d in Path(proc).iterdir():
        if not d.name.isdigit():
            continue
        try:
            fields = {}
            for line in (d / "status").read_text().splitlines():
                k, _, v = line.partition(":")
                fields[k] = v.strip()
            cg = (d / "cgroup").read_text().strip().rsplit("::", 1)[-1]
        except OSError:
            continue
        kb = lambda k: int(fields.get(k, "0 kB").split()[0])  # noqa: E731
        out.append({"pid": int(d.name), "ppid": int(fields.get("PPid", 0)), "comm": fields.get("Name", "?"),
                    "mem": (kb("RssAnon") + kb("VmSwap")) * 1024, "cgroup": cg})
    return out


def cgroup_limits(path, root="/sys/fs/cgroup"):
    """(anonymous memory + swap, memory.max or None) of one cgroup. Not memory.current: page cache counts there,
    and the kernel reclaims it before the cap bites, so an agent reading big files would look full."""
    base = Path(root + path)
    try:
        stat = dict(line.split() for line in (base / "memory.stat").read_text().splitlines())
        swap = (base / "memory.swap.current").read_text().strip() if (base / "memory.swap.current").exists() else "0"
        mx = (base / "memory.max").read_text().strip()
        return int(stat.get("anon", 0)) + int(swap), None if mx == "max" else int(mx)
    except (OSError, ValueError):
        return None, None


# ------------------------------------------------------------------ attribution
def session_pids(sessions, procs, cwd_of=None):
    """{pid: session} for every live session's own process. Claude sessions name their pid; a Codex process is
    matched to the one live Codex session with its working directory."""
    cwd_of = cwd_of or (lambda pid: os.readlink("/proc/%d/cwd" % pid))
    out = {s["pid"]: s for s in sessions if s.get("cli") == "claude" and s.get("live") and s.get("pid")}
    by_cwd = {}
    for s in sessions:
        if s.get("cli") == "codex" and s.get("live") and s.get("cwd"):
            by_cwd.setdefault(s["cwd"], []).append(s)
    for p in procs:
        if p["comm"].startswith("codex") and p["pid"] not in out:
            try:
                match = by_cwd.get(cwd_of(p["pid"]), [])
            except OSError:
                continue
            if len(match) == 1:
                out[p["pid"]] = match[0]
    return out


def env_session_id(pid):
    """CLAUDE_CODE_SESSION_ID from a process's environment: Claude's Bash tool sets it, and every process the
    agent starts inherits it, also after `nohup ... &` reparents it or `systemd-run --scope` moves it."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            for item in f.read().split(b"\0"):
                if item.startswith(b"CLAUDE_CODE_SESSION_ID="):
                    return item.split(b"=", 1)[1].decode(errors="replace")
    except OSError:
        pass
    return None


def owners(procs, sessions_by_pid, sessions=(), env_of=env_session_id, env_min=16 << 20):
    """Memory per owner, biggest first: [{key, label, session, mem, cgroups, top: [(comm, n, bytes)]}].
    A process belongs to, in order: its nearest ancestor that is a session; the Claude session named in its
    environment (a batch detached with nohup has lost its ancestry); a session whose own process shares its
    cgroup (an agent's tmux pane or agent scope); otherwise its cgroup's unit."""
    ppid = {p["pid"]: p["ppid"] for p in procs}
    by_id = {s["id"]: s for s in sessions if s.get("id")}
    by_cgroup = {p["cgroup"]: sessions_by_pid[p["pid"]] for p in procs if p["pid"] in sessions_by_pid}
    groups = {}
    for p in procs:
        if not p["mem"]:
            continue
        cur, sess, hops = p["pid"], None, 0
        while cur and hops < 64:
            if cur in sessions_by_pid:
                sess = sessions_by_pid[cur]
                break
            cur, hops = ppid.get(cur), hops + 1
        if sess is None and p["mem"] >= env_min:
            sess = by_id.get(env_of(p["pid"]))
        if sess is None:
            sess = by_cgroup.get(p["cgroup"])
        if sess:
            key, label = "session:" + sess["id"], "%s %s" % (sess["cli"], sess.get("name") or sess["id"][:8])
        else:
            unit = p["cgroup"].rstrip("/").rsplit("/", 1)[-1] or "/"
            key, label = "unit:" + unit, unit
        g = groups.setdefault(key, {"key": key, "label": label, "session": sess, "mem": 0, "cgroups": set(),
                                    "by_comm": {}})
        g["mem"] += p["mem"]
        g["cgroups"].add(p["cgroup"])
        n, b = g["by_comm"].get(p["comm"], (0, 0))
        g["by_comm"][p["comm"]] = (n + 1, b + p["mem"])
    out = sorted(groups.values(), key=lambda g: -g["mem"])
    for g in out:
        g["top"] = sorted(((c, n, b) for c, (n, b) in g.pop("by_comm").items()), key=lambda t: -t[2])[:4]
    return out


# ------------------------------------------------------------------ decisions
def level(u, s):
    if u["ram_pct"] >= s["urgent_pct"] or (u["swap_total"] and u["swap_pct"] >= s["swap_urgent_pct"]):
        return 2
    return 1 if u["ram_pct"] >= s["warn_pct"] else 0


def targets(u, own, s):
    """The agents holding most of the memory in use: walk owners, biggest first, until half of it is covered."""
    out, covered = [], 0
    for g in own:
        if covered >= u["ram_used"] / 2 or len(out) >= s["max_agents"]:
            break
        covered += g["mem"]
        if g["session"] and g["mem"] >= s["min_agent_gb"] * GB:
            out.append(g)
    return out


def decide(state, u, own, caps, s, now):
    """-> (actions, new state). actions: ("ping", owner, level, text) | ("email", subject, body).
    `caps`: [(owner, current, max)] for agent scopes near their own MemoryMax."""
    state = {"sent": dict(state.get("sent", {}))}
    sent = state["sent"]
    lvl = level(u, s)
    actions = []
    if lvl == 0 and u["ram_pct"] < s["rearm_pct"] and u["swap_pct"] < s["swap_urgent_pct"] / 2:
        sent = state["sent"] = {k: v for k, v in sent.items() if k.startswith("cap:")}
    due = lambda key, minutes: key not in sent or now - sent[key] >= minutes * 60  # noqa: E731
    if lvl:
        for g in targets(u, own, s):
            key = "%s:%d" % (g["key"], lvl)
            if due(key, s["repeat_minutes"]):
                actions.append(("ping", g, lvl, ping_text(u, g, lvl, s)))
                sent[key] = now
        if lvl == 2 and s["email"] and due("email", s["email_repeat_minutes"]):
            actions.append(("email",) + email_text(u, own))
            sent["email"] = now
    for g, cur, mx in caps:
        key = "cap:" + g["key"]
        if g["session"] and cur >= mx * s["cap_warn_pct"] / 100 and due(key, s["repeat_minutes"]):
            actions.append(("ping", g, "cap", cap_text(g, cur, mx)))
            sent[key] = now
    return actions, state


# ------------------------------------------------------------------ text
def gb(n):
    return "%.1f GB" % (n / GB)


def headline(u):
    return "Memory is at %d%% (%s of %s in use; swap %d%%, %s of %s)." % (
        u["ram_pct"], gb(u["ram_used"]), gb(u["ram_total"]), u["swap_pct"], gb(u["swap_used"]), gb(u["swap_total"]))


def what(g):
    return ", ".join("%s%s %s" % (c, " x%d" % n if n > 1 else "", gb(b)) for c, n, b in g["top"])


def ping_text(u, g, lvl, s):
    owner = s.get("owner_name") or "the owner"
    tail = ("At %d%% %s is emailed; when memory runs out the kernel starts killing other agents and services."
            % (s["urgent_pct"], owner)) if lvl == 1 else (
            "URGENT: %s has been emailed. If memory runs out the kernel kills other agents and services, and it can "
            "kill the user manager, ending every session." % owner)
    return ("[agent-memwatch] %s Your session's processes hold %s (%s). If that is a batch you started, cut its "
            "workers or pause it now, and say so in your next note. %s" % (headline(u), gb(g["mem"]), what(g), tail))


def cap_text(g, cur, mx):
    return ("[agent-memwatch] Your session's scope is at %s of its %s memory cap (%s). At the cap the kernel kills "
            "the biggest process in it, normally one of your workers. Cut workers, or if the job really needs more, "
            "raise it: systemctl --user set-property --runtime %s MemoryMax=<size>." % (
                gb(cur), gb(mx), what(g), sorted(g["cgroups"])[0].rsplit("/", 1)[-1]))


def table(own, n=8):
    return "\n".join("%10s  %s  (%s)" % (gb(g["mem"]), g["label"], what(g)) for g in own[:n])


def email_text(u, own):
    top = own[0] if own else None
    subject = "RAM %d%%%s" % (u["ram_pct"], (": %s holds %s" % (top["label"], gb(top["mem"]))) if top else "")
    body = ("%s\n\nBiggest holders (anonymous memory + swap):\n\n%s\n\nThe agents among them have been messaged. "
            "agent-memwatch emails at most once an hour while memory stays this high." % (headline(u), table(own)))
    return subject, body
