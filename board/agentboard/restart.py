"""Restart or update-and-restart the desktop without losing the agents (agent-restart and the board's restart
panel; the README's Restart the desktop section says what the owner sees and chooses).

plan only reads. run writes the manifest and hands over to a detached worker, which must outlive the SSH connection
that asked. restore runs at boot (agent-restart-restore.service) and brings Codex agents back last, because a
locked keyring can stall Codex. Nothing here runs a model. The manifest lives in STATE; `status.json` is what the
board shows while it runs. Mail goes to the mailroom config's owner_email (sources.mail_owner, agent-mail send
--unrouted); without one, nothing is mailed and the log says so.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import actions, classify, relaunch, sources
from .sources import read_json, write_json

STATE = sources.BOARD_STATE / "restart"
MANIFEST = STATE / "manifest.json"
STATUS = STATE / "status.json"
LOG = STATE / "run.log"
CANCEL = STATE / "cancel"
CHOICES = ("nudge", "quiet", "skip")
ACTIVE = ("pending", "waiting", "updating", "rebooting", "restoring")
CANCELLABLE = ("pending", "waiting")     # the worker checks for CANCEL only before it updates or reboots
STALE_AFTER = 6 * 3600    # an ACTIVE status not updated for this long is a worker that died
RESTORE_WAIT = 60         # seconds a resumed agent has to come up (Codex gets 30 more)
NETWORK_WAIT = 180        # seconds restore waits at boot for name lookups to work before resuming anyway
RESTORED_DAYS = 7         # how long after a restore its quietly resumed agents keep their colour
WAITS_SINCE = 24 * 3600   # a background command older than this is not something an agent still waits on
TOOL_SEARCH = re.compile(r"^(\S*/)?(ugrep|rg) ")
WRAPPER = re.compile(r"^(\S*/)?(bws run|nohup|setsid|timeout|env)\b")
NOT_WORK = re.compile(r"mcp|npm exec|npx |uvx|context7|playwright|chrome-devtools|/claude$|^claude |codex-code-mode-host"
                      r"|/chrome --type=|crashpad|^cat$")


def show_cmd(cmd):
    """What a command really runs (sources.tool_command), on one line."""
    return " ".join(sources.tool_command(cmd).split())


HEADS_UP = """ desktop restart coming
The desktop restarts within {minutes} minutes{why}. Finish or checkpoint your current step, record where you are
with `agent-status note`, then stop and wait. You will be resumed in this same conversation afterwards and told."""

NUDGE = """ the desktop was restarted
The desktop was restarted at {at}{why}, and you have been resumed in tmux '{tmux}' in the same conversation.
Before the restart you were {state}{note}.{waits}{procs}
Carry on with what you were doing."""


def now():
    return time.time()


def log(msg):
    sources.append_log(LOG, msg)


def set_status(status, message, **extra):
    write_json(STATUS, dict(extra, status=status, message=message, updated=now()))
    log("%s: %s" % (status, message))


def in_progress(st):
    """Whether a status dict (status.json) is a restart under way: an ACTIVE status updated in the last STALE_AFTER
    seconds. Reads the clock only."""
    return st.get("status") in ACTIVE and now() - (st.get("updated") or 0) < STALE_AFTER


def status():
    """The last restart's status.json, {} when there is none, with two flags the file does not hold: `active`
    (in_progress) and `cancellable` (active, and early enough that cancel() stops it). Reads STATUS; a damaged one
    raises (read_json)."""
    st = read_json(STATUS, {}) or {}
    if not st:
        return {}
    active = in_progress(st)
    return dict(st, active=active, cancellable=active and st.get("status") in CANCELLABLE)


# ------------------------------------------------------------------ what is pending


# ------------------------------------------------------------------ what each agent is doing


NOTIFICATION = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
BG_STARTED = re.compile(r"Command running in background with ID: (\w+)")
TASK_STOPS = {"TaskStop": "task_id", "KillShell": "shell_id", "KillBash": "shell_id"}   # tool: its task-id argument
BG_KEYS = ('"run_in_background"', "task-notification", "running in background with ID", '"TaskStop"', '"KillShell"',
           '"KillBash"')


def _epoch(iso):
    try:
        return sources.iso_ts(iso)
    except (AttributeError, TypeError, ValueError):
        return None


def background_waits(agent, now=None):
    """What a Claude agent said it was waiting for: the descriptions of its last three `run_in_background` commands
    that are still running. One that ended has a `<task-notification>` with a status (completed, failed, killed)
    or a TaskStop of its task id (the tool result that started it gives that id); one from before this process
    started (a resume) or older than WAITS_SINCE isn't a wait now."""
    if agent["cli"] != "claude":
        return []
    _, home = sources.claude_home_of(agent["id"])
    if not home:
        return []
    after = max((now or time.time()) - WAITS_SINCE, agent.get("started_at") or 0)
    out, task_of, ended = [], {}, set()         # ended: tool-use ids and task ids
    for path in Path(home).glob("projects/*/%s.jsonl" % agent["id"]):
        try:
            lines = sources.transcript_tail(path, 3_000_000)
        except OSError:
            continue
        for line in lines:
            if not any(k in line for k in BG_KEYS):
                continue
            # an end arrives as a user message, or mid-turn as a queued_command attachment / queue operation
            for note in NOTIFICATION.findall(line):
                status = re.search(r"<status>([a-z_]+)</status>", note)
                if status and status.group(1) != "running":
                    ended.update(re.findall(r"<(?:task-id|tool-use-id)>([^<\s\\]+)</", note))
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") not in ("assistant", "user"):
                continue                # api-request-blob and the like repeat messages, without a timestamp
            content = (d.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                inp = c.get("input") or {} if isinstance(c, dict) else {}
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result":
                    m = BG_STARTED.search(json.dumps(c.get("content")))
                    if m:
                        task_of[c.get("tool_use_id")] = m.group(1)
                elif c.get("type") == "tool_use" and c.get("name") in TASK_STOPS:
                    ended.add(str(inp.get(TASK_STOPS[c["name"]]) or ""))
                elif c.get("type") == "tool_use" and c.get("name") == "Bash" and inp.get("run_in_background"):
                    if (_epoch(d.get("timestamp")) or after) >= after:
                        out.append({"id": c.get("id"), "ts": d.get("timestamp", ""),
                                    "what": (inp.get("description") or "")[:160],
                                    "command": (inp.get("command") or "")[:200]})
    return [{k: v for k, v in w.items() if k != "id"} for w in out
            if w["id"] not in ended and task_of.get(w["id"]) not in ended][-3:]


def agent_processes(agent, proc="/proc"):
    """Up to eight local processes a reboot would kill for this agent: its descendants, and (Claude) anything started
    with its CLAUDE_CODE_SESSION_ID, which detached jobs keep. MCP servers, the CLI itself and the turn in progress
    are left out. Claude's Bash tool now ends background commands with `&& pwd -P >| ...` like foreground ones, so a
    tool shell is the turn's only while the agent is mid-turn, and then only the newest one."""
    pid = agent.get("pid")
    busy = agent.get("state") == "busy"
    kids = {}
    try:
        pids = [int(p) for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return []
    parent, started = {}, {}
    for p in pids:
        try:
            with open("%s/%d/stat" % (proc, p)) as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            parent[p] = int(fields[1])
            started[p] = int(fields[19]) if len(fields) > 19 else 0
        except (OSError, ValueError, IndexError):
            continue
    def descends(p):
        seen = 0
        while p and seen < 64:
            if p == pid:
                return True
            p = parent.get(p)
            seen += 1
        return False
    def under(p, group):
        seen = 0
        while p and seen < 64:
            if p in group:
                return True
            p = parent.get(p)
            seen += 1
        return False
    marker = ("CLAUDE_CODE_SESSION_ID=%s" % agent["id"]).encode()
    raws, turn, shells = {}, set(), []
    for p in pids:
        if p == pid:
            continue
        mine = bool(pid) and descends(p)
        if not mine and agent["cli"] == "claude":
            try:
                with open("%s/%d/environ" % (proc, p), "rb") as fh:
                    mine = marker in fh.read()
            except OSError:
                pass
        if not mine:
            continue
        raw = " ".join(sources.cmdline(p, proc))
        if "&& pwd -P >|" in raw:
            shells.append(p)                 # a Bash tool call: the turn's or a background job, decided below
            raws[p] = raw
        elif parent.get(p) == pid and TOOL_SEARCH.match(raw):
            if busy:
                turn.add(p)                  # the Grep tool's search: part of the turn
        elif raw and not NOT_WORK.search(raw):
            raws[p] = raw
    if busy and shells:                      # the turn in progress runs its newest tool shell
        newest = max(shells, key=lambda q: (started.get(q, 0), q))
        turn.add(newest)
        raws.pop(newest, None)
    for p, raw in raws.items():
        if not under(p, turn):
            kids[p] = show_cmd(raw)[:200]
    # the outermost of a chain is enough (a shell's child repeats it), unless it only wraps what it runs
    children = {}
    for p in kids:
        children.setdefault(parent.get(p), []).append(p)
    out = []
    for p in sorted(q for q in kids if parent.get(q) not in kids):
        while WRAPPER.match(kids[p]) and len(children.get(p, [])) == 1:
            p = children[p][0]
        out.append({"pid": p, "cmd": kids[p]})
    return out[:8]


def default_choice(agent):
    return "nudge" if agent["state"] in ("busy", "background") else "quiet"


def inspect_agent(a):
    """What a restart records about a live agent beyond its board row: {model, effort, waits, processes}. Reads its
    login's settings and transcript (sources.model_effort, background_waits) and /proc (agent_processes)."""
    model, effort = sources.model_effort(a)
    return {"model": model, "effort": effort, "waits": background_waits(a) if a["state"] == "background" else [],
            "processes": agent_processes(a)}


def plan(collector=None, include_updates=True, inspect=inspect_agent):
    """Everything the confirmation panel shows: {created, updates, agents, warnings, status}. Read-only. collector
    gives the board's agent rows (collect.Collector when None); inspect(row) adds what inspect_agent returns;
    include_updates runs collect.updates_info."""
    from . import collect
    col = collector or collect.Collector()
    rows, _, _ = col.agents(with_git=False)
    agents = []
    for a in rows:
        if not a["live"]:
            continue
        agents.append(dict({
            "id": a["id"], "cli": a["cli"], "name": a.get("name") or "", "display": a["display"], "state": a["state"],
            "reason": a["reason"], "tmux": a.get("tmux") or "", "cwd": a["cwd"], "kind": a.get("kind") or "",
            "config_dir": a.get("config_dir") or "", "account": a.get("account") or "",
            "last_activity": a.get("last_activity"), "last_note": a.get("last_note"), "choice": default_choice(a)},
            **inspect(a)))
    agents.sort(key=lambda x: (classify.STATES.index(x["state"]) if x["state"] in classify.STATES else 99, x["display"]))
    upd = collect.updates_info() if include_updates else {}
    warnings = []
    busy = [a["display"] for a in agents if a["state"] == "busy"]
    if busy:
        warnings.append("%d agent(s) mid-turn: %s. They get a heads-up and time to stop." % (len(busy), ", ".join(busy)))
    with_procs = [a["display"] for a in agents if a["processes"]]
    if with_procs:
        warnings.append("%d agent(s) have local processes a reboot will kill (listed under each): %s" % (
            len(with_procs), ", ".join(with_procs)))
    if any(a["cli"] == "codex" for a in agents):
        warnings.append("Codex agents are restored last: if the desktop's keyring is locked after boot they can stall at "
                        "\"Resuming\"; logging in to the desktop once fixes it.")
    if upd and not upd.get("gpu_ok") and upd.get("gpu_message"):
        warnings.append("GPU unusable now (%s); a reboot loads the matching driver." % upd["gpu_message"])
    return {"created": now(), "updates": upd, "agents": agents, "warnings": warnings, "status": status()}


# ------------------------------------------------------------------ run
def start_run(update, wait_minutes, choices, dry_run=False, launcher=None, collector=None, inspect=inspect_agent):
    """Write the manifest and start the detached worker. choices: {id or name: nudge|quiet|skip}; collector and
    inspect go to plan."""
    st = status()
    if in_progress(st):
        return {"ok": False, "error": "a restart is already in progress (%s: %s)" % (st["status"], st.get("message", ""))}
    if not isinstance(wait_minutes, int) or not 0 <= wait_minutes <= 60:
        return {"ok": False, "error": "wait must be 0 to 60 minutes"}
    p = plan(collector=collector, include_updates=False, inspect=inspect)
    gone = []
    for key, choice in (choices or {}).items():
        if choice not in CHOICES:
            return {"ok": False, "error": "choice for %s must be one of %s" % (key, ", ".join(CHOICES))}
        hits = [a for a in p["agents"] if key in (a["id"], a["name"], a["display"])]
        if not hits:
            gone.append(key)       # stopped since the panel was opened (or between turns): nothing to restart or resume
        for a in hits:
            a["choice"] = choice
    m = {"version": 1, "created": now(), "status": "pending", "update": bool(update), "wait_minutes": wait_minutes,
         "agents": p["agents"], "results": []}
    note = (" (%d agent(s) in the list are no longer running, so they are left out)" % len(gone)) if gone else ""
    if dry_run:
        return {"ok": True, "dry_run": True, "manifest": m, "steps": steps(m), "gone": gone}
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        CANCEL.unlink()
    except OSError:
        pass
    write_json(MANIFEST, m)
    write_json(STATE / ("manifest-%d.json" % int(m["created"])), m)
    set_status("pending", "starting the restart")
    err = (launcher or launch_worker)()
    if err:
        set_status("failed", err)
        return {"ok": False, "error": err}
    return {"ok": True, "output": "Restart started: %s%s" % ("; ".join(steps(m)), note)}


def steps(m):
    n = {c: sum(1 for a in m["agents"] if a["choice"] == c) for c in CHOICES}
    busy = sum(1 for a in m["agents"] if a["state"] == "busy" and a["choice"] != "skip")
    out = []
    if busy and m["wait_minutes"]:
        out.append("give %d busy agent(s) up to %d min to stop" % (busy, m["wait_minutes"]))
    if m["update"]:
        out.append("apt-get update + full-upgrade, snap refresh")
    out.append("reboot")
    out.append("resume %d agent(s) (%d nudged, %d quietly), %d not resumed" % (
        n["nudge"] + n["quiet"], n["nudge"], n["quiet"], n["skip"]))
    return out


def launch_worker():
    """systemd-run --user: the worker lives in the user manager, not in the SSH session that asked (the reboot ends
    that), and it has no SSH_CONNECTION, so agent-mail --unrouted can mail the owner."""
    exe = str(sources.BOARD_BIN / "agent-restart")
    code, out, err = sources.run(sources.detached_argv("agent-restart-run-%d" % int(now()), [sys.executable, exe, "worker"],
                                                       {}, ()), timeout=30)
    return None if code == 0 else "could not start the worker: %s" % (err or out).strip()[-300:]


def cancel():
    """Ask the worker to stop before it updates or reboots: touches CANCEL and logs the request. {ok, output}, or
    {ok: False, error} when the status is not cancellable."""
    st = status()
    if not st.get("cancellable"):
        return {"ok": False, "error": "nothing to cancel (status %s)" % (st.get("status") or "idle")}
    CANCEL.touch()
    log("cancel requested (status %s)" % st["status"])
    return {"ok": True, "output": "cancel requested; the restart stops before updating or rebooting"}


def cancelled():
    return CANCEL.exists()


def mail(subject, body):
    """Email the owner (mailroom config owner_email; sources.mail_owner), and log whether it went. An ordinary notice,
    of a restart the owner said yes to, whose progress the board's Desktop chip shows: no alert kind, so the mailroom
    may keep it for the Mail tab alone."""
    sent, why = sources.mail_owner(subject, body, alert=None)
    log("mail '%s': %s" % (subject, "sent" if sent else "NOT SENT: " + why))


def worker(run=sources.run, sleep=time.sleep, collector=None):
    """The detached half of run, from MANIFEST: heads-up to busy agents (agent-send) and wait, the updates, the
    reboot. run (as sources.run) carries the apt, snap and reboot commands, all under sudo -n. Returns 0 once the
    reboot command is accepted or the restart is cancelled, else non-zero with a "failed" status. Writes STATUS,
    MANIFEST and LOG, and mails the owner when an apt step or the reboot command fails and just before it reboots. A
    damaged MANIFEST raises (read_json)."""
    from . import collect
    m = read_json(MANIFEST)
    if not m or m.get("status") != "pending":
        set_status("failed", "no pending manifest")
        return 1
    why = " for system updates" if m["update"] else ""
    col = collector or collect.Collector()
    # 1. heads-up to agents that are mid-turn now, then wait for them to finish
    busy_now = lambda: {a["id"] for a in col.agents(with_git=False)[0] if a["live"] and a["state"] == "busy"}
    targets = {a["id"]: a for a in m["agents"] if a["choice"] != "skip"}
    waiting_for = busy_now() & set(targets)
    if waiting_for and m["wait_minutes"]:
        for sid in waiting_for:
            a = targets[sid]
            try:
                code, out = sources.run_merged(sources.agent_send_argv(a["cli"], sid),
                                               stdin=actions.header() + HEADS_UP.format(minutes=m["wait_minutes"],
                                                                                        why=why), timeout=45)
            except RuntimeError as e:
                code, out = 127, str(e)
            log("heads-up to %s: %s" % (a["display"], "sent" if code == 0 else "FAILED (%d): %s" % (code, out[-200:])))
        deadline = now() + 60 * m["wait_minutes"]
        while now() < deadline:
            if cancelled():
                set_status("cancelled", "restart cancelled while waiting; nothing was updated or rebooted")
                m["status"] = "cancelled"
                write_json(MANIFEST, m)
                return 0
            left = busy_now() & waiting_for
            if not left:
                break
            set_status("waiting", "waiting for %d busy agent(s) to stop (%d min left): %s" % (
                len(left), int((deadline - now()) / 60) + 1, ", ".join(targets[s]["display"] for s in left)))
            sleep(15)
    if cancelled():
        set_status("cancelled", "restart cancelled; nothing was updated or rebooted")
        m["status"] = "cancelled"
        write_json(MANIFEST, m)
        return 0
    # the last cancel check: CANCELLABLE lists the statuses set before it, so the two change together
    # 2. updates
    snap_failed = ""
    if m["update"]:
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", PATH=sources.LOGIN_PATH)
        lock = ["-o", "DPkg::Lock::Timeout=900"]       # unattended-upgrades may hold the lock: wait for it, don't fail
        for label, cmd in (("apt-get update", ["sudo", "-n", "apt-get"] + lock + ["update"]),
                           ("apt-get full-upgrade", ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-y"] + lock +
                                                    ["-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
                                                     "full-upgrade"])):
            set_status("updating", label)
            code, so, se = run(cmd, timeout=3600, env=env)
            log("%s exit %d\n%s\n%s" % (label, code, so[-4000:], se[-2000:]))
            if code != 0:
                set_status("failed", "%s failed (exit %d); not rebooting" % (label, code))
                m["status"] = "failed"
                write_json(MANIFEST, m)
                mail("Desktop restart stopped: %s failed" % label,
                     "The desktop restart stopped before rebooting: %s exited %d.\nLast lines:\n%s\n\nNo agent was touched."
                     % (label, code, (se or so)[-1500:]))
                return 1
        set_status("updating", "snap refresh")
        code, so, se = run(["sudo", "-n", "snap", "refresh"], timeout=1800, env=env)
        log("snap refresh exit %d\n%s\n%s" % (code, so[-2000:], se[-1000:]))
        if code != 0:               # snaps refresh again on their own: the restart goes on, and the owner is told
            snap_failed = "snap refresh failed (exit %d): %s" % (code, " ".join((se or so).split())[-300:])
    # 3. last snapshot of activity times (quiet agents keep them), then reboot
    rows = {a["id"]: a for a in col.agents(with_git=False)[0]}
    for a in m["agents"]:
        r = rows.get(a["id"])
        if r:
            a["last_activity"] = r.get("last_activity") or a["last_activity"]
    m["status"] = "rebooting"
    m["rebooting_at"] = now()
    write_json(MANIFEST, m)
    set_status("rebooting", "rebooting now; agents come back after boot" + ("; " + snap_failed if snap_failed else ""))
    mail("Desktop restarting now", "The desktop is restarting%s. %s\n%sYou'll get a summary when the agents are back." % (
        why, "; ".join(steps(m)), snap_failed + "\n" if snap_failed else ""))
    sleep(3)
    code, so, se = run(["sudo", "-n", "systemctl", "reboot"], timeout=60)
    if code != 0:
        failed = "reboot command failed (exit %d): %s" % (code, " ".join((se or so).split())[-300:])
        set_status("failed", failed + "; not rebooting")
        m["status"] = "failed"          # restore must not act on this manifest at some later boot
        write_json(MANIFEST, m)
        mail("Desktop restart failed: the reboot command failed",
             "The desktop did not restart: %s.\nThe agents are still running. No later boot resumes them from this "
             "restart, so start a new one once the cause is fixed." % failed)
    return code


# ------------------------------------------------------------------ restore
def launch_line(a, inherited=()):
    """Resume exactly as it ran: its own login (no re-placement), model and effort (a per-token login's own model and
    settings: relaunch.login_flags). inherited: the tmux server's variable names (relaunch.resume_command)."""
    return relaunch.resume_command(a["cli"], a["id"], a["cwd"], a.get("config_dir") or None, a.get("model"),
                                   a.get("effort"), a.get("name") or a["display"], inherited)


def nudge_text(a, at, tmux, why):
    note = a.get("last_note") or {}
    waits = ""
    if a.get("waits"):
        waits = ("\nYou were waiting on: " + "; ".join("'%s'" % (w.get("what") or w.get("command", "")[:80]) for w in a["waits"])
                 + ". The restart killed those background commands; whatever they watched (a remote pod, a job) may still "
                 "be running, so check it and start the wait again if you still need it.")
    procs = ""
    if a.get("processes"):
        procs = ("\nThese local processes of yours were killed: " + "; ".join(p["cmd"][:100] for p in a["processes"][:4])
                 + ". Restart any you still need.")
    state = {"busy": "in the middle of a turn", "background": "waiting on background work"}.get(a["state"], a["state"])
    return actions.header() + NUDGE.format(
        at=time.strftime("%H:%M", time.localtime(at)), why=why, tmux=tmux, state=state,
        note=(" (your last note: %s)" % note["text"][:300]) if note.get("text") else "", waits=waits, procs=procs)


def restore_one(a, why=""):
    if a["choice"] == "skip":
        return {"id": a["id"], "name": a["display"], "result": "not resumed (your choice)"}
    if not a.get("cwd") or not os.path.isdir(a["cwd"]):
        return {"id": a["id"], "name": a["display"], "result": "failed: its folder is gone", "ok": False}
    if sources.is_live(a):
        return {"id": a["id"], "name": a["display"], "result": "already running", "ok": True}
    name = a.get("tmux") or relaunch.tmux_name_for(a, set())
    if name in sources.tmux_sessions():
        name = relaunch.tmux_name_for(dict(a, name=name), set(sources.tmux_sessions()))
    wait = RESTORE_WAIT + (30 if a["cli"] == "codex" else 0)
    line = launch_line(a, relaunch.tmux_env_names(sources.find_bin("tmux"), sources.run_merged) if a["cli"] == "claude"
                       else ())
    up, memory, err, note = relaunch.launch_in_tmux(a, line, name, run=sources.run_merged, live=sources.is_live,
                                                    wait=wait)
    res = {"id": a["id"], "name": a["display"], "tmux": name, "ok": up, "choice": a["choice"]}
    note = "; " + note if note else ""
    if err or not up:
        res["result"] = "failed: " + (err or "not live after %ds (open tmux '%s' to see why)%s" % (wait, name, note))
        return res
    res["result"] = "resumed" + (" and nudged" if a["choice"] == "nudge" else " quietly")
    if a["choice"] == "nudge":
        time.sleep(2)
        try:
            code, out = sources.run_merged(sources.agent_send_argv(a["cli"], a["id"]),
                                           stdin=nudge_text(a, now(), name, why), timeout=45)
        except RuntimeError as e:
            code, out = 127, str(e)
        if code != 0:
            res["result"] = "resumed; nudge FAILED (%s)" % (out[-200:] or "agent-send exit %d" % code)
    res["result"] += note
    return res


def wait_for_network(timeout=NETWORK_WAIT, sleep=time.sleep):
    """True once api.anthropic.com resolves (getent), False after `timeout` seconds of trying."""
    t0 = now()
    while now() - t0 < timeout:
        if sources.run(["getent", "hosts", "api.anthropic.com"], timeout=10)[0] == 0:
            return True
        sleep(5)
    return False


def restore(force=False, resume=None, sleep=time.sleep, parallel=4, network=wait_for_network):
    """At boot: resume what the manifest says, once, and return 0, or 2 when an agent failed. `force` restores without
    a reboot or the network wait (the test cycle). resume(agent) defaults to restore_one; network(timeout, sleep) to
    wait_for_network, and when it gives up the restore goes on and says so in LOG and the summary mail. Without
    `force`, a boot whose time is unknown (sources.boot_time) restores nothing, says so in LOG and returns 1. Writes
    STATUS, MANIFEST and LOG, and mails the owner the summary (not for a test cycle). A damaged MANIFEST raises
    (read_json)."""
    m = read_json(MANIFEST)
    if not m or m.get("status") != "rebooting":
        return 0
    boot = sources.boot_time()
    if not force and boot is None:
        log("not restoring: this boot's time is unknown (no btime in /proc/stat), so whether the restart's reboot "
            "has happened cannot be told")
        return 1
    if not force and boot < (m.get("rebooting_at") or 0):
        # this boot is older than the restart request: not ours to act on yet
        log("not restoring: this boot (%d) predates the restart request (%d)" % (boot, m["rebooting_at"]))
        return 0
    tag = {"test": True} if m.get("test") else {}      # a test cycle: no chip on the board, no mail
    set_status("restoring", "bringing %d agents back" % sum(1 for a in m["agents"] if a["choice"] != "skip"), **tag)
    m["status"] = "restoring"
    write_json(MANIFEST, m)
    offline = ""
    if not force and not network(timeout=NETWORK_WAIT, sleep=sleep):
        offline = "network not up after %d s; restoring anyway" % NETWORK_WAIT
        log(offline)
    why = " for system updates" if m.get("update") else ""
    one = resume or (lambda a: restore_one(a, why=why))
    results, lock = [], threading.Lock()
    claude = [a for a in m["agents"] if a["cli"] == "claude"]
    codex = [a for a in m["agents"] if a["cli"] != "claude"]
    for batch in (claude, codex):                # Codex last: a locked keyring can stall it
        queue = list(batch)
        def work():
            while True:
                with lock:
                    if not queue:
                        return
                    a = queue.pop(0)
                try:
                    r = one(a)
                except Exception as e:  # one agent must never stop the rest
                    r = {"id": a["id"], "name": a["display"], "ok": False, "result": "failed: %s: %s" % (type(e).__name__, e)}
                with lock:
                    results.append(r)
                    set_status("restoring", "%d of %d done" % (len(results), len(m["agents"])), **tag)
        threads = [threading.Thread(target=work) for _ in range(min(parallel, len(queue)) or 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    m["status"], m["restored_at"], m["results"] = "done", now(), results
    write_json(MANIFEST, m)
    ok = [r for r in results if r.get("ok")]
    bad = [r for r in results if r.get("ok") is False]
    set_status("done", "restored %d of %d agents%s" % (len(ok), len(results), (", %d failed" % len(bad)) if bad else ""),
               restored_at=m["restored_at"], **tag)
    if not tag:
        mail("Desktop is back: %d of %d agents restored" % (len(ok), len(results)),
             "The desktop restarted%s and the agents are back.\n\n%s" % (why, offline + "\n\n" if offline else "") + "\n".join(
                 "- %s: %s" % (r["name"], r["result"]) for r in sorted(results, key=lambda r: (r.get("ok") is not False, r["name"]))))
    return 0 if not bad else 2


def restored_activity():
    """{session id: (restored_at, last activity before the restart)} for agents quietly resumed in the last
    RESTORED_DAYS, so the board keeps their colour: a resume is not activity. A damaged MANIFEST raises
    (read_json)."""
    m = read_json(MANIFEST)
    if not m or m.get("status") != "done" or now() - (m.get("restored_at") or 0) > RESTORED_DAYS * 86400:
        return {}
    before = {a["id"]: a.get("last_activity") or 0 for a in m.get("agents", [])}
    return {r["id"]: (m["restored_at"], before.get(r["id"], 0)) for r in m.get("results", [])
            if r.get("ok") and r.get("choice") == "quiet"}


def test_cycle(names, choices=None):
    """The restore path for real, without a reboot: record these agents, kill their tmux sessions (as a reboot
    would), then restore them. For testing on scratch agents only."""
    if in_progress(status()):
        print("a restart is in progress; not testing")
        return 1
    p = plan(include_updates=False)
    agents = [a for a in p["agents"] if a["display"] in names or a["name"] in names]
    if len(agents) != len(names):
        print("not all found: %s" % ", ".join(sorted(set(names) - {a["display"] for a in agents})))
        return 1
    for a in agents:
        a["choice"] = (choices or {}).get(a["display"], (choices or {}).get(a["name"], a["choice"]))
        if a["choice"] not in CHOICES:
            print("bad choice for %s: %s" % (a["display"], a["choice"]))
            return 1
    m = {"version": 1, "created": now(), "status": "rebooting", "rebooting_at": now(), "update": False,
         "wait_minutes": 0, "agents": agents, "results": [], "test": True}
    write_json(MANIFEST, m)
    tmux = sources.find_bin("tmux")
    for a in agents:
        if a.get("tmux"):
            subprocess.run([tmux, "kill-session", "-t", "=" + a["tmux"]], capture_output=True)
    t0 = now()
    while now() - t0 < 30 and any(sources.is_live(a) for a in agents):        # 30 s for them to exit
        time.sleep(1)
    print("killed: %s" % ", ".join("%s (%s)" % (a["display"], "gone" if not sources.is_live(a) else "STILL LIVE") for a in agents))
    code = restore(force=True)
    for r in read_json(MANIFEST)["results"]:
        print("  %s: %s" % (r["name"], r["result"]))
    return code
