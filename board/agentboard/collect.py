"""Assemble one board snapshot from the cached readers. Cheap enough to run every few seconds."""
import os
import threading
import time

from . import SNAPSHOT_VERSION, classify, config, sources

EVENTS_PER_AGENT = 25
NOTES_PER_AGENT = 8
TIMELINE_EVENTS = ("note", "spawn", "closed", "quota", "failover", "limit", "limit-wait", "api-error")
LIMIT_EVENTS = ("quota", "limit", "limit-wait")    # a turn stopped on a plan limit (hooks: Notification, StopFailure)


def _realpath(p):
    try:
        return os.path.realpath(p)
    except (OSError, ValueError):
        return p


ORCHESTRATOR_DIRS = {_realpath(str(config.orchestrator_dir()))}    # mailroom config orchestrator_dir


def is_orchestrator(sid, name, cwd):
    """Orchestrator runs are not agents: no tiles, no timeline rows."""
    return sid == "orchestrator" or (name or "").startswith("orchestrator") or (
        bool(cwd) and _realpath(cwd) in ORCHESTRATOR_DIRS)


def aggregate_events(events, now):
    """Per session id: turns, notes, recent events and the timestamps the classifier needs."""
    out = {}
    for e in events:
        sid = e.get("session_id") or ""
        if not sid:
            continue
        a = out.setdefault(sid, {"events": [], "notes": [], "turns_24h": 0, "last_ts": 0, "last_turn_end": 0,
                                 "quota_ts": 0, "limit": None, "error": None, "failover": None,
                                 "last_start": 0, "last_end": 0, "first_ts": e.get("ts") or 0,
                                 "name": "", "cwd": "", "cli": "", "branch": ""})
        ts, ev = e.get("ts") or 0, e.get("event") or ""
        a["last_ts"] = max(a["last_ts"], ts)
        for k in ("name", "cwd", "cli", "branch"):
            if e.get(k):
                a[k] = e[k]
        if ev == "idle":
            a["last_turn_end"] = max(a["last_turn_end"], ts)
            if now - ts < 86400:
                a["turns_24h"] += 1
        elif ev == "note":
            a["notes"].append({"ts": ts, "text": (e.get("text") or "")[:500]})
        # hooks run async, so lines can land out of order: keep the newest by timestamp, not by file position
        elif ev in LIMIT_EVENTS:
            a["quota_ts"] = max(a["quota_ts"], ts)
            if ts >= (a["limit"] or {}).get("ts", 0):
                a["limit"] = {"ts": ts, "kind": ev, "text": (e.get("text") or "")[:200]}
        elif ev == "api-error":
            if ts >= (a["error"] or {}).get("ts", 0):
                a["error"] = {"ts": ts, "text": (e.get("text") or "")[:200]}
        elif ev == "failover":                # moved to another Claude login (usage-aware routing)
            if ts >= (a["failover"] or {}).get("ts", 0):
                a["failover"] = {"ts": ts, "text": (e.get("text") or "")[:200], "account": e.get("account") or ""}
        elif ev == "start":
            a["last_start"] = max(a["last_start"], ts)
        elif ev == "end":
            a["last_end"] = max(a["last_end"], ts)
        a["events"].append({"ts": ts, "event": ev, "text": (e.get("text") or "")[:300]})
    for a in out.values():
        a["closed"] = any(e["event"] == "closed" and e["ts"] >= a["last_start"] for e in a["events"])
        a["events"] = a["events"][-EVENTS_PER_AGENT:]
        a["notes"] = a["notes"][-NOTES_PER_AGENT:]
    return out


def inside(path, root):
    """path is root or somewhere below it."""
    p, r = _realpath(path), _realpath(root)
    return p == r or p.startswith(r + os.sep)


def occupied(wt, live_cwds, tile_cwds, live_repos=()):
    """Is someone in this worktree? An agent working in it (or below it), a tile already showing it (Offline),
    or, for Claude Code's own subagent worktrees (<repo>/.claude/worktrees/...), a live agent anywhere in that
    repo (any of its worktrees), whose subagent may still be using it."""
    if any(inside(c, wt["path"]) for c in list(live_cwds) + list(tile_cwds)):
        return True
    if os.sep + os.path.join(".claude", "worktrees") + os.sep in wt["path"]:
        return wt["repo"] in live_repos or any(inside(c, wt["main"]) for c in live_cwds)
    return False


class DesktopInfo:
    """Pending updates / reboot needed / GPU, for the board's Desktop chip. apt-check takes a few seconds, so this
    refreshes in the background every `interval` seconds and polls only read the cache."""

    def __init__(self, interval=1800, reader=None):
        from . import restart
        self.interval, self.reader = interval, reader or restart.updates_info
        self.info, self.at, self.busy = {}, 0, False

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        try:
            self.info = self.reader()
        except Exception:   # the board must not depend on apt
            pass
        finally:
            self.busy = False

    def maybe_refresh(self, now=None, force=False):
        now = now or time.time()
        if force or now - self.at > self.interval:
            self.at = now
            threading.Thread(target=self.refresh, daemon=True).start()
        return self.info


class WorktreeScan:
    """Linked worktrees and their git state. git for ~70 trees takes a few seconds, so this runs in a
    background thread every `interval` seconds (and on a manual refresh); polls only read the cache."""

    def __init__(self, interval=600, lister=sources.list_worktrees, facts=sources.git_facts):
        self.interval, self.lister, self.facts = interval, lister, facts
        self.items, self.at, self.busy = [], 0, False
        self.lock = threading.Lock()

    def refresh(self):
        with self.lock:
            if self.busy:
                return
            self.busy = True
        try:
            self.items = [dict(w, facts=self.facts(w["path"]) or {}) for w in self.lister()]
        except Exception:      # a broken repo must not take the board down
            pass
        finally:
            self.busy = False

    def maybe_refresh(self, now=None, force=False, sync=False):
        now = now or time.time()
        if force or now - self.at > self.interval:
            self.at = now
            if sync:
                self.refresh()
            else:
                threading.Thread(target=self.refresh, daemon=True).start()
        return self.items


def tmux_name(reg_tmux):
    """Claude registers tmux as "session:@window.%pane"; the session name is what attach needs."""
    return (reg_tmux or "").split(":", 1)[0]


class Collector:
    def __init__(self, events=None, codex=None, mail=None, git=None, metrics=None, usage=None,
                 claude=sources.claude_sessions_all, panes=sources.tmux_panes, prs=None, gmail=None, worktrees=None,
                 desktop=None, restored=None, waits=sources.stuck_waits, routing=sources.claude_routing):
        self.events = events or sources.EventLog()
        self.codex = codex or sources.CodexReader()
        self.mail = mail or sources.MailStore()
        self.git = git or sources.GitCache()
        self.metrics = metrics
        self.usage = usage
        self.claude = claude
        self.prs = prs
        self.gmail = gmail
        self.worktrees = worktrees
        self.desktop = desktop
        self.restored_fn = restored            # {id: (restored_at, activity before)}: keep quiet agents' colours
        self.panes_fn = panes
        self.panes, self.panes_at = {}, 0
        self.waits_fn = waits
        self.routing_fn = routing
        self.models = {}                       # (session id, pid) -> model, for agents on a later login
        self.waits, self.waits_at = {}, 0
        self.sessions_fn = sources.tmux_sessions
        self.subagent_ids = set()
        self.transient = set()
        self.owner = config.owner_name(default=None)   # "waiting on <name>" in a note reads as blocked

    def _panes(self, now):
        if now - self.panes_at > 30:
            self.panes, self.panes_at = self.panes_fn(), now
            self.tmux_names = self.sessions_fn()
        return self.panes

    def _waits(self, pids, now):
        """stuck_waits() for agents in Claude's `shell` state, re-read every 30 s or when a new one appears."""
        if now - self.waits_at > 30 or not set(pids) <= set(self.waits_for):
            self.waits, self.waits_at, self.waits_for = (self.waits_fn(pids) if pids else {}), now, list(pids)
        return self.waits

    tmux_names = {}
    waits_for = []

    def _own_tmux(self, cwd, name, recorded=""):
        """The agent's own tmux session if still open. `recorded` is the session its registry entry named
        (proof it ran there); a session merely called `name` counts only with a pane in the agent's
        directory, so an unrelated session sharing the name never does. With the directory gone,
        only the recorded session can be trusted."""
        want = _realpath(cwd) if cwd and os.path.isdir(cwd) else None
        for t in (recorded, name):
            panes = self.tmux_names.get(t) if t else None
            if not panes:
                continue
            if t == recorded and want is None:      # directory gone: the registry's record is the only proof
                return t
            if want and any(p and (_realpath(p) == want or _realpath(p).startswith(want + os.sep)) for p in panes):
                return t
        return ""

    def refresh(self):
        """A manual refresh: refetch git, PRs and tmux on the next poll."""
        self.git.invalidate()
        if self.prs:
            self.prs.invalidate()
        if self.gmail is not None:
            self.gmail.maybe_poll(force=True)
        if self.worktrees is not None:
            self.worktrees.maybe_refresh(force=True)
        if self.desktop is not None:
            self.desktop.maybe_refresh(force=True)
        self.panes_at = 0

    def agents(self, now=None, with_git=True):
        now = now or time.time()
        evs = aggregate_events(self.events.poll(now), now)
        recent = {sid for sid, a in evs.items() if now - a["last_ts"] < classify.OFFLINE_WINDOW}
        reg = {}
        panes = self._panes(now)
        for s in self.claude():
            # `claude -p` / SDK runs (probes, orchestrator, preflights) are not agents; but one started straight from a
            # tmux pane's shell is an interactive agent that inherited CLAUDE_CODE_ENTRYPOINT from a tmux server an
            # orchestrator run started. (A probe an agent runs from its tool shell is deeper down: still a probe.)
            if s.get("entrypoint") == "sdk-cli" and not (s["live"] and s.get("pid") and (
                    int(s["pid"]) in panes or sources.ppid(int(s["pid"])) in panes)):
                self.transient.add(s["id"])
            elif s["id"] and (s["live"] or s["id"] not in reg):
                reg[s["id"]] = s
        subagents = {}
        for s in self.codex.sessions(ids_of_interest=[sid for sid in recent if evs[sid]["cli"] == "codex"], now=now):
            if s.get("parent"):          # a Codex subagent shares its parent's process: list it on the parent's tile
                self.subagent_ids.add(s["id"])
                if s["live"]:
                    subagents.setdefault(s["parent"], []).append({
                        "id": s["id"], "nickname": s.get("nickname") or s["id"][-4:], "path": s.get("agent_path") or "",
                        "busy": s.get("raw_status") == "busy", "last_activity": s.get("activity_at") or s.get("status_at") or 0})
                continue
            reg.setdefault(s["id"], s)
        for sid in recent - set(reg) - self.subagent_ids:
            a = evs[sid]
            reg[sid] = {"cli": a["cli"] or "?", "id": sid, "name": a["name"], "cwd": a["cwd"], "raw_status": "",
                        "kind": "", "live": False, "pid": None, "tmux": "", "waiting_for": "",
                        "started_at": a["first_ts"], "status_at": a["last_ts"]}

        mail = self.mail.read(now)
        try:
            restored = self.restored_fn() if self.restored_fn else {}
        except Exception:
            restored = {}
        rows = []
        for sid, s in reg.items():
            ev = evs.get(sid, {})
            if is_orchestrator(sid, s.get("name") or ev.get("name"), s.get("cwd") or ev.get("cwd")):
                continue
            if sid in self.transient:
                continue
            last_activity = max(ev.get("last_ts", 0), s.get("status_at") or 0)
            if sid in restored:
                at, before = restored[sid]
                real = [e["ts"] for e in ev.get("events", [])
                        if not (e["event"] in ("start", "end") and at - 120 <= e["ts"] <= at + 300) and e["ts"] > at + 300]
                status_at = (s.get("status_at") or 0) if (s.get("status_at") or 0) > at + 300 else 0
                last_activity = max([before, status_at] + real)
            m = mail["by_session"].get((s["cli"], sid), {"sent": [], "inbound": []})
            ms = classify.mail_state(m["sent"], m["inbound"], now)
            if self.gmail is not None and ms["unanswered"]:
                ms["read"] = self.gmail.is_read(ms["last_out_id"])
            if self.gmail is not None and ms.get("last_out_id"):
                self.gmail.is_read(ms["last_out_id"])            # also asks for its thread, for "open in Gmail"
                ms["gmail_thread"] = self.gmail.thread_of(ms["last_out_id"])
            if not s["live"] and not classify.keep_offline(ev, ms, last_activity, now):
                continue
            tm = tmux_name(s.get("tmux"))
            if not tm and s["live"] and s.get("pid"):
                tm = sources.tmux_session_for(int(s["pid"]), panes)
            a = {
                "id": sid, "cli": s["cli"], "name": s.get("name") or ev.get("name") or "", "title": s.get("title") or "",
                "cwd": s.get("cwd") or ev.get("cwd") or "", "live": bool(s["live"]), "raw_status": s.get("raw_status") or "",
                "kind": s.get("kind") or "", "waiting_for": s.get("waiting_for") or "", "pid": s.get("pid"),
                "tmux": tm, "model": s.get("model") or "",
                "started_at": s.get("started_at") or ev.get("first_ts") or 0, "last_activity": last_activity,
                "last_turn_end": ev.get("last_turn_end", 0), "turns_24h": ev.get("turns_24h", 0),
                "quota_ts": ev.get("quota_ts", 0), "limit": ev.get("limit"), "error": ev.get("error"),
                "failover": ev.get("failover"), "notes": ev.get("notes", []), "events": ev.get("events", []),
                "last_note": (ev.get("notes") or [None])[-1], "closed": ev.get("closed", False),
                "mail": ms,
                "branch": ev.get("branch") or s.get("branch_hint") or "", "repo": "", "dirty": None, "last_commit": {},
                "git": None, "bg": s.get("raw_status") == "shell", "account": s.get("account") or "",
                "account_default": s.get("account_default", True), "config_dir": s.get("config_dir") or "",
                "subagents": sorted(subagents.get(sid, []), key=lambda x: x["nickname"]),
            }
            if not a["live"]:
                a["events"] = a["events"][-10:]
                # what "offline" leaves behind: the directory/worktree, and possibly an empty tmux session
                a["left_behind"] = {"cwd_exists": bool(a["cwd"]) and os.path.isdir(a["cwd"]),
                                    "tmux": self._own_tmux(a["cwd"], a["name"], tmux_name(s.get("tmux")))}
            rows.append(a)

        if with_git:
            facts = self.git.get_many([a["cwd"] for a in rows], now)
            for a in rows:
                f = facts.get(a["cwd"]) or {}
                a["repo"] = f.get("repo") or os.path.basename(a["cwd"].rstrip("/"))
                a["branch"] = f.get("branch") or a["branch"]
                a["dirty"] = f.get("dirty")
                a["last_commit"] = f.get("last_commit") or {}
                pr = self.prs.lookup(f.get("slug"), f.get("branch"), now) if self.prs and f.get("branch") not in classify.TRUNK else None
                a["git"] = classify.git_stage(f, pr)
                if a["git"]:
                    a["git"].update({k: f.get(k) for k in ("upstream", "ahead", "behind", "untracked", "default", "not_in_default", "linked")})
        shell = [int(a["pid"]) for a in rows if a["live"] and a["cli"] == "claude" and a["bg"] and a["pid"]]
        waits = self._waits(shell, now)
        for a in rows:
            if a["bg"] and a["pid"]:
                a["bg_waits"] = waits.get(int(a["pid"]))
            a["display"] = classify.display_name(a)
            a["state"], a["reason"] = classify.classify(a, now, owner=self.owner)
            a["fresh"] = round(classify.freshness(a["state"], now - (a["last_activity"] or 0)), 2)
            a["stranded"] = classify.stranded(a)
        classify.disambiguate(rows)
        order = {s: i for i, s in enumerate(classify.STATES)}
        rows.sort(key=lambda a: (order.get(a["state"], 99), -(a["last_activity"] or 0)))
        return rows, mail, evs

    def snapshot(self, now=None):
        now = now or time.time()
        rows, mail, evs = self.agents(now)
        names = {a["id"]: a["display"] for a in rows}
        timeline = []
        recent = sorted((e for e in self.events.events if now - (e.get("ts") or 0) <= 86400),
                        key=lambda e: -(e.get("ts") or 0))
        for e in recent:
            if e.get("event") in TIMELINE_EVENTS and not is_orchestrator(e.get("session_id"), e.get("name"), e.get("cwd")):
                timeline.append({"ts": e.get("ts"), "event": e.get("event"), "text": (e.get("text") or "")[:300],
                                 "id": e.get("session_id") or "",
                                 "name": names.get(e.get("session_id")) or e.get("name") or os.path.basename(e.get("cwd") or "")})
                if len(timeline) >= 150:
                    break
        summary = classify.summarise(rows)
        summary["turns_24h"] = sum(a["turns_24h"] for a in rows)
        summary["notes_24h"] = sum(1 for t in timeline if t["event"] == "note")
        summary["unanswered"] = sum(1 for a in rows if a["mail"]["unanswered"])
        summary["stranded"] = sum(1 for a in rows if a.get("stranded"))
        snap = {
            "type": "snapshot", "v": SNAPSHOT_VERSION, "host": sources.os.uname().nodename, "generated": now,
            "agents": rows, "timeline": timeline, "summary": summary,
            "mail": {"ok": mail.get("ok", False), "held": mail["held"][:30], "held_count": len(mail["held"]),
                     "held_owner": sum(1 for h in mail["held"] if h["owner"])},
            "states": classify.STATES,
            "owner": (sources.owner_addresses() or [""])[0],
        }
        if self.usage is not None:
            snap["usage"] = self.usage.maybe_refresh(now)
            self.mark_logins(rows, snap["usage"].get("accounts") or [], now)
        if self.gmail is not None:
            snap["gmail"] = dict(self.gmail.maybe_poll(now))
        if self.worktrees is not None:
            snap["worktrees"] = self.free_worktrees(rows, now)
        if self.desktop is not None:
            from . import restart
            snap["desktop"] = dict(self.desktop.maybe_refresh(now))
            st = restart.status()
            snap["restart"] = st if st and not st.get("test") and now - (st.get("updated") or 0) < 2 * 86400 else {}
        return snap

    def mark_logins(self, rows, accounts, now):
        """login_note on agents running on a later login while the first has room (a red star on the tile). The
        model comes from the process arguments or the transcript, read once per process; an unknown one is asked
        again after 5 minutes (the transcript may not have a model line yet)."""
        order, thresholds = self.routing_fn()
        live = set()
        for a in rows:
            if not (a["live"] and a["cli"] == "claude" and order and a.get("account") not in ("", order[0])):
                continue
            if not a.get("model"):
                key = (a["id"], a.get("pid"))
                live.add(key)
                model, at = self.models.get(key, ("", 0))
                if not model and now - at > 300:
                    from . import restart
                    try:
                        model = restart.model_effort(a)[0] or ""
                    except Exception:            # an unreadable transcript only means "model unknown" for now
                        model = ""
                    self.models[key] = (model, now)
                a["model"] = model
            try:
                note = classify.login_note(a, accounts, order, thresholds, now)
            except Exception:                    # a malformed usage reading must never take the snapshot down
                note = None
            if note:
                a["login_note"] = note
        self.models = {k: v for k, v in self.models.items() if k in live}

    def free_worktrees(self, rows, now):
        """Linked worktrees with no agent in them: the board's bottom section."""
        items = self.worktrees.maybe_refresh(now)
        live = [a["cwd"] for a in rows if a["live"] and a["cwd"]]
        tiles = [a["cwd"] for a in rows if not a["live"] and a["cwd"]]
        repos = {a["repo"] for a in rows if a["live"] and a.get("repo")}
        out = []
        for w in items:
            if occupied(w, live, tiles, repos):
                continue
            f = w.get("facts") or {}
            branch = f.get("branch") or w["branch"] or "HEAD"
            pr = self.prs.lookup(f.get("slug"), branch, now) if self.prs and branch not in classify.TRUNK else None
            g = classify.git_stage(f, pr) if f else None
            last = f.get("last_commit") or {}
            out.append({"path": w["path"], "name": os.path.basename(w["path"].rstrip("/")), "repo": w["repo"],
                        "branch": "" if w["detached"] else branch, "locked": w["locked"], "git": g, "last_commit": last,
                        "subagent_tree": os.sep + os.path.join(".claude", "worktrees") + os.sep in w["path"],
                        "tmux": self._own_tmux(w["path"], os.path.basename(w["path"].rstrip("/")))})
        out.sort(key=lambda w: -((w["last_commit"] or {}).get("ts") or 0))
        return out
