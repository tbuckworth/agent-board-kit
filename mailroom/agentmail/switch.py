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
told it was interrupted and should carry on. Only agents in a tmux pane whose shell started the CLI (agent-spawn's
layout) can be switched, and Claude Code background sessions (claude --bg), which can change login, model or effort
but not CLI (agentmail/failover.py moves them with the daemon's own commands). Background commands keep running and
are listed in the note it resumes with (a background session's are stopped with it, and the note says so).
"""
import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

from . import config, failover, registry, usage

CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MODEL_CACHE_SECONDS = 86400
HANDOFF_BYTES = 200 << 10
BUSY_MARK = "esc to interrupt"     # both TUIs show it while a turn runs


class SwitchError(Exception):
    pass


# ------------------------------------------------------------------ reading an agent
def flag_value(argv, *names):
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def codex_effort(argv):
    for i, a in enumerate(argv):
        v = argv[i + 1] if a == "-c" and i + 1 < len(argv) else (a[3:] if a.startswith("-c=") else None)
        if v and v.startswith("model_reasoning_effort="):
            return v.split("=", 1)[1].strip("'\"")
    return None


def children(pid):
    from . import memcap
    return [p for p, pp in memcap.parents().items() if pp == pid]


def codex_pane(sess):
    """(pane, codex pid) for a Codex agent: its tmux session is named after it (agent-spawn), and the pane's shell
    is the parent of a codex process. (None, None) if not found."""
    name = sess.get("name") or ""
    if not name:
        return None, None
    r = failover.tmux("list-panes", "-t", "=" + name, "-F", "#{pane_id} #{pane_pid}")
    for line in (r.stdout or "").split("\n"):
        parts = line.split()
        if len(parts) != 2:
            continue
        for kid in children(int(parts[1])):
            argv = failover.cmdline(kid)
            if argv and "codex" in os.path.basename(argv[0]):
                return parts[0], kid
    return None, None


def pane_busy(pane):
    text = failover.tmux("capture-pane", "-p", "-t", pane).stdout or ""
    return BUSY_MARK in text.lower()


def state(sess, cfg):
    """What the board needs to know about one live agent."""
    cli = sess["cli"]
    out = {"name": sess.get("name") or "", "id": sess["id"], "cli": cli, "account": sess.get("account"),
           "cwd": sess.get("cwd") or "", "status": sess.get("status") or "", "live": bool(sess.get("live"))}
    if cli == "claude":
        pane, why = failover.movable(sess)
        pid = int(sess["pid"]) if sess.get("pid") else None
        argv = failover.cmdline(pid) if pid else []
        path = None if flag_value(argv, "--model") and flag_value(argv, "--effort") else failover.transcript(sess)
        # A background session's process (a daemon's spare) names no flags: the transcript says what it runs.
        recorded = failover.transcript_flags(failover.read_tail(path, 256 << 10)) if path else []
        out.update(model=flag_value(argv, "--model") or flag_value(recorded, "--model"),
                   effort=flag_value(argv, "--effort") or flag_value(recorded, "--effort"))
        busy = sess.get("status") == "busy" or bool(sess.get("waiting_for")) or \
            bool(pane and pane != failover.BG and pane_busy(pane))
        out["background_jobs"] = len(failover.background_jobs(pid)) if pid else 0
    else:
        pane, pid = codex_pane(sess)
        why = "" if pane else "no tmux pane named %s running codex (not agent-spawn's layout)" % (sess.get("name") or "?")
        argv = failover.cmdline(pid) if pid else []
        out.update(model=flag_value(argv, "-m", "--model"), effort=codex_effort(argv))
        busy = bool(pane and pane_busy(pane))
        out["background_jobs"] = 0
    out.update(pane=pane, pid=pid, movable=bool(pane), why_not=why, busy=busy)
    if not out["model"]:
        out["model"] = cfg.get("spawn_%s_model" % cli)
    if not out["effort"]:
        out["effort"] = cfg.get("spawn_%s_effort" % cli)
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
    answer (no model call), cached for a day per login."""
    cache_file = config.STATE_DIR / "models-cache.json"
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cache = {}
    key = "claude:" + (account or "default")
    hit = cache.get(key)
    if hit and time.time() - hit.get("at", 0) < MODEL_CACHE_SECONDS:
        return hit["models"]
    acct = usage.account(usage.settings(cfg), account) if account else {}
    req = {"type": "control_request", "request_id": "agent-switch", "request": {"subtype": "initialize"}}
    models = []
    try:
        r = run(usage.claude_probe_cmd(registry.claude_bin() or "claude"), input=json.dumps(req) + "\n",
                capture_output=True, text=True, timeout=60, env=usage.claude_env(acct), cwd=str(config.STATE_DIR))
        for line in (r.stdout or "").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "control_response":
                for m in ((d.get("response") or {}).get("response") or {}).get("models") or []:
                    if m.get("value") and m.get("value") != "default":
                        models.append({"value": m["value"], "label": m.get("displayName") or m["value"],
                                       "efforts": m.get("supportedEffortLevels") or []})
    except (OSError, subprocess.SubprocessError):
        pass
    if models:
        cache[key] = {"at": time.time(), "models": models}
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache))
        except OSError:
            pass
    return models or [{"value": cfg.get("spawn_claude_model") or "opus", "label": "default", "efforts": CLAUDE_EFFORTS}]


def codex_models(path=None):
    """The models Codex lists (its own cache of the server's catalog): [{"value", "label", "efforts"}]."""
    try:
        d = json.loads(Path(path or registry.CODEX_HOME / "models_cache.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for m in sorted(d.get("models") or [], key=lambda m: m.get("priority", 99)):
        if m.get("visibility") != "list":
            continue
        out.append({"value": m["slug"], "label": m.get("display_name") or m["slug"],
                    "efforts": [x.get("effort") for x in m.get("supported_reasoning_levels") or [] if x.get("effort")]})
    return out


# ------------------------------------------------------------------ options for the board
def target_of(st):
    return "codex" if st["cli"] == "codex" else "claude:%s" % (st.get("account") or "default")


def options(name, cfg, snaps=None, models=None):
    """Everything the board's menu needs, for one agent."""
    sess = find(name, cfg)
    st = state(sess, cfg)
    s = usage.settings(cfg)
    snaps = usage.snapshots(cfg) if snaps is None else snaps
    current = target_of(st)
    targets = []
    for t in usage.targets(s):
        cli = "codex" if t == "codex" else "claude"
        acct = None if cli == "codex" else t.split(":", 1)[1]
        # Headroom for the model the agent would run there: its own on the same CLI (a Fable agent needs Fable).
        model = st.get("model") if cli == st["cli"] else cfg.get("spawn_%s_model" % cli)
        a = usage.assess(snaps.get(t) or {"windows": [], "error": "not read"}, s, model)
        ready = True if cli == "codex" else usage.account_ready(usage.account(s, acct))[0]
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
            "defaults": {cli: {"model": cfg.get("spawn_%s_model" % cli), "effort": cfg.get("spawn_%s_effort" % cli)}
                         for cli in ("claude", "codex")}}


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


def codex_rollout(thread_id):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % (registry.CODEX_HOME / "state_5.sqlite"), uri=True, timeout=2)
        row = con.execute("select rollout_path from threads where id=?", (thread_id,)).fetchone()
        con.close()
        return Path(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None


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


HANDOFF_REF = re.compile(r"(/\S+/agent-switch/\S+-handoff\.md)")


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


def handoff_text(st, src_desc, dst_desc, transcript_path, brief, lines, jobs, owner="the owner"):
    head = ["# Handoff for agent %s" % (st["name"] or st["id"][:8]),
            "",
            "Switched by %s on %s from %s to %s." % (owner, time.strftime("%Y-%m-%d %H:%M"), src_desc, dst_desc),
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
    d = Path(os.path.expanduser("~/.local/state/agent-switch"))
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / ("%s-%d-handoff.md" % (re.sub(r"[^A-Za-z0-9._-]+", "-", name or "agent"), int(time.time())))
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


def handoff_brief(st, handoff_path, src_desc, dst_desc, preamble="", owner="the owner"):
    return (preamble +
            "You are taking over agent %s, which was running as %s until %s switched it to you (%s). "
            "Work in %s. First read %s: it holds the original brief and a condensed record of the conversation "
            "so far, latest at the end, plus any background commands still running. Then carry on with the task "
            "from where it stopped. Check git status and the state of the work before redoing anything. If the "
            "brief says to email %s when done or blocked, that still applies."
            % (st["name"] or st["id"][:8], src_desc, owner, dst_desc, st["cwd"], handoff_path, owner))


# ------------------------------------------------------------------ acting
def describe(cli, account, model, effort):
    who = "Claude Code (%s login)" % account if cli == "claude" else "Codex"
    return "%s, %s%s" % (who, model or "default model", (" at effort %s" % effort) if effort else "")


def type_line(pane, session, line, config_dir=None, set_env=True):
    if session and set_env:
        if config_dir:
            failover.tmux("set-environment", "-t", session, "CLAUDE_CONFIG_DIR", config_dir)
        else:
            failover.tmux("set-environment", "-t", session, "-r", "CLAUDE_CONFIG_DIR")
    failover.tmux("send-keys", "-t", pane, "-l", line)
    time.sleep(0.3)
    failover.tmux("send-keys", "-t", pane, "Enter")


def stop(pid):
    try:
        os.kill(pid, signal.SIGKILL)   # background commands keep running (see agentmail/failover.py)
    except ProcessLookupError:
        pass
    return failover.wait_dead(pid)


def wait_for(pred, timeout, step=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def relaunch_codex(st, model, effort, prompt, codex):
    """Same Codex thread, new model/effort, same pane."""
    argv = failover.cmdline(st["pid"])
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
    cmd = [codex, "resume", st["id"]] + keep + registry.CODEX_NO_UPDATE_PROMPT + [
        "-m", model, "-c", "model_reasoning_effort=%s" % effort]
    if prompt:
        cmd.append(prompt)
    if not stop(st["pid"]):
        raise SwitchError("codex pid %d did not exit" % st["pid"])
    type_line(st["pane"], st["name"], "stty sane; unset CLAUDE_CONFIG_DIR; " + " ".join(shlex.quote(c) for c in cmd))
    # a lock file can outlive a killed codex, so wait for a new codex process in the pane, not for the lock

    def relaunched():
        pane, pid = codex_pane({"name": st["name"]})
        return pid if pid and pid != st["pid"] else None
    ok = wait_for(relaunched, 60)
    return bool(ok), "resumed Codex thread %s with %s, effort %s" % (st["id"][:8], model, effort) if ok else \
        "relaunched codex but thread %s not live after 60 s" % st["id"][:8]


def start_claude(st, account, model, effort, brief, cfg, claude, trust=None):
    """A new Claude session in the agent's pane (handoff). Returns its session id or None."""
    acct = usage.account(usage.settings(cfg), account)
    dst_dir = usage.expand(acct.get("config_dir"))
    if trust and st["cwd"]:
        trust(st["cwd"], dst_dir)
    brief_dir = Path(os.path.expanduser("~/.local/state/agent-spawn"))
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / ("%s-%d-switch.md" % (st["name"] or "agent", int(time.time())))
    brief_path.write_text(brief)
    os.chmod(brief_path, 0o600)
    env = ("export CLAUDE_CONFIG_DIR=%s" % shlex.quote(dst_dir)) if dst_dir else "unset CLAUDE_CONFIG_DIR"
    cmd = [claude] + (["--name", st["name"]] if st["name"] else []) + ["--dangerously-skip-permissions"]
    cmd += (["--model", model] if model else []) + (["--effort", effort] if effort else [])
    line = "stty sane; %s; %s \"$(cat %s)\"" % (env, " ".join(shlex.quote(c) for c in cmd), shlex.quote(str(brief_path)))
    type_line(st["pane"], st["name"], line, dst_dir)
    want = dst_dir or None

    def new_session():
        for s in registry.claude_sessions():
            if s["live"] and s["id"] != st["id"] and s.get("name") == st["name"] and (s.get("config_dir") or None) == want:
                return s["id"]
        return None
    return wait_for(new_session, 60)


def start_codex(st, model, effort, brief, codex, trust=None):
    """A new Codex thread in the agent's pane (handoff), named after the agent, with the brief queued."""
    if trust and st["cwd"]:
        trust(st["cwd"], None)
    before = registry.codex_live_ids()
    cmd = [codex, "-s", "danger-full-access", "-a", "never", "-C", st["cwd"] or "."] + registry.CODEX_NO_UPDATE_PROMPT + [
        "-m", model, "-c", "model_reasoning_effort=%s" % effort]
    type_line(st["pane"], st["name"], "stty sane; unset CLAUDE_CONFIG_DIR; " + " ".join(shlex.quote(c) for c in cmd))
    new = wait_for(lambda: (registry.codex_live_ids() - before) or None, 60)
    if not new:
        return None
    tid = sorted(new)[0]

    def prompt_up():
        pane = failover.tmux("capture-pane", "-p", "-t", st["pane"]).stdout or ""
        if "Press enter to continue" in pane or "Do you trust" in pane:
            failover.tmux("send-keys", "-t", st["pane"], "Enter")
            return None
        return "Ask Codex" in pane or "›" in pane
    wait_for(prompt_up, 45, 1.5)
    if st["name"]:
        failover.tmux("send-keys", "-t", st["pane"], "-l", "/rename " + st["name"])
        time.sleep(0.3)
        failover.tmux("send-keys", "-t", st["pane"], "Enter")
        time.sleep(2)
    subprocess.run([codex, "queue", "--thread", tid, "--message", brief], capture_output=True, text=True, timeout=60)
    return tid


def switch(name, cfg, to=None, model=None, effort=None, force=False, dry_run=False, trust=None, preamble=None,
           rehome=None, claude=None, codex=None):
    """Switch one agent. Returns {"ok", "detail", "needs_force", "from", "to"}. Raises SwitchError for a request
    that cannot be done (unknown agent, not movable, unknown target or model)."""
    sess = find(name, cfg)
    st = state(sess, cfg)
    s = usage.settings(cfg)
    cur_cli, cur_acct = st["cli"], st.get("account")
    if to:
        try:
            dst_cli, dst_acct = usage.parse_target(to, s)
        except ValueError as e:
            raise SwitchError(str(e))
    else:
        dst_cli, dst_acct = cur_cli, cur_acct
    if dst_cli == "claude" and not usage.account_ready(usage.account(s, dst_acct))[0]:
        raise SwitchError("Claude login %s is not set up" % dst_acct)
    if not st["movable"]:
        raise SwitchError("cannot switch %s: %s" % (name, st["why_not"]))
    same_cli = dst_cli == cur_cli
    if st["pane"] == failover.BG and not same_cli:
        raise SwitchError("cannot hand %s over to %s: it is a background session (claude --bg), with no tmux pane "
                          "to start %s in; it can change Claude login, model or effort" % (name, dst_cli, dst_cli))
    model = model or (st["model"] if same_cli else cfg.get("spawn_%s_model" % dst_cli))
    effort = effort or (st["effort"] if same_cli else cfg.get("spawn_%s_effort" % dst_cli))
    catalog = claude_models(dst_acct, cfg) if dst_cli == "claude" else codex_models()
    known = {m["value"]: m for m in catalog}
    if model and catalog and model not in known and not (dst_cli == "claude" and model.startswith("claude-")):
        raise SwitchError("unknown %s model %r (choose from %s)" % (dst_cli, model, ", ".join(known)))
    if model in known and not known[model]["efforts"]:
        effort = None   # a model without effort levels (Haiku): pass none
    efforts = (known.get(model) or {}).get("efforts") or (CLAUDE_EFFORTS if dst_cli == "claude" else [])
    if effort and efforts and effort not in efforts:
        raise SwitchError("%s does not take effort %r (choose from %s)" % (model, effort, ", ".join(efforts)))
    src = describe(cur_cli, cur_acct, st["model"], st["effort"])
    dst = describe(dst_cli, dst_acct, model, effort)
    result = {"ok": False, "needs_force": False, "from": src, "to": dst, "detail": ""}
    if same_cli and dst_acct == cur_acct and model == st["model"] and effort == st["effort"]:
        result.update(ok=True, detail="already %s" % dst)
        return result
    if st["busy"] and not force:
        result.update(needs_force=True, detail="%s is mid-turn: switching will interrupt it (use --force)" % name)
        return result
    owner = config.owner_name(cfg)
    note = ("[agent-switch] %s switched this session from %s to %s, interrupting your turn. Carry on from where "
            "you were." % (owner, src, dst)) if st["busy"] else None
    claude = claude or registry.claude_bin() or "claude"
    codex = codex or registry.codex_bin() or "codex"
    if same_cli and cur_cli == "claude":
        if dry_run:
            result.update(ok=True, detail="would resume the same conversation as %s" % dst)
            return result
        ok, detail = failover.migrate(sess, dst_acct, note, cfg, trust=trust, claude=claude,
                                      why="switched by %s" % owner, overrides={"--model": model, "--effort": effort})
        result.update(ok=ok, detail=detail)
    elif same_cli:
        if dry_run:
            result.update(ok=True, detail="would resume Codex thread %s with %s, effort %s" % (st["id"][:8], model, effort))
            return result
        ok, detail = relaunch_codex(st, model, effort, note, codex)
        result.update(ok=ok, detail=detail)
    else:
        if cur_cli == "claude":
            path = failover.transcript(sess)
            brief, lines = condense_claude(path) if path else ("", [])
            jobs = failover.background_jobs(st["pid"])
        else:
            path = codex_rollout(st["id"])
            brief, lines = condense_codex(path) if path else ("", [])
            jobs = []
        brief, lines = carry_forward(brief, lines)
        doc = handoff_text(st, src, dst, path, brief, lines, jobs, owner=owner)
        if dry_run:
            result.update(ok=True, detail="would hand over to %s with a %d-line handoff record" % (dst, len(lines)))
            return result
        hpath = write_handoff(st["name"], doc)
        text = handoff_brief(st, hpath, src, dst, preamble(st, dst_cli) if preamble else "", owner=owner)
        if not stop(st["pid"]):
            raise SwitchError("pid %d did not exit" % st["pid"])
        if cur_cli == "claude":
            registry.remove_dead_entry(sess)   # claude removes its own on exit; a killed one leaves it behind
        new_id = (start_claude(st, dst_acct, model, effort, text, cfg, claude, trust) if dst_cli == "claude"
                  else start_codex(st, model, effort, text, codex, trust))
        if not new_id:
            result.update(detail="handed over to %s, but the new session did not show up within 60 s; handoff record "
                                 "%s" % (dst, hpath))
            return result
        moved = rehome(cur_cli, st["id"], dst_cli, new_id, st["name"], st["cwd"]) if rehome else 0
        result.update(ok=True, new_id=new_id, handoff=str(hpath),
                      detail="handed over to %s as session %s (handoff record %s; %s)" % (
                          dst, new_id[:8], hpath, "mail routing moved" if moved else "no mail routing to move"))
    return result
