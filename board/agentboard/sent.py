"""What the owner sends from the board, in full, and whether each message has been dealt with, so the owner can keep
track of what was said (the README's Sent messages section says what the Mac shows).

Two append-only files in the board's state directory, both readable by the owner only:

  sent.jsonl        one record per message, note, brief, orchestrator request or group-chat post (actions.py
                    writes them), with a short ref such as "k3f9" that the delivered text carries in its last line
  sent-marks.jsonl  one line per mark: {"ts", "ref", "event": "done" | "checked" | "unchecked", "note"?, "by"?}

A message's state comes from its marks: checked (ticked off), done (by the agent, not yet checked), failed (never
delivered) or waiting. Messages recovered from transcripts start ticked off: they are history. seen_ticks ticks a
done message off by "mail" once the owner has seen its agent's emails about it, unless the owner has ticked or
unticked it by hand: the owner's own tick wins.
"""
import hashlib
import os
import re
import secrets
import time

from . import sources

DIR = sources.BOARD_STATE
PATH = DIR / "sent.jsonl"
MARKS = DIR / "sent-marks.jsonl"
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"        # no i, l, o, 0 or 1: easy to read back and type
REF_RE = re.compile(r"^[a-hjkmnp-z2-9]{4}$")
MAX_NOTE = 500
EVENTS = ("done", "checked", "unchecked")
DONE_CMD = "%s done" % (sources.BOARD_BIN / "agent-board")
SEEN_AFTER = 3600          # an agent's email up to an hour after it marked a message done is about that message
SEEN_DAYS = 7              # how far back the mailroom view (and so this) looks
BY_OWNER = "owner"         # a tick or untick the owner made on the board ("mail": seen_ticks)


def done_line(ref, what="it asks"):
    """How a delivered message ends: how to mark it done, the command alone on the last line."""
    return ("Once you have dealt with all of it (done what %s, or answered it), mark it done so it is ticked off on "
            "the board:\n    %s %s -m \"what you did, in one line\"" % (what, DONE_CMD, ref))


def _lines(path):
    return [r for r in sources.read_jsonl(path) if isinstance(r, dict)]


def record(kind, to, text, ok, **facts):
    """Append one message the owner sent. ts: when it was sent (default now)."""
    if not isinstance(text, str) or not text.strip():
        return
    sources.append_jsonl(PATH, dict({"ts": time.time()}, **facts, kind=kind, to=to, ok=ok, text=text.strip()))


def _derived_ref(r):
    """A stable ref for a record written before refs existed."""
    h = hashlib.sha1(("%r|%s" % (r.get("ts"), r.get("text"))).encode("utf-8")).digest()
    return "".join(ALPHABET[b % len(ALPHABET)] for b in h[:4])


def records():
    """Every message on record, oldest first, each with its ref."""
    rows = [r for r in _lines(PATH) if isinstance(r.get("text"), str)]
    for r in rows:
        if not (isinstance(r.get("ref"), str) and REF_RE.match(r["ref"])):
            r["ref"] = _derived_ref(r)
    rows.sort(key=lambda r: r.get("ts") or 0)        # recovered messages were added after later ones
    return rows


def new_ref():
    taken = {r["ref"] for r in records()}
    while True:
        ref = "".join(secrets.choice(ALPHABET) for _ in range(4))
        if ref not in taken:
            return ref


def _fold(rows):
    """Each record with its marks: "done" ({ts, note, by}, the latest), "checked" (bool) and "state"."""
    marks = {}
    for m in sorted(_lines(MARKS), key=lambda m: m.get("ts") or 0):
        if m.get("event") in EVENTS and isinstance(m.get("ref"), str):
            got = marks.setdefault(m["ref"], {})
            if m["event"] == "done":
                got["done"] = {k: m.get(k) for k in ("ts", "note", "by") if m.get(k) is not None}
            else:
                got["checked"] = m["event"] == "checked"
                got["checked_at"] = m.get("ts")
                got["tick"] = {k: m.get(k) for k in ("ts", "by", "note") if m.get(k) is not None}
                got["tick"]["checked"] = got["checked"]
    for r in rows:
        got = marks.get(r["ref"], {})
        r["checked"] = got.get("checked", bool(r.get("recovered")))
        if got.get("done"):
            r["done"] = got["done"]
        if got.get("tick"):
            r["tick"] = got["tick"]         # the latest tick or untick: by BY_OWNER, or by "mail" (seen_ticks)
        if r["checked"] and got.get("checked_at"):
            r["checked_at"] = got["checked_at"]
        r["state"] = ("checked" if r["checked"] else "done" if r.get("done") else
                      "waiting" if r.get("ok") else "failed")
    return rows


def messages(limit=None):
    """The newest `limit` (all if None) messages, oldest first, each with its state."""
    rows = _fold(records())
    return rows[-limit:] if limit else rows


_cache = {}


def _folded():
    """messages(), re-read only when a file has changed: the board's stream asks every few seconds. Read only."""
    key = [str(PATH), str(MARKS)]
    for p in (PATH, MARKS):
        try:
            st = os.stat(p)
            key.append((st.st_mtime_ns, st.st_size))
        except OSError:
            key.append(None)
    if _cache.get("key") != key:
        _cache["rows"], _cache["key"] = messages(), key
    return _cache["rows"]


def counts():
    """{"to_check": done by an agent but not ticked off, "waiting": delivered, not yet marked}."""
    states = [r["state"] for r in _folded()]
    return {"to_check": states.count("done"), "waiting": states.count("waiting")}


def mark(refs, event, note=None, by=None):
    """Mark messages done (an agent) or checked/unchecked (the owner). (marked refs, refs not on record)."""
    if event not in EVENTS:
        raise ValueError("unknown mark %r" % (event,))
    known = {r["ref"] for r in records()}
    marked, unknown = [], []
    for ref in refs:
        if not (isinstance(ref, str) and ref in known):
            unknown.append(ref)
            continue
        entry = {"ts": time.time(), "ref": ref, "event": event}
        if isinstance(note, str) and note.strip():
            entry["note"] = note.strip()[:MAX_NOTE]
        if by:
            entry["by"] = by
        sources.append_jsonl(MARKS, entry)
        marked.append(ref)
    return marked, unknown


def _tick_once(ref, note):
    """Tick a message off by "mail" unless it has a tick or untick already. Several board streams may run this at
    once, so the check and the write share the marks file's lock. Raises OSError when it cannot be written."""
    return sources.append_jsonl(
        MARKS, {"ts": time.time(), "ref": ref, "event": "checked", "by": "mail", "note": note[:MAX_NOTE]},
        unless=lambda marks: any(isinstance(m, dict) and m.get("ref") == ref and m.get("event") in ("checked", "unchecked")
                                 for m in marks))


def seen_ticks(mail_of, is_read=None, now=None):
    """Tick off each done message whose agent's emails about it the owner has all seen; the refs ticked.

    mail_of: session id -> {"sent": its mails to the owner, "inbound": the owner's mails to it} (the mailroom view, rows
    with thread_id, sent_at / received_at, message_id, subject). is_read: Message-ID -> True, False or None (not
    known), or None when neither the Mail tab nor Gmail can say. A mail is seen once the owner replied in its thread
    or has read it. A done message with no such mail is left for the owner: nothing says the owner has seen what the
    agent did."""
    now = now or time.time()
    ticked = []
    for r in _folded():
        if r["state"] != "done" or r.get("tick") or now - (r.get("ts") or 0) > SEEN_DAYS * 86400:
            continue
        mails, inbound = {}, []
        for sid in {r.get("id"), r["done"].get("by")} - {None, ""}:
            got = mail_of.get(sid) or {}
            for m in got.get("sent") or []:
                if int(r["ts"]) <= (m.get("sent_at") or 0) <= (r["done"].get("ts") or now) + SEEN_AFTER:
                    mails[m.get("message_id") or (m.get("thread_id"), m.get("sent_at"))] = m
            inbound += got.get("inbound") or []
        if not mails:
            continue
        how = []
        for m in mails.values():             # every one, not up to the first unseen: asking is what has Gmail look
            if any(i.get("thread_id") and i.get("thread_id") == m.get("thread_id")
                   and (i.get("received_at") or 0) >= m["sent_at"] for i in inbound):
                how.append(("replied to", m))
            elif is_read is not None and m.get("message_id") and is_read(m["message_id"]) is True:
                how.append(("read", m))
        if len(how) < len(mails):
            continue
        verb, last = max(how, key=lambda h: h[1]["sent_at"])
        note = "you %s its email \"%s\"" % (verb, (last.get("subject") or "")[:160])
        if _tick_once(r["ref"], note):
            ticked.append(r["ref"])
    return ticked
