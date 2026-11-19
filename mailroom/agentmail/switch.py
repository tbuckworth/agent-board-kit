"""Switch a live agent to another CLI, Claude login, model or effort: from the agent board, or by hand.

Claude Code -> Claude Code (another login, model or effort): the same conversation continues. It is agent-failover's
  move with new --model/--effort: SIGKILL, transcript copied to the other login if it changes, the same session id
  resumed in the same tmux pane.
Codex -> Codex (model or effort): the same thread continues: `codex resume ID -m MODEL -c model_reasoning_effort=E`
  in the same pane.
Claude Code <-> Codex: one CLI cannot continue the other's conversation, so this is a handoff. The new CLI starts in
  the same pane and folder with a brief pointing at a handoff file: the original brief and a condensed record of
  the conversation (messages and tool calls, no tool output). The mailroom's routing for the old session moves to
  the new one, so replies to mail the agent sent still reach it.

An idle agent switches at once. A busy one (mid-turn) is switched only with force: its turn is cut off, and it is
told it was interrupted and should carry on. Two kinds of agent can be switched: one in a tmux pane whose shell
started the CLI (agent-spawn's layout), and a Claude Code background session (claude --bg), which can change login,
model or effort but not CLI (agentmail/failover.py moves it with the daemon's own commands). Background commands
keep running and are listed in the note it resumes with (a background session's are stopped with it, and the note
says so).
"""
import json
import os
import re
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

from . import accounts, claude_session, commands, config, failover, jsonfile, launch, procs, registry, reviewgate, usage

MODEL_CACHE_SECONDS = 86400
HANDOFF_BYTES = 200 << 10
BUSY_MARK = "esc to interrupt"     # both TUIs show it while a turn runs


class SwitchError(Exception):
    pass


# ------------------------------------------------------------------ reading an agent
def effort_in_codex_argv(argv):
    """The reasoning effort a codex command line sets (-c model_reasoning_effort=E), or None."""
    for i, a in enumerate(argv):
        v = argv[i + 1] if a == "-c" and i + 1 < len(argv) else (a[3:] if a.startswith("-c=") else None)
        if v and v.startswith("model_reasoning_effort="):
            return v.split("=", 1)[1].strip("'\"")
    return None


def codex_pane(sess):
    """(pane, codex pid) for a Codex agent: its tmux session is named after it (agent-spawn), and the pane's shell
    is the parent of a codex process. (None, None) if not found."""
    name = sess.get("name") or ""
    if not name:
        return None, None
    r = commands.tmux("list-panes", "-t", "=" + name, "-F", "#{pane_id} #{pane_pid}", check=False)
    for line in (r.stdout or "").split("\n"):
        parts = line.split()
        if len(parts) != 2:
            continue
        for kid in procs.children(int(parts[1])):
            argv = procs.cmdline(kid)
            if argv and "codex" in os.path.basename(argv[0]):
                return parts[0], kid
    return None, None


def pane_busy(pane):
    text = commands.tmux("capture-pane", "-p", "-t", pane, check=False).stdout or ""
    return BUSY_MARK in text.lower()


def state(sess):
    """What the board needs to know about one live agent. Its model and effort are None when neither its command
    line nor its transcript says (shown as unknown)."""
    cli = sess["cli"]
    out = {"name": sess.get("name") or "", "id": sess["id"], "cli": cli, "account": sess.get("account"),
           "cwd": sess.get("cwd") or "", "status": sess.get("status") or "", "live": bool(sess.get("live"))}
    if cli == "claude":
        pane, why = claude_session.movable(sess)
        pid = int(sess["pid"]) if sess.get("pid") else None
        argv = procs.cmdline(pid) if pid else []
        flag = commands.flag_value
        path = None if flag(argv, "--model") and flag(argv, "--effort") else claude_session.transcript(sess)
        # A background session's process (a daemon's spare) names no flags: the transcript says what it runs.
        recorded = claude_session.transcript_flags(jsonfile.read_tail(path, 256 << 10)) if path else []
        out.update(model=flag(argv, "--model") or flag(recorded, "--model"),
                   effort=flag(argv, "--effort") or flag(recorded, "--effort"))
        busy = sess.get("status") == "busy" or bool(sess.get("waiting_for")) or \
            bool(pane and pane != claude_session.BG and pane_busy(pane))
        out["background_jobs"] = len(claude_session.background_jobs(pid)) if pid else 0
    else:
        pane, pid = codex_pane(sess)
        why = "" if pane else "no tmux pane named %s running codex (not agent-spawn's layout)" % (sess.get("name") or "?")
        argv = procs.cmdline(pid) if pid else []
        out.update(model=commands.flag_value(argv, "-m", "--model"), effort=effort_in_codex_argv(argv))
        busy = bool(pane and pane_busy(pane))
        out["background_jobs"] = 0
    out.update(pane=pane, pid=pid, movable=bool(pane), why_not=why, busy=busy)
    return out


def find(name, cfg):
    sess, code, cands = registry.resolve(name)
    if code == 3:
        raise SwitchError("%r is ambiguous: %s" % (name, ", ".join("%s %s" % (c["cli"], c["id"][:8]) for c in cands)))
    if code != 0 or not sess.get("live"):
        raise SwitchError("no live agent %r" % name)
    return sess


# ------------------------------------------------------------------ models
def claude_models(account, cfg, run=subprocess.run):
    """The model catalog Claude Code offers this login: [{"value", "label", "efforts"}]. From its own initialize
    answer (runs the claude CLI; no model call), cached for a day per login in STATE_DIR/models-cache.json.
    Raises SwitchError with the probe's error when claude cannot be run or names no model. A cache file that is
    not JSON raises (jsonfile.read); it is only a cache, so the operator deletes it."""
    cache_file = config.STATE_DIR / "models-cache.json"
    cache = jsonfile.read(cache_file)
    key = usage.claude_target(account or "default")
    hit = cache.get(key)
    if hit and time.time() - hit.get("at", 0) < MODEL_CACHE_SECONDS:
        return hit["models"]
    acct = accounts.account(usage.settings(cfg), account) if account else {}
    what = "the model catalog of Claude login %s" % (account or "default")
    try:
        resp = claude_session.claude_control(registry.claude_bin() or "claude", acct, {"subtype": "initialize"}, 60,
                                    str(config.STATE_DIR), run)
    except (OSError, subprocess.SubprocessError, claude_session.ControlError) as e:
        raise SwitchError("cannot read %s: %s: %s" % (what, type(e).__name__, e)) from e
    models = [{"value": m["value"], "label": m.get("displayName") or m["value"],
               "efforts": m.get("supportedEffortLevels") or []}
              for m in resp.get("models") or [] if m.get("value") and m.get("value") != "default"]
    if not models:
        raise SwitchError("cannot read %s: claude's initialize answer listed no models" % what)
    cache[key] = {"at": time.time(), "models": models}
    jsonfile.write(cache_file, cache, jsonfile.PLAIN, None)
    return models


def codex_models(path=None):
    """The models Codex lists (its own cache of the server's catalog): [{"value", "label", "efforts"}]; [] where
    Codex has no cache. One that is not JSON raises ValueError naming it (jsonfile.read), as an empty catalog
    would turn off the model check."""
    d = jsonfile.read(path or registry.CODEX_HOME / "models_cache.json")
    out = []
    for m in sorted(d.get("models") or [], key=lambda m: m.get("priority", 99)):
        if m.get("visibility") != "list":
            continue
        out.append({"value": m["slug"], "label": m.get("display_name") or m["slug"],
                    "efforts": [x.get("effort") for x in m.get("supported_reasoning_levels") or [] if x.get("effort")]})
    return out


# ------------------------------------------------------------------ options for the board
def target_of(st):
    return "codex" if st["cli"] == "codex" else usage.claude_target(st.get("account") or "default")


def options(name, cfg, snaps=None, models=None):
    """Everything the board's menu needs, for one agent."""
    sess = find(name, cfg)
    st = state(sess)
    s = usage.settings(cfg)
    snaps = usage.snapshots(cfg) if snaps is None else snaps
    current = target_of(st)
    targets = []
    for t in usage.targets(s):
        cli = "codex" if t == "codex" else "claude"
        acct = usage.account_of(t)
        # Headroom for the model the agent would run there: its own on the same CLI (a Fable agent needs Fable).
        model = st.get("model") if cli == st["cli"] else cfg[config.SPAWN_KEYS[cli]["model"]]
        a = usage.assess(snaps.get(t) or {"windows": [], "error": "not read"}, s, model)
        ready = True if cli == "codex" else accounts.account_ready(accounts.account(s, acct))[0]
        label = ("Codex" if cli == "codex" else "Claude Code %s" % acct)
        kind = "current" if t == current else ("handoff" if cli != st["cli"] else "resume")
        targets.append({"target": t, "label": label, "cli": cli, "account": acct, "current": t == current,
                        "kind": kind, "status": a["status"] if ready else "unavailable",
                        "usage": usage.summary(snaps.get(t) or {}, model), "available": ready})
    if models is None:
        models = {"claude": claude_models(st.get("account") if st["cli"] == "claude" else None, cfg),
                  "codex": codex_models()}
    return {"agent": {k: st[k] for k in ("name", "id", "cli", "account", "model", "effort", "status", "busy",
                                         "movable", "why_not", "background_jobs", "cwd")},
            "will_interrupt": st["busy"],
            "targets": targets, "models": models,
            "defaults": {cli: {what: cfg[key] for what, key in keys.items()}
                         for cli, keys in config.SPAWN_KEYS.items()}}


# ------------------------------------------------------------------ handoff record
def _clip(text, n=2000):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " [...%d more chars]" % (len(text) - n)


def _tool_line(name, inp):
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            return "%s: %s" % (name, _clip(inp, 300))
    if isinstance(inp, dict):
        for key in ("command", "cmd", "file_path", "path", "pattern", "url", "description", "prompt"):
            if inp.get(key):
                return "%s: %s" % (name, _clip(str(inp[key]), 300))
        return "%s: %s" % (name, _clip(json.dumps(inp), 300))
    return name


def condense_claude(path):
    """(original brief, [lines]) from a Claude Code transcript: user and assistant text, one line per tool call."""
    brief, lines = None, []
    with open(path, errors="replace") as fh:
        entries = fh.readlines()
    for raw in entries:
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("type") not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
            continue
        content = (d.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                if d["type"] == "user" and brief is None:
                    brief = b["text"].strip()
                    continue
                if d.get("isApiErrorMessage"):
                    lines.append("[limit] " + _clip(b["text"], 300))
                else:
                    lines.append("[%s] %s" % (d["type"], _clip(b["text"])))
            elif b.get("type") == "tool_use":
                lines.append("[tool] " + _tool_line(b.get("name", "tool"), b.get("input")))
    return brief or "", lines


SKIP_CODEX_USER = ("# AGENTS.md instructions", "<environment_context>", "<user_instructions>", "<permissions",
                   "<skills_instructions>", "<turn_aborted>")


def condense_codex(path):
    """(original brief, [lines]) from a Codex rollout: user and assistant messages, one line per tool call."""
    brief, lines = None, []
    with open(path, errors="replace") as fh:
        entries = fh.readlines()
    for raw in entries:
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("type") != "response_item":
            continue
        p = d.get("payload") or {}
        kind = p.get("type")
        if kind == "message" and p.get("role") in ("user", "assistant"):
            text = "\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)).strip()
            if not text or (p["role"] == "user" and text.startswith(SKIP_CODEX_USER)):
                continue
            if p["role"] == "user" and brief is None:
                brief = text
                continue
            lines.append("[%s] %s" % (p["role"], _clip(text)))
        elif kind in ("function_call", "custom_tool_call", "local_shell_call"):
            lines.append("[tool] " + _tool_line(p.get("name") or kind, p.get("arguments") or p.get("input") or p.get("action")))
    return brief or "", lines


HANDOFF_DIR = config.HOME / ".local" / "state" / "agent-switch"
HANDOFF_SUFFIX = "-handoff.md"
# where a brief names a handoff record: a *-handoff.md file in a folder named like HANDOFF_DIR
HANDOFF_REF = re.compile(r"(/\S+/%s/\S+%s)" % (re.escape(HANDOFF_DIR.name), re.escape(HANDOFF_SUFFIX)))


def carry_forward(brief, lines):
    """A session that itself began with a handoff has the handoff brief as its first message. Take the real
    original brief and the earlier conversation from that previous handoff record, so history survives any
    number of switches."""
    m = HANDOFF_REF.search(brief or "")
    if not m or not os.path.exists(m.group(1).rstrip(":.,")):
        return brief, lines
    ref = m.group(1).rstrip(":.,")
    prev = Path(ref).read_text(errors="replace")
    orig = re.search(r"## Original brief\n\n(.*?)\n\n## Conversation so far[^\n]*\n\n", prev, re.S)
    convo = re.search(r"## Conversation so far[^\n]*\n\n(.*?)(?:\n\n## Background commands|\Z)", prev, re.S)
    if not orig:
        return brief, lines
    earlier = [x for x in (convo.group(1).splitlines() if convo else []) if x.strip()]
    return orig.group(1), earlier + ["[switch] --- the agent was switched here (previous handoff record %s) ---" % ref] \
        + list(lines)


def handoff_text(st, src_desc, dst_desc, transcript_path, brief, lines, jobs, owner_name):
    head = ["# Handoff for agent %s" % (st["name"] or st["id"][:8]),
            "",
            "Switched by %s on %s from %s to %s." % (owner_name, time.strftime("%Y-%m-%d %H:%M"), src_desc, dst_desc),
            "Working directory: %s" % st["cwd"],
            "Previous transcript, full detail including tool output: %s" % transcript_path,
            "",
            "## Original brief",
            "",
            brief or "(not found)",
            "",
            "## Conversation so far (condensed: messages and tool calls, no tool output; oldest first)",
            ""]
    tail = []
    if jobs:
        tail = ["", "## Background commands still running from before the switch", ""] + [
            "- pid %d: %s%s" % (j["pid"], j["command"], (" (output: %s)" % j["output"]) if j["output"] else "")
            for j in jobs]
    body = list(lines)
    size = sum(len(x) + 1 for x in body)
    dropped = 0
    while body and size > HANDOFF_BYTES:   # keep the most recent part
        size -= len(body[0]) + 1
        body.pop(0)
        dropped += 1
    if dropped:
        body.insert(0, "[... %d earlier entries left out; see the full transcript ...]" % dropped)
    return "\n".join(head + body + tail) + "\n"


def write_handoff(name, text):
    return launch.write_private(HANDOFF_DIR, "%s-%d%s" % (re.sub(r"[^A-Za-z0-9._-]+", "-", name or "agent"),
                                                          int(time.time()), HANDOFF_SUFFIX), text)


def handoff_brief(st, handoff_path, src_desc, dst_desc, owner_name, preamble=""):
    return (preamble +
            "You are taking over agent %s, which was running as %s until %s switched it to you (%s). "
            "Work in %s. First read %s: it holds the original brief and a condensed record of the conversation "
            "so far, latest at the end, plus any background commands still running. Then carry on with the task "
            "from where it stopped. Check git status and the state of the work before redoing anything. If the "
            "brief says to email %s when done or blocked, that still applies."
            % (st["name"] or st["id"][:8], src_desc, owner_name, dst_desc, st["cwd"], handoff_path, owner_name))


# ------------------------------------------------------------------ acting
def describe(cli, account, model, effort):
    who = "Claude Code (%s login)" % account if cli == "claude" else "Codex"
    return "%s, %s%s" % (who, model or "default model", (" at effort %s" % effort) if effort else "")


def stop(pid):
    try:
        os.kill(pid, signal.SIGKILL)   # background commands keep running (see agentmail/failover.py)
    except ProcessLookupError:
        pass
    return claude_session.wait_dead(pid)


def relaunch_codex(st, model, effort, prompt, codex):
    """Same Codex thread, new model/effort (None: none given, so the CLI's own), same pane."""
    argv = procs.cmdline(st["pid"])
    keep = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-s", "--sandbox", "-a", "--ask-for-approval", "-C", "--cd") and i + 1 < len(argv):
            keep += [a, argv[i + 1]]
            i += 2
            continue
        i += 1
    if "-C" not in keep and "--cd" not in keep and st["cwd"]:
        keep += ["-C", st["cwd"]]
    cmd = [codex, "resume", st["id"]] + keep + registry.CODEX_NO_UPDATE_PROMPT + reviewgate.codex_hook_args(st["cwd"]) + \
        (["-m", model] if model else []) + (launch.codex_effort(effort) if effort else [])
    if prompt:
        cmd.append(prompt)
    if not stop(st["pid"]):
        return False, "codex pid %d did not exit after SIGKILL; nothing relaunched" % st["pid"]
    try:
        launch.type_line(st["pane"], launch.shell_line(None, cmd, None), None)
    except commands.CommandError as e:
        return False, "stopped codex pid %d, but relaunching it in pane %s failed: %s" % (st["pid"], st["pane"], e)
    # a lock file can outlive a killed codex, so wait for a new codex process in the pane, not for the lock

    def relaunched():
        pane, pid = codex_pane({"name": st["name"]})
        return pid if pid and pid != st["pid"] else None
    ok = launch.wait_for(relaunched, 60)
    return bool(ok), "resumed Codex thread %s with %s, effort %s" % (st["id"][:8], model, effort) if ok else \
        "relaunched codex but thread %s not live after 60 s" % st["id"][:8]


def start_claude(st, dst_dir, model, effort, brief, claude):
    """A new Claude session in the agent's pane (handoff), on the login whose config dir is dst_dir (None: the
    default), with brief as its first prompt: the brief is written to an owner-only file under launch.BRIEF_DIR
    (launch.write_brief), which launch.start_claude types into the pane. Returns its session id, or None if none
    showed up within 60 s. Raises commands.CommandError if tmux fails."""
    brief_path = launch.write_brief("%s-switch" % (st["name"] or "agent"), brief)
    launch.start_claude(st["pane"], claude, st["name"], dst_dir, model, effort, brief_path)

    def new_session():
        return next((s["id"] for s in registry.live_on(dst_dir) if s["id"] != st["id"] and s.get("name") == st["name"]),
                    None)
    return launch.wait_for(new_session, 60)


def start_codex(st, model, effort, brief, codex):
    """A new Codex thread in the agent's pane (handoff), named after the agent, with the brief queued
    (launch.start_codex). Returns (thread id, or None if none appeared; "" or why the brief was not queued; its notes
    on the start-up, a list). Raises commands.CommandError if tmux fails."""
    started = launch.start_codex(st["pane"], codex, st["cwd"] or ".", model, effort, brief, st["name"])
    return started["thread"], started.get("error", ""), started["notes"]


def switch(name, cfg, to=None, model=None, effort=None, force=False, dry_run=False, trust=None, preamble=None,
           rehome=None, claude=None, codex=None):
    """Switch one agent. Returns {"ok", "detail", "needs_force", "from", "to", "id"} (id: the agent's session before
    the switch; a handoff adds "new_id" and "handoff"). Raises SwitchError, before anything
    is stopped, for a request that cannot be done (unknown agent, not movable, unknown target or model, a handoff
    with no transcript or rollout to hand over, or a handoff to Codex in a worktree whose hooks Codex would not run,
    reviewgate.codex_hooks_gap)."""
    sess = find(name, cfg)
    st = state(sess)
    s = usage.settings(cfg)
    cur_cli, cur_acct = st["cli"], st.get("account")
    if to:
        try:
            dst_cli, dst_acct = usage.parse_target(to, s)
        except ValueError as e:
            raise SwitchError(str(e))
    else:
        dst_cli, dst_acct = cur_cli, cur_acct
    if dst_cli == "claude" and not accounts.account_ready(accounts.account(s, dst_acct))[0]:
        raise SwitchError("Claude login %s is not set up" % dst_acct)
    if not st["movable"]:
        raise SwitchError("cannot switch %s: %s" % (name, st["why_not"]))
    same_cli = dst_cli == cur_cli
    if st["pane"] == claude_session.BG and not same_cli:
        raise SwitchError("cannot hand %s over to %s: it is a background session (claude --bg), with no tmux pane "
                          "to start %s in; it can change Claude login, model or effort" % (name, dst_cli, dst_cli))
    gap = reviewgate.codex_hooks_gap(st["cwd"]) if dst_cli == "codex" and not same_cli and st["cwd"] else None
    if gap:
        raise SwitchError("cannot hand %s over to Codex in %s: %s. Bring the main checkout up to date, or leave the "
                          "agent on Claude" % (name, st["cwd"], gap))
    model = model or (st["model"] if same_cli else cfg[config.SPAWN_KEYS[dst_cli]["model"]])
    effort = effort or (st["effort"] if same_cli else cfg[config.SPAWN_KEYS[dst_cli]["effort"]])
    catalog = claude_models(dst_acct, cfg) if dst_cli == "claude" else codex_models()
    known = {m["value"]: m for m in catalog}
    if model and catalog and model not in known and not (dst_cli == "claude" and model.startswith("claude-")):
        raise SwitchError("unknown %s model %r (choose from %s)" % (dst_cli, model, ", ".join(known)))
    if model in known and not known[model]["efforts"]:
        effort = None   # a model without effort levels (Haiku): pass none
    efforts = (known.get(model) or {}).get("efforts") or (launch.EFFORTS if dst_cli == "claude" else [])
    if effort and efforts and effort not in efforts:
        raise SwitchError("%s does not take effort %r (choose from %s)" % (model, effort, ", ".join(efforts)))
    src = describe(cur_cli, cur_acct, st["model"] or "model unknown", st["effort"] or "unknown")
    dst = describe(dst_cli, dst_acct, model, effort)
    result = {"ok": False, "needs_force": False, "from": src, "to": dst, "detail": "", "id": st["id"]}
    if same_cli and dst_acct == cur_acct and model == st["model"] and effort == st["effort"]:
        result.update(ok=True, detail="already %s" % dst)
        return result
    if st["busy"] and not force:
        result.update(needs_force=True, detail="%s is mid-turn: switching will interrupt it (use --force)" % name)
        return result
    owner = cfg["owner_name"]
    note = ("[agent-switch] %s switched this session from %s to %s, interrupting your turn. Carry on from where "
            "you were." % (owner, src, dst)) if st["busy"] else None
    claude = claude or registry.claude_bin() or "claude"
    codex = codex or registry.codex_bin() or "codex"
    if same_cli and cur_cli == "claude":
        if dry_run:
            result.update(ok=True, detail="would resume the same conversation as %s" % dst)
            return result
        ok, detail = failover.migrate(sess, dst_acct, note, cfg, trust=trust, claude=claude,
                                      why="switched by " + owner, overrides={"--model": model, "--effort": effort})
        result.update(ok=ok, detail=detail)
    elif same_cli:
        if dry_run:
            result.update(ok=True, detail="would resume Codex thread %s with %s, effort %s" % (st["id"][:8], model, effort))
            return result
        ok, detail = relaunch_codex(st, model, effort, note, codex)
        result.update(ok=ok, detail=detail)
    else:
        if cur_cli == "claude":
            path = claude_session.transcript(sess)
            if not path:
                raise SwitchError("no transcript of %s under %s: nothing to hand over" % (
                    name, registry.claude_home(sess.get("config_dir"))))
            brief, lines = condense_claude(path)
            jobs = claude_session.background_jobs(st["pid"])
        else:
            try:
                path = registry.codex_rollout(st["id"])
            except sqlite3.Error as e:
                raise SwitchError("cannot read Codex's state database for thread %s: %s" % (st["id"][:8], e)) from e
            if not path:
                raise SwitchError("no rollout log for Codex thread %s: nothing to hand over" % st["id"][:8])
            brief, lines = condense_codex(path)
            jobs = []
        brief, lines = carry_forward(brief, lines)
        doc = handoff_text(st, src, dst, path, brief, lines, jobs, owner)
        if dry_run:
            result.update(ok=True, detail="would hand over to %s with a %d-line handoff record" % (dst, len(lines)))
            return result
        dst_dir = accounts.expand(accounts.account(s, dst_acct).get("config_dir")) if dst_cli == "claude" else None
        if trust and st["cwd"]:
            try:
                trust(st["cwd"], dst_dir)
            except (OSError, ValueError) as e:
                raise SwitchError("cannot trust %s for %s: %s" % (st["cwd"], dst, e)) from e
        hpath = write_handoff(st["name"], doc)
        text = handoff_brief(st, hpath, src, dst, owner, preamble(st, dst_cli) if preamble else "")
        if not stop(st["pid"]):
            result.update(detail="pid %d did not exit after SIGKILL; nothing started (handoff record %s)" % (
                st["pid"], hpath))
            return result
        if cur_cli == "claude":
            registry.remove_dead_entry(sess)   # claude removes its own on exit; a killed one leaves it behind
        try:
            if dst_cli == "claude":
                new_id, not_briefed, notes = start_claude(st, dst_dir, model, effort, text, claude), "", []
            else:
                new_id, not_briefed, notes = start_codex(st, model, effort, text, codex)
        except commands.CommandError as e:
            result.update(detail="stopped %s, but starting %s in pane %s failed: %s; handoff record %s" % (
                name, dst, st["pane"], e, hpath))
            return result
        said = "".join("; " + n for n in notes)
        if not new_id:
            result.update(detail="handed over to %s, but the new session did not show up within 60 s; handoff record "
                                 "%s%s" % (dst, hpath, said))
            return result
        moved = rehome(cur_cli, st["id"], dst_cli, new_id, st["name"], st["cwd"]) if rehome else 0
        result.update(ok=not not_briefed, new_id=new_id, handoff=str(hpath),
                      detail="handed over to %s as session %s (handoff record %s; %s%s)%s" % (
                          dst, new_id[:8], hpath, "mail routing moved" if moved else "no mail routing to move", said,
                          ", but it has no brief: %s" % not_briefed if not_briefed else ""))
    return result
