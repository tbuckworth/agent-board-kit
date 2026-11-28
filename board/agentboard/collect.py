"""Assemble one board snapshot from the cached readers. Cheap enough to run every few seconds."""
import os
import re
import time
from pathlib import Path

from . import classify, config, sources

EVENTS_PER_AGENT = 25
NOTES_PER_AGENT = 8
TIMELINE_EVENTS = ("note", "spawn", "closed", "quota", "failover", "limit", "limit-wait", "api-error")
LIMIT_EVENTS = ("quota", "limit", "limit-wait")    # a turn stopped on a plan limit (hooks: Notification, StopFailure)


_REAL = {}      # path -> real path, for one poll: free_worktrees asks about the same few hundred paths ~6,500 times


def _realpath(p):
    r = _REAL.get(p)
    if r is None:
        try:
            r = os.path.realpath(p)
        except (OSError, ValueError):
            r = p
        if len(_REAL) < 20000:
            _REAL[p] = r
    return r


def forget_paths():
    """Each poll starts here, so a symlink or directory made since the last one is seen within a poll."""
    _REAL.clear()


def orchestrator_dir():
    """Where the orchestrator's events run (config.orchestrator_dir: the mailroom config's orchestrator_dir, else
    <repos_root>/orchestrator)."""
    return _realpath(str(config.orchestrator_dir(sources.mail_config())))


def is_orchestrator(sid, name, cwd, orch_dir):
    """Whether a session is one of the orchestrator's (they are not tiles); orch_dir is orchestrator_dir()."""
    return sid == "orchestrator" or (name or "").startswith("orchestrator") or (bool(cwd) and _realpath(cwd) == orch_dir)


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


def worktree_name(path):
    """A worktree's name on the board (its folder's name): what its row shows and what a force remove must be
    given."""
    return os.path.basename(path.rstrip("/"))


def is_subagent_tree(path):
    """Whether a worktree is one of Claude Code's own subagent worktrees (<repo>/.claude/worktrees/...)."""
    return os.sep + os.path.join(".claude", "worktrees") + os.sep in path


def occupied(wt, live_cwds, tile_cwds, live_repos=()):
    """Is someone in this worktree? An agent working in it (or below it), a tile already showing it (Offline),
    or, for a subagent worktree (is_subagent_tree), a live agent anywhere in that repo (any of its worktrees),
    whose subagent may still be using it."""
    if any(inside(c, wt["path"]) for c in list(live_cwds) + list(tile_cwds)):
        return True
    if is_subagent_tree(wt["path"]):
        return wt["repo"] in live_repos or any(inside(c, wt["main"]) for c in live_cwds)
    return False


def free_worktrees(rows, worktrees):
    """The worktrees (list_worktrees rows) that no agent in `rows` occupies: the board's bottom section, and all
    that bulk clean-up offers. A live agent's repo is its row's "repo", or git_facts' for its directory when the row
    has none (rows read without git: one git call each)."""
    live = [a["cwd"] for a in rows if a.get("live") and a.get("cwd")]
    tiles = [a["cwd"] for a in rows if not a.get("live") and a.get("cwd")]
    repos = {a.get("repo") or (sources.git_facts(a["cwd"]) or {}).get("repo")
             for a in rows if a.get("live") and a.get("cwd")}
    return [w for w in worktrees if not occupied(w, live, tiles, repos)]


def desktop_info(sync):
    """Pending updates / reboot needed / GPU for the board's Desktop chip (updates_info), as a Periodic read every
    30 minutes: apt-check takes a few seconds. In the background unless `sync`."""
    return sources.Periodic(updates_info, 1800, {}, sync)


def scan_worktrees():
    """Every linked worktree (list_worktrees) with its git_facts as "facts" ({} when git has none)."""
    return [dict(w, facts=sources.git_facts(w["path"]) or {}) for w in sources.list_worktrees()]


def worktree_scan(sync):
    """scan_worktrees() as a Periodic read every 10 minutes and on a manual refresh: git across every worktree takes
    a few seconds. In the background unless `sync`."""
    return sources.Periodic(scan_worktrees, 600, [], sync)


class RetryMemo:
    """One answer per key, asked for once: an unknown answer (None), or a failed ask, is asked for again `retry`
    seconds later, and the memo's answer stands in between."""

    def __init__(self, retry):
        self.retry = retry
        self.memo = {}         # key -> (answer, asked at, error)

    def get(self, key, now, ask):
        """(answer, error) for key: ask()'s answer, asked for when the memo has none due; error is why the last ask
        failed (error_text, with answer None), else None."""
        answer, at, err = self.memo.get(key, (None, 0, None))
        if answer is None and now - at > self.retry:
            try:
                answer, err = ask(), None
            except Exception as e:      # unknown for now; the caller reports err
                answer, err = None, sources.error_text(e)
            self.memo[key] = (answer, now, err)
        return answer, err

    def keep(self, keys):
        """Forget every key not in `keys`."""
        self.memo = {k: v for k, v in self.memo.items() if k in keys}


class Collector:
    """Reads the agents (agents()) and assembles the board's snapshot (snapshot()). usage, gmail, worktrees,
    desktop, keyring, meetings and mailbox are the caches whose sections the snapshot carries (Usage, GmailState,
    worktree_scan, desktop_info, keyring.watch, meetings.Meetings, a Periodic of mailbox.summary); security_alerts
    reads the mailroom's recent security alerts (sources.security_alerts) with each snapshot, for the Mac's
    notifications; prs (PullRequests) feeds the agents' git chips and the worktree rows; sent and restart are the
    agentboard modules whose counts and status it carries; logins gives the logins a new agent can be started on by
    name (sources.manual_logins), read with each snapshot.
    Whether the owner has read an agent's mail (is_read) comes from gmail and mailbox together.
    None leaves a section out. The other arguments replace the readers their defaults name."""

    def __init__(self, events=None, codex=None, mail=None, git=None, metrics=None, usage=None,
                 claude=sources.claude_sessions_all, panes=sources.tmux_list_panes, prs=None, gmail=None,
                 worktrees=None, desktop=None, restored=None, waits=sources.stuck_waits, routing=sources.claude_routing,
                 stops=sources.last_turn_failed, stuck_mail=sources.stuck_mail, sent=None, restart=None,
                 chats=None, meetings=None, mailbox=None, security_alerts=sources.security_alerts, keyring=None,
                 logins=None):
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
        self.keyring = keyring                 # the browser's keyring on the desktop, with login beside it (None: off)
        self.sent, self.restart = sent, restart
        self.chats = chats                     # a sources.Periodic of subchats.summary, for the Mac's Chats button
        self.meetings = meetings               # for the Mac's meeting alerts (None: off, no meetings section)
        self.mailbox = mailbox                 # the Mail tab's unread count, and what the owner has read there
        self.security_alerts = security_alerts  # the mailroom's recent security alerts, for the Mac's notifications
        self.logins = logins                   # the start dialog's logins besides Automatic
        self.restored_fn = restored            # {id: (restored_at, activity before)}: keep quiet agents' colours
        self.panes_fn = panes
        self.panes, self.tmux_names = {}, {}   # pane pid -> session; session -> its panes' directories
        self.tmux = sources.Periodic(self._read_tmux, 30, ({}, {}), sync=True)
        self.waits_fn, self.waits_for = waits, []
        self.waits = sources.Periodic(self._read_waits, 30, {}, sync=True)
        self.mail_fn = stuck_mail
        self.stuck = sources.Periodic(self._read_stuck_mail, 30, {}, sync=True)
        self.routing_fn = routing
        self.models = RetryMemo(300)           # (session id, pid) -> model, for agents on a later login
        self.stops_fn = stops
        self.stops = RetryMemo(300)            # (session id, limit/error ts) -> transcript ends on a failed request?
        # what a manual refresh reads again, and whose errors the snapshot lists, by the name it lists them under
        self.caches = {name: c for name, c in (
            ("usage", usage), ("Gmail", gmail), ("worktrees", worktrees), ("Desktop", desktop), ("keyring", keyring),
            ("pull requests", prs), ("meetings", meetings), ("mailbox", mailbox), ("tmux", self.tmux),
            ("background waits", self.waits),
            ("stuck mail", self.stuck)) if c is not None}
        self.problems = []                     # {source, error}: what failed in the last agents() and snapshot()
        self.last_mail = {"by_session": {}}    # the mail the last agents() read, for tick_seen()
        self.subagent_ids = set()
        self.transient = set()
        self.owner = config.owner_name(default=None)   # "waiting on <name>" in a note reads as blocked

    def _read_tmux(self):
        rows = self.panes_fn()
        return sources.tmux_panes(rows), sources.tmux_sessions(rows)

    def _panes(self, now):
        """{pane pid: session}, read every 30 s with tmux_names from the same tmux call."""
        self.panes, self.tmux_names = self.tmux.maybe_refresh(now)
        return self.panes

    def _read_waits(self, pids):
        self.waits_for = list(pids)
        return self.waits_fn(pids) if pids else {}

    def _waits(self, pids, now):
        """stuck_waits() for agents in Claude's `shell` state, re-read every 30 s or when a new one appears."""
        return self.waits.maybe_refresh(now, force=not set(pids) <= set(self.waits_for), args=(pids,))

    def _read_stuck_mail(self, pids, now):
        return self.mail_fn(pids, now=now) if pids else {}

    def _stuck_mail(self, pids, now):
        """stuck_mail() for every live agent, re-read every 30 s."""
        return self.stuck.maybe_refresh(now, args=(pids, now))

    def _unconfirmed_stop(self, a, now, kept):
        """The time of the limit or API-error event that would show a live Claude agent as blocked, when its
        transcript doesn't end on a failed request (a side request's StopFailure after a finished turn), else 0.
        The transcript is read once per event, from 30 s after it (Claude Code may still be writing its record);
        an unreadable one keeps the agent blocked, is listed in problems and is asked again after 5 minutes."""
        stop = max(a.get("quota_ts") or 0, (a.get("error") or {}).get("ts") or 0)
        if not (a["live"] and a["cli"] == "claude" and stop and stop >= (a.get("last_turn_end") or 0)
                and now - stop >= 30):
            return 0
        key = (a["id"], stop)
        kept.add(key)
        failed, err = self.stops.get(key, now, lambda: self.stops_fn(a["id"]))
        if err:
            self.problems.append({"source": "last turn of " + a["display"], "error": err})
        return stop if failed is False else 0

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
            if want and any(p and inside(p, want) for p in panes):
                return t
        return ""

    def refresh(self):
        """A manual refresh: the next poll reads git and every cache in `caches` again (usage through agent-usage's
        own cache, so no probe runs)."""
        self.git.invalidate()
        for c in self.caches.values():
            c.invalidate()

    def agents(self, now=None, with_git=True):
        """(rows, mail, events): the agents the board shows, the mailroom view and aggregate_events'. Starts a new
        `problems` list with what failed to read."""
        now = now or time.time()
        self.problems = []
        forget_paths()
        evs = aggregate_events(self.events.poll(now), now)
        recent = {sid for sid, a in evs.items() if now - a["last_ts"] < classify.OFFLINE_WINDOW}
        reg = {}
        panes = self._panes(now)
        if self.mailbox is not None:
            self.mailbox.maybe_refresh(now)    # before the tiles ask is_read, so a one-shot snapshot has it too
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
        codex = self.codex.sessions(ids_of_interest=[sid for sid in recent if evs[sid]["cli"] == "codex"], now=now)
        if self.codex.error:
            self.problems.append({"source": "Codex sessions", "error": self.codex.error})
        for s in codex:
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

        mail = self.last_mail = self.mail.read(now)
        if mail.get("error"):
            self.problems.append({"source": "mail", "error": mail["error"]})
        try:
            restored = self.restored_fn() if self.restored_fn else {}
        except Exception as e:      # quiet agents lose their kept colour this poll; problems says why
            self.problems.append({"source": "restored agents", "error": sources.error_text(e)})
            restored = {}
        rows, orch = [], orchestrator_dir()
        for sid, s in reg.items():
            ev = evs.get(sid, {})
            if is_orchestrator(sid, s.get("name") or ev.get("name"), s.get("cwd") or ev.get("cwd"), orch):
                continue
            if sid in self.transient:
                continue
            last_activity = max(ev.get("last_ts", 0), s.get("status_at") or 0)
            if sid in restored:
                at, before = restored[sid]
                # the restart's own start/end lines are not activity; anything else is, even in the first minutes
                # (an agent can get a reply during the restore, act on it and close itself)
                real = [e["ts"] for e in ev.get("events", [])
                        if e["event"] not in ("start", "end") or e["ts"] > at + 300]
                status_at = (s.get("status_at") or 0) if (s.get("status_at") or 0) > at + 300 else 0
                last_activity = max([before, status_at] + real)
            m = mail["by_session"].get((s["cli"], sid), {"sent": [], "inbound": []})
            ms = classify.mail_state(m["sent"], m["inbound"], now, reviews=m.get("reviews"))
            if ms["unanswered"] and (self.gmail is not None or self.mailbox is not None):
                ms["read"] = self.is_read(ms["last_out_id"])
            if self.gmail is not None and ms.get("last_out_id"):
                self.gmail.is_read(ms["last_out_id"])            # also asks for its thread, for "open in Gmail"
                ms["gmail_thread"] = self.gmail.thread_of(ms["last_out_id"])
            if not s["live"] and not classify.keep_offline(ev, ms, last_activity, now):
                continue
            tm = sources.tmux_name(s.get("tmux"))
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
                "account_default": s.get("account_default", True), "account_label": s.get("account_label"),
                "config_dir": s.get("config_dir") or "",
                "subagents": sorted(subagents.get(sid, []), key=lambda x: x["nickname"]),
            }
            if not a["live"]:
                a["events"] = a["events"][-10:]
                # what "offline" leaves behind: the directory/worktree, and possibly an empty tmux session
                a["left_behind"] = {"cwd_exists": bool(a["cwd"]) and os.path.isdir(a["cwd"]),
                                    "tmux": self._own_tmux(a["cwd"], a["name"], sources.tmux_name(s.get("tmux")))}
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
        stuck = self._stuck_mail([int(a["pid"]) for a in rows if a["live"] and a["pid"]], now)
        kept = set()
        for a in rows:
            if a["bg"] and a["pid"]:
                a["bg_waits"] = waits.get(int(a["pid"]))
            if a["live"] and a["pid"] and stuck.get(int(a["pid"])):
                a["mail_stuck"] = stuck[int(a["pid"])]
            a["display"] = classify.display_name(a)
            a["stop_unconfirmed"] = self._unconfirmed_stop(a, now, kept)
            a["state"], a["reason"] = classify.classify(a, now, owner=self.owner)
            a["fresh"] = round(classify.freshness(a["state"], now - (a["last_activity"] or 0)), 2)
            a["stranded"] = classify.stranded(a)
        self.stops.keep(kept)
        classify.disambiguate(rows)
        order = {s: i for i, s in enumerate(classify.STATES)}
        rows.sort(key=lambda a: (order.get(a["state"], 99), -(a["last_activity"] or 0)))
        return rows, mail, evs

    def snapshot(self, now=None):
        """The board's JSON snapshot: agents, timeline, summary, mail, and the section of each cache and module the
        collector was given, and "spawn_logins" (its `logins`'). It ticks no message off (tick_seen() does that); the
        files it can write are the Codex credits state, the usage readings for the usage breakdown and the spend
        meters' transcript cache, through the usage cache's annotate step. A source that fails is left out or kept at
        its last reading, and listed in "problems" ({source, error})."""
        now = now or time.time()
        rows, mail, evs = self.agents(now)
        names = {a["id"]: a["display"] for a in rows}
        timeline, orch = [], orchestrator_dir()
        recent = sorted((e for e in self.events.events if now - (e.get("ts") or 0) <= 86400),
                        key=lambda e: -(e.get("ts") or 0))
        for e in recent:
            if e.get("event") in TIMELINE_EVENTS and not is_orchestrator(e.get("session_id"), e.get("name"), e.get("cwd"), orch):
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
            "type": "snapshot", "host": sources.os.uname().nodename, "generated": now,
            "agents": rows, "timeline": timeline, "summary": summary,
            "mail": {"ok": mail.get("ok", False), "held": mail["held"][:30], "held_count": len(mail["held"]),
                     "held_owner": sum(1 for h in mail["held"] if h["owner"])},
            "states": classify.STATES,
            "owner": sources.owner(),
            "problems": self.problems,
        }
        problems = snap["problems"]
        if self.logins is not None:
            try:
                snap["spawn_logins"] = self.logins()           # the start dialog's logins besides Automatic
            except (OSError, ValueError, RuntimeError, ImportError) as e:
                problems.append({"source": "the mailroom's logins", "error": sources.error_text(e)})
        if self.usage is not None:
            snap["usage"] = self.usage.maybe_refresh(now)
            self.mark_logins(rows, snap["usage"].get("accounts") or [], now)
        if self.sent is not None:
            try:
                snap["sent"] = self.sent.counts()   # the Mac's "Sent" chip: messages to check, messages waiting
            except Exception as e:
                problems.append({"source": "sent", "error": sources.error_text(e)})
        if self.chats is not None:
            value = self.chats.maybe_refresh(now)
            if self.chats.error:
                problems.append({"source": "group chats", "error": self.chats.error})
            if value is not None:
                snap["chats"] = {"chats": value["chats"]}
                problems += [{"source": "sub-group chats", "error": p} for p in value["problems"]]
        if self.gmail is not None:
            snap["gmail"] = dict(self.gmail.maybe_refresh(now))
        if self.mailbox is not None and self.mailbox.value is not None:   # None: no mailbox, so the Mac's tab says so
            snap["mailbox"] = {k: self.mailbox.value[k] for k in ("unread", "inbox")}
        if self.meetings is not None:
            value = self.meetings.maybe_refresh(now)
            if value is not None:             # until the first read ends, the Mac keeps the meetings it has
                snap["meetings"] = {k: value[k] for k in ("notify_minutes", "fullscreen_minutes", "events")}
                problems += [{"source": "meetings: %s calendar" % f["calendar"], "error": f["error"]} for f in value["failed"]]
        if self.security_alerts is not None:
            try:
                alerts, bad = self.security_alerts(now)
            except OSError as e:              # left out: the Mac keeps what it has notified
                problems.append({"source": "security alerts", "error": sources.error_text(e)})
            else:
                snap["security_alerts"] = alerts
                if bad:
                    problems.append({"source": "security alerts", "error": "%d line(s) of %s are not alerts"
                                     % (bad, sources.SECURITY_ALERTS.name)})
        if self.worktrees is not None:
            snap["worktrees"] = self.worktree_section(rows, now)
        if self.desktop is not None:
            snap["desktop"] = dict(self.desktop.maybe_refresh(now))
        if self.keyring is not None:
            value = self.keyring.maybe_refresh(now)
            if value is not None:             # None until the first read ends
                snap["keyring"] = value
                problems += [{"source": "keyring (%s)" % name, "error": c["error"]}
                             for name, c in value["collections"].items() if c["error"]]
        if self.restart is not None:
            try:
                st = self.restart.status()
            except (OSError, ValueError) as e:      # a damaged status.json: no restart chip; problems says why
                problems.append({"source": "restart", "error": sources.error_text(e)})
            else:
                snap["restart"] = st if st and not st.get("test") and now - (st.get("updated") or 0) < 2 * 86400 else {}
        for name, c in self.caches.items():
            if c.error:
                problems.append({"source": name, "error": c.error})
        return snap

    def tick_seen(self, now=None):
        """Tick off the done messages whose agent's emails the owner has seen (sent.seen_ticks), from the mail the last
        agents() read and whether the owner has read each (is_read: the Mail tab's or Gmail's read state); the refs ticked.
        Needs `sent`. It writes the sent marks file, so the stream runs it after each snapshot. Raises what seen_ticks
        raises (OSError, ValueError)."""
        return self.sent.seen_ticks({k[1]: v for k, v in self.last_mail["by_session"].items()},
                                    self.is_read if self.gmail is not None or self.mailbox is not None else None, now)

    def is_read(self, message_id):
        """Whether the owner has read the agent mail with this Message-ID: True when the Mail tab counts it read (the
        mailbox's last summary). Otherwise, for mail in the tab alone (sources.tab_only), the tab's answer: False once
        there is a summary, which lists every agent mail the tab counts read from the last 8 days (the mailroom's
        SUMMARY_READ_DAYS), more than the week of mail the collector reads (MailStore.read), and None before. For other
        mail, Gmail's answer (True, False, or None when Gmail cannot tell or is not asked). Needs gmail or mailbox."""
        tab = self.mailbox.value if self.mailbox is not None else None
        if tab and sources.norm_id(message_id) in tab["read"]:
            return True
        if sources.tab_only(message_id):
            return None if tab is None else False
        return self.gmail.is_read(message_id) if self.gmail is not None else None

    def mark_logins(self, rows, accounts, now):
        """login_note on agents running on a later login while the first has room (a red star on the tile). The
        model comes from the process arguments or the transcript, read once per process; an unknown one is asked
        again after 5 minutes (the transcript may not have a model line yet). A failed read, or a usage reading or
        routing config login_note cannot use, is listed in problems."""
        order, thresholds = self.routing_fn()
        live = set()
        for a in rows:
            if not (a["live"] and a["cli"] == "claude" and order and a.get("account") not in ("", order[0])):
                continue
            if not a.get("model"):
                key = (a["id"], a.get("pid"))
                live.add(key)
                model, err = self.models.get(key, now, lambda: sources.model_effort(a)[0] or None)
                if err:                          # "model unknown" for now: every model window must have room
                    self.problems.append({"source": "model of " + a["display"], "error": err})
                a["model"] = model or ""
            try:
                note = classify.login_note(a, accounts, order, thresholds, now)
            except Exception as e:               # no star this poll; problems says why
                self.problems.append({"source": "login note of " + a["display"], "error": sources.error_text(e)})
                note = None
            if note:
                a["login_note"] = note
        self.models.keep(live)

    def worktree_section(self, rows, now):
        """The board's bottom section: free_worktrees() of the latest worktree scan, each with its git stage, PR
        and own tmux session, newest commit first."""
        out = []
        for w in free_worktrees(rows, self.worktrees.maybe_refresh(now)):
            f = w.get("facts") or {}
            branch = f.get("branch") or w["branch"] or "HEAD"
            pr = self.prs.lookup(f.get("slug"), branch, now) if self.prs and branch not in classify.TRUNK else None
            g = classify.git_stage(f, pr) if f else None
            last = f.get("last_commit") or {}
            out.append({"path": w["path"], "name": worktree_name(w["path"]), "repo": w["repo"],
                        "branch": "" if w["detached"] else branch, "locked": w["locked"], "git": g, "last_commit": last,
                        "tmux": self._own_tmux(w["path"], worktree_name(w["path"]))})
        out.sort(key=lambda w: -((w["last_commit"] or {}).get("ts") or 0))
        return out


def _kver(name):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", name)]


def updates_info(run=sources.run):
    """Pending apt and snap updates, whether a reboot is needed, the running and newest kernels, the GPU."""
    out = {"checked": time.time()}
    code, _, err = run(["/usr/lib/update-notifier/apt-check"], timeout=120)
    m = re.match(r"\s*(\d+);(\d+)", err or "")
    out["total"], out["security"] = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    rr = Path("/var/run/reboot-required")
    out["reboot_required"] = rr.exists()
    out["reboot_since"] = rr.stat().st_mtime if rr.exists() else None
    try:
        pkgs = Path("/var/run/reboot-required.pkgs").read_text().split()
        out["reboot_pkgs"] = sorted(set(pkgs))[:8]
    except OSError:
        out["reboot_pkgs"] = []
    out["kernel"] = os.uname().release
    try:
        kernels = sorted((d for d in os.listdir("/lib/modules") if d[0].isdigit()), key=_kver)
        out["newest_kernel"] = kernels[-1] if kernels else ""
    except OSError:
        out["newest_kernel"] = ""
    smi = sources.find_bin("nvidia-smi")
    if smi:
        code, so, se = run([smi, "-L"], timeout=20)
        out["gpu_ok"] = code == 0
        out["gpu_message"] = "" if code == 0 else ((so or se).strip().splitlines() or [""])[0][:160]
    else:                        # no NVIDIA tools: no GPU to check, which is not a fault
        out["gpu_ok"], out["gpu_message"] = None, ""
    code, so, se = run(["snap", "refresh", "--list"], timeout=60)
    lines = [l for l in (so or "").splitlines()[1:] if l.strip()] if code == 0 else []
    out["snaps"] = [l.split()[0] for l in lines]
    code, so, _ = run(["apt", "list", "--upgradable"], timeout=120)
    names = [l.split("/")[0] for l in (so or "").splitlines() if "/" in l]
    out["highlights"] = [n for n in names if re.match(r"(linux-(image|generic|headers-generic)|nvidia-(driver|dkms|utils|kernel))", n)][:10]
    return out
