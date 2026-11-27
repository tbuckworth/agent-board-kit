"""The only things the Mac app may make the desktop do. Each request is one JSON object on stdin.

  {"action": "close",       "id": ID}                       ask the agent to land its work and close itself
  {"action": "check_close", "id": ID}                       agent-close --dry-run: what a force close would discard
  {"action": "cleanup",     "id": ID}                       an offline agent: plain agent-close, which removes its
                                                            worktree only if every change is landed, else refuses
  {"action": "force_close", "id": ID, "confirm": NAME}       agent-close --force (discards unlanded work)
  {"action": "send",        "id": ID, "text": TEXT, "via": V?, "files": [{"name": N, "data": B64}, ...]?}
                                                            a message from the owner into the session; V names what
                                                            relayed it for the owner besides the board itself (VIAS:
                                                            "desk", the owner's desk, desk.py), on both records; the
                                                            files
                                                            (as a mail reply's, _request_files) are saved for the
                                                            agent and the message ends with their paths (agent-send
                                                            --input-json); TEXT may be empty when there are files

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

  {"action": "orchestrate", "text": TEXT, "source": S, "key": K, "files": [{"name": N, "data": B64}, ...]?}
                                                            one orchestrator event (agent-orchestrate), in the
                                                            background in a systemd scope of its own; it answers
                                                            the owner by email. S, a key of ORCH_SOURCES, says where
                                                            the owner wrote it; K (mailbox.KEY_RE), made by the Mac
                                                            or the phone's page, is its idempotency key: a K that
                                                            started a run already starts none, and answers
                                                            "already": true. Files as send's (agent-orchestrate
                                                            --input-json)

Worktrees with no agent in them (the board's bottom section) are named by path, never by anything else:

  {"action": "wt_check",   "path": P}                        what a clean up would do (agent-close's dry run)
  {"action": "wt_cleanup", "path": P}                        remove it and its branch only if everything is landed
  {"action": "wt_force",   "path": P, "confirm": NAME}        remove it even with unlanded work
  {"action": "wt_spawn",   "path": P, "text": TEXT, "account": NAME?}
                                                            start an agent there with the owner's instructions, on
                                                            the login usage picks, or on NAME, one of the mailroom's
                                                            manual logins (the snapshot's spawn_logins)
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
  {"action": "chat_posts", "group": G, "since": POST?}       its posts in order (after POST), each with its topic
                                                            and agent-group's label for its sender; the owner's
                                                            (chats.is_owners) carry their ref
  {"action": "chat_post", "group": G, "post": POST, "title": T, "text": TEXT,
   "topic": TOPIC? | "new_topic": TITLE?, "reply_to": POST?}
                                                            the owner's post; POST is a new id the Mac made, the
                                                            post's idempotency key; T, the chat's title, names it
                                                            on the record. It goes in topic TOPIC (an id) or opens
                                                            a topic TITLE; with neither, a reply (to POST) stays
                                                            in its post's topic and any other post is in none
  {"action": "chat_new", "title": T, "members": [ID, ...]}    a new chat with these agents; answers with it as
                                                            chats.show gives a chat (chats.new), or, made but
                                                            not understood, ok false with "made": its id or null
  {"action": "chat_members", "group": G, "op": "add" | "remove", "members": [ID, ...]}
                                                            refused for a sub-group's chat
  {"action": "chat_hide", "group": G, "hidden": B}           hide it on the board, or show it again

The Mail tab: mail between the owner and the agents, kept by the mailroom (agentboard/mailbox.py, over its
`agent-mail mailbox`, whose answers these return as they are). A thread is named by its id, a message by its key
(s<id> an agent's mail, i<id> the owner's own):

  {"action": "mail_list", "box": BOX?, "query": Q?, "limit": N?, "sessions": B?}
                                                            threads in BOX (mailbox.BOXES, default inbox), newest
                                                            first, matching the search Q, at most N (default 100);
                                                            with B true, each thread's "sessions" too, the board's
                                                            agents that wrote in it ([{"cli", "id"}], read from the
                                                            mailroom's store), or "sessions_error" saying why not;
                                                            and its group's "group", "subgroup", "colour" and
                                                            "shade" (mailbox.add_groups), or "groups_error"
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
                                                            Mac, is its idempotency key. A reply refused before
                                                            anything is kept (by the board's checks, or by agent-mail
                                                            with mailbox.ANSWER_REFUSED) answers "refused": true;
                                                            any other failure may come after the mailroom kept it
                                                            (agent-mail timing out, say)
  {"action": "mail_fast", "thread": T, "on": B}              fast mode for the thread's agent, on or off:
                                                            while on, the agent answers the owner there in a few
                                                            plain lines at low effort, its replies unreviewed, until
                                                            the owner switches it off or writes nothing there for the
                                                            mailroom's minutes; answers the thread as mail_list
                                                            lists it, its "fast" saying until when, the agent's
                                                            effort and a note
  {"action": "mail_researcher", "thread": T, "on": B, "expected_tag": TAG}
                                                            Researcher for the agent behind the shown routing tag
                                                            (agent-mail mailbox researcher; README "Researcher");
                                                            answers the thread with its saved setting and delivery
                                                            state
  {"action": "mail_part", "key": M, "index": N}              an attachment of message M, base64
  {"action": "mail_forward", "thread": T, "key": M}          agent mail M of thread T as a draft in the owner's Gmail,
                                                            to send on: a fresh mail of its content, images and
                                                            attachments (mailforward.py), read from the mailroom's
                                                            store; answers the draft's message id (needs the board
                                                            config's gmail_credentials)

The owner's drafts of replies, kept on the desktop, so that every window of the Mac app and the phone app see the same
one and a draft outlives either (agentboard/maildrafts.py, README "Drafts"). D is the device saving ("mac" or "phone";
the phone's page says "phone" whatever the request says), R the draft's rev the
device last read (a stale one is a conflict: nothing is lost, the current draft stays), CONTENT {text, picks,
comments, files, key} (maildrafts.check_content), F a file's id (the SHA-256 of its bytes). draft_get and each change
answer the thread's draft with its versions (maildrafts.Store.view):

  {"action": "draft_list"}                                   the threads whose draft holds something: {T: {rev, updated}}
  {"action": "draft_get", "thread": T}                       its draft, rev, who last saved it, and its versions
  {"action": "draft_put", "thread": T, "base_rev": R, "draft": CONTENT, "device": D}
                                                            save it; "conflict": true when not taken, the device's
                                                            content then kept as a version ("kept")
  {"action": "draft_discard", "thread": T, "base_rev": R, "device": D}
                                                            discard it, kept as a version ("version", for Undo)
  {"action": "draft_restore", "thread": T, "version": V, "base_rev": R, "device": D}
                                                            bring version V back, the draft it replaces kept as one
  {"action": "draft_file_add", "thread": T, "data": B64}     keep a file for the thread's draft to name: {id, size}

A mail_reply's files may be named from its thread's drafts ({"name", "draft": F}) rather than sent; once the mailroom
has the reply, the thread's draft goes if it holds the reply's key (the window sending saved it first), so sending
from one window empties it in all ("draft": {"cleared", "rev"}; when the drafts cannot be read, the answer's error
says so, and the reply has still gone).

The Mac's tile groups, for the Time page, the usage breakdown, sub-group chats and the Mail tab's group colours (the
layout lives on the Mac; the app sends it whenever it changes). U is the Mac's UUID for a sub-group; after storing the
groups, the desktop brings each sub-group's chat in line (subchats.sync) and answers with what it could not do in
"chat_problems":

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

The Mail tab on the owner's phone (agentboard/mailweb.py, README "Mail on the phone"), from the Mac's Phone sheet:

  {"action": "mail_web_enrol"}                               a one-time code to enrol the phone's passkey, as the
                                                            page's address the Mac shows as a QR code, and when
                                                            it lapses; it replaces any code still waiting
  {"action": "mail_web_sign_out", "forget": B}               end every signed-in session on the phone; with B true
                                                            also forget its passkeys, so it must be enrolled again

The Mac's ask box (⌃⌥A; README "The ask box on the Mac"), over the voice desk (agentboard/voicedesk.py). Agents in the
voice page's excluded groups may be asked here, as from the desktop's shell: nothing goes to Google (the voice page's
exclusion, voiceweb.py, is for what goes to Gemini). Neither web page offers these (mailweb.API, voiceweb.API):

  {"action": "voice_looking"}                                what the owner has in front of them on the Mac and the
                                                            agent behind it (voicedesk.looking), with "agents": the
                                                            live agents, for the box's picker
  {"action": "voice_ask", "agent": A, "question": Q, "history": [{"question", "answer"}, ...]?}
                                                            Q to agent A (its id, or its name) through a read-only copy
                                                            of its conversation (voicedesk.ask), after the box's last
                                                            HISTORY_PAIRS questions and answers (ask_text): {ok, agent,
                                                            answer, seconds, parts}, the answer spoken by the desktop's
                                                            voice sentence by sentence (speech.Narrator): each part is
                                                            {seq, text, audio (WAV, base64) | error}, sent as it is
                                                            ready over the stream (PARTS), else listed in `parts`
  {"action": "desk_say", "text": T, "looking": L?}           T, the owner's words, to the desk (desk.py's daemon: one
                                                            always-running Claude, which may ask an agent's copy, or
                                                            send an agent a message for the owner) with L, what
                                                            voice_looking gave the box (or true, for the desk to read
                                                            the owner's screen): {ok, text, sends, failed,
                                                            screen_error?, timings, parts}, spoken as voice_ask's
                                                            answer is, each tool the desk calls a part {status} too
                                                            ("Asking loader-fix…"); `sends`, what it sent for the
                                                            owner, for the box to show
  {"action": "desk_new"}                                     a fresh conversation for the desk

The id must be a full session id that is on the board right now (live, for close/send); force_close also
needs the display name typed back. Nothing is ever passed through a shell: argv lists and stdin only.
dispatch runs a request; ACTIONS, at the end, is the table of actions it looks each one up in.
Every text an agent reads names the owner by the mailroom config's owner_name (config.owner_name).
"""
import base64
import collections
import contextlib
import functools
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import time
import unicodedata

from . import (chats, collect, config, gmailstate, mailbox, maildrafts, mailforward, mailwebstate, relaunch, sent,
               sources, speech, subchats, voicedesk)

LOGIN = r"[A-Za-z0-9_-]{1,32}"                              # a Claude login's name
WORD = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}"
TARGET_RE = re.compile(r"^(claude:%s|codex)$" % LOGIN)
LOGIN_RE = re.compile(r"^%s$" % LOGIN)
WORD_RE = re.compile(r"^%s$" % WORD)
MODEL_RE = re.compile(r"^%s(/%s)?$" % (WORD, WORD))          # a word, or OpenRouter's maker/model
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
# agent-send's exit code for a message a Codex agent took into its queue and holds there (the mailroom's
# deliver.HELD); its output's last line says why. Not sent, and not to be sent again: _result says it waits.
AGENT_SEND_HELD = 7
VIAS = ("desk",)        # what may relay the owner's message besides the board itself (a send's "via"): the desk, desk.py
# why gmailstate.for_owner gave no Gmail
NO_GMAIL = ("the board config names no Gmail login of the owner's (gmail_credentials), or the mailroom's config names "
            "no agents' account")


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


def audit(entry, res):
    """Put an action on the record (actions.jsonl) and return its result `res`; a record that cannot be written is
    added to the result's error. The board's actions here and the phone page's keyring unlock (mailweb.py) use it."""
    try:
        sources.append_jsonl(AUDIT, dict(entry, ts=time.time()))
    except OSError as e:
        _add_error(res, "the board could not record this action: %s" % e)
    return res


def _keep(kind, to, req, res, **facts):
    """Record a request carrying text or files (their names) with its outcome (sent.py), failed ones too so nothing
    the owner sent is lost. A result with note_sent (a resume, which can succeed with its note unsent) is judged by
    that, not by its ok. A record that cannot be written is added to the result's error, which the Mac shows."""
    ok = bool(res.get("note_sent", res.get("ok")))
    if not ok:
        facts["error"] = (res.get("error") or (not res.get("ok") and res.get("output")) or "not delivered")[-300:]
    if _has_files(req):
        facts["files"] = [f["name"] for f in req["files"]]
    try:
        sent.record(kind, to, req.get("text") if isinstance(req, dict) else None, ok,
                    **{k: v for k, v in facts.items() if v is not None})
    except OSError as e:
        _add_error(res, "the board could not keep its record of this message: %s" % e)


def _ref_for(req):
    """A new ref when the request carries text worth recording, or files, else None."""
    text = req.get("text") if isinstance(req, dict) else None
    return sent.new_ref() if (isinstance(text, str) and text.strip()) or _has_files(req) else None


def _has_files(req):
    """Whether the request carries files that _request_files took: a list of {"name", "data"}."""
    files = req.get("files") if isinstance(req, dict) else None
    return isinstance(files, list) and bool(files) and all(isinstance(f, dict) and isinstance(f.get("name"), str)
                                                          for f in files)


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
        for v, what, rule in ((model, "model", MODEL_RE), (effort, "effort", WORD_RE)):
            if v is not None and not _matching(v, rule):
                return None, "bad %s name" % what
        if to is None and model is None and effort is None:
            return None, "nothing to change"
        if not isinstance(req.get("force", False), bool):
            return None, "force must be true or false"
    if action == "send":
        files = req.get("files", [])
        err = _request_files(files) or _check_text(req, "message", MAX_TEXT, required=not files)
        if err:
            return None, err
        if req.get("via") is not None and req["via"] not in VIAS:
            return None, "via must be one of " + ", ".join(VIAS)
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


def _chat_id(req, key="group", rule=chats.ID_RE):
    """The request's chat id (or, by key, a post's or topic's id), or None when it isn't one. rule: chats.ID_RE for an
    id agent-group gave, chats.NEW_ID_RE for one the Mac made."""
    return _matching(req.get(key), rule)


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


def _post_place(req):
    """(where a chat post goes, error): {"topic", "new_topic", "reply_to"}, each None when not given. topic and
    reply_to are ids, new_topic a title (chats.title_error), stripped; topic and new_topic exclude each other. A reply
    with neither stays in the topic of the post it answers (chats.post)."""
    place = {k: req.get(k) for k in ("topic", "new_topic", "reply_to")}
    for key, what in (("topic", "a topic's"), ("reply_to", "a post's")):
        if place[key] is not None and not _chat_id(req, key):
            return None, "%s must be %s id" % (key, what)
    if place["new_topic"] is not None:
        err = chats.title_error(place["new_topic"], "topic")
        if err:
            return None, err
        if place["topic"] is not None:
            return None, "a post goes in a topic or opens a new one, not both"
        place["new_topic"] = place["new_topic"].strip()
    return place, None


@_answering(chats.ChatError)
def chat_post_action(req, run=sources.run, ref=None):
    """The owner's post: delivered by agent-group to every member, recorded with its ref (dispatch's keep). It goes in
    a topic, opens one, or answers a post (_post_place)."""
    group, post_id = _chat_id(req), _chat_id(req, "post", chats.NEW_ID_RE)
    if not group or not post_id:
        return {"ok": False, "error": "group must be a chat id and post a new post id"}
    err = _check_text(req, "post", MAX_TEXT, required=True) or chats.title_error(req.get("title"))
    place, place_err = (None, None) if err else _post_place(req)
    if err or place_err:
        return {"ok": False, "error": err or place_err}
    out = chats.post(group, post_id, req["text"].strip(), ref, run=run, **place)
    return audit(dict({"action": "chat_post", "group": group, "post": post_id, "chars": len(req["text"])}, **place),
                  {"ok": True, "output": out[-4000:]})


@_answering(chats.ChatError)
def chat_new_action(req, agents, run=sources.run):
    err = chats.title_error(req.get("title"))
    names, err = (None, err) if err else _chat_names(req, agents)
    if err:
        return {"ok": False, "error": err}
    entry = {"action": "chat_new", "title": req["title"].strip(), "members": names}
    try:
        chat = chats.new(req["title"].strip(), names, run)
    except chats.ChatMadeError as e:   # made, but its answer is not understood: on the record, and "made" for the Mac
        return audit(dict(entry, group=e.group, error=str(e)), {"ok": False, "error": str(e), "made": e.group})
    return audit(dict(entry, group=chat.get("id")), {"ok": True, "chat": chat})


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
    return audit({"action": "chat_members", "group": group, "op": op, "members": names},
                  {"ok": True, "output": out})


@_answering(chats.ChatError)
def chat_hide_action(req, run=sources.run):
    group, hidden = _chat_id(req), req.get("hidden")
    if not group or not isinstance(hidden, bool):
        return {"ok": False, "error": "group must be a chat id and hidden true or false"}
    out = chats.hide(group, hidden, run)
    return audit({"action": "chat_hide", "group": group, "hidden": hidden}, {"ok": True, "output": out})


@_answering(sources.CommandError)
def mail_list_action(req, run=sources.run, mail_store=None):
    """mail_store: the mailroom's store the threads' sessions and their newest mail's agents are read from
    (sources.MailStore of MAIL_DB)."""
    box, query, limit = req.get("box", "inbox"), req.get("query"), req.get("limit", 100)
    sessions = req.get("sessions", False)
    if box not in mailbox.BOXES:
        return {"ok": False, "error": "box must be one of " + ", ".join(mailbox.BOXES)}
    err = _check_text(req, "query", mailbox.MAX_QUERY, required=False, key="query")
    if err:
        return {"ok": False, "error": err}
    if not _whole(limit, 1, mailbox.MAX_LIMIT):
        return {"ok": False, "error": "limit must be a whole number from 1 to %d" % mailbox.MAX_LIMIT}
    if not isinstance(sessions, bool):
        return {"ok": False, "error": "sessions must be true or false"}
    listed = dict(mailbox.listing(box, query.strip() if query and query.strip() else None, limit, run), ok=True)
    if not sessions:
        return listed
    store = mail_store or sources.MailStore(sources.MAIL_DB)
    return mailbox.add_groups(mailbox.add_sessions(listed, store), store)


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
    return audit({"action": "mail_mark", "threads": threads, "op": op, "upto": upto, "until": until}, res)


@_answering(sources.CommandError)
def mail_reply_action(req, run=sources.run, drafts=None):
    """The owner's reply, which the mailroom keeps in full (an inbound mail from the owner) with its files, and routes
    within a second or two; the record (actions.jsonl) has its thread, key, length and its files' names and sizes. A
    file comes with the request ({"name", "data"}) or from the thread's drafts ({"name", "draft": its id},
    maildrafts.Store). A request that does not check out never reaches agent-mail, and agent-mail refuses one before it
    records anything with its exit code mailbox.ANSWER_REFUSED (the exit code alone says so, never its words); both
    answers say so ("refused": true), so the Mac knows the reply was not kept. When agent-mail fails otherwise
    (sources.CommandError), the mailroom may have kept it, and the answer is the plain error. Once the mailroom has
    it, the thread's draft goes if it holds this key (maildrafts.Store.sent): "draft" says {"cleared", "rev"}; when
    the drafts cannot be read, the answer is still ok (the reply went) with an error saying so, which the record keeps
    too ("draft_error"). drafts: a stand-in maildrafts.Store, for a test."""
    thread, key = _matching(req.get("thread"), mailbox.THREAD_RE), req.get("key")
    if not thread or not _matching(key, mailbox.KEY_RE):
        return {"ok": False, "error": "thread must be a thread id and key a new reply key", "refused": True}
    files = req.get("files", [])
    try:
        files = _draft_files(thread, files, drafts)
    except (maildrafts.Refused, OSError, ValueError) as e:
        return {"ok": False, "error": "not sent: %s" % e, "refused": True}
    err = _request_files(files) or _check_text(req, "reply", mailbox.MAX_REPLY, required=not files)
    if err:
        return {"ok": False, "error": err, "refused": True}
    text = req.get("text") or ""
    try:
        res = dict(mailbox.answer(thread, key, text.strip(), files, run), ok=True)
    except sources.CommandError as e:
        if e.code != mailbox.ANSWER_REFUSED:
            raise
        return {"ok": False, "error": str(e), "refused": True}
    record = {"action": "mail_reply", "thread": thread, "key": key, "chars": len(text), "files": _file_facts(files)}
    try:
        rev = (drafts or maildrafts.Store()).sent(thread, key)
        res["draft"] = record["draft"] = {"cleared": rev is not None, "rev": rev}
    except (OSError, ValueError) as e:
        record["draft_error"] = str(e)
        _add_error(res, "the reply went, but its draft was not emptied: %s" % e)
    return audit(record, res)


def _draft_files(thread, files, drafts):
    """A reply's files with each one named from the thread's drafts ({"name", "draft": id}) read from them as
    {"name", "data"}; the rest as they are. Raises maildrafts.Refused for one the drafts do not hold."""
    if not isinstance(files, list) or not any(isinstance(f, dict) and "draft" in f for f in files):
        return files
    if len(files) > mailbox.MAX_FILES:
        raise maildrafts.Refused("a reply carries at most %d files" % mailbox.MAX_FILES)
    store = drafts or maildrafts.Store()
    out = []
    for f in files:
        if isinstance(f, dict) and "draft" in f:
            if not (set(f) == {"name", "draft"} and isinstance(f["draft"], str) and maildrafts.FILE_RE.fullmatch(f["draft"])
                    and isinstance(f["name"], str)):
                raise maildrafts.Refused("a file from the drafts is {name, draft: its id}")
            out += store.reply_files(thread, [f])
        else:
            out.append(f)
    return out


def _file_facts(files):
    """What the record (actions.jsonl) keeps of a request's files, which _request_files took: each one's name and size
    in bytes, never its data."""
    return [{"name": f["name"], "bytes": len(base64.b64decode(f["data"]))} for f in files]


def _request_files(files):
    """Why the files of a request won't do (a mail reply, a message to an agent or the orchestrator, which the
    mailroom checks the same way), or None: a list of at most mailbox.MAX_FILES {"name": text, "data": base64}, at
    most mailbox.MAX_FILE_BYTES decoded together."""
    if not isinstance(files, list) or len(files) > mailbox.MAX_FILES:
        return "files must be a list of at most %d files" % mailbox.MAX_FILES
    total = 0
    for f in files:
        if not (isinstance(f, dict) and set(f) == {"name", "data"} and isinstance(f["name"], str)
                and 0 < len(f["name"]) <= mailbox.MAX_FILE_NAME and "\x00" not in f["name"] and isinstance(f["data"], str)):
            return "each file must be {name, data}, its name text of 1 to %d characters" % mailbox.MAX_FILE_NAME
        try:
            total += len(base64.b64decode(f["data"], validate=True))
        except ValueError:
            return "the data of %s is not base64" % f["name"]
    if total > mailbox.MAX_FILE_BYTES:
        return "the files come to %d bytes; at most %d go with one message" % (total, mailbox.MAX_FILE_BYTES)
    return None


def _set_mail_mode(req, command, run):
    thread, on = _matching(req.get("thread"), mailbox.THREAD_RE), req.get("on")
    if not thread or not isinstance(on, bool):
        raise sources.CommandError("thread must be a thread id and on true or false")
    return thread, on, mailbox.set_mode(command, thread, on, run)


@_answering(sources.CommandError)
def mail_fast_action(req, run=sources.run):
    """Switch Fast through agent-mail and return its thread. Write the agent and effort to actions.jsonl, or a problem
    when the answer had no thread. A command failure is returned as an error.
    """
    thread, on, answer = _set_mail_mode(req, ["fast"], run)
    res = dict(answer, ok=True)
    item = res.get("thread")
    state = item.get("fast") if isinstance(item, dict) else None
    state = state if isinstance(state, dict) else {}
    record = {"action": "mail_fast", "thread": thread, "on": on, "agent": state.get("agent"),
              "effort": state.get("effort"), "until": state.get("until")}
    if not isinstance(item, dict):
        record["problem"] = "agent-mail's answer had no thread"
    return audit(record, res)


@_answering(sources.CommandError)
def mail_researcher_action(req, run=sources.run):
    """Save Researcher through agent-mail for the expected routing tag and return the confirmed thread.

    Record the confirmed agent or a problem with the mailroom's answer in actions.jsonl. Return an error for a command
    failure or an unconfirmed switch.
    """
    expected_tag = _matching(req.get("expected_tag"), mailbox.TAG_RE)
    if not expected_tag:
        return {"ok": False, "error": "expected_tag must be a mail routing tag. Refresh the thread before switching "
                "Researcher."}
    thread, on, res = _set_mail_mode(req, ["researcher", "--expect-tag", expected_tag], run)
    record = {"action": "mail_researcher", "thread": thread, "on": on, "expected_tag": expected_tag}
    item = res.get("thread") if isinstance(res, dict) else None
    state = item.get("researcher") if isinstance(item, dict) and item.get("id") == thread else None
    if (isinstance(res, dict) and res.get("ok") is True and isinstance(state, dict)
            and state.get("on") is on and state.get("tag") == expected_tag):
        agent = state.get("agent")
        if isinstance(agent, str) and agent.strip():
            return audit(dict(record, agent=agent), res)
    why = res.get("error") if isinstance(res, dict) and res.get("ok") is False else None
    if not isinstance(why, str) or not why:
        why = "agent-mail did not confirm the requested Researcher state"
    return audit(dict(record, problem=why), {"ok": False, "error": why})


@_answering(sources.CommandError)
def mail_part_action(req, run=sources.run):
    key, index = req.get("key"), req.get("index")
    if not _matching(key, mailbox.MESSAGE_RE) or not _whole(index, 1, 10 ** 4):
        return {"ok": False, "error": "key must be a message's key (s<id> or i<id>) and index a part's number"}
    return dict(mailbox.part(key, index, run), ok=True)


@_answering((RuntimeError, ValueError, OSError, sqlite3.Error))
def mail_forward_action(req, gmail=None, mail_store=None):
    """An agent's mail as a draft in the owner's Gmail, to send on (mailforward.forward, gmailstate.create_draft), on
    the record (actions.jsonl) with the draft and its size. gmail, mail_store: stand-ins for the owner's Gmail
    (gmailstate.for_owner) and the mailroom's store (sources.MailStore), for a test."""
    thread, key = _matching(req.get("thread"), mailbox.THREAD_RE), _matching(req.get("key"), mailbox.MESSAGE_RE)
    if not thread or not key or not key.startswith("s"):
        return {"ok": False, "error": "thread must be a thread id and key an agent's mail (s<id>)"}
    gmail = gmail or gmailstate.for_owner(sync=True)
    if gmail is None:
        return {"ok": False, "error": "the tab cannot make a Gmail draft: " + NO_GMAIL}
    fwd = mailforward.forward(mail_store or sources.MailStore(sources.MAIL_DB), thread, int(key[1:]))
    made = gmail.create_draft(fwd.raw)
    message = (made.get("message") or {}).get("id")
    res = ({"ok": True, "draft": made.get("id"), "message": message} if isinstance(message, str) and message else
           {"ok": False, "error": "Gmail made the draft but named no message to open; it is in Gmail's Drafts"})
    return audit({"action": "mail_forward", "thread": thread, "key": key, "draft": made.get("id"), "message": message,
                   "bytes": len(fwd.raw), "images": fwd.images, "files": fwd.files}, res)


# ------------------------------------------------------------------ drafts (maildrafts.py)
# Each takes `drafts`, a stand-in maildrafts.Store for a test. A change goes on the record (actions.jsonl) with its
# thread, rev, device and sizes (maildrafts.measure), never a draft's words.

def _draft_thread(req):
    thread = _matching(req.get("thread"), mailbox.THREAD_RE)
    if not thread:
        raise maildrafts.Refused("thread must be a thread id")
    return thread


def _draft_change(req):
    """(thread, base_rev, device) of a request that changes a draft."""
    thread, base, device = _draft_thread(req), req.get("base_rev"), req.get("device")
    if not _whole(base, 0, 10 ** 15):
        raise maildrafts.Refused("base_rev must be the draft's rev the device last read")
    if device not in maildrafts.DEVICES:
        raise maildrafts.Refused("device must be one of " + ", ".join(maildrafts.DEVICES))
    return thread, base, device


_DRAFT_ERRORS = (maildrafts.Refused, ValueError, OSError)


@_answering(_DRAFT_ERRORS)
def draft_list_action(req, drafts=None):
    return {"ok": True, "drafts": (drafts or maildrafts.Store()).listing()}


@_answering(_DRAFT_ERRORS)
def draft_get_action(req, drafts=None):
    return dict((drafts or maildrafts.Store()).view(_draft_thread(req)), ok=True)


@_answering(_DRAFT_ERRORS)
def draft_put_action(req, drafts=None):
    thread, base, device = _draft_change(req)
    content = maildrafts.check_content(req.get("draft"))
    res = (drafts or maildrafts.Store()).put(thread, base, content, device)
    return audit(dict({"action": "draft_put", "thread": thread, "base_rev": base, "rev": res["rev"], "device": device,
                       "key": content["key"] is not None, "conflict": bool(res.get("conflict"))},
                      **maildrafts.measure(content)), dict(res, ok=True))


@_answering(_DRAFT_ERRORS)
def draft_discard_action(req, drafts=None):
    thread, base, device = _draft_change(req)
    res = (drafts or maildrafts.Store()).discard(thread, base, device)
    return audit({"action": "draft_discard", "thread": thread, "base_rev": base, "rev": res["rev"], "device": device,
                   "version": res.get("version"), "conflict": bool(res.get("conflict"))}, dict(res, ok=True))


@_answering(_DRAFT_ERRORS)
def draft_restore_action(req, drafts=None):
    thread, base, device = _draft_change(req)
    version = req.get("version")
    if not (isinstance(version, str) and maildrafts.VERSION_RE.fullmatch(version)):
        raise maildrafts.Refused("version must be a version's id")
    res = (drafts or maildrafts.Store()).restore(thread, version, base, device)
    return audit({"action": "draft_restore", "thread": thread, "base_rev": base, "rev": res["rev"], "device": device,
                   "version": version, "conflict": bool(res.get("conflict"))}, dict(res, ok=True))


@_answering(_DRAFT_ERRORS)
def draft_file_add_action(req, drafts=None):
    thread, data = _draft_thread(req), req.get("data")
    if not isinstance(data, str) or len(data) > mailbox.MAX_FILE_BYTES * 4 // 3 + 4:
        raise maildrafts.Refused("data must be the file's bytes in base64, at most %d of them" % mailbox.MAX_FILE_BYTES)
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError:
        raise maildrafts.Refused("data is not base64") from None
    got = (drafts or maildrafts.Store()).add_file(thread, raw)
    return audit({"action": "draft_file_add", "thread": thread, "file": got["id"], "bytes": got["size"]},
                 {"ok": True, "file": got})


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
        res = audit({"action": "time", "refresh": True}, {"ok": True})
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
    return _agent_send(req, agent, close_text(), run, [])


def _send(req, agent, run=sources.run_merged, ref=None):
    """The owner's message into a live agent, ending with how to mark it done (ref: its ref on sent.py's record), and
    with the owner's files, which agent-send saves for the agent, giving their paths after the message
    (--input-json)."""
    body = header() + "\n" + (req.get("text") or "").strip() + send_footer(ref)
    return _agent_send(req, agent, body, run, req.get("files") or [])


def _agent_send(req, agent, body, run, files):
    """agent-send of body into the agent, with files ([{"name", "data"}], none for a close) as the owner's
    (--input-json)."""
    try:
        argv = sources.agent_send_argv(agent["cli"], agent["id"])
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if files:
        argv, body = argv + ["--from", owner(), "--input-json"], json.dumps({"text": body, "files": files})
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
    `record` that name its target, unless `record` is None (a dry run). A message a Codex agent holds in its queue
    (AGENT_SEND_HELD) is not ok, and its error says it waits there and why, which the Mac shows and sent.py keeps."""
    res = {"ok": code == 0, "code": code, "output": output[-4000:], "action": req["action"], "name": name}
    if req["action"] == "send" and code == AGENT_SEND_HELD:
        why = (output.strip().splitlines() or ["agent-send gave no reason"])[-1]
        res["error"] = "waiting in Codex's queue: " + why
    if record is None:
        return res
    via = {"via": req["via"]} if req.get("via") else {}
    files = {"files": _file_facts(req["files"])} if _has_files(req) else {}
    return audit(dict(record, action=req["action"], code=code, chars=len(req.get("text") or ""), **via, **files), res)


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
    return audit({"action": "switch", "id": agent["id"], "name": agent["display"], "code": code, "to": req.get("to"),
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
        err = _check_login(req.get("account"))
        if err:
            return None, err
    return wt, None


def _check_login(name):
    """Why `name` is not a login a new agent may be started on by name, or None (None: no name, Automatic). It must
    be one of the mailroom's manual logins (sources.manual_logins), the ones the start dialog offers."""
    if name is None:
        return None
    if not _matching(name, LOGIN_RE):
        return "login must be the name of one of the mailroom's logins"
    try:
        names = [x["name"] for x in sources.manual_logins()]
    except (OSError, ValueError, RuntimeError, ImportError) as e:
        return "can't read the mailroom's logins: %s" % sources.error_text(e)
    return None if name in names else "%s is not a login the board can start an agent on (the mailroom's manual " \
        "logins: %s)" % (name, ", ".join(names) or "none")


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
    """agent-spawn --dir on the worktree with the owner's instructions in its brief, on the login usage picks or, with
    the request's "account", on that one (--account), put on the record (actions.jsonl). state: its git state for the
    brief (None: read now). ref: the instructions' ref on sent.py's record."""
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
    login = ["--account", req["account"]] if req.get("account") else []          # validate_wt checked it
    code, out = run([exe, "--dir", path] + login + ["--brief-text", brief, "--json"], timeout=240)
    return _result(req, collect.worktree_name(path), code, out, dict({"path": path}, **(
        {"account": req["account"]} if req.get("account") else {})))


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
        audit({"action": "wt_landed_cleanup", "path": p, "code": code}, out)
    done = sum(1 for r in results if r["ok"])
    return dict(out, removed=done, kept=len(results) - done, results=results)


# ------------------------------------------------------------------ the orchestrator
# where the owner wrote a message to the orchestrator (the request's "source") -> how its header names that place:
# the Mac app sends "mac" (Dialogs.orchestrator), the phone's page "phone" (mailweb.ORCH_SOURCE, whatever it asks)
ORCH_SOURCES = {"mac": "the Mac app, over SSH", "phone": "the phone app, Agent Mail, over the tailnet"}
ORCH_HEADER_FMT = ("[message from {owner} via the agent board ({source}), not by email. This is {owner} writing: act "
                   "on it as you would an email from {owner}. There is no thread to reply in: answer with "
                   "`agent-mail send --to owner --subject \"...\" --body-file ...`; the reply to that mail comes "
                   "back to you.]")           # "owner": the mailroom's alias for the owner's address


def orch_header(cfg=None, source="mac"):
    """The header of a message to the orchestrator from `source` (a key of ORCH_SOURCES); cfg: the mailroom's config,
    read now when None."""
    cfg = cfg if cfg is not None else config.mail_config()
    return ORCH_HEADER_FMT.format(owner=config.owner_name(cfg=cfg), source=ORCH_SOURCES[source])


ORCH_LOGS = sources.BOARD_STATE
ORCH_START_SECONDS = 1     # how long a new run is watched for failing at once (systemd-run with no user bus, say)
ORCH_STOP_SECONDS = 150    # how long `systemctl --user stop` may take on a run's scope (its stop timeout is 90 s)
ORCH_STARTED = "The orchestrator is running; it will email you."
ZWJ = "\u200d"            # the zero-width joiner, which joins emoji (❤️‍🔥): the one Cf allowed


def _invisible(text):
    """Why `text` won't do as a message to the orchestrator, or None: it must hold no invisible characters, Unicode
    format characters (category Cf: bidi controls, zero-width spaces and non-joiners...) but ZWJ, or the tag block
    (U+E0000 to U+E007F), which text pasted from agent mail can carry and which can hide words the orchestrator reads
    but the owner does not see."""
    found = []
    for c in text:
        hidden = (unicodedata.category(c) == "Cf" and c != ZWJ) or 0xE0000 <= ord(c) <= 0xE007F
        if hidden and "U+%04X" % ord(c) not in found:
            found.append("U+%04X" % ord(c))
    if not found:
        return None
    return ("the message holds invisible characters (%s), likely from pasted text; take them out and send it again"
            % ", ".join(found[:5] + (["and %d more" % (len(found) - 5)] if len(found) > 5 else [])))


def _started(p, data):
    """Give the run `p` just started `data` on its stdin, then watch it for ORCH_START_SECONDS: (its exit code if it
    ended within them, else None as it is running; whether it took all of `data`). A run that closed its stdin before
    the end refuses the write (BrokenPipeError): it took none or part of the message, which the caller must answer."""
    try:
        p.stdin.write(data)
        p.stdin.close()
        took = True
    except BrokenPipeError:
        took = False
        with contextlib.suppress(BrokenPipeError):           # what was left in the pipe's buffer cannot go either
            p.stdin.close()
    try:
        return p.wait(timeout=ORCH_START_SECONDS), took
    except subprocess.TimeoutExpired:
        return None, took


@_answering((OSError, ValueError))
def orchestrate(req, popen=None, ref=None, run=sources.run):
    """Start one orchestrator event with the owner's text, headed by orch_header naming the request's source, and the
    owner's files (_request_files; with files the text may be empty), which agent-orchestrate saves for it, giving
    their paths after the text (--input-json), and return once it has run ORCH_START_SECONDS without failing; the run
    takes 30 s to a few minutes. It runs under systemd-run in a scope of its own
    (agent-orchestrate-<time>-<random>.scope, its result's `unit`, with no memory cap), so it outlives the SSH session
    that started it. Its output, and systemd-run's, go to ORCH_LOGS/orchestrate-<time>-<random>.log, the result's
    `log`. A request whose key started a run before starts none and answers "already": true with that run's log.
    Refused, with nothing run, for text that won't do (too long, a NUL, invisible characters) or files, a source not
    in ORCH_SOURCES, a key not mailbox.KEY_RE's, or no agent-orchestrate or systemd-run on the desktop. Refused as not
    started: a run that ends at once with an error (systemd-run with no user bus), with its log's last lines, and one
    that stopped reading its message before the end: one still running has part of the message or none, so its scope
    is stopped (`systemctl --user stop`) and the refusal says so, or names it and says that it could not be stopped.
    Started runs and failed starts go in actions.jsonl with their key, so a key whose start failed can be sent again,
    one start at a time (a lock on ORCH_LOGS/orchestrate.lock), so a key is checked and recorded together.
    ref: the text's ref on sent.py's record. popen: subprocess.Popen, or a test's stand-in; run: sources.run (for
    systemctl), or a test's stand-in."""
    files = req.get("files", [])
    err = (_request_files(files) or _check_text(req, "message", MAX_BRIEF, required=not files)
           or _invisible(req.get("text") or ""))
    if err:
        return {"ok": False, "error": err}
    source, key = req.get("source"), _matching(req.get("key"), mailbox.KEY_RE)
    if not (isinstance(source, str) and source in ORCH_SOURCES):
        return {"ok": False, "error": "source must be one of " + ", ".join(ORCH_SOURCES)}
    if not key:
        return {"ok": False, "error": "key must be a new message key"}
    text = req.get("text") or ""
    exe, scope = sources.find_bin("agent-orchestrate"), sources.find_bin("systemd-run")
    if not exe:
        return {"ok": False, "error": "agent-orchestrate is not installed on the desktop"}
    if not scope:
        return {"ok": False, "error": "systemd-run is not on the desktop, so the orchestrator has no scope to run in"}
    with sources.file_lock(ORCH_LOGS / "orchestrate.lock"):
        earlier = next((e for e in sources.read_jsonl(AUDIT) if isinstance(e, dict) and e.get("action") == "orchestrate"
                        and e.get("key") == key and e.get("started")), None)
        if earlier:
            return {"ok": True, "already": True, "log": earlier.get("log"),
                    "output": "This message started the orchestrator already; it will email you."}
        name = "%d-%s" % (time.time(), secrets.token_hex(3))         # the run's time and a random part
        unit, log = "agent-orchestrate-" + name, ORCH_LOGS / ("orchestrate-%s.log" % name)
        body = orch_header(source=source) + "\n\n" + text.strip()
        if ref:
            body += "\n\nThis is message %s on the board. %s" % (ref, sent.done_line(ref))
        # before anything starts: text it cannot encode starts nothing. The owner's files go with it as the
        # agent-send of a message does (--input-json: saved for the orchestrator, their paths after the text)
        data = (json.dumps({"text": body, "files": files}) if files else body).encode()
        with open(log, "ab") as fh:
            p = (popen or subprocess.Popen)([scope, "--user", "--scope", "--collect", "--unit", unit, "--", exe,
                                             "--source", "board"] + (["--input-json"] if files else []),
                                            stdin=subprocess.PIPE, stdout=fh, stderr=subprocess.STDOUT,
                                            start_new_session=True)
            code, took = _started(p, data)
        entry = dict({"action": "orchestrate", "source": source, "key": key, "chars": len(text), "log": str(log),
                      "unit": unit}, **({"files": _file_facts(files)} if files else {}))
        if code is None and not took:            # running on part of the message or none: it must not act on it
            stop, out, err = run(["systemctl", "--user", "stop", unit + ".scope"], timeout=ORCH_STOP_SECONDS)
            why = "the orchestrator did not take the message: it stopped reading it before the end, " + (
                "so its run %s was stopped" % unit if stop == 0 else
                "and its run %s, which may act on part of it, COULD NOT BE STOPPED (systemctl exit %d: %s); stop it "
                "with: systemctl --user stop %s.scope" % (unit, stop, (err or out).strip()[-300:], unit))
            return audit(dict(entry, started=False, stopped=stop == 0, error=why),
                          {"ok": False, "error": why, "log": str(log)})
        if code or not took:
            said = log.read_text(errors="replace")[-300:].strip()
            why = ("the orchestrator did not start (exit %d): %s" % (code, said) if code else
                   "the orchestrator did not take the message: it stopped reading it before the end, and ended: %s"
                   % said)
            return audit(dict(entry, started=False, error=why), {"ok": False, "error": why, "log": str(log)})
        return audit(dict(entry, started=True), {"ok": True, "output": ORCH_STARTED, "log": str(log), "unit": unit})


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
                                       agent.get("name") or name,
                                       relaunch.tmux_env_names(tmux, run) if agent["cli"] == "claude" else ())
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
    return audit({"action": "resume", "id": agent["id"], "name": agent["display"], "tmux": name, "live": up},
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
        return audit({"action": "restart_cancel", "ok": res.get("ok")}, res)
    update, wait, choices = req.get("update"), req.get("wait_minutes", 15), req.get("choices") or {}
    if not isinstance(update, bool) or not isinstance(wait, int) or not isinstance(choices, dict):
        return {"ok": False, "error": "update must be true/false, wait_minutes a number, choices an object"}
    if not all(_matching(k, ID_RE) and v in restart.CHOICES for k, v in choices.items()):
        return {"ok": False, "error": "choices must map full session ids to nudge, quiet or skip"}
    res = restart.start_run(update, wait, choices)
    return audit({"action": "restart_run", "update": update, "wait": wait, "choices": len(choices), "ok": res.get("ok")},
                  res)


# ------------------------------------------------------------------ the phone page (mailweb.py)
def mail_web_enrol_action(req, run=sources.run, store=None, now=None, page="/enrol"):
    """A one-time code for enrolling the phone's passkey (mailwebstate.Store.mint_code), as the page's enrolment
    address (PAGE#CODE: the code after #, so it never reaches a log), with when it lapses. On the record without the
    code. run, store, now: stand-ins a test gives for tailscale, the page's state and the clock. page: the enrolling
    page's path, the Mail page's or the voice page's ("/voice/enrol", from `agent-board voice-enrol`). Refused, saying
    why, when the page's settings cannot be read or Tailscale gives no tailnet name."""
    try:
        cfg = mailwebstate.settings()
        host = mailwebstate.tailnet_name(run)
    except (ValueError, RuntimeError, sources.CommandError) as e:
        return {"ok": False, "error": "the phone page is not set up: %s" % sources.error_text(e)}
    store = store or mailwebstate.Store(mailwebstate.STATE)
    code, lapses = store.mint_code(now or time.time(), cfg["enrol_minutes"])
    res = {"ok": True, "url": mailwebstate.page_url(host, cfg["port"], page + "#" + code), "lapses": lapses}
    return audit({"action": "mail_web_enrol", "page": page, "lapses": lapses}, res)


def mail_web_sign_out_action(req, store=None):
    """End every session on the phone page; with `forget`, forget its passkeys too. On the record. store: a test's
    stand-in for the page's state."""
    forget = req.get("forget")
    if not isinstance(forget, bool):
        return {"ok": False, "error": "forget must be true or false"}
    ended, forgot = (store or mailwebstate.Store(mailwebstate.STATE)).sign_out(forget)
    res = {"ok": True, "sessions": ended, "passkeys": forgot}
    return audit({"action": "mail_web_sign_out", "forget": forget, "sessions": ended, "passkeys": forgot}, res)


# ------------------------------------------------------------------ the Mac's ask box (voicedesk.py)
# Read-only, as `agent-board looking` and `ask` are: not on the record here (voicedesk.ask logs each ask in asks.jsonl)
HISTORY_PAIRS = 3           # the box's earlier questions and answers a voice_ask carries, at most (mac AskBox.swift,
                            # AskRules.historyPairs, sends no more)
HISTORY_CHARS = 4 * voicedesk.MAX_QUESTION      # an earlier question or answer, at most (ask_text cuts what won't
                                                # fit; the Mac sends at most AskRules.historyChars of each)
AGENT_CHARS = 100           # the agent a voice_ask names: its id, or its name as voicedesk.resolve takes it
HISTORY_HEAD_FMT = "[Earlier in this ask box, {owner} asked a copy of you these, and it answered:]"
HISTORY_NOW_FMT = "[{owner}'s question now:]"


def voice_looking_action(req, rows=None, **seams):
    """What the owner has in front of them on the Mac and the agent behind it, as voicedesk.looking gives them (it passes
    over the box's own window, titled voicedesk.VOICE_TITLE...), with "agents": the live agents ([{name, id, status,
    folder}]; voicedesk.agents, or `rows`) for the box to offer when none is found. seams: looking's stand-ins (`seen`,
    the Mac's windows; `db`; `run`), for a test."""
    rows = voicedesk.agents() if rows is None else rows
    res = voicedesk.looking(rows=rows, **seams)
    return dict(res, agents=[{k: a.get(k) or "" for k in ("name", "id", "status", "folder")} for a in rows])


def ask_text(question, history, limit=voicedesk.MAX_QUESTION):
    """What a voice_ask puts to the agent's copy: the owner's `question` after the box's earlier questions and answers
    (`history`, [{question, answer}], oldest first; its last HISTORY_PAIRS), so that a follow-up such as "and the second
    one?" can be answered, since each question forks a copy that saw none of them (voicedesk.ask). Pairs are left out,
    oldest first, until it is at most `limit` characters; a last pair still too long has its answer cut, or is left out
    too when under 200 characters of it would be left. `question` alone fits (voice_ask_action checks it)."""
    who = owner()

    def text(pairs):
        if not pairs:
            return question
        return "\n".join([HISTORY_HEAD_FMT.format(owner=who)]
                         + ["%s: %s\nYou: %s\n" % (who, p["question"], p["answer"]) for p in pairs]
                         + [HISTORY_NOW_FMT.format(owner=who), question])
    pairs = [{"question": h["question"].strip(), "answer": h["answer"].strip()} for h in history][-HISTORY_PAIRS:]
    while pairs and len(text(pairs)) > limit:
        if len(pairs) > 1:
            pairs = pairs[1:]
            continue
        keep = len(pairs[0]["answer"]) - (len(text(pairs)) - limit) - 1
        pairs = [dict(pairs[0], answer=pairs[0]["answer"][:keep] + "…")] if keep >= 200 else []
    return text(pairs)


def _history_error(history):
    """Why a voice_ask's history won't do, or None: at most HISTORY_PAIRS {question, answer}, each text of at most
    HISTORY_CHARS characters with no NUL, the question not blank."""
    if isinstance(history, list) and len(history) <= HISTORY_PAIRS and all(
            isinstance(h, dict) and set(h) == {"question", "answer"} and isinstance(h["answer"], str)
            and not _check_text(h, "question", HISTORY_CHARS, required=True, key="question")
            and not _check_text(h, "answer", HISTORY_CHARS, required=False, key="answer") for h in history):
        return None
    return ("history must be a list of at most %d {question, answer}, each text of at most %d characters"
            % (HISTORY_PAIRS, HISTORY_CHARS))


def voice_ask_action(req, on_part=None, **seams):
    """The owner's question in the ask box to the agent `agent` names (voicedesk.resolve: the Mac sends its id), answered by
    a read-only copy of its conversation (voicedesk.ask, which logs it): {ok, agent, answer, seconds, parts}, or {ok:
    False, error}, with seconds and parts once the copy was asked. `history`, the box's earlier questions to that agent
    and their answers, goes before the question (ask_text). The answer is spoken as it is written, a sentence at a time
    (speech.Narrator, on the daemon speech.settings names): each part goes to on_part as soon as it is ready, and
    `parts` counts them; without on_part (an `act` run over ssh of its own), `parts` lists them. seams: voicedesk.ask's
    stand-ins (popen for the claude or codex CLI, codex_bin, rows, homes, log, copies_path, upstream), for a test."""
    err = (_check_text(req, "agent", AGENT_CHARS, required=True, key="agent")
           or _check_text(req, "question", voicedesk.MAX_QUESTION, required=True, key="question")
           or _history_error(req.get("history", [])))
    if err:
        return {"ok": False, "error": err}
    kept = []
    narrator = _narrator(on_part or kept.append)
    res = voicedesk.ask(req["agent"].strip(), ask_text(req["question"].strip(), req.get("history", [])),
                        on_text=narrator.feed, **seams)
    said = narrator.finish(res.get("answer"))
    parts = said["parts"] if on_part else kept
    if not res["ok"]:
        return {"ok": False, "error": res["error"], "seconds": res["seconds"], "parts": parts}
    return {"ok": True, "agent": res["agent"], "answer": res["answer"], "seconds": res["seconds"], "parts": parts}


def _narrator(on_part):
    """A speech.Narrator handing each sentence's part to on_part, on the daemon speech.settings names (or saying in each
    part why it cannot, when those settings are bad)."""
    try:
        cfg, bad = speech.settings(), None
    except (ValueError, OSError) as e:
        cfg, bad = None, str(e)
    return speech.Narrator(on_part, cfg, bad)


# what the box shows while the desk runs a tool (a desk tool event's name; {agent}: the agent the tool names)
DESK_STATUS = {"agents": "Looking over your agents…", "recent": "Reading {agent}'s latest messages…",
               "ask": "Asking {agent}…", "looking": "Looking at your screen…", "send": "Sending {agent} a message…",
               "mail_list": "Looking through your mail…", "mail_show": "Reading a mail…",
               "WebSearch": "Searching the web…", "Read": "Reading your files…", "Grep": "Searching your files…",
               "Glob": "Looking for files…"}


def desk_status(tool):
    """The status line for a desk tool event {name, input}: DESK_STATUS's, naming the agent the model gave the tool."""
    agent = tool["input"].get("agent") if isinstance(tool.get("input"), dict) else None
    line = DESK_STATUS.get(tool.get("name"), "Working…")
    return line.format(agent=agent.strip()[:60] if isinstance(agent, str) and agent.strip() else "an agent")


def desk_say_action(req, on_part=None):
    """The owner's words in the ask box to the desk (desk.py's daemon, on the socket desk.settings names), with what is
    on the owner's screen (`looking`: what voice_looking gave the box, or true for the desk to read it): {ok, text, sends, failed,
    screen_error?, timings, parts}, or {ok: False, error, ...}. The desk's reply is spoken as it streams, a sentence at
    a time (_narrator, as voice_ask's answer is; what it says before a tool, at once), and each tool it calls is a part
    too, {"status": DESK_STATUS's line}; each part goes to on_part as soon as it is ready, and `parts` counts the spoken
    ones; without on_part, `parts` lists them all. Its sends are the desk's own (on the record as via the desk),
    returned in `sends` for the box."""
    from . import desk          # desk imports this module
    err = _check_text(req, "words", MAX_TEXT, required=True, key="text")
    if err:
        return {"ok": False, "error": err}
    try:
        cfg = desk.settings()
    except (ValueError, OSError) as e:
        return {"ok": False, "error": "the desk's settings: %s" % e}
    kept = []
    take = on_part or kept.append
    narrator, final, error = _narrator(take), None, None
    turn = dict({"text": req["text"].strip()}, **({"looking": req["looking"]} if "looking" in req else {}))
    try:
        for event in desk.events(cfg, turn):              # a `looking` it will not take is its refusal, in `final`
            if event.get("type") == "delta":
                narrator.feed(event["text"])
            elif event.get("type") == "tool":       # what it said before the tool is heard now, not after it
                narrator.pause()
                take({"status": desk_status(event)})
            elif event.get("type") == "final":
                final = event
                break
    except (OSError, ValueError) as e:
        error = "could not reach the desk on %s: %s" % (cfg["socket"], e)
    said = narrator.finish((final or {}).get("text"))
    final = final or {"ok": False, "error": error or "the desk gave no answer"}
    out = {k: final[k] for k in ("ok", "text", "sends", "failed", "screen_error", "timings", "error") if k in final}
    return dict(out, parts=said["parts"] if on_part else kept)


def desk_new_action(req):
    """A fresh conversation for the desk (desk.new_conversation): {ok, text} or {ok: False, error}."""
    from . import desk          # desk imports this module
    try:
        final = desk.new_conversation(desk.settings())
    except (ValueError, OSError) as e:
        return {"ok": False, "error": "could not reach the desk: %s" % e}
    if not final:
        return {"ok": False, "error": "the desk gave no answer"}
    return {k: final[k] for k in ("ok", "text", "error") if k in final}


# ------------------------------------------------------------------ the table of actions
# run: the handler, called with the request and then what `needs` names, in that order (the board's agent rows, the
# repos' linked worktrees). keep: for an action carrying the owner's text, the kind sent.py records it as and a function
# of (request, agent rows) giving (to whom, facts that find it again); None for the rest.
Action = collections.namedtuple("Action", "run needs keep")
NOTHING, AGENTS, WORKTREES = (), ("agents",), ("agents", "worktrees")


def _to_agent(req, agents):
    agent = next((a for a in agents if a["id"] == req.get("id")), None) or {}
    via = req.get("via") if req.get("via") in VIAS else None
    return agent.get("display") or str(req.get("id"))[:36], {"id": agent.get("id"), "cli": agent.get("cli"),
                                                              "via": via}


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
    "mail_fast": Action(mail_fast_action, NOTHING, None),
    "mail_researcher": Action(mail_researcher_action, NOTHING, None),
    "mail_part": Action(mail_part_action, NOTHING, None),
    "mail_forward": Action(mail_forward_action, NOTHING, None),
    "draft_list": Action(draft_list_action, NOTHING, None),
    "draft_get": Action(draft_get_action, NOTHING, None),
    "draft_put": Action(draft_put_action, NOTHING, None),
    "draft_discard": Action(draft_discard_action, NOTHING, None),
    "draft_restore": Action(draft_restore_action, NOTHING, None),
    "draft_file_add": Action(draft_file_add_action, NOTHING, None),
    "groups": Action(groups_action, AGENTS, None),
    "time": Action(time_action, NOTHING, None),
    "usage_split": Action(usage_split_action, NOTHING, None),
    "screen": Action(screen_action, NOTHING, None),
    "restart_plan": Action(restart_action, NOTHING, None),
    "restart_run": Action(restart_action, NOTHING, None),
    "restart_cancel": Action(restart_action, NOTHING, None),
    "mail_web_enrol": Action(mail_web_enrol_action, NOTHING, None),
    "mail_web_sign_out": Action(mail_web_sign_out_action, NOTHING, None),
    "voice_looking": Action(voice_looking_action, NOTHING, None),
    "voice_ask": Action(voice_ask_action, NOTHING, None),
    "desk_say": Action(desk_say_action, NOTHING, None),
    "desk_new": Action(desk_new_action, NOTHING, None),
}
PARTS = ("voice_ask", "desk_say")   # the actions that answer in parts first (on_part; agent-board act --parts)


def dispatch(req, agents, worktrees, ref=None, **seams):
    """Run one request from the Mac (any JSON value) and return its result: {"ok": bool, ...}, with "error" saying
    why not. agents, worktrees: functions giving the board's agent rows and the repos' linked worktrees, called only
    for an action that needs them. seams: stand-ins a test passes on to the handler (run, closer, popen, fetch,
    state, gmail, store, mail_store, drafts, table, now; and voicedesk's, for the ask box's). A request of an action
    with a `keep` gets a ref (`ref`, when the caller made it: the desk's sends, which tell the owner the ref), and the
    owner's text goes on sent.py's record with its outcome, unless its action answers that it did it already
    (`already`: a repeat of a request by its key); a Mail tab reply (mail_reply) has none, since the mailroom records
    it."""
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
    t0, ref = time.time(), ref or _ref_for(req)
    res = spec.run(req, *args, ref=ref, **seams)
    if res.get("already"):
        return res
    name, facts = to(req, built.get("agents", []))
    _keep(kind, name, req, res, ts=t0, ref=ref, log=res.get("log"), **facts)     # log: an orchestrator run's output
    return res
