"""Restart or update-and-restart the desktop without losing the agents.

  plan     what a restart would do: pending updates, and every live agent with its state, last note, what it is
           waiting on (background commands from its transcript) and the local processes a reboot would kill
  run      write the manifest, then hand over to a detached worker (it must outlive the SSH connection that asked)
  worker   heads-up to busy agents and wait (up to N min) for them to finish their turn; optional apt full-upgrade
           and snap refresh (abort on failure, no reboot); email the owner; reboot
  restore  at boot (a systemd user unit): bring every agent back in its tmux session with the same conversation,
           login, model and effort. Quiet agents come back idle; agents that were working get one note saying what
           they were doing and waiting on. Codex last (a locked keyring can stall it). Email a summary.

Nothing here runs a model. The manifest lives in STATE; `status.json` is what the board shows while it runs.
Mail goes to the mailroom config's owner_email (agent-mail send --unrouted); without one, nothing is mailed.
"""
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import actions, classify, config, sources

STATE = sources.HOME / ".local/state/agent-board/restart"
MANIFEST = STATE / "manifest.json"
STATUS = STATE / "status.json"
LOG = STATE / "run.log"
CANCEL = STATE / "cancel"
CHOICES = ("nudge", "quiet", "skip")
ACTIVE = ("pending", "waiting", "updating", "rebooting", "restoring")
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
    STATE.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as fh:
        fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def set_status(status, message, **extra):
    write_json(STATUS, dict(extra, status=status, message=message, updated=now()))
    log("%s: %s" % (status, message))


def status():
    return read_json(STATUS, {}) or {}


def boot_time():
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return 0.0


# ------------------------------------------------------------------ what is pending
def _run(cmd, timeout=60, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def _kver(name):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", name)]


def updates_info(run=_run):
    """Pending apt and snap updates, whether a reboot is needed, the running and newest kernels, the GPU."""
    out = {"checked": now()}
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


# ------------------------------------------------------------------ what each agent is doing
def _cmdline(pid, proc="/proc"):
    try:
        with open("%s/%d/cmdline" % (proc, int(pid)), "rb") as fh:
            return [a.decode("utf-8", "replace") for a in fh.read().split(b"\x00") if a]
    except (OSError, ValueError, TypeError):
        return []


def model_effort(agent):
    """(model, effort) as the agent runs now: its process arguments first, else its transcript / thread."""
    argv = _cmdline(agent.get("pid")) if agent.get("pid") else []
    model = effort = None
    for i, a in enumerate(argv):
        if a in ("--model", "-m") and i + 1 < len(argv):
            model = argv[i + 1]
        elif a == "--effort" and i + 1 < len(argv):
            effort = argv[i + 1]
        elif a.startswith("model_reasoning_effort="):
            effort = a.split("=", 1)[1].strip("'\"")
    if agent["cli"] == "codex" and not (model and effort):
        m, e = actions.codex_settings(agent["id"])
        model, effort = model or m, effort or e
    if agent["cli"] == "claude" and not model:
        _, home = actions.claude_home_of(agent["id"])
        model = actions.last_claude_model(home, agent["id"]) if home else None
    return model, effort


NOTIFICATION = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
BG_STARTED = re.compile(r"Command running in background with ID: (\w+)")
TASK_STOPS = {"TaskStop": "task_id", "KillShell": "shell_id", "KillBash": "shell_id"}   # tool: its task-id argument
BG_KEYS = ('"run_in_background"', "task-notification", "running in background with ID", '"TaskStop"', '"KillShell"',
           '"KillBash"')


def _epoch(iso):
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def background_waits(agent, since=24 * 3600, limit=3, now=None):
    """What a Claude agent said it was waiting for: the descriptions of its recent `run_in_background` commands
    that are still running. One that ended has a `<task-notification>` with a status (completed, failed, killed)
    or a TaskStop of its task id (the tool result that started it gives that id); one from before this process
    started (a resume) or older than `since` isn't a wait now."""
    if agent["cli"] != "claude":
        return []
    _, home = actions.claude_home_of(agent["id"])
    if not home:
        return []
    after = max((now or time.time()) - since, agent.get("started_at") or 0)
    out, task_of, ended = [], {}, set()         # ended: tool-use ids and task ids
    for path in Path(home).glob("projects/*/%s.jsonl" % agent["id"]):
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, os.path.getsize(path) - 3_000_000))
                lines = fh.read().decode("utf-8", "replace").splitlines()
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
            if w["id"] not in ended and task_of.get(w["id"]) not in ended][-limit:]


def agent_processes(agent, proc="/proc", limit=8):
    """Local processes a reboot would kill for this agent: its descendants, and (Claude) anything started with its
    CLAUDE_CODE_SESSION_ID, which detached jobs keep. MCP servers, the CLI itself and the turn in progress are left
    out. Claude's Bash tool now ends background commands with `&& pwd -P >| ...` like foreground ones, so a tool
    shell is the turn's only while the agent is mid-turn, and then only the newest one."""
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
        raw = " ".join(_cmdline(p, proc))
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
    return out[:limit]


def default_choice(agent):
    return "nudge" if agent["state"] in ("busy", "background") else "quiet"


def plan(collector=None, run=_run, include_updates=True):
    """Everything the confirmation panel shows. Read-only."""
    from . import collect
    col = collector or collect.Collector()
    rows, _, _ = col.agents(with_git=False)
    agents = []
    for a in rows:
        if not a["live"]:
            continue
        model, effort = model_effort(a)
        waits = background_waits(a) if a["state"] == "background" else []
        agents.append({
            "id": a["id"], "cli": a["cli"], "name": a.get("name") or "", "display": a["display"], "state": a["state"],
            "reason": a["reason"], "tmux": a.get("tmux") or "", "cwd": a["cwd"], "kind": a.get("kind") or "",
            "config_dir": a.get("config_dir") or "", "account": a.get("account") or "", "model": model, "effort": effort,
            "last_activity": a.get("last_activity"), "last_note": a.get("last_note"), "waits": waits,
            "processes": agent_processes(a), "choice": default_choice(a)})
    agents.sort(key=lambda x: (classify.STATES.index(x["state"]) if x["state"] in classify.STATES else 99, x["display"]))
    upd = updates_info(run) if include_updates else {}
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
def start_run(update, wait_minutes, choices, dry_run=False, launcher=None, collector=None):
    """Write the manifest and start the detached worker. choices: {id or name: nudge|quiet|skip}."""
    st = status()
    if st.get("status") in ACTIVE and now() - (st.get("updated") or 0) < 6 * 3600:
        return {"ok": False, "error": "a restart is already in progress (%s: %s)" % (st["status"], st.get("message", ""))}
    if not isinstance(wait_minutes, int) or not 0 <= wait_minutes <= 60:
        return {"ok": False, "error": "wait must be 0 to 60 minutes"}
    p = plan(collector=collector, include_updates=False)
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
    exe = str(Path(__file__).resolve().parent.parent / "bin" / "agent-restart")
    code, out, err = _run(["systemd-run", "--user", "--collect", "--unit", "agent-restart-run-%d" % int(now()),
                           "--setenv=PATH=%s" % sources_path(), sys.executable, exe, "worker"], timeout=30)
    return None if code == 0 else "could not start the worker: %s" % (err or out).strip()[-300:]


def sources_path():
    home = str(sources.HOME)
    return ":".join([home + "/.local/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/snap/bin"])


def cancel():
    st = status()
    if st.get("status") not in ("pending", "waiting"):
        return {"ok": False, "error": "nothing to cancel (status %s)" % (st.get("status") or "idle")}
    CANCEL.touch()
    return {"ok": True, "output": "cancel requested; the restart stops before updating or rebooting"}


def cancelled():
    return CANCEL.exists()


def mail(subject, body):
    """One mail to the owner (mailroom config owner_email) through agent-mail; logged and skipped without one."""
    to = config.owner_email()
    if not to:
        log("no owner_email in %s; not mailing: %s" % (config.MAIL_CONFIG, subject))
        return
    exe = sources.find_bin("agent-mail")
    if not exe:
        log("agent-mail not found; not mailing: %s" % subject)
        return
    env = {k: v for k, v in os.environ.items() if not k.startswith("SSH_")}
    env["PATH"] = sources_path()
    try:
        subprocess.run([exe, "send", "--unrouted", "--to", to, "--subject", subject], input=body, text=True,
                       capture_output=True, timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        log("mail failed: %s" % e)


def worker(reboot=None, run=_run, sleep=time.sleep, collector=None):
    """The detached half of run: heads-up and wait, update, reboot."""
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
        exe = sources.find_bin("agent-send")
        for sid in waiting_for:
            a = targets[sid]
            try:
                subprocess.run([exe, "--raw", "--cli", a["cli"], sid],
                               input=actions.header() + HEADS_UP.format(minutes=m["wait_minutes"], why=why),
                               text=True, capture_output=True, timeout=45)
            except (OSError, subprocess.TimeoutExpired):
                pass
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
    # 2. updates
    if m["update"]:
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", PATH=sources_path())
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
    # 3. last snapshot of activity times (quiet agents keep them), then reboot
    rows = {a["id"]: a for a in col.agents(with_git=False)[0]}
    for a in m["agents"]:
        r = rows.get(a["id"])
        if r:
            a["last_activity"] = r.get("last_activity") or a["last_activity"]
    m["status"] = "rebooting"
    m["rebooting_at"] = now()
    write_json(MANIFEST, m)
    set_status("rebooting", "rebooting now; agents come back after boot")
    mail("Desktop restarting now", "The desktop is restarting%s. %s\nYou'll get a summary when the agents are back." % (
        why, "; ".join(steps(m))))
    sleep(3)
    return (reboot or (lambda: run(["sudo", "-n", "systemctl", "reboot"], timeout=60)[0]))()


# ------------------------------------------------------------------ restore
def launch_line(a):
    """Resume exactly as it ran: its own login (no re-placement), model and effort."""
    sid, cwd = a["id"], a["cwd"]
    if a["cli"] == "claude":
        exe = sources.find_bin("claude") or "claude"
        cd = a.get("config_dir") or ""
        env = ("env %s -u CLAUDE_CONFIG_DIR" % actions.SCRUB_ENV if not cd or Path(cd).resolve() == sources.DEFAULT_CLAUDE_HOME.resolve()
               else "env %s CLAUDE_CONFIG_DIR=%s" % (actions.SCRUB_ENV, shlex.quote(cd)))
        parts = [env, shlex.quote(exe), "--resume", sid, "--dangerously-skip-permissions"]
        if a.get("model"):
            parts += ["--model", shlex.quote(a["model"])]
        if a.get("effort"):
            parts += ["--effort", shlex.quote(a["effort"])]
        parts += ["--name", shlex.quote(a.get("name") or a["display"])]
        return " ".join(parts)
    exe = sources.find_bin("codex") or "codex"
    parts = ["env", actions.SCRUB_ENV, shlex.quote(exe), "resume", "-s", "danger-full-access", "-a", "never", "-C", shlex.quote(cwd),
             actions.CODEX_NO_UPDATE_PROMPT]
    if a.get("model"):
        parts += ["-m", shlex.quote(a["model"])]
    if a.get("effort"):
        parts += ["-c", "model_reasoning_effort=%s" % shlex.quote(a["effort"])]
    return " ".join(parts + [sid])


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


def restore_one(a, run=actions._run, live=None, wait=60, why=""):
    if a["choice"] == "skip":
        return {"id": a["id"], "name": a["display"], "result": "not resumed (your choice)"}
    if not a.get("cwd") or not os.path.isdir(a["cwd"]):
        return {"id": a["id"], "name": a["display"], "result": "failed: its folder is gone", "ok": False}
    live = live or actions.is_live
    if live(a):
        return {"id": a["id"], "name": a["display"], "result": "already running", "ok": True}
    name = a.get("tmux") or actions.tmux_name_for(a, set())
    if name in sources.tmux_sessions():
        name = actions.tmux_name_for(dict(a, name=name), set(sources.tmux_sessions()))
    up, memory, err = actions.launch_in_tmux(a, launch_line(a), name, run=run, live=live,
                                             wait=wait + (30 if a["cli"] == "codex" else 0))
    res = {"id": a["id"], "name": a["display"], "tmux": name, "ok": up, "choice": a["choice"]}
    if err or not up:
        res["result"] = "failed: " + (err or "not live after %ds (open tmux '%s' to see why)" % (wait, name))
        return res
    res["result"] = "resumed" + (" and nudged" if a["choice"] == "nudge" else " quietly")
    if a["choice"] == "nudge":
        exe = sources.find_bin("agent-send")
        time.sleep(2)
        run([exe, "--raw", "--cli", a["cli"], a["id"]], stdin=nudge_text(a, now(), name, why), timeout=45)
    return res


def wait_for_network(timeout=180, sleep=time.sleep):
    t0 = now()
    while now() - t0 < timeout:
        if _run(["getent", "hosts", "api.anthropic.com"], timeout=10)[0] == 0:
            return True
        sleep(5)
    return False


def restore(force=False, resume=None, sleep=time.sleep, parallel=4):
    """At boot: resume what the manifest says, once. `force` restores without a reboot (the test cycle)."""
    m = read_json(MANIFEST)
    if not m or m.get("status") != "rebooting":
        return 0
    if not force and boot_time() < (m.get("rebooting_at") or 0):
        return 0                       # this boot is older than the restart request: not ours to act on yet
    tag = {"test": True} if m.get("test") else {}      # a test cycle: no chip on the board, no mail
    set_status("restoring", "bringing %d agents back" % sum(1 for a in m["agents"] if a["choice"] != "skip"), **tag)
    m["status"] = "restoring"
    write_json(MANIFEST, m)
    if not force:
        wait_for_network(sleep=sleep)
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
             "The desktop restarted%s and the agents are back.\n\n" % why + "\n".join(
                 "- %s: %s" % (r["name"], r["result"]) for r in sorted(results, key=lambda r: (r.get("ok") is not False, r["name"]))))
    return 0 if not bad else 2


def restored_activity(window=7 * 86400):
    """{session id: (restored_at, last activity before the restart)} for quietly resumed agents, so the board keeps
    their colour: a resume is not activity."""
    m = read_json(MANIFEST)
    if not m or m.get("status") != "done" or now() - (m.get("restored_at") or 0) > window:
        return {}
    before = {a["id"]: a.get("last_activity") or 0 for a in m.get("agents", [])}
    return {r["id"]: (m["restored_at"], before.get(r["id"], 0)) for r in m.get("results", [])
            if r.get("ok") and r.get("choice") == "quiet"}


def test_cycle(names, choices=None, wait=30):
    """The restore path for real, without a reboot: record these agents, kill their tmux sessions (as a reboot
    would), then restore them. For testing on scratch agents only."""
    st = status()
    if st.get("status") in ACTIVE and now() - (st.get("updated") or 0) < 6 * 3600:
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
    while now() - t0 < wait and any(actions.is_live(a) for a in agents):
        time.sleep(1)
    print("killed: %s" % ", ".join("%s (%s)" % (a["display"], "gone" if not actions.is_live(a) else "STILL LIVE") for a in agents))
    code = restore(force=True)
    for r in read_json(MANIFEST)["results"]:
        print("  %s: %s" % (r["name"], r["result"]))
    return code
