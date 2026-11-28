"""Delivering a message to a live Codex thread, for agent-send (README.md, section "agent-send", says what it
reports). An agent that is not mid-turn, whose pane is ready and composer empty, gets it typed at its idle prompt
(paste), which Codex takes at once. Otherwise `codex queue` puts it in the thread's queue, and follow says what became
of it: Codex took it (DELIVERED), it waits for the turn under way to end (QUEUED), or Codex holds it (HELD), and
releasing one held because the thread's last turn was interrupted.

How Codex drains its queue, as read in openai/codex at rust-v0.171.0 (commit ccf9be8):
- `codex queue` adds a row to queued_items in queue_1.sqlite (state/src/runtime/queued_items.rs, enqueue) and prints
  "Queued message ITEM for thread THREAD." (tui/src/session_queue_commands.rs).
- The codex process running the thread looks for changed queues every 10 s (ext/queue/src/service.rs,
  watch_external_messages). For a thread that is not Running, Interrupted, Shutdown or NotFound it starts the
  queue's first item as a turn and then deletes its row (dispatch_if_idle); a turn that ends starts the next item at
  once (on_thread_idle).
- A turn ended by an interrupt (Esc) or a budget limit leaves the thread Interrupted (core/src/agent/status.rs), as
  does resuming a thread whose rollout ends so (core/src/session/mod.rs). The look passes an Interrupted thread by
  (watch_external_messages, wake_if_loaded), and its idle signal then carries the cause Interrupted, which
  on_thread_idle ignores (core/src/tasks/lifecycle.rs, emit_thread_idle_lifecycle_if_idle): the item waits. Only a
  new turn ends that state. The TUI starts none for the queue (it ignores thread/queue/changed), and
  thread/queue/start, which would start the item, is served only by the app server that has the thread loaded: the
  agent's own codex, which other processes cannot reach unless it runs under a shared app-server daemon, and the
  agents here do not. So a message held this way is released by typing NOTE at the agent's idle prompt: the note's
  turn starts, and when it ends Codex takes the message.

Codex's queue database is only read, read-only (mode=ro: registry.codex_read); the rollout log, which says whether a
turn is under way and how the last one ended, likewise. What cannot be read is said (UNCHECKED), never guessed."""
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple, Optional

from . import commands, config, launch, paneeffort, registry

# Config key "codex_queue": each number, in seconds. check: how long agent-send waits for Codex to take a message it
# typed at the agent's prompt, or one it can take from its queue (an idle thread's codex looks every 10 s). release:
# how long, after typing NOTE, it waits for Codex to take the message (its note's turn needs a model reply). lock: how
# long it waits for another program typing in a pane (commands.keys_lock) before it queues the message rather than
# typing it, or gives up on the release.
DEFAULTS = {"check_seconds": 15, "release_seconds": 20, "lock_seconds": 5}
STEP = 0.5          # how often the queue and the rollout are read meanwhile
CHUNK = 256 << 10   # the rollout is read backward in pieces of this size, until a turn event
QUEUE_DB = "queue_1.sqlite"       # where registry.codex_db finds it
ITEM = re.compile(r"Queued message (\S+) for thread")   # `codex queue`'s answer, with the queued item's id
# What agent-send types at the idle prompt of an agent whose queue Codex holds after an interrupt.
NOTE = ("[agent-send] A queued message is waiting for you; it arrives when this turn ends. Reply in one line and end "
        "your turn.")
# The tmux buffer a message is pasted from: this process's own, deleted as it is pasted.
BUFFER = "agent-send-%d"
# The wait between a paste and its Enter: past Codex's window after pasted-in text in which Enter does not submit
# (tui/src/bottom_pane/paste_burst.rs). A trial on Codex 0.172.0 took every paste at this wait.
PASTE_SETTLE = 0.4
# What Codex's composer shows when it is empty (tui/src/chatwidget.rs PLACEHOLDER, SIDE_PLACEHOLDER).
PLACEHOLDERS = ("Ask Codex to do anything", "Ask a follow-up question")

DELIVERED, QUEUED, HELD, UNCHECKED = "delivered", "queued", "held", "unchecked"   # an Outcome's state
RUNNING, INTERRUPTED, IDLE = "running", "interrupted", "idle"                     # last_turn's
RUNNING_SINCE = "running since"                                                  # _watch's, beside those
STARTED = ("task_started", "turn_started")        # a turn event's names in a rollout line (Codex reads both)
ENDED = ("task_complete", "turn_complete")
ABORTED = "turn_aborted"
STOPPED = ("interrupted", "budget_limited")       # the abort reasons that leave the thread Interrupted
TURN_EVENTS = tuple(name.encode() for name in STARTED + ENDED + (ABORTED,))   # for a quick look at each line


class Outcome(NamedTuple):
    state: str   # DELIVERED, QUEUED, HELD or UNCHECKED (not confirmed: nothing could be read to tell, or a
                 # typed message was not seen to arrive; why says which)
    why: str     # one line, for the sender


class Before(NamedTuple):
    """Where the thread's rollout log ended just before the message was queued: what happened since is after size."""
    rollout: Optional[Path]   # None: Codex records none (a thread with no turn yet), or error says why not known
    size: int
    error: str                # why the rollout could not be read, or ""


def settings(cfg):
    """DEFAULTS with config codex_queue over them. Raises ValueError naming the key for one that is not a number of
    seconds (0 or more), or not DEFAULTS'."""
    got = dict(DEFAULTS, **(cfg.get("codex_queue") or {}))
    for key, value in got.items():
        if key not in DEFAULTS:
            raise ValueError("config codex_queue has no key %r (it takes %s)" % (key, ", ".join(DEFAULTS)))
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError("config codex_queue.%s must be a number of seconds, not %r" % (key, value))
    return got


def item_id(answer):
    """The queued item's id in `codex queue`'s answer, or None when it names none."""
    m = ITEM.search(answer or "")
    return m.group(1) if m else None


def waiting(thread_id, item):
    """Whether item is still in thread_id's queue in Codex's queue database, read with mode=ro (registry.codex_read),
    so that nothing here can write to Codex's queue, whose rows are Codex's alone. Raises sqlite3.Error naming it
    when it cannot be read (missing, a lock Codex holds past 2 s, another schema). On macOS, where mode=ro may fail for
    a WAL database such as this one (registry.codex_read), a Mac agent's message is then reported unchecked and one
    held after an interrupt is not released there."""
    return bool(registry.codex_read(QUEUE_DB, "select 1 from queued_items where thread_id = ? and id = ?",
                                    (thread_id, item), read_only=True))


def _turn_event(line):
    """The state a rollout line's turn event leaves the thread in, or None for any other line (one cut short too)."""
    if b'"event_msg"' not in line or not any(name in line for name in TURN_EVENTS):
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    payload = d.get("payload") if isinstance(d, dict) and d.get("type") == "event_msg" else None
    kind = payload.get("type") if isinstance(payload, dict) else None
    if kind in STARTED:
        return RUNNING
    if kind in ENDED:
        return IDLE
    if kind == ABORTED:
        return INTERRUPTED if payload.get("reason") in STOPPED else IDLE
    return None


def last_turn(path):
    """(state, offset): what the last turn event in the rollout log at path leaves the thread in, as Codex derives
    its status from the same events (RUNNING: a turn started and not ended; INTERRUPTED: it ended by an interrupt or
    a budget limit; IDLE: any other end, or no turn yet), and the byte offset its line starts at (-1: none). Read
    backward in CHUNK pieces until one is found. Raises OSError when the file cannot be read."""
    with open(path, "rb") as fh:
        pos, tail = fh.seek(0, os.SEEK_END), b""
        while pos > 0:
            start = max(0, pos - CHUNK)
            fh.seek(start)
            lines = (fh.read(pos - start) + tail).split(b"\n")
            tail, whole = (lines[0], lines[1:]) if start > 0 else (b"", lines)
            offset = start + (len(tail) + 1 if start > 0 else 0)
            offsets = []
            for line in whole:
                offsets.append(offset)
                offset += len(line) + 1
            for line, at in zip(reversed(whole), reversed(offsets)):
                state = _turn_event(line)
                if state:
                    return state, at
            pos = start
    return IDLE, -1


def before(thread_id):
    """Before(the thread's rollout log, its size now, ""): taken just before the message is queued. Codex records no
    rollout for a thread with no turn yet: None. One that cannot be read, nor where it is (Codex's state database),
    is Before(None, 0, why)."""
    try:
        rollout = registry.codex_rollout(thread_id)
        return Before(rollout, rollout.stat().st_size if rollout else 0, "")
    except (sqlite3.Error, OSError) as e:
        return Before(None, 0, str(e))


def _turn(rollout):
    """last_turn of rollout; a thread with no rollout (None) has had no turn. Raises OSError (last_turn)."""
    return last_turn(rollout) if rollout else (IDLE, -1)


def composer_empty(screen):
    """Whether Codex's composer on screen (paneeffort.view's: its idle prompt) holds no draft: its line shows only
    the placeholder Codex draws in an empty one."""
    lines = [line for line in screen.rstrip("\n").split("\n") if line.startswith(paneeffort.PROMPT["codex"])]
    return bool(lines) and lines[-1][len(paneeffort.PROMPT["codex"]):].strip() in PLACEHOLDERS


def in_the_way(sess, pane):
    """What keeps agent-send from typing at the idle prompt of sess's Codex agent in pane now (a message, or the note
    releasing a held one): paneeffort.ready's reason (no idle prompt alone, copy mode, a dialog, someone typing in
    TYPING_SECONDS), or a draft in its composer; "" when nothing does. Raises CommandError (tmux)."""
    return paneeffort.ready(sess, pane) or ("" if composer_empty(paneeffort.screen(pane)) else
                                            "a draft is in its composer")


def _watch(thread_id, item, mark, seconds):
    """Follow item for up to seconds: DELIVERED once it has left the queue; at once, RUNNING when a turn that
    started before it was queued is under way (it arrives when that turn ends), INTERRUPTED when the last turn was
    interrupted (Codex holds it); at the end, RUNNING_SINCE when a turn started since and is under way, else IDLE.
    A turn started since is given the time, as Codex writes the item's own turn start before it deletes its row."""
    deadline = time.monotonic() + seconds
    while True:
        if not waiting(thread_id, item):
            return DELIVERED
        state, at = _turn(mark.rollout)
        if state == INTERRUPTED or (state == RUNNING and at < mark.size):
            return state
        if time.monotonic() >= deadline:
            return RUNNING_SINCE if state == RUNNING else IDLE
        time.sleep(STEP)


def _idle_why(sess, seconds):
    """Why an idle agent's codex has not taken its message, as far as its pane says."""
    said = "Codex has not taken it in %g s, though the agent is not mid-turn" % seconds
    pane, why = paneeffort.place(sess)
    if pane is None:
        return "%s; its pane was not found to see why (%s)" % (said, why)
    try:
        screen = paneeffort.screen(pane)
    except commands.CommandError as e:
        return "%s; its pane could not be read to see why (%s)" % (said, e)
    if paneeffort.view("codex", screen) is None:
        return "%s: its pane shows a dialog or a menu, not its idle prompt (Codex's question about a cheaper " \
               "model holds a queued message until it is answered)" % said
    return "%s, and nothing on its screen says why" % said


HELD_WHY = "its last turn was interrupted, so Codex holds it until another turn starts"


def _type_note(sess, thread_id, item, mark, lock_seconds):
    """Type NOTE and Enter at the idle prompt of sess's agent, held after an interrupt, if that is safe: under the keys
    lock (waiting lock_seconds for it), with its pane ready (paneeffort.ready: the agent's idle prompt alone, no copy
    mode, no dialog, nobody typing in TYPING_SECONDS), its composer empty, the item still waiting and the thread
    still interrupted. Returns (the rollout's size just before the note went, None), or (None, the Outcome when it
    did not go)."""
    pane, why = paneeffort.place(sess)
    if pane is None:
        return None, Outcome(HELD, "%s; agent-send did not type a note to release it: %s" % (HELD_WHY, why))
    try:
        with commands.keys_lock(lock_seconds):
            if not waiting(thread_id, item):
                return None, Outcome(DELIVERED, "Codex took it from its queue")
            state, at = _turn(mark.rollout)
            if state == RUNNING:
                return None, Outcome(QUEUED, "a turn started after it was queued: it arrives when that turn ends")
            if state != INTERRUPTED:   # its last turn ended since, so Codex takes it within its 10 s look
                return None, Outcome(QUEUED, "its interrupted turn was followed by another, which ended: Codex takes "
                                             "it at its next look at the queue")
            why = in_the_way(sess, pane)
            if why:
                return None, Outcome(HELD, "%s; agent-send did not type a note to release it, since %s" % (HELD_WHY,
                                                                                                           why))
            size = mark.rollout.stat().st_size if mark.rollout else 0
            launch.send_line(pane.id, NOTE)
            return size, None
    except commands.LockBusy:
        return None, Outcome(HELD, "%s; agent-send did not type a note to release it, since another program is "
                                   "typing in an agent's pane (an agent-spawn, agent-switch or failover move)"
                                   % HELD_WHY)
    except commands.CommandError as e:
        return None, Outcome(HELD, "%s; agent-send did not type a note to release it, since tmux failed: %s"
                                   % (HELD_WHY, e))


def _released(thread_id, item, mark, typed, seconds):
    """After NOTE went at the rollout's size typed: DELIVERED once the item leaves the queue within seconds; at the
    end, QUEUED while the note's turn is under way, else HELD with what became of the note."""
    deadline = time.monotonic() + seconds
    while waiting(thread_id, item):
        if time.monotonic() >= deadline:
            state, at = _turn(mark.rollout)
            if at >= typed and state == RUNNING:
                return Outcome(QUEUED, "its last turn was interrupted, so Codex held it; agent-send typed a note at "
                                       "its idle prompt to release it, and it arrives when the note's turn ends")
            if at >= typed and state == INTERRUPTED:
                return Outcome(HELD, "its last turn was interrupted, so Codex holds it; the turn of the note "
                                     "agent-send typed at its idle prompt to release it was interrupted too")
            if at >= typed:
                return Outcome(HELD, "its last turn was interrupted, so Codex held it; agent-send typed a note at its "
                                     "idle prompt, whose turn ended, and Codex has not taken it in %g s" % seconds)
            return Outcome(HELD, "its last turn was interrupted, so Codex holds it; a note typed at its idle prompt "
                                 "to release it started no turn in %g s" % seconds)
        time.sleep(STEP)
    return Outcome(DELIVERED, "its last turn was interrupted, so Codex held it; agent-send typed a note at its idle "
                              "prompt, and Codex took it once the note's turn ended")


def said_since(rollout, offset):
    """The user messages in rollout after byte offset, oldest first, as Codex records them: a user message item
    (its input_text parts joined) or a user_message event."""
    out = []
    with open(rollout, "rb") as fh:
        fh.seek(offset)
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:   # a line cut short: Codex is still writing it
                continue
            p = d.get("payload") if isinstance(d, dict) else None
            if not isinstance(p, dict):
                continue
            if d.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                out.append("".join(c.get("text") or "" for c in p.get("content") or []
                                   if isinstance(c, dict) and c.get("type") == "input_text"))
            elif d.get("type") == "event_msg" and p.get("type") == "user_message":
                out.append(p.get("message") or "")
    return out


TYPED = "typed at its idle prompt, as a paste and Enter"


def paste(sess, mark, text):
    """Type text at the idle prompt of sess's live Codex agent, as a person would, so Codex takes it at once rather than
    at its next look at the queue (every 10 s), when nothing is in the way: under the keys lock (waiting lock_seconds
    for it), no turn under way (one interrupted is no obstacle: a typed message starts a turn, where Codex holds a
    queued one), its pane ready (paneeffort.ready: the agent's idle prompt alone, no copy mode, no dialog, nobody
    typing in TYPING_SECONDS) and its composer empty. It goes in as one bracketed paste (tmux paste-buffer -p), so its
    new lines stay in the message and a leading / @ or ! stays text, then Enter. mark is before(sess's thread), taken
    just before; it reads config codex_queue (settings) and the agent's pane through tmux, and pastes from a tmux
    buffer of its own, which the paste deletes. Returns (None, why not) when nothing was typed, for the caller to queue
    it (a tmux command that fails before the paste's own is no paste either); else (the Outcome, None): DELIVERED once
    its rollout shows text as a user message, within check_seconds; otherwise UNCHECKED, saying why: a turn started but
    its message was not found to be text, the rollout could not be read, Enter could not be pressed, or no turn started,
    text being then perhaps in its composer, unsent. Never HELD, which says a message waits in Codex's queue. Never
    raises for what it cannot read or do."""
    if mark.error:
        return None, "its rollout cannot be read (%s)" % mark.error
    try:
        nums = settings(config.load())
    except (OSError, ValueError) as e:
        return None, "the config cannot be read (%s)" % e
    pane, why = paneeffort.place(sess)
    if pane is None:
        return None, why
    try:
        with commands.keys_lock(nums["lock_seconds"]):
            if _turn(mark.rollout)[0] == RUNNING:
                return None, "the agent is mid-turn"
            why = in_the_way(sess, pane)
            if why:
                return None, why
            buffer = BUFFER % os.getpid()
            # a failed load-buffer or paste-buffer (the pane gone, say) typed nothing, and the caller queues it
            commands.tmux("load-buffer", "-b", buffer, "-", check=True, input=text)
            commands.tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", pane.id, check=True)
            try:   # text is in its composer from here: the caller must not queue it as well
                time.sleep(PASTE_SETTLE)
                commands.tmux("send-keys", "-t", pane.id, "Enter", check=True)
            except commands.CommandError as e:
                return Outcome(UNCHECKED, "it was pasted at its idle prompt, but Enter could not be pressed (%s): it "
                                          "is in its composer, unsent" % e), None
    except commands.LockBusy:
        return None, "another program is typing in an agent's pane"
    except (commands.CommandError, sqlite3.Error, OSError) as e:
        return None, "its pane or rollout could not be read, or tmux failed (%s)" % e
    return _taken(sess["id"], mark, text, nums["check_seconds"]), None


def _taken(thread_id, mark, text, seconds):
    """After text was pasted and Enter pressed: DELIVERED once the thread's rollout (found anew for a thread that had
    none) shows it as a user message after mark, within seconds; then UNCHECKED, saying whether a turn started since."""
    start = mark.size if mark.rollout else 0
    deadline = time.monotonic() + seconds
    try:
        while True:
            rollout = mark.rollout or registry.codex_rollout(thread_id)
            if rollout and text in said_since(rollout, start):
                return Outcome(DELIVERED, TYPED)
            if time.monotonic() >= deadline:
                if _turn(rollout)[1] >= start:
                    return Outcome(UNCHECKED, "%s; a turn started, but its rollout does not show this message in "
                                              "%g s" % (TYPED, seconds))
                return Outcome(UNCHECKED, "it was pasted at its idle prompt and Enter pressed, but no turn started in "
                                          "%g s: it may be in its composer, unsent" % seconds)
            time.sleep(STEP)
    except (sqlite3.Error, OSError) as e:
        return Outcome(UNCHECKED, "%s, but its rollout could not be read (%s), so whether it arrived was not checked"
                       % (TYPED, e))


def follow(sess, mark, answer):
    """What became of the message that `codex queue` (its answer, answer) just put in live Codex session sess's
    queue, mark being before(sess's thread) taken just before: an Outcome. Codex has it either way. This reads config
    codex_queue (settings), Codex's queue database (waiting) and the thread's rollout log, and the agent's pane
    through tmux (paneeffort) when Codex holds the message; it types NOTE there to release one held after an
    interrupt (_type_note says when). Never raises for what it cannot read or do: that is an UNCHECKED or HELD
    Outcome that says so."""
    item = item_id(answer)
    if not item:
        return Outcome(UNCHECKED, "Codex's answer named no queued item, so whether it arrived was not checked")
    if mark.error:
        return Outcome(UNCHECKED, "its rollout could not be read (%s), so whether it arrived was not checked"
                       % mark.error)
    try:
        nums = settings(config.load())
    except (OSError, ValueError) as e:
        return Outcome(UNCHECKED, "the config cannot be read (%s), so whether it arrived was not checked" % e)
    tid = sess["id"]
    try:
        seen = _watch(tid, item, mark, nums["check_seconds"])
        if seen == DELIVERED:
            return Outcome(DELIVERED, "Codex took it from its queue")
        if seen == RUNNING:
            return Outcome(QUEUED, "the agent is mid-turn: it arrives when that turn ends")
        if seen == RUNNING_SINCE:
            return Outcome(QUEUED, "a turn started after it was queued: it arrives when that turn ends")
        if seen == IDLE:
            return Outcome(HELD, _idle_why(sess, nums["check_seconds"]))
        typed, outcome = _type_note(sess, tid, item, mark, nums["lock_seconds"])
    except (sqlite3.Error, OSError) as e:
        return Outcome(UNCHECKED, "its queue or rollout could not be read (%s), so whether it arrived was not checked"
                       % e)
    if outcome:
        return outcome
    try:
        return _released(tid, item, mark, typed, nums["release_seconds"])
    except (sqlite3.Error, OSError) as e:
        return Outcome(UNCHECKED, "its last turn was interrupted, so Codex held it; agent-send typed a note at its "
                                  "idle prompt to release it, but its queue or rollout could not be read again (%s)"
                       % e)
