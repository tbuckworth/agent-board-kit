"""A live agent's pane, and its effort read and changed from outside it: for fast mode (agentmail/fastmode.py), and
whether keys may go to the agent's idle prompt now (place, screen, view, ready), which agent-send checks too before
it types at a Codex agent's prompt: a message, or the note releasing one held after an interrupt
(codexqueue.in_the_way).

Claude Code and Codex both step the effort of the session in front of them one level down on shift+down and up on
shift+up, for that session only (nothing is saved), mid-turn too, leaving a half-typed draft alone, and show the level
they reach: Claude Code for a moment over its prompt box, or in the box's top rule ("◐ medium · /effort"), Codex
always in its status line under its prompt ("GPT-6.1-Sol medium · ~/repo"). Their keys stop at xhigh (Claude Code
says "shift+↑ stops below max"; Codex's go on to max). Neither has another way in from outside: Claude Code runs a
slash command only when it is typed (a message on its inbox socket reaches the model as text, never as a command),
and a typed /effort low..xhigh also saves the level as that model's default for new sessions in
~/.claude/settings.json, which the keys do not.

The keys were tried, in throwaway sessions with an isolated config, on the versions tested names: TESTED's, tried by
hand while fast mode was written, and those agent-mail fast-trial (agentmail/fasttrial.py) has passed on this machine
since, in TESTED_PATH. Any other version gets no keys (untested), since there shift+down may be another key's (a
choice in a dialog, the history). Before each
key the pane must show the agent's idle prompt and nothing else that takes keys (ready): not tmux's copy mode, the
agent itself in front, no dialog, nobody typing; and the level is read only from the agent's own status line
(view), waiting up to READ_SECONDS for it to change.

Where the agent is (place), whether its version was tried (untested), whether its pane takes keys now (ready), the
level it runs at (current) and setting it (set_level) are separate, so fast mode can say which stood in its way."""
import contextlib
import os
import re
import time
from typing import NamedTuple

from . import claude_session, commands, config, jsonfile, procs, registry

# The versions whose effort keys, prompt and status line were tried by hand as described above, by CLI. With the
# versions in TESTED_PATH, they make the one list fast mode reads (tested).
TESTED = {"claude": ("2.1.325", "2.1.327", "2.1.328", "2.1.329", "2.1.330"),
          "codex": ("0.169.0", "0.170.0", "0.171.0")}
# The versions agent-mail fast-trial passed on this machine, {cli: {version: when it passed, ISO 8601}}.
TESTED_PATH = config.STATE_DIR / "fast-tested.json"
# Every effort level either CLI names, lowest first: the order the keys step through.
ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
LOW = "low"
KEYED = ("medium", "high", "xhigh")   # the levels above LOW that the keys can lower to it and put back
KEYS = {-1: "S-Down", 1: "S-Up"}
NAME = {"claude": "Claude Code", "codex": "Codex"}   # each CLI's name in what fast mode says
AGENT_COMMANDS = ("claude", "node", "codex")   # what tmux names the agent in front of its pane (#{pane_current_command})
# A Claude Code session's registry status while its prompt takes keys: idle alone, for mid-turn a dialog (a
# permission prompt) can open between the check before a key and the key itself.
AGENT_STATUSES = ("idle",)
# How long a key is given to show a new level. Claude Code leaves a level out of the box's top rule while a notice
# holds the line over the box (only the level it was started at, it seems), and shows it over the box once the notice
# goes, some 6 s later in the trials.
READ_SECONDS = 10.0
READ_STEP = 0.1      # how often the pane is read meanwhile
TYPING_SECONDS = 5   # keys from a tmux client of the pane's window this recently: someone may be typing there
TRANSCRIPT_TAIL = 256 << 10
RULE = re.compile(r"^─{3,}")                                   # Claude Code's prompt box: its top and bottom rules
PROMPT = {"claude": "❯", "codex": "›"}   # what the line a draft is typed in starts with: the prompt
CHOICE = re.compile(r"^\s*(?:[❯›>]\s*)?\d+\.\s")               # a numbered choice: a dialog, a menu or a picker
DIALOG = re.compile(r"esc to cancel|enter to (?:select|confirm|save)", re.I)   # a dialog's or picker's own hints
NOTICE_INDENT = 8    # Claude Code draws a notice over its box flush right, after at least this many spaces
LAST_LINES = 6       # Codex's composer is within this many lines of the screen's last one (a blank line, its status
                     # line, which a long folder wraps, and its footer under it)
STOPS = "stops below max"   # what Claude Code says over its box for shift+up at xhigh: proof it runs at xhigh
LEVEL_LINE = {"claude": re.compile(r"\b(%s) · /effort" % "|".join(ORDER)),
              "codex": re.compile(r"^  \S.*?\b(%s) · " % "|".join(ORDER))}
VERSION_IN_PATH = re.compile(r"/releases/(\d+\.\d+\.\d+)-[^/]*/bin/codex")   # Codex's standalone install layout


class Pane(NamedTuple):
    id: str          # the tmux pane id, "%17"
    agent_pid: int   # the agent's process in it: claude, or the codex that holds its thread's lock


class EffortError(Exception):
    """The keys were pressed and the level asked for was not read back. level is the last level read after a key
    (None: none was), pressed how many keys were sent, so a caller knows whether anything may have changed."""

    def __init__(self, message, level, pressed):
        super().__init__(message)
        self.level, self.pressed = level, pressed


class Wait(Exception):
    """The pane does not take keys now (ready says why, the message); pressed keys were sent before it stopped. risk:
    the last key was followed at once by the prompt going (a dialog opening as it was pressed), so it may have moved a
    dialog's highlight."""

    def __init__(self, message, pressed, risk):
        super().__init__(message)
        self.pressed, self.risk = pressed, risk


def place(sess):
    """(the Pane of live session sess (a registry session), "") or (None, why there is none): a Claude Code agent in
    agent-spawn's layout (claude_session.movable), or a Codex thread's codex in its pane (registry.codex_pane)."""
    if sess["cli"] == "claude":
        pane, why = claude_session.movable(sess)
        if pane == claude_session.BG:
            return None, "it is a background session (claude --bg), with no pane to press keys in"
        return (Pane(pane, int(sess["pid"])), "") if pane else (None, "its pane was not found: " + why)
    pane, pid, why = registry.codex_pane(sess["id"])
    return (Pane(pane, pid), "") if pane else (None, why)


def version(sess, pane):
    """The version of the agent in pane: Claude Code's as its registry entry records it, Codex's from the path of
    the program its process runs (VERSION_IN_PATH), or None when neither says."""
    if sess["cli"] == "claude":
        return sess.get("version") or None
    try:
        m = VERSION_IN_PATH.search(os.readlink("/proc/%d/exe" % pane.agent_pid))
    except OSError:
        return None
    return m.group(1) if m else None


def _tested_file():
    """TESTED_PATH's {cli: {version: when}}, {} while there is none. Raises ValueError naming the file when it is not
    JSON (jsonfile.read) or not of that shape, and OSError when it cannot be read: a damaged list is never read as
    empty, nor written over (add_tested). The operator looks at it and fixes it, or moves it aside, which leaves the
    versions TESTED names until the trial passes the others again."""
    data = jsonfile.read(TESTED_PATH)
    if not (isinstance(data, dict) and all(isinstance(v, dict) and all(isinstance(k, str) and isinstance(w, str)
                                                                        for k, w in v.items())
                                           for v in data.values())):
        raise ValueError("%s is not {cli: {version: when}}" % TESTED_PATH)
    return data


def tested(cli):
    """The versions of cli whose keys were tried, the one list: TESTED's, and those agent-mail fast-trial passed here
    (TESTED_PATH), read anew at each call, so that a version the trial adds gets keys in a running daemon. Raises
    ValueError or OSError when TESTED_PATH cannot be read (_tested_file)."""
    return set(TESTED[cli]) | set(_tested_file().get(cli, {}))


def add_tested(cli, version, when):
    """Record in TESTED_PATH, written whole or not at all, that agent-mail fast-trial passed cli's version at when (an
    ISO 8601 time), keeping what the file holds. Raises ValueError or OSError, writing nothing, when the file there
    cannot be read (_tested_file)."""
    data = _tested_file()
    data.setdefault(cli, {})[version] = when
    jsonfile.write(TESTED_PATH, data, jsonfile.PLAIN, 1)


def untested(sess, pane):
    """Why the agent in pane gets no keys, or "": its version could not be told, or is not on the tested list
    (tested), or the list cannot be read. The words name the command that tries a version's keys and adds it."""
    name = NAME[sess["cli"]]
    ver = version(sess, pane)
    if ver is None:
        return "which %s version runs it could not be told" % name
    try:
        known = tested(sess["cli"])
    except (OSError, ValueError) as e:
        return "fast mode's tested list cannot be read: %s" % e
    if ver not in known:
        return "%s %s isn't on fast mode's tested list yet: `agent-mail fast-trial %s --version %s` tries its keys " \
               "and adds it" % (name, ver, sess["cli"], ver)
    return ""


def view(cli, text):
    """The status lines of a cli agent's idle prompt on screen text, which the level is read from, or None when the
    screen's foot is not that prompt alone. Neither may show a numbered choice or a dialog's hints (DIALOG) from the
    prompt down.
    Claude Code: the prompt box (a rule, a line starting ❯, perhaps the draft's further lines, a rule) with at most
    two lines of footer under it, and over its top rule a blank line or a notice drawn flush right (NOTICE_INDENT), so
    that the agent's own output, which starts at the left, is never read as a level. Its status lines are that line
    and the top rule. A dialog that Claude Code draws over a box it keeps is not told from output here: its registry
    entry says so (ready).
    Codex: its composer (a line starting ›) within LAST_LINES of the screen's foot, and as the next line that is not
    blank, with at most one blank line between, the status line, its status line; an overlay or a pager puts something
    else there."""
    lines = text.rstrip("\n").split("\n")
    mark = PROMPT[cli]
    starts = [i for i, line in enumerate(lines) if line.startswith(mark)]
    if not starts:
        return None
    p = starts[-1]
    if any(CHOICE.match(line) or DIALOG.search(line) for line in lines[p:]):
        return None
    if cli == "codex":
        status = p + 1 + (p + 1 < len(lines) and not lines[p + 1].strip())
        if p < len(lines) - LAST_LINES or status >= len(lines) or not LEVEL_LINE["codex"].match(lines[status]):
            return None
        return [lines[status]]
    if p < 2 or not RULE.match(lines[p - 1]):
        return None
    over, rule = lines[p - 2], lines[p - 1]
    if over.strip() and not (len(over) - len(over.lstrip()) >= NOTICE_INDENT and len(over) >= len(rule) - 2):
        return None
    bottom = next((i for i in range(p + 1, len(lines)) if RULE.match(lines[i])), None)
    if bottom is None or len([line for line in lines[bottom + 1:] if line.strip()]) > 2:
        return None
    return [over, rule]


def shown(cli, status):
    """The level the status lines status (view's) show, or None: Claude Code's "shift+↑ stops below max" (STOPS) is
    xhigh, since shift+up says it only there."""
    found = ["xhigh" if cli == "claude" and STOPS in line else m.group(1)
             for line, m in ((line, LEVEL_LINE[cli].search(line)) for line in status or []) if m or STOPS in line]
    return found[-1] if found else None


def screen(pane):
    """What pane shows now. Raises commands.CommandError when tmux cannot read it."""
    return commands.tmux("capture-pane", "-p", "-t", pane.id, check=True).stdout


def ready(sess, pane):
    """Why keys pressed in pane now could reach anything but session sess's idle prompt, or "": the pane in tmux's
    copy mode or running something else in front; a Claude Code session whose registry entry (read again) says it is
    not idle (AGENT_STATUSES), or has a dialog open, or whose pane's shell no longer started it, or a codex no longer
    the child of the pane's shell; someone typing in its window within TYPING_SECONDS; or a screen whose foot is not
    the agent's idle prompt (view). Raises commands.CommandError when tmux cannot read the pane."""
    info = commands.tmux("display-message", "-p", "-t", pane.id, "#{pane_in_mode}\t#{pane_current_command}\t#{pane_pid}",
                         check=True).stdout.rstrip("\n").split("\t")
    in_mode, command, pane_pid = (info + ["", "", ""])[:3]
    if in_mode != "0":
        return "its pane is in tmux's copy mode"
    if command not in AGENT_COMMANDS:
        return "its pane runs %s, not the agent" % (command or "nothing tmux can name")
    if sess["cli"] == "claude":
        now, code, _ = registry.by_id(registry.claude_sessions(), sess["id"])
        if code != 0 or not now["live"]:
            return "the agent is not running"
        if now["status"] not in AGENT_STATUSES:
            return "its session is %s, not idle" % (now["status"] or "of no status")
        if now["waiting_for"]:
            return "a dialog is open in its pane (%s)" % now["waiting_for"]
    if str(procs.ppid(pane.agent_pid)) != pane_pid:
        return "the pane's shell is not the agent's parent"
    activity = commands.tmux("list-clients", "-t", pane.id, "-F", "#{client_activity}", check=False).stdout.split()
    if any(a.isdigit() and time.time() - int(a) < TYPING_SECONDS for a in activity):
        return "someone typed in its window in the last %d s" % TYPING_SECONDS
    if view(sess["cli"], screen(pane)) is None:
        return "its pane does not show the agent's prompt (a dialog or menu may be open)"
    return ""


def current(sess, pane):
    """The effort session sess runs at, or None when nothing says. Codex shows it in its status line. Claude Code
    shows it only for a moment after a change, so it comes from the newest reply in the transcript
    (claude_session.session_effort), else, before any reply, the --effort it was started with."""
    if sess["cli"] == "codex":
        return shown("codex", view("codex", screen(pane)))
    path = claude_session.transcript(sess)
    level = claude_session.session_effort(jsonfile.read_tail(path, TRANSCRIPT_TAIL)) if path else None
    if level:
        return level
    return commands.flag_value(procs.cmdline(int(sess["pid"])), "--effort") if sess.get("pid") else None


def changed(cli, pane, before):
    """(the level the agent's status line shows once it shows one other than before, within READ_SECONDS, "") or
    (None, why none was read: the pane stopped showing the agent's prompt, or ""). Raises commands.CommandError when
    tmux cannot read the pane."""
    deadline = time.time() + READ_SECONDS
    while True:
        status = view(cli, screen(pane))
        level = shown(cli, status)
        if level is not None and level != before:
            return level, ""
        if time.time() >= deadline:
            return None, "" if status is not None else "its pane does not show the agent's prompt (a dialog or menu " \
                                                       "may be open)"
        time.sleep(READ_STEP)


def set_level(sess, pane, target, start, sent):
    """Step the effort of session sess's agent in pane to target with its keys, its pane ready before each key
    (ready), reading the level after each one (changed); start is the level it is believed to run at, which its
    status line overrides when it shows one. sent() is called before each key goes, so that a caller cut off in the
    middle (killed, timed out) has recorded every key that may have gone. Returns how many keys were sent. Raises
    Wait when the pane stops taking keys (before a key, or after one when it no longer shows the agent's prompt:
    Wait.risk), EffortError when no new level shows after a key or target is not read within len(ORDER) keys, and
    commands.CommandError when tmux fails; each may come after some keys changed the level. Holds the keys lock
    (commands.keys_lock) throughout, and never waits for it: another program typing in a pane is a Wait, so that the
    daemon's poll is never held up by a relaunch that holds it for a minute."""
    cli, pressed = sess["cli"], 0
    with contextlib.ExitStack() as keys:
        try:
            keys.enter_context(commands.keys_lock(False))
        except commands.LockBusy:
            raise Wait("another program is typing in an agent's pane (an agent-spawn, agent-switch or failover "
                       "move)", 0, False) from None
        level = shown(cli, view(cli, screen(pane))) or start
        for _ in range(len(ORDER)):
            if level == target:
                return pressed
            if level not in ORDER:
                raise EffortError("its effort reads %r, which is no level its keys step through" % level, None, pressed)
            why = ready(sess, pane)
            if why:
                raise Wait(why, pressed, False)
            key = KEYS[1 if ORDER.index(level) < ORDER.index(target) else -1]
            sent()
            pressed += 1
            commands.tmux("send-keys", "-t", pane.id, key, check=True)
            got, gone = changed(cli, pane, level)
            if gone:
                raise Wait(gone, pressed, True)
            if got is None:
                raise EffortError("no new effort level showed in its pane within %g s of %s (it was %s)"
                                  % (READ_SECONDS, key, level), None, pressed)
            level = got
        raise EffortError("its effort did not reach %s (it reads %s)" % (target, level), level, pressed)

