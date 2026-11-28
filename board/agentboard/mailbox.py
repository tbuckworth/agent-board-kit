"""The Mail tab: mail between the owner and the agents, kept by the mailroom (its README, "The Mail tab's mailbox"),
which holds each mail's content and the owner's read, archive and snooze state. The board reads and changes it as the
owner through `agent-mail mailbox ...` and hands its JSON answers to the Mac as they are, but for the sessions and
group colours the lists may ask for (add_sessions, add_groups), which it reads from the mailroom's store and the
board's layout. That command refuses to change anything inside an agent session; the collector and `agent-board act`
run from the Mac's SSH login, outside any.
"""
import json
import os
import re
import sqlite3
import time

from . import colours, groups, sources

REFRESH = 30             # seconds between the snapshot's reads of the summary
# BOXES, OPS and UPTO_OPS are the mailroom's (agentmail/mailbox.py), repeated here to check a request before it goes
BOXES = ("inbox", "snoozed", "archived", "all")
OPS = ("read", "unread", "archive", "inbox", "snooze")
UPTO_OPS = ("read", "archive", "snooze")       # the ops that name the newest agent mail the owner saw (`upto`)
# whole values (matched with fullmatch):
THREAD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")   # never an option
TAG_RE = re.compile(r"[a-z0-9]{6,12}")                       # the mailroom's routing tag (its routing.TAG_RE)
# an idempotency key, made by the Mac or the phone's page: a reply's, or a message to the orchestrator's; not "-..."
KEY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{15,63}")
MESSAGE_RE = re.compile(r"[si][0-9]{1,12}")   # s<id>: an agent's mail; i<id>: the owner's own
MAX_QUERY = 200          # characters in a search
MAX_LIMIT = 500          # threads in one list
MAX_THREADS = 200        # threads in one mark
MAX_REPLY = 20000        # characters in a reply (the mailroom's own limit)
MAX_FILES = 20           # files with one reply, and their bytes together (the mailroom's MAX_ANSWER_FILES and
MAX_FILE_BYTES = 25 << 20   # MAX_ANSWER_FILE_BYTES; the Mac's MailModel.maxFiles and maxFileBytes)
MAX_FILE_NAME = 255      # characters in a file's name; the mailroom makes a safe name of it
ANSWER_REFUSED = 6       # `mailbox answer`'s exit code when it refuses a reply before recording it (the mailroom's
                         # mailbox.ANSWER_REFUSED); any other failure may come after it recorded it
# characters (or bytes) of the largest mail_reply request: the most files as base64, and a megabyte for the rest (the
# longest text, at most 6 bytes a character in JSON, the names and the fields)
MAX_REPLY_REQUEST = MAX_FILE_BYTES * 4 // 3 + (1 << 20)
BIG_FILES = sources.BOARD_STATE / "mail-files"   # files too big for a reply, copied here for the agent (receive)
MAX_BIG_FILE = 16 << 30  # bytes in one of them


def _call(args, run, stdin=None):
    """agent-mail mailbox ARGS: its JSON answer. Raises sources.CommandError."""
    return sources.call("agent-mail", ["mailbox"] + args, run, stdin)


def installed():
    """Whether the mailroom (sources.mailroom_root: the kit's mailroom/, or AGENT_MAILROOM_DIR) keeps a mailbox (its
    agentmail/mailbox.py) and its agent-mail is on PATH."""
    root = sources.mailroom_root()
    return (bool(root) and os.path.isfile(os.path.join(root, "agentmail", "mailbox.py"))
            and sources.find_bin("agent-mail") is not None)


def listing(box, query, limit, run=sources.run):
    """The threads in `box` (one of BOXES), newest first, matching `query` (None: all), at most `limit`; with the
    counts of the inbox, its unread threads and the snoozed ones."""
    args = ["list", "--box", box, "--limit", str(limit)] + (["--query", query] if query else [])
    return _call(args, run)


def add_sessions(listed, store):
    """mail_list's answer `listed`, each of its threads given "sessions": the board's agents that wrote in it, [] for
    none (`store`, a sources.MailStore: thread_sessions), so that the Mac can mark the threads of a busy agent; or,
    when the store cannot be read, the threads as they were and "sessions_error" saying why. Changes and returns
    `listed`."""
    threads = listed.get("threads")
    rows = [t for t in threads if isinstance(t, dict) and isinstance(t.get("id"), str)] if isinstance(threads, list) else []
    try:
        by = store.thread_sessions([t["id"] for t in rows])
    except (sqlite3.Error, OSError) as e:
        listed["sessions_error"] = sources.error_text(e)
        return listed
    for t in rows:
        t["sessions"] = by.get(t["id"], [])
    return listed


def add_groups(listed, store):
    """mail_list's answer `listed`, each of its threads given the group of the agent of its newest agent mail
    ("newest"), as the board places it (groups.placed: the layout and each tile's latest group), with that group's
    colours (colours.of: "group", "subgroup", "colour", "shade"), for the strip down its row on the Mac:
    a thread with no agent mail, or whose newest is from a session on another machine, the orchestrator or a tile in no
    group, is Ungrouped. When the store, the layout, the tiles' groups or colours.json cannot be read, the threads as
    they were and "groups_error" saying why. Changes and returns `listed`."""
    threads = listed.get("threads") if isinstance(listed.get("threads"), list) else []
    rows = [t for t in threads if isinstance(t, dict) and isinstance(t.get("id"), str)
            and isinstance(t.get("newest"), int) and not isinstance(t["newest"], bool)]
    try:
        layout = groups.layout()
        place = groups.placed(groups.history(), layout)
        coloured = colours.for_layout(layout, colours.load())
        by = store.sent_sessions([t["newest"] for t in rows if t["newest"]])
    except (sqlite3.Error, OSError, ValueError) as e:
        listed["groups_error"] = sources.error_text(e)
        return listed
    for t in rows:
        t.update(colours.of(coloured, place.get(by.get(t["newest"]))))
    return listed


def show(thread, run=sources.run):
    """One thread: its list entry, its messages, and the whole thread as one sanitized HTML page (`document`)."""
    return _call(["show", "--", thread], run)


def mark(threads, op, upto, until, run=sources.run):
    """Apply `op` (one of OPS) to `threads`. `upto` (the newest agent mail the owner saw) goes with UPTO_OPS, `until`
    (epoch seconds) with snooze; None leaves either out."""
    args = ["mark", "--op", op] + (["--upto", str(upto)] if upto is not None else []) + (
        ["--until", repr(float(until))] if until is not None else [])
    return _call(args + ["--"] + list(threads), run)


def answer(thread, key, text, files, run=sources.run):
    """The owner's reply to the newest agent mail in `thread`, `key` its idempotency key, with `files` ([{"name",
    "data": base64}], [] for none), which the agent gets as files on its own machine. The mailroom routes it as it
    routes the owner's emailed replies, within a second or two. A reply without files goes as plain text on stdin,
    with them as JSON (`--json`)."""
    if not files:
        return _call(["answer", "--key", key, "--", thread], run, stdin=text)
    return _call(["answer", "--key", key, "--json", "--", thread], run, stdin=json.dumps({"text": text, "files": files}))


def set_mode(command, thread, on, run):
    """Run `agent-mail mailbox COMMAND -- THREAD on|off` and return its decoded JSON.

    COMMAND is the mode subcommand and its options. The mailroom changes the mode for the thread's route and
    returns the thread. A command or decoding failure raises sources.CommandError.
    """
    return _call([*command, "--", thread, "on" if on else "off"], run)


def part(key, index, run=sources.run):
    """An attachment of a stored message, an agent's mail or the owner's reply: {name, type, data (base64)}."""
    return _call(["part", "--", key, str(index)], run)


def receive(stream, now):
    """`agent-board receive`: one file too big for a reply, sent from the Mail tab, kept for the agent the reply names
    it to. `stream` (binary) holds a JSON line {"name": text, "size": bytes}, then the file's bytes. It is written to
    BIG_FILES/<day>/<name> (made safe; " (2)" and on when taken), readable by the owner only, and its path returned.
    Raises ValueError for a header that is not one, a file over MAX_BIG_FILE, or fewer bytes than it said, and OSError
    when it cannot be written; either way it leaves nothing."""
    head = json.loads(stream.readline(4096) or b"null")
    if not (isinstance(head, dict) and isinstance(head.get("name"), str) and isinstance(head.get("size"), int)
            and not isinstance(head["size"], bool) and 0 <= head["size"] <= MAX_BIG_FILE):
        raise ValueError("the first line must be {name, size}, at most %d bytes" % MAX_BIG_FILE)
    name = re.sub(r"[/\\\x00-\x1f]", "_", head["name"]).strip().lstrip(".")[:MAX_FILE_NAME] or "file"
    day = BIG_FILES / time.strftime("%Y-%m-%d", time.localtime(now))
    day.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem, ext = os.path.splitext(name)
    path, n = day / name, 1
    while path.exists() or path.with_name(path.name + ".part").exists():
        n += 1
        path = day / ("%s (%d)%s" % (stem, n, ext))
    part = path.with_name(path.name + ".part")
    left = head["size"]
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # ours alone; one already there is not touched
    try:
        with open(fd, "wb") as out:
            while left:
                chunk = stream.read(min(left, 1 << 20))
                if not chunk:
                    raise ValueError("the file ended %d bytes short of %d" % (left, head["size"]))
                out.write(chunk)
                left -= len(chunk)
    except BaseException:
        part.unlink(missing_ok=True)      # our own partial copy: nothing of it is kept
        raise
    part.rename(path)
    return {"path": str(path), "bytes": head["size"]}


def summary(run=sources.run):
    """The snapshot's view of the mailbox: None when the mailroom keeps none (installed), else {"unread", "inbox",
    "read"}, read being the Message-IDs of agent mail to the owner from the last few days that the tab counts as read,
    normalised as sources.norm_id does. Raises sources.CommandError, or ValueError on an answer of another shape.
    The collector reads it every REFRESH seconds."""
    if not installed():
        return None
    got = _call(["summary"], run)
    if not (isinstance(got, dict) and all(isinstance(got.get(k), int) for k in ("unread", "inbox"))
            and isinstance(got.get("read"), list) and all(isinstance(m, str) for m in got["read"])):
        raise ValueError("agent-mail mailbox summary is not {unread, inbox, read}: %s" % str(got)[:200])
    return {"unread": got["unread"], "inbox": got["inbox"], "read": sorted({sources.norm_id(m) for m in got["read"]})}
