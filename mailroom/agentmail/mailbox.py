"""The owner's mailbox of agent mail, which the board's Mail tab reads and changes through `agent-mail mailbox`
(README.md, "The Mail tab's mailbox"): the threads they are in (threads), their flags on each (read, marked unread,
archived, snoozed: the store's mailbox_threads) and the rules that make a thread's box and unread state of them
(state), and the commands: list, mark, sync (the tab and the owner's Gmail brought into step), answer (a reply they
write in the tab, with any files they attach, which the daemon routes as it routes their mail; answer_input reads it),
fast (fast mode on a thread, for quick back-and-forths with its agent: agentmail/fastmode.py), researcher (the routed
agent's Researcher preference: agentmail/researchermode.py), show (a thread and its page, mailview.document), part
(one part of a stored message, theirs or an agent's), summary (the counts and what the board's tiles count as read),
and the backfill of mail sent before the message store. Each command returns what agent-mail prints as JSON, and
raises outbound.MailError where it stops.

A thread is a Gmail thread id, or the mailroom's own for mail kept for the tab alone (outbound.local_thread_id). It
is in the mailbox when agent mail in it went to or cc an owner address (config allowlist), or mail in it came from
one. Its messages are all its agent mail (sent rows, to anyone) and the owner's mail (inbound rows from an owner
address); not the mail of anyone else. A message's key is "s<sent.id>" or "i<inbound.id>". Flags hold sent.ids
("read up to agent mail 123"), so a mark never races a mail that arrives while the tab shows the thread."""
import base64
import binascii
import json
import re
import sqlite3
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from typing import NamedTuple, Optional

from . import (compose, config, fastmode, gmail, mailroom, mailview, messages, outbound, piped, replyfiles,
               researcher_payload, researchermode, routing)
from .outbound import MailError, session_label
from .store import Store

BOXES = ("inbox", "snoozed", "archived")
OPS = ("read", "unread", "archive", "inbox", "snooze")
UPTO_OPS = ("read", "archive", "snooze")   # the marks that take --upto
LIST_LIMIT_MAX = 500
SNIPPET_CHARS = 160   # a thread's snippet: its newest message's words as the reader sees them (snippet)
SNIPPET_SOURCE_CHARS = 1200   # how much of that message's text is rendered to make it
# agent-board's Collector.is_read (collect.py) relies on this being longer than the week of mail sources.MailStore.read
# reads: change both together.
SUMMARY_READ_DAYS = 8   # how far back summary lists the agent mail the tab counts as read
KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")   # a Mail-tab reply's key, made by the tab
MAX_ANSWER_CHARS = 20000
MAX_ANSWER_FILES = 20                 # files in one reply; the board and the Mac app check the same two limits
MAX_ANSWER_FILE_BYTES = 25 << 20      # their bytes together, decoded
# The exit code of `mailbox answer` when it refuses the reply for something the owner can change by editing it (empty,
# too long, too many or too large files, files for a group), before it records anything: the board then says the
# draft was refused and can be edited and sent again. Every other error keeps its own code: 1 (as a crash), 2 (usage,
# or a run inside an agent session), 4 (a thread not in the mailbox); a timeout kills it with a signal.
ANSWER_REFUSED = 6
# What became of a reply of the owner's, by its inbound row's action, as the tab shows it; held mail adds why.
STATUSES = {"local": "sending", "pending": "sending", "delivered": "delivered", "queued": "queued",
            "orchestrating": "with the orchestrator"}


class Message(NamedTuple):
    """One message of a thread: an agent's mail (a Store.mailbox_sent row) or the owner's (a Store.inbound_from
    row)."""
    key: str      # "s<sent.id>" or "i<inbound.id>"
    mine: bool    # the owner's
    sender: str   # sender_label, or the owner's name
    ts: float
    row: object


class State(NamedTuple):
    """A thread's place in the mailbox, as state works it out."""
    box: str                        # inbox, snoozed or archived
    unread: bool
    last: float                     # the newest message's time, or when it woke from a snooze
    newest: int                     # the highest sent.id in it, 0 if none
    snooze_until: Optional[float]   # while snoozed
    unread_keys: frozenset          # its agent mail that is unread


def sender_label(row, host_id):
    """How the mailbox names the sender of sent row row: its session as session_label names it (config host_id is
    this machine), "mailroom" for unrouted mail, and its tag for a tag the store has no session for."""
    if not row["tag"]:
        return "mailroom"
    if row["cli"] is None:
        return "unknown session [#%s]" % row["tag"]
    return session_label(dict(row), host_id)


def to_owner(row, owners):
    """Whether sent row row went to or cc one of owners (lower-case addresses)."""
    return any(addr.lower() in owners for _name, addr in getaddresses([row["to_addr"] or "", row["cc_addr"] or ""]))


def threads(st, cfg):
    """{thread id: [Message, ...] oldest first} of every thread in the mailbox. Reads the store."""
    owners = set(cfg["allowlist"])
    sent = st.mailbox_sent()
    mine = st.inbound_from(sorted(owners)) if owners else []
    wanted = {r["thread_id"] for r in sent if to_owner(r, owners)} | {r["thread_id"] for r in mine}
    out = {}
    for r in sent:
        if r["thread_id"] in wanted:
            out.setdefault(r["thread_id"], []).append(
                Message("s%d" % r["id"], False, sender_label(r, cfg["host_id"]), r["sent_at"], r))
    for r in mine:
        out.setdefault(r["thread_id"], []).append(Message("i%d" % r["id"], True, cfg["owner_name"], r["received_at"], r))
    for msgs in out.values():
        msgs.sort(key=lambda m: (m.ts, m.mine, m.row["id"]))
    return out


def state(msgs, flags, now):
    """The State of a thread of messages msgs under the owner's flags (its mailbox_threads row, or None) at time now, by
    Gmail's rules. Snoozed until snooze_until, unless an agent mail newer than the snooze came, which ends it. Archived
    until an agent mail newer than the archive comes. Unread when an agent mail is newer than the one read up to, when
    marked unread after it was read, or when it woke from a snooze after it was read; then its newest agent mail is
    the unread one. The owner's own mail changes none of this."""
    f = dict(flags) if flags else {}
    agent = [m for m in msgs if not m.mine]
    newest = max((m.row["id"] for m in agent), default=0)
    seen_id, seen_at, until = f.get("seen_id") or 0, f.get("seen_at") or 0, f.get("snooze_until")
    snoozing = until is not None and newest <= f["snooze_id"]
    snoozed, woke = snoozing and until > now, snoozing and until <= now
    archived = f.get("archived_id") is not None and newest <= f["archived_id"]
    flagged = (f.get("unread_at") or 0) > seen_at or (woke and until > seen_at)
    unread_keys = frozenset(m.key for m in agent if m.row["id"] > seen_id or (flagged and m.row["id"] == newest))
    return State("snoozed" if snoozed else "archived" if archived else "inbox", bool(unread_keys) or flagged,
                 max([m.ts for m in msgs] + ([until] if woke else [])), newest, until if snoozed else None, unread_keys)


def states(all_threads, flags, now):
    return {t: state(msgs, flags.get(t), now) for t, msgs in all_threads.items()}


def counts(by_thread):
    """{"inbox": threads in the inbox, "unread": those of them unread, "snoozed": threads snoozed}."""
    inbox = [s for s in by_thread.values() if s.box == "inbox"]
    return {"inbox": len(inbox), "unread": sum(s.unread for s in inbox),
            "snoozed": sum(s.box == "snoozed" for s in by_thread.values())}


def snippet(text):
    """A thread's snippet of its newest message's text: the words its reader sees (compose.glance of the HTML compose
    renders of it), folded to SNIPPET_CHARS (mailview.snippet). Only the paragraphs within its first
    SNIPPET_SOURCE_CHARS characters are rendered, which hold far more than a snippet shows: rendering every long mail
    whole would slow the list."""
    head = text[:SNIPPET_SOURCE_CHARS]
    if len(text) > SNIPPET_SOURCE_CHARS and "\n\n" in head:
        head = head[:head.rindex("\n\n")]
    return mailview.snippet(compose.glance(compose.text_to_html(head)), SNIPPET_CHARS)


def message_texts(st, msgs):
    """{key: plain text} of msgs (None for agent mail sent before the message store and not backfilled)."""
    sent = st.texts("sent", [m.row["id"] for m in msgs if not m.mine])
    mine = st.texts("inbound", [m.row["id"] for m in msgs if m.mine])
    return {m.key: (mine if m.mine else sent)[m.row["id"]] for m in msgs}


def items(st, cfg, all_threads, by_thread, ids):
    """The list objects of threads ids (in that order): what `mailbox list` gives for each, its fast mode
    (fastmode.item) and the Researcher switch of its routed agent (researcher_owner; null for a thread without one)
    among them."""
    newest = message_texts(st, [all_threads[t][-1] for t in ids])
    fast = st.fast_rows()
    modes = st.researcher_modes()
    out = []
    for t in ids:
        msgs, s = all_threads[t], by_thread[t]
        owner = researcher_owner(st, cfg, t, msgs)
        mode = modes.get(owner["tag"]) if owner else None
        delivery = None
        delivery_detail = None
        if mode is not None:
            delivery = "applied" if researchermode.is_applied(mode) else mode["status"]
            if delivery == "unavailable" and (mode["detail"] or "").startswith(researcher_payload.FAILURE_PREFIX):
                delivery_detail = mode["detail"]
        out.append({"id": t, "subject": routing.bare_subject(msgs[0].row["subject"] or ""),
                    "agents": list(dict.fromkeys(m.sender for m in msgs if not m.mine)),
                    "snippet": snippet(newest[msgs[-1].key] or ""), "last": s.last, "count": len(msgs),
                    "unread": s.unread, "box": s.box, "snooze_until": s.snooze_until, "newest": s.newest,
                    "attachments": any(bool(m.row["eml"] if m.mine else m.row["attachments"]) for m in msgs),
                    "fast": fastmode.item(st, cfg, fast.get(t)),
                    "researcher": {"on": bool(mode and mode["enabled"]),
                                   "agent": session_label(dict(owner), cfg["host_id"]), "tag": owner["tag"],
                                   "delivery": delivery, "delivery_detail": delivery_detail} if owner else None})
    return out


def matching(st, all_threads, ids, query):
    """The threads of ids in which every word of query appears, ignoring case, in a message's subject, an agent's
    name or a message's text."""
    words = query.casefold().split()
    msgs = [m for t in ids for m in all_threads[t]]
    texts = message_texts(st, msgs)
    out = []
    for t in ids:
        hay = "\n".join([routing.bare_subject(m.row["subject"] or "") for m in all_threads[t]]
                        + [m.sender for m in all_threads[t] if not m.mine]
                        + [texts[m.key] or "" for m in all_threads[t]]).casefold()
        if all(w in hay for w in words):
            out.append(t)
    return out


def list_threads(st, cfg, box, query, limit, now):
    """`mailbox list`: the threads in box ("all": every box) matching query (None: all), newest first, at most limit,
    and the counts of the whole mailbox."""
    if box not in BOXES + ("all",):
        raise MailError("--box is one of %s or all" % ", ".join(BOXES), 1)
    if not 1 <= limit <= LIST_LIMIT_MAX:
        raise MailError("--limit is 1 to %d" % LIST_LIMIT_MAX, 1)
    all_threads = threads(st, cfg)
    by_thread = states(all_threads, st.mailbox_flags(), now)
    ids = [t for t in all_threads if box == "all" or by_thread[t].box == box]
    if query is not None:
        ids = matching(st, all_threads, ids, query)
    ids = sorted(ids, key=lambda t: by_thread[t].last, reverse=True)[:limit]
    return {"threads": items(st, cfg, all_threads, by_thread, ids), "counts": counts(by_thread)}


def flag_changes(op, flags, upto, until, now):
    """The mailbox_threads columns mark op sets on a thread with flags (its row or a dict of it, or None). Reading
    never moves the read mark back; archiving ends a snooze and snoozing an archive, as in Gmail."""
    if op == "read":
        return {"seen_id": max(upto, (dict(flags).get("seen_id") if flags else None) or 0), "seen_at": now}
    if op == "unread":
        return {"unread_at": now}
    if op == "archive":
        return {"archived_id": upto, "snooze_id": None, "snooze_until": None}
    if op == "snooze":
        return {"snooze_id": upto, "snooze_until": until, "archived_id": None}
    if op == "inbox":
        return {"archived_id": None, "snooze_id": None, "snooze_until": None}
    raise ValueError("no mark %r" % op)


def mark(st, cfg, thread_ids, op, upto, until, now):
    """`mailbox mark`: op on each of thread_ids. upto (read, archive, snooze, and only they) is the newest sent.id
    the tab showed; sent.ids rise across threads, so one serves several. until (snooze only) is when it wakes."""
    if op not in OPS:
        raise MailError("--op is one of %s" % ", ".join(OPS), 1)
    if (upto is None) == (op in UPTO_OPS):
        raise MailError("--upto goes with --op %s, and only with them" % ", ".join(UPTO_OPS), 1)
    if (until is None) == (op == "snooze"):
        raise MailError("--until goes with --op snooze, and only with it", 1)
    if upto is not None and not 0 <= upto <= st.max_sent_id():
        raise MailError("--upto %d is not the id of agent mail" % upto, 1)
    if until is not None and until <= now:
        raise MailError("--until is in the past", 1)
    all_threads = threads(st, cfg)
    wanted = list(dict.fromkeys(thread_ids))
    unknown = [t for t in wanted if t not in all_threads]
    if unknown:
        raise MailError("not in the mailbox: %s" % ", ".join(unknown), 4)
    flags = st.mailbox_flags()
    st.set_mailbox_flags({t: flag_changes(op, flags.get(t), upto, until, now) for t in wanted})
    flags = st.mailbox_flags()
    by_thread = {t: state(all_threads[t], flags.get(t), now) for t in wanted}
    return {"ok": True, "threads": items(st, cfg, all_threads, by_thread, wanted)}


def norm_id(message_id):
    """A Message-ID as sync compares them: without outer spaces and <>, lower case."""
    return (message_id or "").strip().strip("<>").strip().lower()


def sync(st, cfg, gmail_state, now):
    """`mailbox sync`: the tab and the owner's Gmail brought into step, what is done in either counting as done in both.
    gmail_state is {"inbox": [Message-ID, ...], "unread": [...]}: the agent mail in the owner's Gmail inbox and the agent mail
    unread there. Each thread not snoozed in the tab stays in the inbox only when it is there in the tab and some of its
    agent mail is in the owner's Gmail inbox, else is archived up to its newest; and stays unread only when it is unread in the
    tab and some of its agent mail is unread in Gmail, else is read up to its newest. A thread of the owner's own mail alone is
    archived and read; a snoozed thread is left as it is, here and in Gmail. Gmail never had agent mail kept for the tab
    alone (no Gmail id; config mailbox.owner_delivery "local"), so it archives no thread with kept mail the tab has not
    archived, and marks read no thread with kept mail unread in the tab; Gmail still follows the tab for the mail of it
    that Gmail holds, which is all it is asked to change. Writes only the threads that change.
    Returns {"ok": true, "archived": N, "read": N} (the threads it archived and marked read in the tab) with "gmail":
    {"archive": [...], "read": [...]}, the Message-IDs (as given) that Gmail must archive and mark read to match: those
    in its inbox of the threads now archived, and those unread there of the threads now read."""
    if not (isinstance(gmail_state, dict) and all(isinstance(gmail_state.get(k), list)
                                                  and all(isinstance(m, str) for m in gmail_state[k])
                                                  for k in ("inbox", "unread"))):
        raise MailError('sync wants {"inbox": [Message-ID, ...], "unread": [Message-ID, ...]}', 1)
    given = {}                                      # (list, normalised Message-ID) -> the Message-IDs as given
    for k in ("inbox", "unread"):
        for m in gmail_state[k]:
            if norm_id(m):
                given.setdefault((k, norm_id(m)), []).append(m)
    inbox, unread = ({n for (k, n) in given if k == which} for which in ("inbox", "unread"))
    all_threads = threads(st, cfg)
    flags = st.mailbox_flags()
    done = dict.fromkeys(("archived", "read"), 0)
    to_gmail = {"archive": set(), "read": set()}
    changes = {}
    for t, msgs in all_threads.items():
        before = dict(flags[t]) if t in flags else {}
        was = state(msgs, before, now)
        if was.box == "snoozed":
            continue
        agent = {norm_id(m.row["message_id"]) for m in msgs if not m.mine and m.row["gmail_id"]}   # what Gmail holds
        kept = [m for m in msgs if not m.mine and not m.row["gmail_id"]]
        cols = {}
        if (was.box == "inbox" and not agent & inbox
                and all(m.row["id"] <= (before.get("archived_id") or 0) for m in kept)):
            cols.update(flag_changes("archive", before, was.newest, None, now))
        unread_now = state(msgs, dict(before, **cols), now)   # an archive can end a woken snooze
        if unread_now.unread and not agent & unread and not any(m.key in unread_now.unread_keys for m in kept):
            cols.update(flag_changes("read", dict(before, **cols), was.newest, None, now))
        now_is = state(msgs, dict(before, **cols), now)
        if now_is.box == "archived":
            to_gmail["archive"] |= agent & inbox
        if not now_is.unread:
            to_gmail["read"] |= agent & unread
        if not cols:
            continue
        changes[t] = cols
        done["archived"] += now_is.box != was.box
        done["read"] += now_is.unread != was.unread
    st.set_mailbox_flags(changes)
    gmail_lists = {k: sorted(m for n in to_gmail[k] for m in given[("inbox" if k == "archive" else "unread", n)])
                   for k in ("archive", "read")}
    return dict(ok=True, gmail=gmail_lists, **done)


def show(st, cfg, thread_id, now):
    """`mailbox show`: thread thread_id's list object, its messages, and the thread as one sanitized HTML page
    (mailview.document), headed by its subject and who wrote in it, open at its unread agent mail and its last
    message. Agent mail comes from the message
    store; mail sent before it and not backfilled shows its text when the store has it, and, with Gmail on, a link to
    it in the agents account's Gmail. Each agent message lists the widgets it kept ({"id": KEY-N, "kind", "spec"};
    agentmail/widgets.py), whose places the page marks <div class="widget" data-widget=ID>. Marks nothing read.
    Raises ValueError for a stored message that is damaged."""
    all_threads = threads(st, cfg)
    if thread_id not in all_threads:
        raise MailError("thread %s is not in the mailbox" % thread_id, 4)
    msgs = all_threads[thread_id]
    s = state(msgs, st.mailbox_flags().get(thread_id), now)
    texts = message_texts(st, msgs)
    shown, entries = [], []
    for i, m in enumerate(msgs):
        entry = {"key": m.key, "sender": m.sender, "ts": m.ts, "mine": m.mine,
                 "open": m.key in s.unread_keys or i == len(msgs) - 1}
        if m.mine:
            answered = st.session_by_tag(m.row["matched_tag"]) if m.row["matched_tag"] else None
            raw = messages.load(m.row["eml"]) if m.row["eml"] else None   # a reply with files
            shown.append({"key": m.key, "from": m.sender, "mine": True, "ts": m.ts, "unread": False, "stored": True,
                          "attachments": mailview.attachments(mailview.parse(raw)) if raw else [],
                          "status": status(m.row)})
            entry.update(recipients=session_label(dict(answered), cfg["host_id"]) if answered else "",
                         status=status(m.row), widgets=0, **({"raw": raw} if raw else {"text": texts[m.key] or ""}))
        else:
            raw = messages.load(m.row["eml"]) if m.row["eml"] else None
            kept = json.loads(m.row["widgets"]) if m.row["widgets"] else []
            shown.append({"key": m.key, "from": m.sender, "mine": False, "to": m.row["to_addr"] or "",
                          "cc": m.row["cc_addr"] or "", "ts": m.ts, "subject": m.row["subject"] or "",
                          "unread": m.key in s.unread_keys, "stored": raw is not None,
                          "attachments": mailview.attachments(mailview.parse(raw)) if raw else [], "status": None,
                          "widgets": [{"id": "%s-%d" % (m.key, n), "kind": w["kind"], "spec": w["spec"]}
                                      for n, w in enumerate(kept)]})
            entry.update(recipients=", ".join(a for a in (m.row["to_addr"], m.row["cc_addr"]) if a), raw=raw,
                         widgets=len(kept))
            if raw is None:   # a link to it in Gmail only while the mailroom uses Gmail
                url = gmail.web_url(cfg["account"], "all/" + m.row["gmail_id"]) if config.gmail_on(cfg) else None
                entry.update(text=texts[m.key] or "", gmail_url=url)
        entries.append(entry)
    thread = items(st, cfg, all_threads, {thread_id: s}, [thread_id])[0]
    return {"thread": thread, "messages": shown,
            "document": mailview.document(thread["subject"], thread["agents"], entries)}


def part(st, key, index):
    """`mailbox part`: part number index (mailview.part; the INDEX of the document's mailbox-part:KEY/INDEX links) of
    stored message key, agent mail "s<sent id>" or a reply of the owner's with files "i<inbound id>", as {"name", "type",
    "data": base64}."""
    m = re.fullmatch(r"([si])([0-9]+)", key)
    row = (st.sent_row if m.group(1) == "s" else st.inbound)(int(m.group(2))) if m else None
    if row is None or not row["eml"]:
        raise MailError("%s is no stored message (agent mail s<sent id>, or a reply with files i<inbound id>)" % key, 4)
    try:
        p = mailview.part(messages.load(row["eml"]), index)
    except KeyError:
        raise MailError("message %s has no part %d" % (key, index), 4)
    return {"name": p.name, "type": p.type, "data": base64.b64encode(p.data).decode()}


def status(row):
    """What became of the owner's mail of inbound row row, as the tab shows it (STATUSES; held mail says why). Raises
    ValueError for an action the owner's mail never has."""
    if row["action"] in STATUSES:
        return STATUSES[row["action"]]
    if row["action"] == "held":
        return "held: " + (row["detail"] or "")
    if row["action"] == "dry-run":
        return "held: a dry run of the mailroom took it, so it was never routed (%s)" % row["detail"]
    raise ValueError("inbound mail #%d has action %r, which the mailbox does not know" % (row["id"], row["action"]))


def answer_input(obj, what):
    """A text with files, {"text": str, "files": [{"name": str, "data": base64}, ...]}, as (text, [(name, bytes)]), the
    names as replyfiles.unique makes them: what `mailbox answer --json` reads (`what` "the reply"), and agent-send's and
    agent-orchestrate's --input-json ("the message"); `what` names it in errors. "files" may be left out. Raises
    MailError for any other shape or data that is not base64 (exit code 1), and for more than MAX_ANSWER_FILES files
    or more than MAX_ANSWER_FILE_BYTES in all (ANSWER_REFUSED)."""
    files = obj.get("files", []) if isinstance(obj, dict) else None
    if not isinstance(obj, dict) or not isinstance(obj.get("text"), str) or not isinstance(files, list) or not all(
            isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("data"), str) for f in files):
        raise MailError('%s is not {"text": TEXT, "files": [{"name": NAME, "data": BASE64}, ...]}' % what, 1)
    if len(files) > MAX_ANSWER_FILES:
        raise MailError("%s has %d files; the most is %d" % (what, len(files), MAX_ANSWER_FILES), ANSWER_REFUSED)
    datas = []
    for f in files:
        try:
            datas.append(base64.b64decode(f["data"], validate=True))
        except (binascii.Error, ValueError):
            raise MailError("file %r: its data is not base64" % f["name"], 1)
    total = sum(map(len, datas))
    if total > MAX_ANSWER_FILE_BYTES:
        raise MailError("%s's files are %s; the most is %s"
                        % (what, mailview.size(total), mailview.size(MAX_ANSWER_FILE_BYTES)), ANSWER_REFUSED)
    return obj["text"], list(zip(replyfiles.unique([f["name"] for f in files]), datas))


def piped_input(what):
    """answer_input of the JSON piped in (piped.piped_text): agent-send's and agent-orchestrate's --input-json, `what`
    naming it in errors. Raises MailError (exit code 1) when what was piped in is not JSON or nothing was, and as
    answer_input does; TimeoutError as piped_text does."""
    try:
        obj = json.loads(piped.piped_text() or "null")
    except ValueError as e:
        raise MailError("%s is not JSON: %s" % (what, e), 1)
    return answer_input(obj, what)


def reply_message(cfg, owner, subject, key, in_reply_to, text, files):
    """The MIME bytes the store keeps of the owner's reply with files ([(name, bytes)]): from owner, their address, under
    subject, its Message-ID made of key, answering in_reply_to, text as its body and each file attached
    (outbound.attach)."""
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["owner_name"], owner))
    msg["Subject"] = subject
    msg["Message-ID"] = routing.local_message_id(key)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg.set_content(text)
    outbound.attach(msg, files)
    return msg.as_bytes()


def owner_session(st, cfg, thread_id):
    """(the newest agent mail in thread thread_id, the sessions row the routing slip finds for a reply to it or None,
    the routing rule): who gets what the owner writes in the thread (mailroom.claim, as for their email). Raises MailError
    when the thread holds no agent mail."""
    answered = st.sent_in_thread(thread_id)
    if answered is None:
        raise MailError("thread %s holds no agent mail to answer" % thread_id, 1)
    session, rule = mailroom.claim(st, {"subject": routing.answer_subject(answered["subject"] or ""),
                                        "in-reply-to": answered["message_id"] or ""}, thread_id, cfg["account"])
    return answered, session, rule


def researcher_owner(st: Store, cfg: dict, thread_id: str, msgs: list[Message]) -> Optional[sqlite3.Row]:
    """Use the reply route only when every outgoing message belongs to the same supported routing tag."""
    tags = {m.row["tag"] for m in msgs if not m.mine}
    if len(tags) != 1 or not next(iter(tags)):
        return None
    _answered, session, _rule = owner_session(st, cfg, thread_id)
    return session if researchermode.supported(session) and session["tag"] in tags else None


def answer(st, cfg, thread_id, key, text, files):
    """`mailbox answer`: record text, and files ([(name, bytes)] as answer_input gives them), as the owner's reply in
    thread thread_id to its newest agent mail, as an inbound row the daemon routes as it routes the owner's mail (action
    "local"; bin/agent-mailroom, process_local): gmail_id and Message-ID made of key (routing.local_gmail_id,
    local_message_id), the subject an answer to that mail's, and the session the routing slip finds for a reply to it
    (mailroom.claim). A reply with files is kept whole in the message store (reply_message; the row's eml), and the
    daemon gives the session the files (replyfiles); a group takes no files. The text may be empty only when there
    are files. A key used already gives the reply it made, not a second one. Writes the store. Raises MailError, with
    exit code ANSWER_REFUSED for a reply the owner can edit to pass (empty, too long, files for a group), before recording
    anything."""
    if not KEY_RE.fullmatch(key):
        raise MailError("--key is 16 to 64 letters, digits, _ and -", 1)
    old = st.inbound_by_gmail_id(routing.local_gmail_id(key))
    if old is None:
        owner = config.owner_address(cfg)     # first: with none, no thread is in the mailbox (threads)
        if not owner:
            raise MailError("config has no owner address (owner_email or allowlist)", 1)
        if thread_id not in threads(st, cfg):
            raise MailError("thread %s is not in the mailbox" % thread_id, 4)
        answered, session, rule = owner_session(st, cfg, thread_id)
        text = text.strip()
        if not text and not files:
            raise MailError("the reply is empty", ANSWER_REFUSED)
        if len(text) > MAX_ANSWER_CHARS:
            raise MailError("the reply is %d characters; the most is %d" % (len(text), MAX_ANSWER_CHARS),
                            ANSWER_REFUSED)
        subject = routing.answer_subject(answered["subject"] or "")
        if files and session is not None and session["cli"] == routing.GROUP_CLI:
            raise MailError("thread %s goes to group %r, which takes text only: send the files to one agent, by mail "
                            "or in a thread of its own" % (thread_id, session["session_name"]), ANSWER_REFUSED)
        eml = messages.keep(reply_message(cfg, owner, subject, key, answered["message_id"], text, files)) \
            if files else None
        old, _made = st.record_inbound_once(
            gmail_id=routing.local_gmail_id(key), thread_id=thread_id, message_id=routing.local_message_id(key),
            from_addr=owner, from_name=cfg["owner_name"], subject=subject,
            matched_tag=session["tag"] if session else None, match_rule=rule, action="local",
            detail="written in the Mail tab", body=text, eml=eml, in_reply_to=answered["message_id"])
    if old["thread_id"] != thread_id:
        raise MailError("key %s answered thread %s, not %s" % (key, old["thread_id"], thread_id), 1)
    return {"ok": True, "key": "i%d" % old["id"], "status": status(old)}


def fast(ctx, thread_id, on, now):
    """`mailbox fast`: switch fast mode on (on True) or off in thread thread_id (fastmode.start, end), for the agent
    that gets what the owner writes there (owner_session). It records the switch and returns: the daemon's next pass
    (fastmode.tick) lowers the agent's effort with its keys, or tells the agent fast mode is over and, when no fast
    thread of its own is left, puts its effort back, alerting the owner (kind fast-mode) once a restore is given up.
    Returns {"ok": true, "thread": its list object, with "fast"} and the lines to log. Raises MailError
    when the thread is not in the mailbox (exit code 4), and, to switch it on, when it holds no agent mail or no single
    agent gets the owner's replies there: the orchestrator's, or a group's."""
    st, cfg = ctx.st, ctx.cfg
    all_threads = threads(st, cfg)
    if thread_id not in all_threads:
        raise MailError("thread %s is not in the mailbox" % thread_id, 4)
    if on:
        _answered, session, _rule = owner_session(st, cfg, thread_id)
        if session is None:
            raise MailError("no agent gets the replies in thread %s (they go to the orchestrator): fast mode is for a "
                            "thread with one agent" % thread_id, 1)
        if session["cli"] == routing.GROUP_CLI:
            raise MailError("thread %s goes to group %r: fast mode is for a thread with one agent"
                            % (thread_id, session["session_name"]), 1)
        lines = fastmode.start(ctx, thread_id, session, now)
    else:
        lines = fastmode.end(ctx, thread_id, "off")
    s = state(all_threads[thread_id], st.mailbox_flags().get(thread_id), now)
    return {"ok": True, "thread": items(st, cfg, all_threads, {thread_id: s}, [thread_id])[0]}, lines


def researcher(st: Store, cfg: dict, thread_id: str, on: bool, now: float, *, expected_tag: str) -> dict:
    """Resolve and compare the thread's tag, then save its setting in one store transaction."""
    if not isinstance(expected_tag, str) or not re.fullmatch(routing.TAG_RE, expected_tag):
        raise MailError("--expect-tag must be a mail routing tag", 2)
    with st.transaction():
        all_threads = threads(st, cfg)
        if thread_id not in all_threads:
            raise MailError("thread %s is not in the mailbox" % thread_id, 4)
        owner = researcher_owner(st, cfg, thread_id, all_threads[thread_id])
        if owner is None or owner["tag"] != expected_tag:
            raise MailError("Researcher owner changed for thread %s. Refresh the thread." % thread_id, 1)
        st.set_researcher_mode(owner["tag"], on, now)
        s = state(all_threads[thread_id], st.mailbox_flags().get(thread_id), now)
        thread = items(st, cfg, all_threads, {thread_id: s}, [thread_id])[0]
    return {"ok": True, "thread": thread}


def summary(st, cfg, now):
    """`mailbox summary`: the inbox and unread counts, and the Message-IDs of the agent mail to an owner address from
    the last SUMMARY_READ_DAYS days that the tab counts as read."""
    all_threads = threads(st, cfg)
    by_thread = states(all_threads, st.mailbox_flags(), now)
    owners, since = set(cfg["allowlist"]), now - SUMMARY_READ_DAYS * 86400
    read = [m.row["message_id"] for t, msgs in all_threads.items() for m in msgs
            if not m.mine and m.ts >= since and m.row["message_id"] and to_owner(m.row, owners)
            and m.key not in by_thread[t].unread_keys]
    c = counts(by_thread)
    return {"unread": c["unread"], "inbox": c["inbox"], "read": read}


def unread_kept(st, cfg, after_id, now):
    """(how many of the agent mails kept for the Mail tab alone, no Gmail id, after sent row after_id are unread in a
    thread in the inbox, the highest id of all those kept after it or None): what the nudge (alerts.nudge) tells of."""
    all_threads = threads(st, cfg)
    by_thread = states(all_threads, st.mailbox_flags(), now)
    kept = [(t, m) for t, msgs in all_threads.items() for m in msgs
            if not m.mine and m.row["gmail_id"] is None and m.row["id"] > after_id]
    return (sum(by_thread[t].box == "inbox" and m.key in by_thread[t].unread_keys for t, m in kept),
            max((m.row["id"] for _, m in kept), default=None))


def backfill(st, svc, limit, say):
    """`mailbox backfill`: keep the message of each sent row from before the message store (Store.sent_to_backfill:
    newest first, at most limit, None for all), fetched from the agents account's Gmail, and fill in its eml, body,
    cc_addr and attachments. say(line) is told of each mail and the total. A mail that cannot be fetched, parsed or
    kept is reported and left for the next run. Returns how many failed."""
    kept = failed = 0
    for r in st.sent_to_backfill(limit):
        try:
            raw = gmail.get_raw(svc, r["gmail_id"])
            sha = messages.keep(raw)
            parsed = mailview.parse(raw)
            cc = parsed.headers["Cc"]
            st.backfill_sent(r["id"], sha, parsed.text, str(cc) if cc is not None else None,
                             len(mailview.attachments(parsed)))
        except Exception as e:   # noqa: BLE001 - one mail Gmail no longer has, or cannot parse, must not stop the rest
            failed += 1
            say("s%d FAILED: %s: %s" % (r["id"], type(e).__name__, e))
            continue
        kept += 1
        say("s%d kept %s (%d bytes)" % (r["id"], sha, len(raw)))
    say("backfill: %d kept, %d failed" % (kept, failed))
    return failed
