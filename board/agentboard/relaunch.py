"""Starting an exited agent again, shared by the board's Resume (actions) and the post-reboot restore (restart): the
login, model and effort to resume it with, the shell line that resumes it, a tmux session for it with the leaked
per-process variables scrubbed, and a memory cap as agent-spawn gives its agents. The mailroom's own modules are
loaded from its checkout (sources.mailroom_root: the kit's mailroom/, or AGENT_MAILROOM_DIR)."""
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import sources


# Never inherited by an agent. A tmux server first started from inside a Claude tool shell (an orchestrator run, an
# agent) keeps that shell's environment and hands it to every later session: AGENT_ORCHESTRATOR=1 makes agent-mail send
# the agent's mail as the orchestrator, CLAUDE_CODE_ENTRYPOINT=sdk-cli makes it look like a `claude -p` run, and the
# rest are another process's session id, messaging socket and token.
LEAKED = ("AGENT_ORCHESTRATOR", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
          "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
          "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT", "CLAUDE_PID")


SCRUB_ENV = " ".join("-u " + v for v in LEAKED)


def agent_env():
    """The environment for a command that starts an agent (agent-spawn): this process's, without LEAKED and the SSH
    connection it may run under, and with the login PATH."""
    env = {k: v for k, v in os.environ.items() if k not in LEAKED and not k.startswith("SSH_")}
    return dict(env, PATH=sources.LOGIN_PATH)


# Once Codex knows of a newer release it opens on an "Update available" menu and waits for a key before loading the
# thread, so an agent the board resumes (or a restart brings back) would sit there. The board's launches skip that
# check.
CODEX_NO_UPDATE_PROMPT = "-c check_for_update_on_startup=false"

# The start-up dialogs Enter answers: an update notice, and the folder trust prompt (Enter takes "trust").
ENTER_DIALOGS = ("Press enter to continue", "Do you trust")
# Codex stops a start on any hook that is new or changed since it was trusted, with no setting to skip the question
# (a plugin update that changes one hook stops every Codex agent a restart brings back). The board takes the choice
# that trusts nothing, by the number Codex shows for it, or by Enter when the highlight is already on it: those hooks
# stay off, as an older Codex left them. Trusting hooks is the owner's decision. The pattern gives whether the
# highlight is on that choice, and its number.
HOOK_REVIEW = "Hooks need review"
HOOK_REVIEW_UNTRUSTED = re.compile(r"(›\s*)?(\d+)\. Continue without trusting")
HOOK_REVIEW_COUNT = re.compile(r"(\d+ hooks? (?:is|are) new or changed)")


def answer_dialog(screen, name):
    """What to do about a start-up dialog on `screen`, in tmux session `name`: (send-keys arguments or None, a note
    for the owner or "")."""
    if any(d in screen for d in ENTER_DIALOGS):
        return ["Enter"], ""
    if HOOK_REVIEW not in screen:
        return None, ""
    choice = HOOK_REVIEW_UNTRUSTED.search(screen)
    if not choice:
        return None, ("Codex asks to review its hooks and offers no 'Continue without trusting': answer it in tmux "
                      "'%s'" % name)
    count = HOOK_REVIEW_COUNT.search(screen)
    # its number picks it (Codex 0.167); should a number only move the highlight there, Enter confirms it next look
    return ["Enter"] if choice.group(1) else ["-l", choice.group(2)], ("Codex: %s, left untrusted (review in Codex's /hooks)" % (
        count.group(1) if count else "some hooks are new or changed"))


def scrub_tmux_env(tmux, run=None):
    """Drop these per-process variables (only these: a user's own CLAUDE_CODE_* settings stay) from the tmux server's
    global environment, so no later session inherits them, agent-spawn's included. Names only are read; values never
    leave tmux. Without a server it does nothing. Returns "" when none is left, else which tmux kept and its answer."""
    run = run or sources.run_merged
    code, out = run([tmux, "show-environment", "-g"], timeout=10)
    if code != 0:
        return ""
    kept = []
    for v in [ln.split("=", 1)[0] for ln in out.splitlines() if "=" in ln]:
        if v in LEAKED:
            code, out = run([tmux, "set-environment", "-g", "-u", v], timeout=10)
            if code != 0:
                kept.append("%s (%s)" % (v, out[-200:] or "exit %d" % code))
    return "tmux kept %s in its global environment" % ", ".join(kept) if kept else ""


def tmux_name_for(agent, existing):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", agent.get("name") or agent["display"]).strip("-")[:40] or agent["id"][:8]
    name, n = base, 2
    while name in existing:
        name, n = "%s-%d" % (base, n), n + 1
    return name


def codex_hook_args(cwd):
    """agent-gate's hook-trust flag for a Codex session in `cwd`, as a shell word or "": Codex skips a repo's
    unapproved hooks silently, so a repo set up with the checklist gate needs it to keep its gate. A mailroom
    that is not installed, or predates agent-gate, has set no repo up, so there is nothing to pass; an agent-gate
    that fails to import raises."""
    gate = sources.optional_mailroom("reviewgate")
    return " ".join(shlex.quote(x) for x in gate.codex_hook_args(cwd)) if gate else ""


def spawn_trust():
    """agent-spawn's own trust writer (the folder in a login's .claude.json), loaded as agent-failover does."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("agent_spawn", os.path.join(sources.mailroom_root(), "bin", "agent-spawn"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    sp = importlib.util.module_from_spec(spec)
    loader.exec_module(sp)
    return lambda cwd, config_dir: sp.ensure_trust(cwd, sp.claude_json_for(config_dir))


def resume_settings(agent, cfg):
    """(config dir, model, effort, note) to resume an exited agent with; cfg is the mailroom's config (its
    config.load()). Claude: the login the mailroom's own offline resumes pick (failover.place_resume: agent-spawn
    --account auto's rule, judged by the model the session runs), and the model and effort its transcript last
    recorded (failover.transcript_flags, as agent-switch reads them). Unless that login holds the newest copy of the
    conversation, the placement copies it there and trusts its folder in that login's .claude.json. A mailroom older
    than place_resume: the login with the newest copy, the model its transcript names and no effort. Codex: no config
    dir, and its thread's model and effort. What the agent's own record lacks is the mailroom's spawn default for its
    CLI, and a null default is None (the CLI's own choice). note: which login and why ("" for Codex). Raises
    RuntimeError for a Claude session with no transcript on any login; whatever the placement raises propagates."""
    sid, cli = agent["id"], agent["cli"]
    home, note = None, ""
    if cli == "codex":
        model, effort = sources.codex_settings(sid)
    elif sources.claude_home_of(sid)[1] is None:
        raise RuntimeError("no transcript for %s on any Claude login" % sid[:8])
    else:
        home, model, effort, note = _claude_settings(agent, cfg)
    return home, model or cfg.get("spawn_%s_model" % cli), effort or cfg.get("spawn_%s_effort" % cli), note


def _claude_settings(agent, cfg):
    sid = agent["id"]
    failover = sources.optional_mailroom("failover")
    if failover is None or not hasattr(failover, "place_resume"):          # a mailroom older than its own placement
        account, home = sources.claude_home_of(sid)
        return home, sources.last_claude_model(home, sid), None, (
            "on login %s, where its newest copy is (this mailroom places no resumes)" % account)
    current = agent.get("config_dir") or None
    if current and Path(current).resolve() == sources.DEFAULT_CLAUDE_HOME.resolve():
        current = None
    d, note = failover.place_resume({"id": sid, "cwd": agent.get("cwd") or "", "config_dir": current}, cfg,
                                    trust=spawn_trust(), dry_run=False)
    home = Path(d) if d else sources.DEFAULT_CLAUDE_HOME
    path = failover.transcript({"id": sid, "config_dir": str(home)})
    flags = failover.transcript_flags(failover.read_tail(path, failover.settings(cfg)["tail_bytes"])) if path else []
    recorded = dict(zip(flags[::2], flags[1::2]))
    return home, recorded.get("--model"), recorded.get("--effort"), note


def resume_command(cli, sid, cwd, home, model, effort, name):
    """The shell line that resumes session `sid` interactively, with the leaked variables scrubbed. Claude runs on
    the login whose config dir is `home` (None: the default login) under the agent name `name`; Codex runs in
    `cwd` with the repo's hook-trust flag. A `model` or `effort` of None is left to the CLI."""
    if cli == "claude":
        exe = sources.find_bin("claude") or "claude"
        env = ("env %s -u CLAUDE_CONFIG_DIR" % SCRUB_ENV
               if not home or Path(home).resolve() == sources.DEFAULT_CLAUDE_HOME.resolve()
               else "env %s CLAUDE_CONFIG_DIR=%s" % (SCRUB_ENV, shlex.quote(str(home))))
        parts = [env, shlex.quote(exe), "--resume", sid, "--dangerously-skip-permissions"]
        if model:
            parts += ["--model", shlex.quote(model)]
        if effort:
            parts += ["--effort", shlex.quote(effort)]
        return " ".join(parts + ["--name", shlex.quote(name)])
    exe = sources.find_bin("codex") or "codex"
    parts = ["env", SCRUB_ENV, shlex.quote(exe), "resume", "-s", "danger-full-access", "-a", "never", "-C",
             shlex.quote(cwd), CODEX_NO_UPDATE_PROMPT]
    hook_args = codex_hook_args(cwd)
    if hook_args:
        parts.append(hook_args)
    if model:
        parts += ["-m", shlex.quote(model)]
    if effort:
        parts += ["-c", "model_reasoning_effort=%s" % shlex.quote(effort)]
    return " ".join(parts + [sid])


def launch_in_tmux(agent, line, name, run=sources.run_merged, live=None, wait=45, cap=None, tmux=None):
    """Start a tmux session in the agent's folder, cap it, type the launch line, answer its start-up dialogs
    (answer_dialog) and wait until the agent is live. Shared by the board's Resume and the post-reboot restore.
    (up, memory, error, note): error says why nothing was launched, when tmux would not start the session or kept a
    leaked variable; note, what a dialog left for the owner (answer_dialog), or ""."""
    tmux = tmux or sources.find_bin("tmux")
    live = live or sources.is_live
    kept = scrub_tmux_env(tmux, run)
    if kept:
        return False, "", kept + ", so the agent was not started", ""
    code, out = run([tmux, "new-session", "-d", "-s", name, "-c", agent["cwd"], "-x", "200", "-y", "50"], timeout=15)
    if code != 0:
        return False, "", "tmux new-session failed: " + out[-200:], ""
    kept = scrub_tmux_env(tmux, run)            # a server this call just started took the caller's environment
    if kept:
        return False, "", "%s, so the agent was not started in tmux session '%s'" % (kept, name), ""
    memory = (cap or memory_cap)(tmux, name, agent)
    run([tmux, "send-keys", "-t", "=%s:" % name, "-l", line], timeout=10)
    time.sleep(0.3)
    run([tmux, "send-keys", "-t", "=%s:" % name, "Enter"], timeout=10)
    t0, note = time.time(), ""
    while time.time() - t0 < wait:
        time.sleep(1.5)
        code, screen = run([tmux, "capture-pane", "-p", "-t", "=%s:" % name], timeout=10)
        keys, said = answer_dialog(screen, name)
        note = said or note
        if keys:
            run([tmux, "send-keys", "-t", "=%s:" % name] + keys, timeout=10)
            continue
        if live(agent):
            return True, memory, "", note
    return False, memory, "", note


def memory_cap(tmux, name, agent):
    """Put the new pane in its own capped agent-NAME.scope, as agent-spawn does (the mailroom's memcap)."""
    try:
        memcap, config = sources.mailroom("memcap"), sources.mailroom("config")
        r = subprocess.run([tmux, "display-message", "-p", "-t", "=%s:" % name, "#{pane_pid} #{pid}"],
                           capture_output=True, text=True, timeout=10)
        pane, server = (int(x) for x in r.stdout.split())
        msg = memcap.adopt(pane, name, memcap.limits(config.load()), "agent %s (resumed from agent-board)" % name,
                           settle=0)[1]
        # a tmux server first started from a service (the boot-time restore) would die with that service
        moved, how = memcap.free_from_service(server, "tmux-server", "tmux server", exclude=[pane])
        return msg + ("; tmux server " + how if moved else "")
    except Exception as e:      # a cap must never stop a resume
        return "uncapped: %s" % type(e).__name__
