"""Starting an agent in a tmux pane: a new session's (agent-spawn), or the pane of the agent it takes over (agent-switch,
agent-failover). The line that starts Claude Code or Codex in the pane's shell for one Claude login, typing it in,
Codex's start sequence (its new thread, its dialogs, /rename, the brief queued and checked), the protocol preamble
every spawned agent's brief starts with, the owner-only files briefs are kept in, and a new pane's memory cap. A
tmux command that fails raises commands.CommandError."""
import os
import re
import shlex
import time

from . import accounts, commands, config, jsonfile, memcap, procs, registry, reviewgate

BRIEF_DIR = config.HOME / ".local" / "state" / "agent-spawn"
EFFORTS = ["low", "medium", "high", "xhigh", "max"]   # the --effort levels Claude Code takes
CODEX_THREAD_SECONDS = 60   # how long a Codex start may take to open its thread
CODEX_PROMPT_SECONDS = 45   # and then to show its prompt
CODEX_PROMPT = ("Ask Codex", "›")   # Codex's prompt is up
CODEX_DIALOGS = ("Press enter to continue", "Do you trust")   # its update and trust dialogs: Enter accepts them
# Codex's start-up question when a hook it runs is new or changed since it was trusted. It has no setting to skip it,
# and it marks its highlighted choice with "›", as the prompt is marked, so it is looked for first. Its "Continue
# without trusting" choice is taken, which leaves those hooks off: trusting hooks is the owner's decision. The pattern
# gives the choice's number and whether the highlight is on it.
CODEX_HOOK_REVIEW = "Hooks need review"
CODEX_HOOK_UNTRUSTED = re.compile(r"(›\s*)?(\d+)\. Continue without trusting")
CODEX_HOOK_COUNT = re.compile(r"(\d+ hooks? (?:is|are) new or changed)")


def preamble(cfg, name, path, branch, cli, base_warning):
    """The protocol every agent's brief starts with, which names the owner (config owner_name) and tells the agent to
    mail them as `--to owner` (agent-mail reads it as config.owner_address: outbound.recipient), writing the body
    file in one command and sending in the next, so that no send command holds the owner's address or a link
    (README.md, "Sending mail from a session", says why).
    base_warning: why its branch's base may be behind (a failed fetch, worktree.fetch_base), or "". Raises ValueError
    when config names no owner address."""
    owner = cfg["owner_name"]
    if not config.owner_address(cfg):
        raise ValueError("config has no owner address for the brief (owner_email or allowlist)")
    stale = (" Its base may be behind: %s. Run `git fetch origin` and rebase before you start." % base_warning
             if base_warning else "")
    return (
        "You were started by agent-spawn on %s's machine, on their behalf (usually relayed by the orchestrator "
        "from an email they sent). Session name: %s. Working directory: %s. Branch: %s.%s\n\n"
        "Protocol: record milestones, blockers and finished work with `agent-status note \"...\"` (free, no model). "
        "Email %s only when you are done or blocked and they must act, or when they asked for email updates: "
        "`agent-mail send --to %s --subject ... --body-file ...`%s. Write the body file in one command and run the "
        "send in the next, so that the send command carries no link. "
        "Before mailing them results, a review, options or a plan, run `agent-mail guide` and follow it: they read "
        "on a phone with no context, and want the bottom line first, plain words and plots inline (`--image`). "
        "Their replies come back into this session automatically. Commit as you go; do not push unless the brief "
        "says so. "
        "If %s asks you to close (in any words), run `agent-close --self --dry-run`. If it reports unlanded work, "
        "tell them what is left and ask whether to land it or discard it (then `--force`). Otherwise reply with a "
        "one-line confirmation and run `agent-close --self` as your very last step: it ends this session and "
        "removes your worktree.\n\n"
        "Brief:\n\n" % (owner, name, path, branch or "-", stale, owner, config.OWNER_ALIAS,
                        (" (Codex: add `--session %s`)" % name) if cli == "codex" else "", owner)
    )


def write_private(folder, filename, text):
    """Save text as folder/filename for the owner only, the folder 0700 and the file 0600 from the start
    (jsonfile.write_text), and return its path: briefs and handoff records carry the owner's instructions."""
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / filename
    jsonfile.write_text(path, text, jsonfile.PRIVATE)
    return path


def write_brief(stem, text):
    """An agent's first prompt, saved as BRIEF_DIR/STEM-<time>.md (write_private). Returns its path."""
    return write_private(BRIEF_DIR, "%s-%d.md" % (stem, int(time.time())), text)


def wait_for(pred, timeout, step=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


# ------------------------------------------------------------------ the pane
def new_session(name, path):
    """Open a detached tmux session NAME, 200x50, in folder path, for a new agent, and return the target of its pane
    ("=NAME:": exactly that session, never a prefix match). Raises CommandError if tmux fails."""
    commands.tmux("new-session", "-d", "-s", name, "-c", path, "-x", "200", "-y", "50", check=True)
    return "=%s:" % name


def cap_pane(target, name, props):
    """Move a new session's pane shell (target; no children yet) into agent-NAME.scope with these caps, and the tmux
    server out of any service's cgroup. Called before the launch line is typed, so the agent starts inside the cap.
    Returns what happened, for the plan."""
    pane = commands.tmux("display-message", "-p", "-t", target, "#{pane_pid} #{pid}", check=False)
    try:
        pane_pid, server_pid = (int(x) for x in pane.stdout.split())
    except ValueError:
        return "uncapped: no pane pid from tmux (%s)" % ((pane.stderr or pane.stdout).strip() or "no output")
    # The server first, never touching the pane: the pane's own move must be the last one, or a slow move could
    # land it in the uncapped tmux-server.scope.
    moved, how = memcap.free_from_service(server_pid, "tmux-server", "tmux server", exclude=procs.tree(pane_pid))
    _, msg = memcap.adopt(pane_pid, name, props, "agent %s (agent-spawn)" % name, settle=0)
    return msg + ("; tmux server " + how if moved else "")


def shell_line(config_dir, cmd, brief_path):
    """The line typed into a pane's shell to start cmd for one Claude login: `stty sane` (a CLI killed in the pane
    can leave the terminal raw), CLAUDE_CONFIG_DIR exported for another login or unset (config_dir None: the
    default login, and Codex), then cmd quoted. With brief_path, the file's text is cmd's last argument
    ("$(cat FILE)"), so a long brief is never typed into the terminal."""
    env = ("export CLAUDE_CONFIG_DIR=%s" % shlex.quote(config_dir)) if config_dir else "unset CLAUDE_CONFIG_DIR"
    line = "stty sane; %s; %s" % (env, " ".join(shlex.quote(str(c)) for c in cmd))
    return line + (' "$(cat %s)"' % shlex.quote(str(brief_path)) if brief_path else "")


def send_line(target, text):
    """Type text into the tmux pane target (a pane id such as %17, or new_session's target) and press Enter, holding
    the keys lock (commands.keys_lock) so no other keys land between them. Raises CommandError if tmux fails."""
    with commands.keys_lock(True):
        commands.tmux("send-keys", "-t", target, "-l", text, check=True)
        time.sleep(0.3)
        commands.tmux("send-keys", "-t", target, "Enter", check=True)


def type_line(target, line, config_dir):
    """Type a shell_line into pane target (send_line), after giving the pane's tmux session this login's
    CLAUDE_CONFIG_DIR, or removing it (None), so a window opened there later, and every agent-mail or agent-send
    the agent runs, see the same login, and after C-u, which clears whatever the shell's line held (a stray key typed
    while the agent was being stopped), all under the keys lock. Raises CommandError if tmux fails."""
    with commands.keys_lock(True):
        if config_dir:
            commands.tmux("set-environment", "-t", target, "CLAUDE_CONFIG_DIR", config_dir, check=True)
        else:
            commands.tmux("set-environment", "-t", target, "-r", "CLAUDE_CONFIG_DIR", check=True)
        commands.tmux("send-keys", "-t", target, "C-u", check=True)
        send_line(target, line)


# ------------------------------------------------------------------ Claude Code
def claude_line(claude, sid, name, flags, config_dir, prompt, brief_path, s=None):
    """The shell_line that starts claude for the login config_dir: `claude [--resume SID] [--name NAME] FLAGS
    [PROMPT]`, or with brief_path, that file's text as its prompt. FLAGS are flags as that login takes them
    (accounts.login_flags; s: usage.settings, None: config.json's): its settings file and its own model, if any."""
    flags = accounts.login_flags(accounts.current_settings() if s is None else s, config_dir, flags)
    cmd = [claude] + (["--resume", sid] if sid else []) + (["--name", name] if name else []) + flags
    return shell_line(config_dir, cmd + ([prompt] if prompt else []), brief_path)


def start_claude(target, claude, name, config_dir, model, effort, brief_path, s=None):
    """Start a new Claude Code agent in pane target on the login config_dir: no permission prompts, model and
    effort when given (None: the CLI's own; a login with its own model runs that, claude_line), named NAME, with
    brief_path's text as its first prompt. Returns the line typed. Raises CommandError if tmux fails."""
    flags = ["--dangerously-skip-permissions"] + (["--model", model] if model else []) + \
        (["--effort", effort] if effort else [])
    line = claude_line(claude, None, name, flags, config_dir, None, brief_path, s)
    type_line(target, line, config_dir)
    return line


# ------------------------------------------------------------------ Codex
def codex_effort(effort):
    """Codex's option for a reasoning effort."""
    return ["-c", "model_reasoning_effort=%s" % effort]


def queue_codex(codex, thread_id, text):
    """`codex queue` text into a live Codex thread: (ok, codex's output). ok only if it exited 0 and said "Queued
    message"; a codex that could not run or took over 60 s is a failure too (commands.run)."""
    r = commands.run([codex, "queue", "--thread", thread_id, "--message", text], 60)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0 and "Queued message" in out, out


def codex_prompt_up(target, notes):
    """Whether Codex's prompt shows in pane target. A start-up dialog means not yet, and is answered under the keys
    lock (commands.keys_lock): Enter on the update and trust dialogs (skip the update, trust the folder). The hook
    review gets the number of its "Continue without trusting" choice, which picks it, or Enter when the highlight is
    already on it (should the number only move the highlight). notes, a list, gets one line on the hook review: how
    many hooks were left off, or that it offers no such choice and was left for the owner."""
    screen = commands.tmux("capture-pane", "-p", "-t", target, check=False).stdout
    if any(d in screen for d in CODEX_DIALOGS):
        with commands.keys_lock(True):
            commands.tmux("send-keys", "-t", target, "Enter", check=True)
        return False
    if CODEX_HOOK_REVIEW in screen:
        choice = CODEX_HOOK_UNTRUSTED.search(screen)
        count = CODEX_HOOK_COUNT.search(screen)
        if choice:
            with commands.keys_lock(True):
                commands.tmux("send-keys", "-t", target, *(["Enter"] if choice.group(1) else ["-l", choice.group(2)]),
                              check=True)
            note = "Codex: %s, left untrusted (review in Codex's /hooks)" % (
                count.group(1) if count else "some hooks are new or changed")
        else:
            note = "Codex asks to review its hooks and offers no 'Continue without trusting': answer it in the agent's window"
        if note not in notes:
            notes.append(note)
        return False
    return any(p in screen for p in CODEX_PROMPT)


def start_codex(target, codex, cwd, model, effort, brief, name):
    """Start a new Codex agent in pane target, in folder cwd: full access, no approval prompts, no update check, the
    model and effort when given. Then wait for its new thread and its prompt,
    answering its start-up dialogs meanwhile (codex_prompt_up), /rename it to name (if any, and only once the prompt
    shows), and `codex queue` the brief. Returns {"launch": the line typed, "thread": its id or None, "notes": lines
    on the hook review (codex_prompt_up) and on a prompt that never showed, "queued", "queue_output"}, with "error"
    when the agent did not get its brief (no thread within CODEX_THREAD_SECONDS, or the queue failed). Raises
    CommandError if tmux fails."""
    before = registry.codex_live_ids()
    cmd = [codex, "-s", "danger-full-access", "-a", "never", "-C", cwd] + registry.CODEX_NO_UPDATE_PROMPT + \
        reviewgate.codex_hook_args(cwd) + (["-m", model] if model else []) + (codex_effort(effort) if effort else [])
    line = shell_line(None, cmd, None)
    type_line(target, line, None)
    notes = []
    started = {"launch": line, "thread": None, "notes": notes}

    def new_thread():
        codex_prompt_up(target, notes)     # a dialog may hold the thread back
        return (registry.codex_live_ids() - before) or None
    new = wait_for(new_thread, CODEX_THREAD_SECONDS, 1.0)
    if not new:
        started["error"] = "codex thread did not appear within %d s; brief not queued, no /rename" % CODEX_THREAD_SECONDS
        return started
    started["thread"] = sorted(new)[0]
    up = wait_for(lambda: codex_prompt_up(target, notes), CODEX_PROMPT_SECONDS, 1.5)
    if name and up:
        send_line(target, "/rename " + name)
        time.sleep(2)
    elif name:
        notes.append("Codex's prompt did not show within %s s, so the agent was not renamed" % CODEX_PROMPT_SECONDS)
    queued, out = queue_codex(codex, started["thread"], brief)
    started.update(queued=queued, queue_output=out[:200])
    if not queued:
        started["error"] = "codex queue failed: " + out[:300]
    return started
