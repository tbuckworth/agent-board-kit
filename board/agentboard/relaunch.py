"""Starting an exited agent again, shared by the board's Resume (actions) and the post-reboot restore (restart): the
login, model and effort to resume it with, the shell line that resumes it, a tmux session for it with the leaked
per-process variables scrubbed, and a memory cap as agent-spawn gives its agents; and the environment and arguments of
any claude the board starts on a login (claude_env, login_flags), and the logins such a claude falls back through
(headless_logins). The mailroom's own modules are loaded from its checkout (sources.mailroom_root: the kit's
mailroom/, or AGENT_MAILROOM_DIR)."""
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


# ------------------------------------------------------------------ a claude on a login
# A claude started from inside the shell of an agent on a per-token login (a model on OpenRouter) inherits that login's
# settings (Claude Code puts its
# --settings file's env in every command the agent runs: ANTHROPIC_BASE_URL, ANTHROPIC_MODEL, ...), and one started
# under another login's CLAUDE_CONFIG_DIR would run on that login. The mailroom's accounts.claude_env is the one rule
# for what a child claude must not inherit; the board asks it rather than keeping a copy.

def is_default_login(home):
    """Whether the Claude login folder `home` is the default login's (None counts as the default)."""
    return not home or Path(home).resolve() == sources.DEFAULT_CLAUDE_HOME.resolve()


def claude_env(home, base=None):
    """The environment for a claude the board starts on the login whose folder is `home` (None: the default login):
    `base` (agent_env()'s when None) through the mailroom's scrub (accounts.claude_env), then CLAUDE_CONFIG_DIR for a
    login other than the default. Without a mailroom installed, `base` without CLAUDE_CONFIG_DIR. A mailroom that
    fails to load raises (sources.optional_mailroom)."""
    base = agent_env() if base is None else base
    accounts = sources.optional_mailroom("accounts")
    env = dict(accounts.claude_env({}, base)) if accounts is not None else dict(base)
    env.pop("CLAUDE_CONFIG_DIR", None)
    if not is_default_login(home):
        env["CLAUDE_CONFIG_DIR"] = str(home)
    return env


def scrubbed_names(names):
    """The names among `names`, the plain names the mailroom's scrub lists itself (accounts.CLAUDE_ENV_DROP without
    its patterns) and those the logins' settings files set (accounts.settings_env), that the scrub keeps from a
    claude, sorted; never CLAUDE_CONFIG_DIR, which each caller sets or removes. A shell line cannot unset by pattern,
    so the names it must unset are asked of accounts.claude_env one by one. [] without a mailroom installed."""
    accounts = sources.optional_mailroom("accounts")
    if accounts is None:
        return []
    listed = [n for n in getattr(accounts, "CLAUDE_ENV_DROP", ()) if not any(c in n for c in "*?[")]
    if hasattr(accounts, "settings_env"):          # what the logins' settings files set: in an agent's every command
        listed += accounts.settings_env(accounts.current_settings())
    asked = set(names) | set(listed)
    kept = accounts.claude_env({}, {n: "" for n in asked})
    return sorted(n for n in asked if n not in kept and n != "CLAUDE_CONFIG_DIR")


def logins():
    """(the mailroom's accounts module, its usage settings as config.json has them now: accounts.current_settings()),
    for the options a claude takes on each login, or None for a mailroom that isn't installed or predates them
    (accounts.login_flags). Raises what loading them raises (sources.optional_mailroom, current_settings)."""
    accounts = sources.optional_mailroom("accounts")
    if accounts is None or not hasattr(accounts, "login_flags"):
        return None
    return accounts, accounts.current_settings()


def login_dir(home):
    """The Claude login folder `home` as the mailroom names a login's: None for the default login."""
    return None if is_default_login(home) else str(home)


def login_flags(home, flags):
    """`flags`, a claude command line's options, as a claude on the login whose folder is `home` takes them, from the
    mailroom (accounts.login_flags): a per-token login's settings file and model after them, in place of any they carry.
    `flags` as they are without per-login options (logins)."""
    m = logins()
    return list(flags) if m is None else [str(x) for x in m[0].login_flags(m[1], login_dir(home), list(flags))]


def login_account(home):
    """(the mailroom's accounts module, the account of the login whose folder is `home`: accounts.account_by_dir), or
    (None, {}) without per-login options (logins)."""
    m = logins()
    return (None, {}) if m is None else (m[0], m[0].account_by_dir(m[1], login_dir(home))[1])


def headless_logins(cmd, base):
    """(name, config dir, command, env) for each Claude login a claude the board starts may run on, in the mailroom's
    order: headless.logins, the list the orchestrator and the pre-send review fall through on a usage limit (each
    set-up login in usage.order as usage.ranked orders them, never a manual one such as Gemini's), with `cmd` as that
    login takes it (accounts.login_flags) and `base` scrubbed for it (accounts.claude_env); config dir None for the
    default login. Neither Codex nor the API key: the config goes without the key's secret name, so its token is never
    read. None without a mailroom, or one older than headless.logins. Raises what loading the mailroom or its config
    raises."""
    headless = sources.optional_mailroom("headless")
    if headless is None or not hasattr(headless, "logins"):
        return None
    cfg = dict(sources.mailroom("config").load(), orchestrator_fallback_secret=None)
    return [(name, env.get("CLAUDE_CONFIG_DIR") or None, command, env)
            for name, command, env in headless.logins(cfg, list(cmd), base, None)]


def resume_model(src, home, recorded, fresh):
    """The model a session that last ran on the login whose folder is `src`, its transcript recording `recorded`, runs
    when resumed on the login whose folder is `home`, by the mailroom's rule for a move (accounts.move_model): that
    login's own model if it has one; never a model only a one-model login runs, which gives way to `fresh` (config
    spawn_claude_model); else `recorded`. `recorded` without per-login options (logins)."""
    m = logins()
    if m is None:
        return recorded
    accounts, s = m
    name = lambda d: accounts.account_by_dir(s, login_dir(d))[0]
    return accounts.move_model(s, name(src), name(home), None, recorded, fresh)


# Once Codex knows of a newer release it opens on an "Update available" menu and waits for a key before loading the
# thread, so an agent the board resumes (or a restart brings back) would sit there. The board's launches skip that
# check.
CODEX_NO_UPDATE = "check_for_update_on_startup=false"         # the setting (voicedesk's copies take it too)
CODEX_NO_UPDATE_PROMPT = "-c " + CODEX_NO_UPDATE

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
    kept = []
    for v in tmux_env_names(tmux, run):
        if v in LEAKED:
            code, out = run([tmux, "set-environment", "-g", "-u", v], timeout=10)
            if code != 0:
                kept.append("%s (%s)" % (v, out[-200:] or "exit %d" % code))
    return "tmux kept %s in its global environment" % ", ".join(kept) if kept else ""


def tmux_env_names(tmux, run=None):
    """The names of the variables in the tmux server's global environment, which every new session's shell inherits
    (names only: values never leave tmux); [] without a server or tmux."""
    if not tmux:
        return []
    code, out = (run or sources.run_merged)([tmux, "show-environment", "-g"], timeout=10)
    return [ln.split("=", 1)[0] for ln in out.splitlines() if "=" in ln] if code == 0 else []


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
    recorded (failover.transcript_flags, as agent-switch reads them), as the mailroom moves a model between logins
    (resume_model: a per-token login's own model, which a Claude model would fail on). Unless that login holds the
    newest copy of the conversation, the placement copies it there and trusts its folder in that login's .claude.json.
    A mailroom older than place_resume: the login with the newest copy, the model its transcript names and no effort.
    Codex: no config dir, and its thread's model and effort. What the agent's own record lacks is the mailroom's spawn default for its
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
    current = login_dir(agent.get("config_dir") or None)
    d, note = failover.place_resume({"id": sid, "cwd": agent.get("cwd") or "", "config_dir": current}, cfg,
                                    trust=spawn_trust(), dry_run=False)
    home = Path(d) if d else sources.DEFAULT_CLAUDE_HOME
    path = failover.transcript({"id": sid, "config_dir": str(home)})
    flags = failover.transcript_flags(failover.read_tail(path, failover.settings(cfg)["tail_bytes"])) if path else []
    recorded = dict(zip(flags[::2], flags[1::2]))
    return home, resume_model(current, home, recorded.get("--model"), cfg.get("spawn_claude_model")), \
        recorded.get("--effort"), note


def resume_command(cli, sid, cwd, home, model, effort, name, inherited=()):
    """The shell line that resumes session `sid` interactively, with the leaked variables scrubbed. Claude runs on
    the login whose config dir is `home` (None: the default login) under the agent name `name`, with its options as
    that login takes them (login_flags: a per-token login's OpenRouter settings, and its model in place of `model`),
    and without what the mailroom's scrub keeps from a claude (scrubbed_names) among this process's variables and
    `inherited`, the names the pane's shell may hold besides (tmux_env_names: the tmux server's). Codex runs in `cwd`
    with the repo's hook-trust flag. A `model` or `effort` of None is left to the CLI."""
    if cli == "claude":
        exe = sources.find_bin("claude") or "claude"
        unset = "".join(" -u " + shlex.quote(n) for n in scrubbed_names(set(os.environ) | set(inherited))
                        if n not in LEAKED)
        env = ("env %s%s -u CLAUDE_CONFIG_DIR" % (SCRUB_ENV, unset) if is_default_login(home)
               else "env %s%s CLAUDE_CONFIG_DIR=%s" % (SCRUB_ENV, unset, shlex.quote(str(home))))
        flags = ["--dangerously-skip-permissions"] + (["--model", model] if model else []) + (
            ["--effort", effort] if effort else []) + ["--name", name]
        return " ".join([env, shlex.quote(exe), "--resume", sid] + [shlex.quote(x) for x in login_flags(home, flags)])
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


def mailroom_keys_lock():
    """The mailroom's keys lock (agentmail/commands.py's keys_lock, the one definition of it), a function of whether to
    wait: whatever types or presses keys in an agent's pane holds it, so the lines and answers typed here never mix
    with the mailroom's keys in a pane (fast mode's effort keys, a relaunch, agent-spawn's lines). Raises RuntimeError
    when the mailroom isn't installed, and AttributeError when its install is older than the lock."""
    return sources.mailroom("commands").keys_lock


def launch_in_tmux(agent, line, name, run=sources.run_merged, live=None, wait=45, cap=None, tmux=None):
    """Start a tmux session in the agent's folder, cap it, type the launch line, answer its start-up dialogs
    (answer_dialog) and wait until the agent is live, each key typed under the mailroom's keys lock
    (mailroom_keys_lock). Shared by the board's Resume and the post-reboot restore. (up, memory, error, note): error
    says why nothing was launched, when the keys lock is not there, or tmux would not start the session or kept a
    leaked variable; note, what a dialog left for the owner (answer_dialog), or ""."""
    try:
        keys_lock = mailroom_keys_lock()
    except (RuntimeError, ImportError, AttributeError) as e:
        return False, "", "the mailroom's keys lock is not there (%s: %s), so the agent was not started" % (
            type(e).__name__, e), ""
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
    with keys_lock(True):
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
            with keys_lock(True):
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
