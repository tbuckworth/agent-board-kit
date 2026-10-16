"""The only things the Mac app may make the desktop do. Each request is one JSON object on stdin.

  {"action": "close",       "id": ID}                       ask the agent to land its work and close itself
  {"action": "check_close", "id": ID}                       agent-close --dry-run: what a force close would discard
  {"action": "cleanup",     "id": ID}                       an offline agent: plain agent-close, which removes its
                                                            worktree only if every change is landed, else refuses
  {"action": "force_close", "id": ID, "confirm": NAME}       agent-close --force (discards unlanded work)
  {"action": "send",        "id": ID, "text": TEXT}          a message from the owner into the session

Switching an agent (the backend is the mailroom's agent-switch):

  {"action": "switch_options", "id": ID}                     agent-switch ID --options --json (changes nothing)
  {"action": "switch", "id": ID, "to": T, "model": M, "effort": E, "force": bool}
                                                            agent-switch ID [--to T] [--model M] [--effort E] [--force]

Resuming an exited agent (an offline, dashed tile):

  {"action": "resume", "id": ID, "text": TEXT?}              start it again in a new tmux session, same conversation,
                                                            model, effort and login, then send it the owner's note

Messaging the orchestrator, like an email to it:

  {"action": "orchestrate", "text": TEXT}                    one orchestrator event (agent-orchestrate), in the
                                                            background; it answers the owner by email

Worktrees with no agent in them (the board's bottom section) are named by path, never by anything else:

  {"action": "wt_check",   "path": P}                        what a clean up would do (agent-close's dry run)
  {"action": "wt_cleanup", "path": P}                        remove it and its branch only if everything is landed
  {"action": "wt_force",   "path": P, "confirm": NAME}        remove it even with unlanded work
  {"action": "wt_spawn",   "path": P, "text": TEXT}           start an agent there with the owner's instructions
  {"action": "wt_landed_check"}                              agent-close's dry run on every agent-less worktree
  {"action": "wt_landed_cleanup", "paths": [P, ...]}          plain clean up of each confirmed path, one by one

Desktop restart (agentboard/restart.py): restart_plan, restart_run, restart_cancel; see restart_action below.

The id must be a full session id that is on the board right now (live, for close/send); force_close also
needs the display name typed back. Nothing is ever passed through a shell: argv lists and stdin only.
Every text an agent reads names the owner by the mailroom config's owner_name.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import config, sources

TARGET_RE = re.compile(r"^(claude:[A-Za-z0-9_-]{1,32}|codex)$")
WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_TEXT = 4000
AUDIT = sources.HOME / ".local/state/agent-board/actions.jsonl"

HEADER_FMT = "[relayed from {owner} via agent-board]"
CLOSE_FMT = """ close
{owner} asked you to close, from the agent board. Follow your close protocol: run `agent-close --self --dry-run`.
If it reports unlanded work, email {owner} (agent-mail send) what is left and ask whether to land it or discard it.
Otherwise run `agent-close --self` as your very last step; no email is needed, the board shows you closing."""
SEND_FOOTER_FMT = ("\n\n(Sent from the agent board, not by email. If you need to answer, use agent-mail send to "
                   "{owner}.)")


def owner():
    """The owner's first name for text agents read (mailroom config owner_name), else "the owner"."""
    return config.owner_name()


def header(name=None):
    return HEADER_FMT.format(owner=name or owner())


def close_text(name=None):
    name = name or owner()
    return header(name) + CLOSE_FMT.format(owner=name)


def send_footer(name=None):
    return SEND_FOOTER_FMT.format(owner=name or owner())


def _audit(entry):
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT, "a") as fh:
            fh.write(json.dumps(dict(entry, ts=time.time())) + "\n")
    except OSError:
        pass


def _run(cmd, stdin=None, timeout=60):
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout
    except OSError as e:
        return 127, str(e)


def validate(req, agents):
    """(agent, error). agents: the board's current rows."""
    if not isinstance(req, dict):
        return None, "request must be a JSON object"
    action, sid = req.get("action"), req.get("id")
    if action not in ("close", "check_close", "cleanup", "force_close", "send", "switch_options", "switch", "resume"):
        return None, "unknown action %r" % (action,)
    if not isinstance(sid, str) or not ID_RE.match(sid):
        return None, "id must be a full session id"
    agent = next((a for a in agents if a["id"] == sid), None)
    if agent is None:
        return None, "no session %s on the board" % sid[:8]
    if action in ("close", "send") and not agent["live"]:
        return None, "%s is not running; use Clean up to tidy it away" % agent["display"]
    if action == "cleanup" and agent["live"]:
        return None, "%s is still running; use Close to ask it to finish" % agent["display"]
    if action in ("cleanup", "force_close"):
        other = sharing(agent, agents)
        if other:
            return None, ("%s is running in the same directory; closing %s could delete that worktree under it. "
                          "Close that agent first, or use agent-close by hand." % (other["display"], agent["display"]))
    if action == "force_close" and req.get("confirm") != agent["display"]:
        return None, "type the agent's name exactly to force close it"
    if action == "resume":
        if agent["live"]:
            return None, "%s is already running" % agent["display"]
        if agent["cli"] not in ("claude", "codex"):
            return None, "can't tell which CLI %s ran" % agent["display"]
        if not agent.get("cwd") or not os.path.isdir(agent["cwd"]):
            return None, "%s's directory is gone, so it can't be resumed there" % agent["display"]
        text = req.get("text")
        if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT or "\x00" in text):
            return None, "note too long or not text"
    if action in ("switch_options", "switch") and not agent["live"]:
        return None, "%s is not running; only a live agent can be switched" % agent["display"]
    if action == "switch":
        to, model, effort = req.get("to"), req.get("model"), req.get("effort")
        if to is not None and not (isinstance(to, str) and TARGET_RE.match(to)):
            return None, "target must be claude:<login> or codex"
        for v, what in ((model, "model"), (effort, "effort")):
            if v is not None and not (isinstance(v, str) and WORD_RE.match(v)):
                return None, "bad %s name" % what
        if to is None and model is None and effort is None:
            return None, "nothing to change"
        if not isinstance(req.get("force", False), bool):
            return None, "force must be true or false"
    if action == "send":
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            return None, "message is empty"
        if len(text) > MAX_TEXT or "\x00" in text:
            return None, "message too long or not text"
    return agent, None


def _inside(a, b):
    a, b = os.path.realpath(a), os.path.realpath(b)
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def sharing(agent, agents):
    """A different live agent working in (or above, or below) this agent's directory, if any. agent-close
    checks for unlanded work, not for other sessions, so removing a shared worktree would pull it out from
    under the live one."""
    cwd = agent.get("cwd") or ""
    if not cwd:
        return None
    return next((o for o in agents if o["id"] != agent["id"] and o.get("live") and o.get("cwd")
                 and _inside(o["cwd"], cwd)), None)


def perform(req, agents, run=_run):
    agent, err = validate(req, agents)
    if err:
        return {"ok": False, "error": err}
    action, sid, cli = req["action"], agent["id"], agent["cli"]
    if action in ("switch_options", "switch"):
        return switch(req, agent, run)
    if action == "resume":
        return resume(req, agent, run)
    if action in ("close", "send"):
        exe = sources.find_bin("agent-send")
        if not exe:
            return {"ok": False, "error": "agent-send not found"}
        body = close_text() if action == "close" else header() + "\n" + req["text"].strip() + send_footer()
        # a full id, but not --exact-id: that refuses when a dead login-move leftover shares the id (agent-send
        # treats them as ambiguous), while plain resolution picks the live one; a UUID can't match a name
        code, out = run([exe, "--raw", "--cli", cli, sid], stdin=body, timeout=45)
    else:
        exe = sources.find_bin("agent-close")
        if not exe:
            return {"ok": False, "error": "agent-close not found"}
        flags = {"check_close": ["--dry-run"], "cleanup": [], "force_close": ["--force"]}[action]
        code, out = run([exe, sid] + flags, timeout=120)
    res = {"ok": code == 0, "code": code, "output": out[-4000:], "action": action, "name": agent["display"]}
    if action != "check_close":
        _audit({"action": action, "id": sid, "name": agent["display"], "code": code,
                "chars": len(req.get("text") or "")})
    return res


def switch(req, agent, run):
    exe = sources.find_bin("agent-switch")
    if not exe:
        return {"ok": False, "error": "agent-switch is not installed on the desktop"}
    if req["action"] == "switch_options":
        code, out = run([exe, agent["id"], "--options", "--json"], timeout=60)
        try:
            res = json.loads(out)
        except ValueError:
            res = None
        if not isinstance(res, dict):
            return {"ok": False, "code": code, "error": out[-600:] or "no answer from agent-switch"}
        if code != 0:          # refused (e.g. not agent-spawn's tmux layout): say why, not "could not read"
            return {"ok": False, "code": code, "error": res.get("detail") or "agent-switch refused (exit %d)" % code}
        return {"ok": True, "code": code, "options": res}
    cmd = [exe, agent["id"]]
    for flag, key in (("--to", "to"), ("--model", "model"), ("--effort", "effort")):
        if req.get(key) is not None:
            cmd += [flag, req[key]]
    if req.get("force"):
        cmd.append("--force")
    code, out = run(cmd + ["--json"], timeout=180)     # a Claude <-> Codex handoff can take a minute
    try:
        res = json.loads(out)
    except ValueError:
        res = None
    if not isinstance(res, dict):
        res = {"ok": False, "detail": out[-600:]}
    _audit({"action": "switch", "id": agent["id"], "name": agent["display"], "code": code,
            "to": req.get("to"), "model": req.get("model"), "effort": req.get("effort"), "force": bool(req.get("force"))})
    return {"ok": code == 0, "code": code, "needs_force": code == 3 or bool(res.get("needs_force")),
            "output": res.get("detail") or "", "result": res, "name": agent["display"]}


# ------------------------------------------------------------------ worktrees with no agent
MAX_BRIEF = 8000
SPAWN_BRIEF = """{owner} started you from the agent board, on an existing git worktree that had no agent in it.

Worktree: {path}
Repo: {repo}   Branch: {branch}
State when {owner} asked: {state}

Instructions from {owner}:

{text}
"""


def mailroom_close():
    """agent-close's own close(): the same landed-work checks (squash and rebase merges count as landed), applied
    to a path. agent-close itself only takes session or tmux names. Imported read-only from the mailroom."""
    return mailroom("close").close


def validate_wt(req, agents, worktrees):
    """(worktree, error). The path must be one of the repos' linked worktrees right now, with no live agent in it."""
    from . import collect
    if not isinstance(req, dict):
        return None, "request must be a JSON object"
    action, path = req.get("action"), req.get("path")
    if action not in ("wt_check", "wt_cleanup", "wt_force", "wt_spawn"):
        return None, "unknown action %r" % (action,)
    if not isinstance(path, str) or not os.path.isabs(path):
        return None, "path must be an absolute worktree path"
    wt = next((w for w in worktrees if w["path"] == path), None)
    if wt is None:
        return None, "%s is not a linked worktree of any repo under %s (any more)" % (path, config.repos_root())
    live = [a["cwd"] for a in agents if a.get("live") and a.get("cwd")]
    repos = {a.get("repo") or (sources.git_facts(a["cwd"]) or {}).get("repo") for a in agents
             if a.get("live") and a.get("cwd")} if "/.claude/worktrees/" in wt["path"] else set()
    if collect.occupied(wt, live, [], repos):
        return None, "an agent is working in %s; use its tile instead" % path
    name = os.path.basename(path.rstrip("/"))
    if action == "wt_force" and req.get("confirm") != name:
        return None, "type the worktree's name (%s) exactly to force remove it" % name
    if action == "wt_spawn":
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            return None, "instructions are empty"
        if len(text) > MAX_BRIEF or "\x00" in text:
            return None, "instructions too long or not text"
    return wt, None


def perform_wt(req, agents, worktrees, closer=None, run=_run, state=None):
    wt, err = validate_wt(req, agents, worktrees)
    if err:
        return {"ok": False, "error": err}
    action, path, name = req["action"], wt["path"], os.path.basename(wt["path"].rstrip("/"))
    if action == "wt_spawn":
        exe = sources.find_bin("agent-spawn")
        if not exe:
            return {"ok": False, "error": "agent-spawn not found"}
        if state is None:
            from . import classify
            g = classify.git_stage(sources.git_facts(path)) or {}
            state = g.get("summary") or "unknown"
        brief = SPAWN_BRIEF.format(owner=owner(), path=path, repo=wt["repo"], branch=wt["branch"] or "(detached)",
                                   state=state, text=req["text"].strip())
        code, out = run([exe, "--dir", path, "--brief-text", brief, "--json"], timeout=240)
        res = {"ok": code == 0, "code": code, "output": out[-4000:], "action": action, "name": name}
    else:
        try:
            close = closer or mailroom_close()
            info = {"name": "worktree " + name, "tmux": None, "cli": "?", "session_id": "", "cwd": path}
            code, lines = close(info, force=(action == "wt_force"), dry_run=(action == "wt_check"))
        except Exception as e:      # report, never half-explain
            code, lines = 1, ["%s: %s" % (type(e).__name__, e)]
        res = {"ok": code == 0, "code": code, "output": "\n".join(lines)[-4000:], "action": action, "name": name}
    if action != "wt_check":
        _audit({"action": action, "path": path, "code": res["code"], "chars": len(req.get("text") or "")})
    return res


# ------------------------------------------------------------------ bulk: every fully landed worktree
def _fetch_repos(mains, run=_run):
    """One `git fetch origin` per repo, a few at a time, so every dry run compares with a fresh origin."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda m: run(["git", "-C", m, "fetch", "--quiet", "origin"], timeout=90), sorted(set(mains))))


def free_worktrees(agents, worktrees):
    """The worktrees the board's bottom section shows: no live agent, no Offline tile, subagent trees guarded."""
    from . import collect
    live = [a["cwd"] for a in agents if a.get("live") and a.get("cwd")]
    tiles = [a["cwd"] for a in agents if not a.get("live") and a.get("cwd")]
    repos = {(sources.git_facts(c) or {}).get("repo") for c in live}
    return [w for w in worktrees if not collect.occupied(w, live, tiles, repos)]


def landed_check(agents, worktrees, closer=None, fetch=_fetch_repos):
    """agent-close's verdict on every agent-less worktree: which a plain clean up would remove, and why not."""
    close = closer or mailroom_close()
    free = free_worktrees(agents, worktrees)
    fetch([w["main"] for w in free])
    out = []
    for w in free:
        name = os.path.basename(w["path"].rstrip("/"))
        try:
            code, lines = close({"name": "worktree " + name, "tmux": None, "cli": "?", "session_id": "", "cwd": w["path"]},
                                force=False, dry_run=True, fetch=False)
        except Exception as e:
            code, lines = 1, ["%s: %s" % (type(e).__name__, e)]
        text = "; ".join(lines)
        out.append({"path": w["path"], "name": name, "repo": w["repo"], "branch": w["branch"],
                    "removable": code == 0 and "remove worktree" in text, "detail": text[:300]})
    return {"ok": True, "candidates": out}


def landed_cleanup(req, agents, worktrees, closer=None, fetch=_fetch_repos):
    """Clean up each confirmed path with agent-close's own checks (never --force); each is re-validated first."""
    paths = req.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths) or len(paths) > 200:
        return {"ok": False, "error": "paths must be a non-empty list of worktree paths"}
    paths = list(dict.fromkeys(paths))          # each worktree once, in the order confirmed
    close = closer or mailroom_close()
    known = {w["path"]: w for w in worktrees}
    fetch([known[p]["main"] for p in paths if p in known])
    results = []
    for p in paths:
        wt, err = validate_wt({"action": "wt_cleanup", "path": p}, agents, worktrees)
        name = os.path.basename(p.rstrip("/"))
        if err:
            results.append({"path": p, "name": name, "ok": False, "detail": err})
            continue
        try:
            code, lines = close({"name": "worktree " + name, "tmux": None, "cli": "?", "session_id": "", "cwd": p},
                                force=False, dry_run=False, fetch=False)
        except Exception as e:
            code, lines = 1, ["%s: %s" % (type(e).__name__, e)]
        results.append({"path": p, "name": name, "ok": code == 0, "detail": "; ".join(lines)[:300]})
        _audit({"action": "wt_landed_cleanup", "path": p, "code": code})
    done = sum(1 for r in results if r["ok"])
    return {"ok": True, "removed": done, "kept": len(results) - done, "results": results}


# ------------------------------------------------------------------ the orchestrator
ORCH_HEADER_FMT = ("[message from {owner} via the agent board (the Mac app, over SSH), not by email. This is {owner} "
                   "writing: act on it as you would an email from {owner}. There is no thread to reply in: answer with "
                   "`agent-mail send --to {email} --subject \"...\" --body-file ...`; the reply to that mail comes back "
                   "to you.]")


def orch_header(cfg=None):
    cfg = cfg if cfg is not None else config.mail_config()
    return ORCH_HEADER_FMT.format(owner=config.owner_name(cfg=cfg),
                                  email=config.owner_email(cfg) or "<the owner's address>")


ORCH_LOGS = sources.HOME / ".local/state/agent-board"


def orchestrate(req, popen=None):
    """Start one orchestrator event with the owner's text and return at once; the run takes 30 s to a few minutes."""
    text = req.get("text") if isinstance(req, dict) else None
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "message is empty"}
    if len(text) > MAX_BRIEF or "\x00" in text:
        return {"ok": False, "error": "message too long or not text"}
    exe = sources.find_bin("agent-orchestrate")
    if not exe:
        return {"ok": False, "error": "agent-orchestrate is not installed on the desktop"}
    ORCH_LOGS.mkdir(parents=True, exist_ok=True)
    log = ORCH_LOGS / ("orchestrate-%d.log" % int(time.time()))
    body = orch_header() + "\n\n" + text.strip()
    import subprocess
    with open(log, "ab") as fh:
        p = (popen or subprocess.Popen)([exe, "--source", "board"], stdin=subprocess.PIPE, stdout=fh,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        p.stdin.write(body.encode())
        p.stdin.close()
    _audit({"action": "orchestrate", "chars": len(text), "log": str(log)})
    return {"ok": True, "output": "the orchestrator is running; it will email you (log %s)" % log}


# ------------------------------------------------------------------ resume an exited agent
# Never inherited by an agent. A tmux server first started from inside a Claude tool shell (an orchestrator run, an
# agent) keeps that shell's environment and hands it to every later session: AGENT_ORCHESTRATOR=1 makes agent-mail send
# the agent's mail as the orchestrator, CLAUDE_CODE_ENTRYPOINT=sdk-cli makes it look like a `claude -p` run, and the
# rest are another process's session id, messaging socket and token.
LEAKED = ("AGENT_ORCHESTRATOR", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
          "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
          "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT", "CLAUDE_PID")
SCRUB_ENV = " ".join("-u " + v for v in LEAKED)


def scrub_tmux_env(tmux, run=None):
    """Drop these per-process variables (only these: a user's own CLAUDE_CODE_* settings stay) from the tmux server's
    global environment, so no later session inherits them, agent-spawn's included. Names only are read; values never
    leave tmux. Without a server it does nothing."""
    run = run or _run
    code, out = run([tmux, "show-environment", "-g"], timeout=10)
    if code != 0:
        return []
    names = [ln.split("=", 1)[0] for ln in out.splitlines() if "=" in ln]
    return [v for v in names if v in LEAKED and run([tmux, "set-environment", "-g", "-u", v], timeout=10)[0] == 0]

RESUME_FMT = """ resumed
Your session had exited; {owner} resumed it from the agent board, in tmux session '{tmux}'. Check where you were
(git status, any background jobs you had running, your last notes) and carry on.{mail}{text}"""


def tmux_name_for(agent, existing):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", agent.get("name") or agent["display"]).strip("-")[:40] or agent["id"][:8]
    name, n = base, 2
    while name in existing:
        name, n = "%s-%d" % (base, n), n + 1
    return name


def claude_home_of(sid):
    """(account, config dir) of the login holding the NEWEST copy of this session's transcript, or (None, None).
    After a login move both logins have a copy and the old one stops at the move, so first-found is wrong."""
    best = None
    for account, home in sources.claude_logins():
        for path in Path(home).glob("projects/*/%s.jsonl" % sid):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, account, Path(home))
    return (best[1], best[2]) if best else (None, None)


def mailroom_root():
    """The mailroom checkout (the kit's sibling mailroom/, or AGENT_MAILROOM_DIR), or None."""
    d = config.mailroom_dir()
    return str(d) if d else None


def mailroom(module):
    """A module of the mailroom package (read-only use, as agent-close's own functions are used)."""
    import importlib
    import sys
    root = mailroom_root()
    if not root:
        raise RuntimeError("the mailroom isn't installed (no agentmail/ in the kit's mailroom/ or AGENT_MAILROOM_DIR)")
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("agentmail." + module)


def spawn_trust():
    """agent-spawn's own trust writer (the folder in a login's .claude.json), loaded as agent-failover does."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("agent_spawn", os.path.join(mailroom_root(), "bin", "agent-spawn"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    sp = importlib.util.module_from_spec(spec)
    loader.exec_module(sp)
    return lambda cwd, config_dir: sp.ensure_trust(cwd, sp.claude_json_for(config_dir))


def place_claude(agent):
    """(config dir, note): the login to resume on, chosen exactly as the mailroom's own offline resumes do
    (failover.place_resume: agent-spawn --account auto's rule, judged by the model the session runs; the newest
    copy of the conversation is copied there if it isn't already). Without the mailroom, the newest copy's login."""
    current = agent.get("config_dir") or None
    if current and Path(current).resolve() == sources.DEFAULT_CLAUDE_HOME.resolve():
        current = None
    try:
        failover, mconfig = mailroom("failover"), mailroom("config")
        d, note = failover.place_resume({"id": agent["id"], "cwd": agent.get("cwd") or "", "config_dir": current},
                                        mconfig.load(), trust=spawn_trust(), dry_run=False)
        return (Path(d) if d else sources.DEFAULT_CLAUDE_HOME), note
    except Exception as e:      # an older mailroom: still never resume from a stale copy
        account, home = claude_home_of(agent["id"])
        return home, "on login %s, where its newest copy is (mailroom placement unavailable: %s)" % (
            account, type(e).__name__)


def last_claude_model(home, sid):
    """The model of the session's last assistant turn, from its transcript tail."""
    for path in Path(home).glob("projects/*/%s.jsonl" % sid):
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, os.path.getsize(path) - 2_000_000))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"model"' in line and '"assistant"' in line:
                try:
                    m = (json.loads(line).get("message") or {}).get("model")
                except ValueError:
                    continue
                if m and not m.startswith("<"):
                    return m
    return None


def codex_settings(tid):
    con = sources.CodexReader()._db()
    if con is None:
        return None, None
    r = con.execute("select model, reasoning_effort from threads where id = ?", (tid,)).fetchone()
    return (r["model"], r["reasoning_effort"]) if r else (None, None)


def launch_line(agent, tmux, cfg):
    """The shell line that resumes this agent interactively, as agent-spawn would have started it. The tmux
    session may be `name-2` when the old one still exists, but Claude keeps the agent's own name: agent-send and
    the mailroom find it by that."""
    import shlex
    sid, cwd = agent["id"], agent["cwd"]
    if agent["cli"] == "claude":
        if claude_home_of(sid)[1] is None:
            raise RuntimeError("no transcript for %s on any Claude login" % sid[:8])
        home, placed = place_claude(agent)
        account = next((n for n, h in sources.claude_logins() if Path(h).resolve() == Path(home).resolve()), "default")
        model = last_claude_model(home, sid) or cfg.get("spawn_claude_model") or "claude-opus-5-5"
        effort = cfg.get("spawn_claude_effort") or "xhigh"
        exe = sources.find_bin("claude") or "claude"
        env = ("env %s -u CLAUDE_CONFIG_DIR" % SCRUB_ENV if Path(home).resolve() == sources.DEFAULT_CLAUDE_HOME.resolve()
               else "env %s CLAUDE_CONFIG_DIR=%s" % (SCRUB_ENV, shlex.quote(str(home))))
        own = agent.get("name") or tmux
        line = "%s %s --resume %s --dangerously-skip-permissions --model %s --effort %s --name %s" % (
            env, shlex.quote(exe), sid, shlex.quote(model), shlex.quote(effort), shlex.quote(own))
        return line, "claude · %s · %s · %s; %s" % (account, model, effort, placed)
    model, effort = codex_settings(sid)
    model = model or cfg.get("spawn_codex_model") or "gpt-6.1-sol"
    effort = effort or cfg.get("spawn_codex_effort") or "xhigh"
    exe = sources.find_bin("codex") or "codex"
    line = "env %s %s resume -s danger-full-access -a never -C %s -m %s -c model_reasoning_effort=%s %s" % (
        SCRUB_ENV, shlex.quote(exe), shlex.quote(cwd), shlex.quote(model), shlex.quote(effort), sid)
    return line, "codex · %s · %s" % (model, effort)


def is_live(agent):
    if agent["cli"] == "claude":
        return any(s["id"] == agent["id"] and s["live"] for s in sources.claude_sessions_all())
    return any(s["id"] == agent["id"] and s["live"] for s in sources.CodexReader().sessions(ids_of_interest=[agent["id"]]))


def stranded_mail(agent):
    """Inbound mail from the owner that arrived after the agent stopped: ids it should read."""
    got = sources.MailStore().read().get("by_session", {}).get((agent["cli"], agent["id"]), {})
    since = agent.get("last_activity") or 0
    return [i["id"] for i in got.get("inbound", []) if (i.get("received_at") or 0) > since]


def running_with(sid, proc="/proc"):
    """pids of claude or codex processes that have this session open (`--resume ID`, `resume ID`), else [].
    Only those CLIs, with the id as its own argument: a `tail` of the transcript or `agent-mail --session ID`
    must not block a resume."""
    out = []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return out
    for p in pids:
        try:
            with open(os.path.join(proc, p, "cmdline"), "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\x00") if a]
        except OSError:
            continue
        exe = os.path.basename(argv[0]) if argv else ""
        script = argv[1] if len(argv) > 1 else ""
        cli = exe in ("claude", "codex") or (exe == "node" and (                      # npm builds run under node
            script.endswith(("/claude-code/cli.js", "/claude.js")) or
            ("/@openai/codex/" in script and script.endswith(".js")) or script.endswith("/codex.js")))
        if sid in argv and cli:
            out.append(int(p))
    return [p for p in out if p != os.getpid()]


def resume(req, agent, run=_run, live=is_live, wait=45, cap=None, busy=running_with):
    """One resume per agent at a time, and never on top of a live or starting process: two processes on one
    conversation would both write its transcript."""
    import fcntl
    tmux = sources.find_bin("tmux")
    if not tmux:
        return {"ok": False, "error": "tmux not found"}
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        lock = open(AUDIT.parent / ("resume-%s.lock" % agent["id"]), "a+")
    except OSError as e:
        return {"ok": False, "error": "can't take the resume lock: %s" % e}
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return {"ok": False, "error": "%s is already being resumed" % agent["display"]}
    try:
        if live(agent):
            return {"ok": False, "error": "%s is already running again" % agent["display"]}
        pids = busy(agent["id"])
        if pids:
            return {"ok": False, "error": "a %s process already has %s's session open (pid %s); wait a moment, or stop "
                                          "that process first" % (agent["cli"], agent["display"], pids[0])}
        return _resume(req, agent, run, live, wait, cap, tmux)
    finally:
        lock.close()


def _resume(req, agent, run, live, wait, cap, tmux):
    try:
        cfg = json.loads(sources.MAIL_CONFIG.read_text())
    except (OSError, ValueError):
        cfg = {}
    existing = set(sources.tmux_sessions())
    name = tmux_name_for(agent, existing)
    try:
        line, how = launch_line(agent, name, cfg)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    up, memory, err = launch_in_tmux(agent, line, name, run, live, wait, cap, tmux)
    if err:
        return {"ok": False, "error": err}
    note = ""
    if up:
        ids = stranded_mail(agent)
        who = owner()
        mail = ("\n%s emailed you while you were down: agent-mail show %s (and answer with agent-mail reply N)."
                % (who, ", ".join(str(i) for i in ids))) if ids else ""
        text = ("\n\nNote from %s:\n" % who + req["text"].strip()) if (req.get("text") or "").strip() else ""
        exe = sources.find_bin("agent-send")
        if exe:
            time.sleep(2)
            run([exe, "--raw", "--cli", agent["cli"], agent["id"]],
                stdin=header(who) + RESUME_FMT.format(owner=who, tmux=name, mail=mail, text=text), timeout=45)
            note = " Told it why" + (" and about mail %s." % ", ".join(map(str, ids)) if ids else ".")
    _audit({"action": "resume", "id": agent["id"], "name": agent["display"], "tmux": name, "live": up})
    return {"ok": up, "tmux": name, "output": ("Resumed %s in tmux '%s' (%s; %s).%s" % (agent["display"], name, how, memory, note))
            if up else "Started in tmux '%s' but it isn't live after %ds: open it in Kitty to see why." % (name, wait),
            "error": "" if up else "not live yet; see tmux '%s'" % name}


def launch_in_tmux(agent, line, name, run=_run, live=None, wait=45, cap=None, tmux=None):
    """Start a tmux session in the agent's folder, cap it, type the launch line, click through trust prompts and
    wait until the agent is live. Shared by the board's Resume and the post-reboot restore. (up, memory, error)."""
    tmux = tmux or sources.find_bin("tmux")
    live = live or is_live
    scrub_tmux_env(tmux, run)
    code, out = run([tmux, "new-session", "-d", "-s", name, "-c", agent["cwd"], "-x", "200", "-y", "50"], timeout=15)
    if code != 0:
        return False, "", "tmux new-session failed: " + out[-200:]
    scrub_tmux_env(tmux, run)            # a server this call just started took the caller's environment
    memory = (cap or memory_cap)(tmux, name, agent)
    run([tmux, "send-keys", "-t", "=%s:" % name, "-l", line], timeout=10)
    time.sleep(0.3)
    run([tmux, "send-keys", "-t", "=%s:" % name, "Enter"], timeout=10)
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(1.5)
        code, screen = run([tmux, "capture-pane", "-p", "-t", "=%s:" % name], timeout=10)
        if "Press enter to continue" in screen or "Do you trust" in screen:
            run([tmux, "send-keys", "-t", "=%s:" % name, "Enter"], timeout=10)
            continue
        if live(agent):
            return True, memory, ""
    return False, memory, ""


def memory_cap(tmux, name, agent):
    """Put the new pane in its own capped agent-NAME.scope, as agent-spawn does (the mailroom's memcap)."""
    try:
        memcap = mailroom("memcap")
        mconfig = mailroom("config")
        r = subprocess.run([tmux, "display-message", "-p", "-t", "=%s:" % name, "#{pane_pid} #{pid}"],
                           capture_output=True, text=True, timeout=10)
        pane, server = (int(x) for x in r.stdout.split())
        msg = memcap.adopt(pane, name, memcap.limits(mconfig.load()), "agent %s (resumed from agent-board)" % name,
                           settle=0)[1]
        # a tmux server first started from a service (the boot-time restore) would die with that service
        moved, how = memcap.free_from_service(server, "tmux-server", "tmux server", exclude=[pane])
        return msg + ("; tmux server " + how if moved else "")
    except Exception as e:      # a cap must never stop a resume
        return "uncapped: %s" % type(e).__name__


# ------------------------------------------------------------------ desktop restart
def restart_action(req):
    """restart_plan (read-only), restart_run {update, wait_minutes, choices}, restart_cancel."""
    from . import restart
    action = req.get("action")
    if action == "restart_plan":
        return {"ok": True, "plan": restart.plan()}
    if action == "restart_cancel":
        return restart.cancel()
    update, wait, choices = req.get("update"), req.get("wait_minutes", 15), req.get("choices") or {}
    if not isinstance(update, bool) or not isinstance(wait, int) or not isinstance(choices, dict):
        return {"ok": False, "error": "update must be true/false, wait_minutes a number, choices an object"}
    if not all(isinstance(k, str) and ID_RE.match(k) and v in restart.CHOICES for k, v in choices.items()):
        return {"ok": False, "error": "choices must map full session ids to nudge, quiet or skip"}
    res = restart.start_run(update, wait, choices)
    _audit({"action": "restart_run", "update": update, "wait": wait, "choices": len(choices), "ok": res.get("ok")})
    return res

