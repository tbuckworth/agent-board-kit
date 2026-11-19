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
                                                            model and effort, then send it the owner's note
                                                            (relaunch.py resume_settings: which login, and what it
                                                            falls back on)

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

What the owner has sent from the board, and whether each has been dealt with (agentboard/sent.py):

  {"action": "sent", "limit": N?}                            the newest N (default 200) messages, newest first,
                                                            each with its state, and the counts
  {"action": "sent_mark", "refs": [REF, ...], "checked": B}   tick messages off (or back on)

Group chats between the owner and some of the agents (agentboard/chats.py, over the mailroom's agent-group, as the
owner).
A chat is named by its 22-character id, agents by their session ids on the board:

  {"action": "chat_list"}                                    every chat, hidden ones included, as the
                                                            snapshot's chats list them (chats.rows)
  {"action": "chat_show", "group": G}                        one chat, with each member's alert setting
                                                            (each chat carries "subgroup": the name of the
                                                            sub-group it belongs to, or null, and "unnamed":
                                                            that sub-group's agents it leaves out for want of a
                                                            name a chat can use; subchats.py)
  {"action": "chat_posts", "group": G, "since": POST?}       its posts in order (after POST); the owner's carry
                                                            their ref
  {"action": "chat_post", "group": G, "post": POST, "title": T, "text": TEXT}
                                                            the owner's post; POST is a new id the Mac made, the
                                                            post's idempotency key; T, the chat's title, names it
                                                            on the record
  {"action": "chat_new", "title": T, "members": [ID, ...]}    a new chat with these agents; answers with it
  {"action": "chat_members", "group": G, "op": "add" | "remove", "members": [ID, ...]}
                                                            refused for a sub-group's chat
  {"action": "chat_hide", "group": G, "hidden": B}           hide it on the board, or show it again

The Mail tab: mail between the owner and the agents, kept by the mailroom (agentboard/mailbox.py, over its
`agent-mail mailbox`, whose answers these return as they are). A thread is named by its id, a message by its key
(s<id> an agent's mail, i<id> the owner's own):

  {"action": "mail_list", "box": BOX?, "query": Q?, "limit": N?}
                                                            threads in BOX (mailbox.BOXES, default inbox), newest
                                                            first, matching the search Q, at most N (default 100)
  {"action": "mail_show", "thread": T}                       one thread, with its messages and the whole thread as
                                                            one sanitized HTML page
  {"action": "mail_mark", "threads": [T, ...], "op": OP, "upto": ID?, "until": TS?}
                                                            read, unread, archive, inbox (out of archive or snooze)
                                                            or snooze until TS; read, archive and snooze name the
                                                            newest agent mail the owner saw (ID, a sent row's id)
  {"action": "mail_reply", "thread": T, "key": K, "text": TEXT, "files": [{"name": N, "data": B64}, ...]?}
                                                            the owner's reply to the thread's newest agent mail,
                                                            routed as the owner's emailed replies are, with files
                                                            (at most mailbox.MAX_FILES, MAX_FILE_BYTES together);
                                                            TEXT may be empty when there are files. K, made by the
                                                            Mac, is its idempotency key
  {"action": "mail_part", "key": M, "index": N}              an attachment of message M, base64
  {"action": "mail_sync"}                                    bring the tab and the owner's Gmail into step: what is
                                                            archived or read in either is so in both (mailbox.sync;
                                                            needs the board config's gmail_credentials)

The Mac's tile groups, for the Time page, the usage breakdown and sub-group chats (the layout lives on the Mac; the app
sends it whenever it changes). U is the Mac's UUID for a sub-group; after storing the groups, the desktop brings
each sub-group's chat in line (subchats.sync) and answers with what it could not do in "chat_problems":

  {"action": "groups", "groups": [{"name": N, "members": [ID, ...], "subgroups": [{"name", "members", "id": U}]}]}

Where the owner's time goes (agentboard/timeuse.py):

  {"action": "time", "refresh": bool?}                      the per-day, per-group minutes as last computed
                                                            (`agent-board time`, agent-board-time.timer), computed now
                                                            when there are none yet or `refresh` is true
  {"action": "screen", "blocks": [[START, APP, TITLE], ...]} store the Mac's screen time (agentboard/screen.py);
                                                            the answer's `next` is where the next upload starts
                                                            and `block` the block length in seconds

Where a plan window's allowance has gone since it last reset (agentboard/allowance.py); TARGET is a pool of the
usage reading ("claude:default", "codex") and WINDOW one of its windows ("session", "weekly", "model:opus"):

  {"action": "usage_split", "target": TARGET, "window": WINDOW}
                                                            each agent's share, by group and sub-group, and other

Restarting the desktop (agentboard/restart.py):

  {"action": "restart_plan"}                                 what a restart would do (changes nothing)
  {"action": "restart_run", "update": B, "wait_minutes": N, "choices": {ID: "nudge" | "quiet" | "skip"}}
                                                            start a restart, updating first when `update` is true
  {"action": "restart_cancel"}                               cancel a restart while it can still be cancelled

The id must be a full session id that is on the board right now (live, for close/send); force_close also
needs the display name typed back. Nothing is ever passed through a shell: argv lists and stdin only.
dispatch runs a request; ACTIONS, at the end, is the table of actions it looks each one up in.
Every text an agent reads names the owner by the mailroom config's owner_name (config.owner_name).
"""
import base64
import collections
import functools
import json
import math
import os
import re
import subprocess
import time

from . import chats, collect, config, gmailstate, mailbox, relaunch, sent, sources, subchats

TARGET_RE = re.compile(r"^(claude:[A-Za-z0-9_-]{1,32}|codex)$")
WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
ID_RE = sources.SESSION_ID_RE
MAX_TEXT = 4000
AUDIT = sources.BOARD_STATE / "actions.jsonl"

HEADER_FMT = "[relayed from {owner} via agent-board]"      # Dialogs.swift quotes it to the owner
CLOSE_FMT = """ close
{owner} asked you to close, from the agent board. Follow your close protocol: run `agent-close --self --dry-run`.
If it reports unlanded work, email {owner} (agent-mail send) what is left and ask whether to land it or discard it.
Otherwise run `agent-close --self` as your very last step; no email is needed, the board shows you closing."""
SEND_FOOTER_FMT = ("\n\n(Sent from the agent board{as_ref}, not by email. If you need to answer, use agent-mail send "
                   "to {owner}.)")


def owner():
    """The owner's first name for text agents read (mailroom config owner_name), else "the owner"."""
    return config.owner_name()


def header(name=None):
    return HEADER_FMT.format(owner=name or owner())


def close_text(name=None):
    name = name or owner()
    return header(name) + CLOSE_FMT.format(owner=name)


def send_footer(ref=None, name=None):
    """The last line of a message: where it came from, and (with a ref) how to tick it off."""
    footer = SEND_FOOTER_FMT.format(owner=name or owner(), as_ref=" as message %s" % ref if ref else "")
    return footer + ("\n" + sent.done_line(ref) if ref else "")


def _add_error(res, msg):
    """Add `msg` to a result's error, which the Mac shows even when the action itself succeeded."""
    res["error"] = "; ".join(x for x in (res.get("error"), msg) if x)


def _audit(entry, res):
    """Put an action on the record (actions.jsonl) and return its result `res`; a record that cannot be written is
    added to the result's error."""
    try:
        sources.append_jsonl(AUDIT, dict(entry, ts=time.time()))
    except OSError as e:
        _add_error(res, "the board could not record this action: %s" % e)
    return res


def _keep(kind, to, req, res, **facts):
    """Record a text-carrying request with its outcome (sent.py), failed ones too so nothing the owner typed is lost. A
    result with note_sent (a resume, which can succeed with its note unsent) is judged by that, not by its ok. A record
    that cannot be written is added to the result's error, which the Mac shows."""
    ok = bool(res.get("note_sent", res.get("ok")))
    if not ok:
        facts["error"] = (res.get("error") or (not res.get("ok") and res.get("output")) or "not delivered")[-300:]
    try:
        sent.record(kind, to, req.get("text") if isinstance(req, dict) else None, ok,
                    **{k: v for k, v in facts.items() if v is not None})
    except OSError as e:
        _add_error(res, "the board could not keep its record of this message: %s" % e)


def _ref_for(req):
    """A new ref when the request carries text worth recording, else None."""
    text = req.get("text") if isinstance(req, dict) else None
    return sent.new_ref() if isinstance(text, str) and text.strip() else None


def _matching(v, pattern):
    """v when it is text that the regex `pattern` matches whole, else None."""
    return v if isinstance(v, str) and pattern.fullmatch(v) else None


def _check_text(req, noun, limit, required, key="text"):
    """Why the request's text at `key` won't do, or None: it must be text of at most `limit` characters with no NUL,
    and not blank when `required` (when not required it may also be absent). `noun` names it in the error."""
    text = req.get(key)
    if text is None:
        return "empty %s" % noun if required else None
    if not isinstance(text, str) or len(text) > limit or "\x00" in text:
        return "%s too long or not text" % noun
    if required and not text.strip():
        return "empty %s" % noun
    return None


def _whole(v, lo, hi):
    """Whether v is a whole number (not a bool) from lo to hi."""
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def sent_action(req):
    limit = req.get("limit", 200)
    if not _whole(limit, 1, 2000):
        return {"ok": False, "error": "limit must be a whole number from 1 to 2000"}
    return {"ok": True, "messages": sent.messages(limit)[::-1], "counts": sent.counts()}


def sent_mark_action(req):
    refs, checked = req.get("refs"), req.get("checked")
    if not isinstance(refs, list) or not refs or len(refs) > 500 or not all(
            _matching(r, sent.REF_RE) for r in refs):
        return {"ok": False, "error": "refs must be a list of message refs"}
    if not isinstance(checked, bool):
        return {"ok": False, "error": "checked must be true or false"}
    marked, unknown = sent.mark(refs, "checked" if checked else "unchecked", by=sent.BY_OWNER)
    if unknown:
        return {"ok": False, "error": "no message %s on record" % ", ".join(unknown[:5]), "marked": marked}
    return {"ok": True, "marked": marked, "counts": sent.counts()}


def validate(req, agents):
    """(agent, error) for a request on one of the board's agents (an action ACTIONS routes through _on_agent).
    agents: the board's current rows."""
    action, sid = req.get("action"), req.get("id")
    if not _matching(sid, ID_RE):
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
        err = _check_text(req, "note", MAX_TEXT, required=False)
        if err:
            return None, err
    if action in ("switch_options", "switch") and not agent["live"]:
        return None, "%s is not running; only a live agent can be switched" % agent["display"]
    if action == "switch":
        to, model, effort = req.get("to"), req.get("model"), req.get("effort")
        if to is not None and not _matching(to, TARGET_RE):
            return None, "target must be claude:<login> or codex"
        for v, what in ((model, "model"), (effort, "effort")):
            if v is not None and not _matching(v, WORD_RE):
                return None, "bad %s name" % what
        if to is None and model is None and effort is None:
            return None, "nothing to change"
        if not isinstance(req.get("force", False), bool):
            return None, "force must be true or false"
    if action == "send":
        err = _check_text(req, "message", MAX_TEXT, required=True)
        if err:
            return None, err
    return agent, None


def groups_action(req, agents, run=sources.run):
    """Store the Mac's groups (for the Time page and the usage breakdown), after checking every name and session id,
    then bring the sub-groups' chats in line with them (subchats.sync). What the sync could not do to a chat is in
    "chat_problems" and in the snapshot's problems. When the sync cannot run at all (its state file cannot be read or
    written) the answer is an error, which the Mac shows and answers by sending the groups again; the groups are
    stored either way."""
    from . import groups as board_groups
    groups, err = board_groups.clean_groups(req.get("groups"))
    if err:
        return {"ok": False, "error": err}
    board_groups.save_groups(groups, sources.live_session_ids())
    try:
        problems = subchats.sync(groups, agents, run)
    except (chats.ChatError, OSError) as e:
        return {"ok": False, "error": "the groups were stored, but the sub-group chats were not brought in line: %s"
                                      % sources.error_text(e)}
    return {"ok": True, "groups": len(groups), "chat_problems": problems}


def _chat_id(req, key="group"):
    """The request's chat id (or, by key, post id), or None when it isn't one."""
    return _matching(req.get(key), chats.ID_RE)


def _chat_names(req, agents):
    """(the session names of the request's `members`, error): each must be an agent on the board with a name."""
    ids = req.get("members")
    if not isinstance(ids, list) or not ids or len(ids) > chats.MAX_MEMBERS:
        return None, "members must be a list of 1 to %d agents" % chats.MAX_MEMBERS
    names = []
    for sid in ids:
        a = next((a for a in agents if isinstance(sid, str) and a["id"] == sid), None)
        if a is None:
            return None, "no session %s on the board" % str(sid)[:8]
        if not chats.NAME_RE.match(a.get("name") or ""):
            return None, "%s has no name a chat can use; name it first (/rename)" % a["display"]
        names.append(a["name"])
    return names, None


def _answering(error):
    """A decorator: the handler's result, or, when it raises `error` (an exception class or a tuple of them),
    {"ok": False} with the exception's message as the error."""
    def wrap(fn):
        @functools.wraps(fn)
        def handler(req, *args, **seams):
            try:
                return fn(req, *args, **seams)
            except error as e:
                return {"ok": False, "error": str(e)}
        return handler
    return wrap


@_answering(chats.ChatError)
def chat_list_action(req, run=sources.run):
    return {"ok": True, "chats": subchats.annotate(chats.rows(chats.chat_list(run)))}


@_answering(chats.ChatError)
def chat_show_action(req, run=sources.run):
    group = _chat_id(req)
    if not group:
        return {"ok": False, "error": "group must be a chat id"}
    chat, = subchats.annotate([chats.show(group, run)])
    return {"ok": True, "chat": chat}


@_answering(chats.ChatError)
def chat_posts_action(req, run=sources.run):
    group, since = _chat_id(req), req.get("since")
    if not group:
        return {"ok": False, "error": "group must be a chat id"}
    if since is not None and not _chat_id(req, "since"):
        return {"ok": False, "error": "since must be a post id"}
    return {"ok": True, "posts": chats.posts(group, since, run)}


@_answering(chats.ChatError)
def chat_post_action(req, run=sources.run, ref=None):
    """The owner's post: delivered by agent-group to every member, recorded with its ref (dispatch's keep)."""
    group, post_id = _chat_id(req), _chat_id(req, "post")
    if not group or not post_id:
        return {"ok": False, "error": "group must be a chat id and post a new post id"}
    err = _check_text(req, "post", MAX_TEXT, required=True) or chats.title_error(req.get("title"))
    if err:
        return {"ok": False, "error": err}
    out = chats.post(group, post_id, req["text"].strip(), ref, run)
    return _audit({"action": "chat_post", "group": group, "post": post_id, "chars": len(req["text"])},
                  {"ok": True, "output": out[-4000:]})


@_answering(chats.ChatError)
def chat_new_action(req, agents, run=sources.run):
    err = chats.title_error(req.get("title"))
    names, err = (None, err) if err else _chat_names(req, agents)
    if err:
        return {"ok": False, "error": err}
    chat = chats.new(req["title"].strip(), names, run)
    return _audit({"action": "chat_new", "group": chat.get("id"), "title": req["title"].strip(), "members": names},
                  {"ok": True, "chat": chat})


@_answering(chats.ChatError)
def chat_members_action(req, agents, run=sources.run):
    group, op = _chat_id(req), req.get("op")
    if not group or op not in ("add", "remove"):
        return {"ok": False, "error": "group must be a chat id and op add or remove"}
    sub = subchats.annotate([{"id": group}])[0]["subgroup"]
    if sub is not None:
        return {"ok": False, "error": 'this chat\'s members follow the sub-group "%s": move tiles into or out of it '
                                      'instead' % sub}
    names, err = _chat_names(req, agents)
    if err:
        return {"ok": False, "error": err}
    out = chats.members(group, op, names, run)
    return _audit({"action": "chat_members", "group": group, "op": op, "members": names},
                  {"ok": True, "output": out})


@_answering(chats.ChatError)
def chat_hide_action(req, run=sources.run):
    group, hidden = _chat_id(req), req.get("hidden")
    if not group or not isinstance(hidden, bool):
        return {"ok": False, "error": "group must be a chat id and hidden true or false"}
    out = chats.hide(group, hidden, run)
    return _audit({"action": "chat_hide", "group": group, "hidden": hidden}, {"ok": True, "output": out})


@_answering(sources.CommandError)
def mail_list_action(req, run=sources.run):
    box, query, limit = req.get("box", "inbox"), req.get("query"), req.get("limit", 100)
    if box not in mailbox.BOXES:
        return {"ok": False, "error": "box must be one of " + ", ".join(mailbox.BOXES)}
    err = _check_text(req, "query", mailbox.MAX_QUERY, required=False, key="query")
    if err:
        return {"ok": False, "error": err}
    if not _whole(limit, 1, mailbox.MAX_LIMIT):
        return {"ok": False, "error": "limit must be a whole number from 1 to %d" % mailbox.MAX_LIMIT}
    return dict(mailbox.listing(box, query.strip() if query and query.strip() else None, limit, run), ok=True)


@_answering(sources.CommandError)
def mail_show_action(req, run=sources.run):
    thread = _matching(req.get("thread"), mailbox.THREAD_RE)
    if not thread:
        return {"ok": False, "error": "thread must be a thread id"}
    return dict(mailbox.show(thread, run), ok=True)


@_answering(sources.CommandError)
def mail_mark_action(req, run=sources.run):
    """Read, unread, archive, back to the inbox, or snooze. Every op but read goes on the record (actions.jsonl):
    a read mark comes with every thread the owner opens."""
    threads, op, upto, until = req.get("threads"), req.get("op"), req.get("upto"), req.get("until")
    if not (isinstance(threads, list) and 1 <= len(threads) <= mailbox.MAX_THREADS
            and all(_matching(t, mailbox.THREAD_RE) for t in threads)):
        return {"ok": False, "error": "threads must be a list of 1 to %d thread ids" % mailbox.MAX_THREADS}
    if op not in mailbox.OPS:
        return {"ok": False, "error": "op must be one of " + ", ".join(mailbox.OPS)}
    if (op in mailbox.UPTO_OPS) != (upto is not None) or (upto is not None and not _whole(upto, 0, 10 ** 12)):
        return {"ok": False, "error": "upto, the newest agent mail shown (a whole number), goes with %s and only "
                                      "with them" % ", ".join(mailbox.UPTO_OPS)}
    if (op == "snooze") != (until is not None) or (until is not None and (
            isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(until)
            or until <= time.time())):
        return {"ok": False, "error": "until, a time in the future, goes with snooze and only with it"}
    res = dict(mailbox.mark(threads, op, upto, until, run), ok=True)
    if op == "read":
        return res
    return _audit({"action": "mail_mark", "threads": threads, "op": op, "upto": upto, "until": until}, res)


@_answering(sources.CommandError)
def mail_reply_action(req, run=sources.run):
    """The owner's reply, which the mailroom keeps in full (an inbound mail from the owner) with its files, and routes
    on its next poll; the record (actions.jsonl) has its thread, key, length and its files' names and sizes."""
    thread, key = _matching(req.get("thread"), mailbox.THREAD_RE), req.get("key")
    if not thread or not _matching(key, mailbox.KEY_RE):
        return {"ok": False, "error": "thread must be a thread id and key a new reply key"}
    files = req.get("files", [])
    sizes, err = _reply_files(files)
    err = err or _check_text(req, "reply", mailbox.MAX_REPLY, required=not files)
    if err:
        return {"ok": False, "error": err}
    text = req.get("text") or ""
    res = dict(mailbox.answer(thread, key, text.strip(), files, run), ok=True)
    return _audit({"action": "mail_reply", "thread": thread, "key": key, "chars": len(text),
                   "files": [{"name": f["name"], "bytes": n} for f, n in zip(files, sizes)]}, res)


def _reply_files(files):
    """(each file's size in bytes, None), or ([], why) the files of a mail reply won't do: a list of at most
    mailbox.MAX_FILES {"name": text, "data": base64}, at most mailbox.MAX_FILE_BYTES decoded together."""
    if not isinstance(files, list) or len(files) > mailbox.MAX_FILES:
        return [], "files must be a list of at most %d files" % mailbox.MAX_FILES
    sizes = []
    for f in files:
        if not (isinstance(f, dict) and set(f) == {"name", "data"} and isinstance(f["name"], str)
                and 0 < len(f["name"]) <= mailbox.MAX_FILE_NAME and "\x00" not in f["name"] and isinstance(f["data"], str)):
            return [], "each file must be {name, data}, its name text of 1 to %d characters" % mailbox.MAX_FILE_NAME
        try:
            sizes.append(len(base64.b64decode(f["data"], validate=True)))
        except ValueError:
            return [], "the data of %s is not base64" % f["name"]
    if sum(sizes) > mailbox.MAX_FILE_BYTES:
        return [], "the files come to %d bytes; a reply carries at most %d" % (sum(sizes), mailbox.MAX_FILE_BYTES)
    return sizes, None


@_answering(sources.CommandError)
def mail_part_action(req, run=sources.run):
    key, index = req.get("key"), req.get("index")
    if not _matching(key, mailbox.MESSAGE_RE) or not _whole(index, 1, 10 ** 4):
        return {"ok": False, "error": "key must be a message's key (s<id> or i<id>) and index a part's number"}
    return dict(mailbox.part(key, index, run), ok=True)


@_answering((sources.CommandError, RuntimeError))
def mail_sync_action(req, run=sources.run, gmail=None):
    """Bring the tab and the owner's Gmail into step (mailbox.sync), on the record (actions.jsonl), a sync whose Gmail
    side failed too. gmail: a stand-in for the owner's Gmail (gmailstate.for_owner), for a test."""
    gmail = gmail or gmailstate.for_owner(sync=True)
    if gmail is None:
        return {"ok": False, "error": "the tab cannot sync with Gmail: the board config names no Gmail login of the "
                                      "owner's (gmail_credentials), or the mailroom's config names no agents' account"}
    try:
        res = mailbox.sync(gmail, run)
    except mailbox.GmailSideFailed as e:
        return _audit(dict({"action": "mail_sync", "gmail_error": str(e.__cause__)}, **e.tab),
                      {"ok": False, "error": str(e)})
    return _audit(dict({"action": "mail_sync"}, **res), dict(res, ok=True))


def time_action(req, table=None, now=None):
    """The Time page's figures: time.json's (timeuse.load), recomputed (timeuse.refresh, on the actions record) when
    asked to or when there are none. table: the sources refresh reads, timeuse.default_sources when None; now: the
    moment a recompute is for, the clock's when None."""
    from . import timeuse
    refresh = req.get("refresh", False)
    if not isinstance(refresh, bool):
        return {"ok": False, "error": "refresh must be true or false"}
    doc = None if refresh else timeuse.load()
    if doc is None:                  # a recompute can make paid model calls (group guesses): on the record first
        res = _audit({"action": "time", "refresh": True}, {"ok": True})
        res["time"] = timeuse.refresh(timeuse.config(), now=now, table=table or timeuse.default_sources)
        return res
    return {"ok": True, "time": doc}


def usage_split_action(req):
    """Where a plan window's allowance has gone since it last reset (allowance.compute)."""
    from . import allowance
    target, window = req.get("target"), req.get("window")
    if not (isinstance(target, str) and isinstance(window, str)):
        return {"ok": False, "error": "target and window must be strings"}
    try:
        return {"ok": True, "split": allowance.compute(target, window)}
    except (ValueError, RuntimeError, OSError) as e:
        return {"ok": False, "error": sources.error_text(e)}


def screen_action(req):
    """Store the Mac's screen-time blocks. Its next upload starts at the newest block held, sent again since it may
    have been part-way through; with none held, at the start of the Time page's span."""
    from . import screen, timeuse
    blocks, err = screen.clean(req.get("blocks"), timeuse.BUCKET)
    if err:
        return {"ok": False, "error": err}
    _, since = timeuse.span(timeuse.config(), time.time())
    held = screen.store(blocks, since, screen.PATH)
    return {"ok": True, "held": len(held), "next": held[-1][0] if held else int(since), "block": timeuse.BUCKET}


def sharing(agent, agents):
    """A different live agent working in (or above, or below) this agent's directory, if any. agent-close
    checks for unlanded work, not for other sessions, so removing a shared worktree would pull it out from
    under the live one."""
    cwd = agent.get("cwd") or ""
    if not cwd:
        return None
    return next((o for o in agents if o["id"] != agent["id"] and o.get("live") and o.get("cwd")
                 and (collect.inside(o["cwd"], cwd) or collect.inside(cwd, o["cwd"]))), None)


def _on_agent(fn):
    """The handler of an action on one of the board's agents: validate(req, agents) first, then
    fn(req, agent, **seams)."""
    def handler(req, agents, **seams):
        agent, err = validate(req, agents)
        return {"ok": False, "error": err} if err else fn(req, agent, **seams)
    return handler


def _close(req, agent, run=sources.run_merged):
    """Ask a live agent to land its work and close itself (close_text)."""
    return _agent_send(req, agent, close_text(), run)


def _send(req, agent, run=sources.run_merged, ref=None):
    """The owner's message into a live agent, ending with how to mark it done (ref: its ref on sent.py's record)."""
    return _agent_send(req, agent, header() + "\n" + req["text"].strip() + send_footer(ref), run)


def _agent_send(req, agent, body, run):
    try:
        argv = sources.agent_send_argv(agent["cli"], agent["id"])
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    code, out = run(argv, stdin=body, timeout=45)
    return _result(req, agent["display"], code, out, {"id": agent["id"], "name": agent["display"]})


def _agent_close(req, agent, *, flags, run=sources.run_merged):
    """agent-close on the agent's session with `flags`; a dry run (--dry-run) is not put on the record."""
    exe = sources.find_bin("agent-close")
    if not exe:
        return {"ok": False, "error": "agent-close not found"}
    code, out = run([exe, agent["id"]] + flags, timeout=120)
    return _result(req, agent["display"], code, out,
                   None if "--dry-run" in flags else {"id": agent["id"], "name": agent["display"]})


def _result(req, name, code, output, record):
    """An agent-send, agent-close or agent-spawn run's result, put on the record (actions.jsonl) with the facts in
    `record` that name its target, unless `record` is None (a dry run)."""
    res = {"ok": code == 0, "code": code, "output": output[-4000:], "action": req["action"], "name": name}
    if record is None:
        return res
    return _audit(dict(record, action=req["action"], code=code, chars=len(req.get("text") or "")), res)


def switch(req, agent, run=sources.run_merged):
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
    return _audit({"action": "switch", "id": agent["id"], "name": agent["display"], "code": code, "to": req.get("to"),
                   "model": req.get("model"), "effort": req.get("effort"), "force": bool(req.get("force"))},
                  {"ok": code == 0, "code": code, "needs_force": code == 3 or bool(res.get("needs_force")),
                   "output": res.get("detail") or "", "result": res, "name": agent["display"]})


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
    to a path. agent-close itself only takes session or tmux names. Imported read-only from the mailroom
    (sources.mailroom: RuntimeError when it isn't installed)."""
    return sources.mailroom("close").close


def validate_wt(req, agents, worktrees):
    """(worktree, error). The path must be one of the repos' linked worktrees right now, with no live agent in it."""
    from . import collect
    action, path = req.get("action"), req.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return None, "path must be an absolute worktree path"
    wt = next((w for w in worktrees if w["path"] == path), None)
    if wt is None:
        return None, "%s is not a linked worktree of any repo under %s (any more)" % (path, sources.REPOS_ROOT)
    live = [a["cwd"] for a in agents if a.get("live") and a.get("cwd")]
    repos = {a.get("repo") or (sources.git_facts(a["cwd"]) or {}).get("repo") for a in agents
             if a.get("live") and a.get("cwd")} if collect.is_subagent_tree(wt["path"]) else set()
    if collect.occupied(wt, live, [], repos):
        return None, "an agent is working in %s; use its tile instead" % path
    name = collect.worktree_name(path)
    if action == "wt_force" and req.get("confirm") != name:
        return None, "type the worktree's name (%s) exactly to force remove it" % name
    if action == "wt_spawn":
        err = _check_text(req, "instructions", MAX_BRIEF, required=True)
        if err:
            return None, err
    return wt, None


def _on_worktree(fn):
    """The handler of an action on a worktree with no agent in it: validate_wt(req, agents, worktrees) first, then
    fn(req, wt, **seams)."""
    def handler(req, agents, worktrees, **seams):
        wt, err = validate_wt(req, agents, worktrees)
        return {"ok": False, "error": err} if err else fn(req, wt, **seams)
    return handler


def _close_worktree(closer, path, force, dry_run, fetch):
    """agent-close's own close() on the worktree at `path` (closer: a stand-in for it, else the mailroom's): (exit
    code, lines). Whatever it raises, a missing mailroom included, is this worktree's failure, given in its lines."""
    try:
        return (closer or mailroom_close())({"name": "worktree " + collect.worktree_name(path), "tmux": None,
                                             "cli": "?", "session_id": "", "cwd": path},
                                            force=force, dry_run=dry_run, fetch=fetch)
    except Exception as e:      # report, never half-explain
        return 1, ["%s: %s" % (type(e).__name__, e)]


def _wt_close(req, wt, *, force, dry_run, closer=None):
    """agent-close's own checks on the worktree: a dry run, a plain clean up (only if all is landed), or, with
    force, a removal whatever is unlanded. All but the dry run go on the record (actions.jsonl)."""
    code, lines = _close_worktree(closer, wt["path"], force=force, dry_run=dry_run, fetch=True)
    return _result(req, collect.worktree_name(wt["path"]), code, "\n".join(lines),
                   None if dry_run else {"path": wt["path"]})


def _wt_spawn(req, wt, run=sources.run_merged, state=None, ref=None):
    """agent-spawn --dir on the worktree with the owner's instructions in its brief, put on the record (actions.jsonl).
    state: its git state for the brief (None: read now). ref: the instructions' ref on sent.py's record."""
    path = wt["path"]
    exe = sources.find_bin("agent-spawn")
    if not exe:
        return {"ok": False, "error": "agent-spawn not found"}
    if state is None:
        from . import classify
        g = classify.git_stage(sources.git_facts(path)) or {}
        state = g.get("summary") or "unknown"
    brief = SPAWN_BRIEF.format(owner=owner(), path=path, repo=wt["repo"], branch=wt["branch"] or "(detached)",
                               state=state, text=req["text"].strip())
    if ref:
        brief += "\nThese instructions are message %s on the board. %s\n" % (ref, sent.done_line(ref, "they ask"))
    code, out = run([exe, "--dir", path, "--brief-text", brief, "--json"], timeout=240)
    return _result(req, collect.worktree_name(path), code, out, {"path": path})


# ------------------------------------------------------------------ bulk: every fully landed worktree
def _fetch_repos(mains, run=sources.run_merged):
    """One `git fetch origin` per repo (by its main checkout), a few at a time, so every dry run compares with a
    fresh origin. {main checkout: why its fetch failed} for each that failed."""
    from concurrent.futures import ThreadPoolExecutor
    mains = sorted(set(mains))
    with ThreadPoolExecutor(max_workers=6) as pool:
        got = list(pool.map(lambda m: run(["git", "-C", m, "fetch", "--quiet", "origin"], timeout=90), mains))
    return {m: out[-200:] or "exit %d" % code for m, (code, out) in zip(mains, got) if code != 0}


def _unfetched(main, failed):
    return "git fetch origin failed in %s (%s), so whether it is landed is unknown" % (main, failed[main])


def landed_check(agents, worktrees, closer=None, fetch=_fetch_repos):
    """agent-close's verdict on each of collect.free_worktrees: which a plain clean up would remove, and why not.
    Fetches each of their repos first; a worktree whose repo could not be fetched is not removable, and its detail
    says so."""
    free = collect.free_worktrees(agents, worktrees)
    failed = fetch([w["main"] for w in free])
    out = []
    for w in free:
        code, lines = _close_worktree(closer, w["path"], force=False, dry_run=True, fetch=False)
        text = "; ".join(lines)
        removable = code == 0 and "remove worktree" in text and w["main"] not in failed
        if w["main"] in failed:
            text = _unfetched(w["main"], failed) + "; " + text
        out.append({"path": w["path"], "name": collect.worktree_name(w["path"]), "repo": w["repo"],
                    "branch": w["branch"], "removable": removable, "detail": text[:300]})
    return {"ok": True, "candidates": out}


def landed_cleanup(req, agents, worktrees, closer=None, fetch=_fetch_repos):
    """Clean up each confirmed path with agent-close's own checks (never --force); each is re-validated first, and
    one whose repo could not be fetched is kept."""
    paths = req.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths) or len(paths) > 200:
        return {"ok": False, "error": "paths must be a non-empty list of worktree paths"}
    paths = list(dict.fromkeys(paths))          # each worktree once, in the order confirmed
    known = {w["path"]: w for w in worktrees}
    failed = fetch([known[p]["main"] for p in paths if p in known])
    results, out = [], {"ok": True}
    for p in paths:
        wt, err = validate_wt({"action": "wt_cleanup", "path": p}, agents, worktrees)
        if not err and wt["main"] in failed:
            err = "not removed: " + _unfetched(wt["main"], failed)
        name = collect.worktree_name(p)
        if err:
            results.append({"path": p, "name": name, "ok": False, "detail": err})
            continue
        code, lines = _close_worktree(closer, p, force=False, dry_run=False, fetch=False)
        results.append({"path": p, "name": name, "ok": code == 0, "detail": "; ".join(lines)[:300]})
        _audit({"action": "wt_landed_cleanup", "path": p, "code": code}, out)
    done = sum(1 for r in results if r["ok"])
    return dict(out, removed=done, kept=len(results) - done, results=results)


# ------------------------------------------------------------------ the orchestrator
ORCH_HEADER_FMT = ("[message from {owner} via the agent board (the Mac app, over SSH), not by email. This is {owner} "
                   "writing: act on it as you would an email from {owner}. There is no thread to reply in: answer with "
                   "`agent-mail send --to owner --subject \"...\" --body-file ...`; the reply to that mail comes "
                   "back to you.]")           # "owner": the mailroom's alias for the owner's address


def orch_header(cfg=None):
    cfg = cfg if cfg is not None else config.mail_config()
    return ORCH_HEADER_FMT.format(owner=config.owner_name(cfg=cfg))


ORCH_LOGS = sources.BOARD_STATE


def orchestrate(req, popen=None, ref=None):
    """Start one orchestrator event with the owner's text and return at once; the run takes 30 s to a few minutes. Its
    output goes to ORCH_LOGS/orchestrate-<time>.log, the result's `log`. ref: the text's ref on sent.py's record."""
    err = _check_text(req, "message", MAX_BRIEF, required=True)
    if err:
        return {"ok": False, "error": err}
    text = req["text"]
    exe = sources.find_bin("agent-orchestrate")
    if not exe:
        return {"ok": False, "error": "agent-orchestrate is not installed on the desktop"}
    ORCH_LOGS.mkdir(parents=True, exist_ok=True)
    log = ORCH_LOGS / ("orchestrate-%d.log" % int(time.time()))
    body = orch_header() + "\n\n" + text.strip()
    if ref:
        body += "\n\nThis is message %s on the board. %s" % (ref, sent.done_line(ref))
    with open(log, "ab") as fh:
        p = (popen or subprocess.Popen)([exe, "--source", "board"], stdin=subprocess.PIPE, stdout=fh,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        p.stdin.write(body.encode())
        p.stdin.close()
    return _audit({"action": "orchestrate", "chars": len(text), "log": str(log)},
                  {"ok": True, "output": "the orchestrator is running; it will email you (log %s)" % log, "log": str(log)})


# ------------------------------------------------------------------ resume an exited agent


RESUME_FMT = """ resumed
Your session had exited; {owner} resumed it from the agent board, in tmux session '{tmux}'. Check where you were
(git status, any background jobs you had running, your last notes) and carry on.{mail}{text}"""


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
        argv = sources.cmdline(p, proc)
        exe = os.path.basename(argv[0]) if argv else ""
        script = argv[1] if len(argv) > 1 else ""
        cli = exe in ("claude", "codex") or (exe == "node" and (                      # npm builds run under node
            script.endswith(("/claude-code/cli.js", "/claude.js")) or
            ("/@openai/codex/" in script and script.endswith(".js")) or script.endswith("/codex.js")))
        if sid in argv and cli:
            out.append(int(p))
    return [p for p in out if p != os.getpid()]


def resume(req, agent, run=sources.run_merged, live=sources.is_live, wait=45, cap=None, busy=running_with, ref=None):
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
        return _resume(req, agent, run, live, wait, cap, tmux, ref)
    finally:
        lock.close()


def _resume(req, agent, run, live, wait, cap, tmux, ref=None):
    name = relaunch.tmux_name_for(agent, set(sources.tmux_sessions()))
    try:
        home, model, effort, placed = relaunch.resume_settings(agent, sources.mailroom("config").load())
        # the tmux session may be `name-2` when the old one still exists, but Claude keeps the agent's own name:
        # agent-send and the mailroom find it by that
        line = relaunch.resume_command(agent["cli"], agent["id"], agent["cwd"], home, model, effort,
                                       agent.get("name") or name)
    except Exception as e:      # no mailroom, a damaged config, a failed placement: this resume stops and says why
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    how = "%s · %s · %s%s" % (agent["cli"], model or "default model", effort or "default effort",
                              "; " + placed if placed else "")
    up, memory, err, dialog = relaunch.launch_in_tmux(agent, line, name, run, live, wait, cap, tmux)
    if err:
        return {"ok": False, "error": err}
    dialog = " %s." % dialog if dialog else ""
    note, note_sent = "", False
    if up:
        ids = stranded_mail(agent)
        who = owner()
        mail = ("\n%s emailed you while you were down: agent-mail show %s (and answer with agent-mail reply N)."
                % (who, ", ".join(str(i) for i in ids))) if ids else ""
        text = ("\n\nNote from %s:\n" % who + req["text"].strip()) if (req.get("text") or "").strip() else ""
        if text and ref:
            text += "\n\nThis note is message %s on the board. %s" % (ref, sent.done_line(ref))
        try:
            argv = sources.agent_send_argv(agent["cli"], agent["id"])
            time.sleep(2)
            code, out = run(argv, stdin=header(who) + RESUME_FMT.format(owner=who, tmux=name, mail=mail, text=text),
                            timeout=45)
        except RuntimeError as e:
            code, out = 127, str(e)
        note_sent = code == 0
        if note_sent:
            note = " Told it why" + (" and about mail %s." % ", ".join(map(str, ids)) if ids else ".")
        else:
            note = " Could not tell it why (%s)%s." % (out[-200:] or "agent-send exit %d" % code,
                                                       ": your note was not passed on" if text else "")
    return _audit({"action": "resume", "id": agent["id"], "name": agent["display"], "tmux": name, "live": up},
                  {"ok": up, "tmux": name, "note_sent": note_sent,
                   "output": ("Resumed %s in tmux '%s' (%s; %s).%s%s" % (agent["display"], name, how, memory, dialog,
                                                                          note)) if up
                   else "Started in tmux '%s' but it isn't live after %ds: open it in Kitty to see why.%s" % (
                       name, wait, dialog),
                   "error": "" if up else "not live yet; see tmux '%s'" % name})


# ------------------------------------------------------------------ desktop restart
def restart_action(req):
    """restart_plan (read-only), restart_run {update, wait_minutes, choices}, restart_cancel."""
    from . import restart
    action = req.get("action")
    if action == "restart_plan":
        return {"ok": True, "plan": restart.plan()}
    if action == "restart_cancel":
        res = restart.cancel()
        return _audit({"action": "restart_cancel", "ok": res.get("ok")}, res)
    update, wait, choices = req.get("update"), req.get("wait_minutes", 15), req.get("choices") or {}
    if not isinstance(update, bool) or not isinstance(wait, int) or not isinstance(choices, dict):
        return {"ok": False, "error": "update must be true/false, wait_minutes a number, choices an object"}
    if not all(_matching(k, ID_RE) and v in restart.CHOICES for k, v in choices.items()):
        return {"ok": False, "error": "choices must map full session ids to nudge, quiet or skip"}
    res = restart.start_run(update, wait, choices)
    return _audit({"action": "restart_run", "update": update, "wait": wait, "choices": len(choices), "ok": res.get("ok")},
                  res)


# ------------------------------------------------------------------ the table of actions
# run: the handler, called with the request and then what `needs` names, in that order (the board's agent rows, the
# repos' linked worktrees). keep: for an action carrying the owner's text, the kind sent.py records it as and a function
# of (request, agent rows) giving (to whom, facts that find it again); None for the rest.
Action = collections.namedtuple("Action", "run needs keep")
NOTHING, AGENTS, WORKTREES = (), ("agents",), ("agents", "worktrees")


def _to_agent(req, agents):
    agent = next((a for a in agents if a["id"] == req.get("id")), None) or {}
    return agent.get("display") or str(req.get("id"))[:36], {"id": agent.get("id"), "cli": agent.get("cli")}


def _to_worktree(req, agents):
    path = str(req.get("path"))
    return "a new agent in " + collect.worktree_name(path), {"path": path}


ACTIONS = {
    "close": Action(_on_agent(_close), AGENTS, None),
    "check_close": Action(_on_agent(functools.partial(_agent_close, flags=["--dry-run"])), AGENTS, None),
    "cleanup": Action(_on_agent(functools.partial(_agent_close, flags=[])), AGENTS, None),
    "force_close": Action(_on_agent(functools.partial(_agent_close, flags=["--force"])), AGENTS, None),
    "send": Action(_on_agent(_send), AGENTS, ("send", _to_agent)),
    "switch_options": Action(_on_agent(switch), AGENTS, None),
    "switch": Action(_on_agent(switch), AGENTS, None),
    "resume": Action(_on_agent(resume), AGENTS, ("resume", _to_agent)),
    "wt_check": Action(_on_worktree(functools.partial(_wt_close, force=False, dry_run=True)), WORKTREES, None),
    "wt_cleanup": Action(_on_worktree(functools.partial(_wt_close, force=False, dry_run=False)), WORKTREES, None),
    "wt_force": Action(_on_worktree(functools.partial(_wt_close, force=True, dry_run=False)), WORKTREES, None),
    "wt_spawn": Action(_on_worktree(_wt_spawn), WORKTREES, ("spawn", _to_worktree)),
    "wt_landed_check": Action(lambda req, agents, worktrees, **seams: landed_check(agents, worktrees, **seams),
                              WORKTREES, None),
    "wt_landed_cleanup": Action(landed_cleanup, WORKTREES, None),
    "orchestrate": Action(orchestrate, NOTHING, ("orchestrate", lambda req, agents: ("the orchestrator", {}))),
    "sent": Action(sent_action, NOTHING, None),
    "sent_mark": Action(sent_mark_action, NOTHING, None),
    "chat_list": Action(chat_list_action, NOTHING, None),
    "chat_show": Action(chat_show_action, NOTHING, None),
    "chat_posts": Action(chat_posts_action, NOTHING, None),
    "chat_post": Action(chat_post_action, NOTHING,
                        ("chat", lambda req, agents: ("group chat " + str(req.get("title") or "?"),
                                                      {"group": req.get("group"), "post": req.get("post")}))),
    "chat_new": Action(chat_new_action, AGENTS, None),
    "chat_members": Action(chat_members_action, AGENTS, None),
    "chat_hide": Action(chat_hide_action, NOTHING, None),
    "mail_list": Action(mail_list_action, NOTHING, None),
    "mail_show": Action(mail_show_action, NOTHING, None),
    "mail_mark": Action(mail_mark_action, NOTHING, None),
    "mail_reply": Action(mail_reply_action, NOTHING, None),
    "mail_part": Action(mail_part_action, NOTHING, None),
    "mail_sync": Action(mail_sync_action, NOTHING, None),
    "groups": Action(groups_action, AGENTS, None),
    "time": Action(time_action, NOTHING, None),
    "usage_split": Action(usage_split_action, NOTHING, None),
    "screen": Action(screen_action, NOTHING, None),
    "restart_plan": Action(restart_action, NOTHING, None),
    "restart_run": Action(restart_action, NOTHING, None),
    "restart_cancel": Action(restart_action, NOTHING, None),
}


def dispatch(req, agents, worktrees, **seams):
    """Run one request from the Mac (any JSON value) and return its result: {"ok": bool, ...}, with "error" saying
    why not. agents, worktrees: functions giving the board's agent rows and the repos' linked worktrees, called only
    for an action that needs them. seams: stand-ins a test passes on to the handler (run, closer, popen, fetch,
    state, gmail, now). A request of an action with a `keep` gets a ref, and the owner's text goes on
    sent.py's record with its outcome; a Mail tab reply (mail_reply) has none, since the mailroom records it."""
    action = req.get("action") if isinstance(req, dict) else None
    spec = ACTIONS.get(action) if isinstance(action, str) else None
    if spec is None:
        return {"ok": False, "error": "unknown action %r" % (action,) if isinstance(req, dict)
                else "request must be a JSON object"}
    built = {n: {"agents": agents, "worktrees": worktrees}[n]() for n in spec.needs}
    args = [built[n] for n in spec.needs]
    if spec.keep is None:
        return spec.run(req, *args, **seams)
    kind, to = spec.keep
    t0, ref = time.time(), _ref_for(req)
    res = spec.run(req, *args, ref=ref, **seams)
    name, facts = to(req, built.get("agents", []))
    _keep(kind, name, req, res, ts=t0, ref=ref, log=res.get("log"), **facts)     # log: an orchestrator run's output
    return res
