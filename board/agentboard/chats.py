"""Group chats: threads between the owner and some of the agents, kept by the mailroom's agent-group (its README,
"Groups"). The board reads and writes them as the owner, `agent-group ... --as owner` (as_owner(): the alias the
mailroom takes for the owner whatever their name), which agent-group refuses inside an agent session; the board's
collector and `agent-board act` run from the Mac's SSH login, outside any.

The owner's post ends with a footer like a board message's: its ref on sent.py's record and the command one member
runs once it is dealt with. posts() takes the footer off again, so the Mac shows what was typed, with the ref beside it.
"""
import json
import re

from . import config, sent, sources

ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{21}$")      # a chat's or post's id: 16 bytes base64url, no leading -
NAME_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,63}$")  # a session's name, as agent-group takes it
# Bytes in a title, and agents in a chat: agent-group's limits (its groups.MAX_TITLE_BYTES and MAX_MEMBERS), checked
# here so a bad request never reaches it. Chats.swift's chatMaxMembers is the Mac's copy of the second.
MAX_TITLE = 200
MAX_MEMBERS = 16
REFRESH = 30             # seconds between the snapshot's reads of the list
FOOTER_FMT = ("\n\n({owner} posted this from the agent board as message %s. One member, whoever deals with it, marks "
              "it done.)\n")


OWNER_ALIAS = "owner"     # the mailroom's config.OWNER_ALIAS: agent-group --as takes it whatever the owner's name


def as_owner():
    """The name agent-group's --as takes for the owner: the mailroom's alias "owner" (its owner_name works too)."""
    return OWNER_ALIAS


def footer_head(owner=None):
    return FOOTER_FMT.format(owner=owner or config.owner_name())


def footer_re(owner=None):
    return re.compile(re.escape(footer_head(owner)).replace("%s", "(?P<ref>[a-hjkmnp-z2-9]{4})") + ".*\\Z", re.S)


class ChatError(Exception):
    """A group chat could not be read or changed: agent-group failed or is missing, or (subchats.state) the sub-group
    chats' state file cannot be read; the message says why, for the Mac to show."""


def footer(ref):
    """The end of the owner's post: its ref, and (sent.done_line) the command one member runs once it is dealt with."""
    return footer_head() % ref + sent.done_line(ref)


def split_footer(text):
    """(text without the board's footer, its ref or None)."""
    m = footer_re().search(text)
    return (text[:m.start()], m.group("ref")) if m else (text, None)


def _call(args, run, stdin=None, parse=True):
    """agent-group ARGS (as the owner): its parsed JSON output when `parse`, else its text (sources.call). Raises
    ChatError."""
    try:
        return sources.call("agent-group", args, run, stdin, parse)
    except sources.CommandError as e:
        raise ChatError(str(e)) from None


def installed():
    return sources.find_bin("agent-group") is not None


def chat_list(run=sources.run):
    """Every group chat, hidden ones included: agent-group's list objects."""
    return _call(["list", "--json", "--as", as_owner()], run)


def show(group, run=sources.run):
    return _call(["show", "--json", "--as", as_owner(), "--", group], run)


def posts(group, since=None, run=sources.run):
    """The chat's posts in order (after `since`), the owner's without the board's footer and with their `ref`."""
    args = ["posts", "--json", "--as", as_owner()] + (["--since", since] if since else []) + ["--", group]
    out = []
    for p in _call(args, run):
        if p.get("from_agent") is None and isinstance(p.get("text"), str):
            p["text"], p["ref"] = split_footer(p["text"])
        out.append(p)
    return out


def post(group, post_id, text, ref, run=sources.run):
    """The owner's post, with the board's footer, on agent-group's stdin; post_id is its idempotency key."""
    return _call(["post", "--as", as_owner(), "--id", post_id, "--", group], run, stdin=text + footer(ref), parse=False)


def new(title, names, run=sources.run):
    """Make the chat; agent-group's object for it, as show() gives it."""
    return _call(["new", "--json", "--as", as_owner(), "--with", ",".join(names), "--", title], run)


def members(group, op, names, run=sources.run):
    return _call([op, "--as", as_owner(), "--", group, ",".join(names)], run, parse=False)


def set_chat(group, title, names, run=sources.run):
    """Give the chat exactly this title and these members (subchats.py); agent-group changes nothing when they are
    already so. Its object for the chat, as show() gives it. The values go in --opt=VALUE form, so a title starting
    with "-" is not read as an option."""
    return _call(["set", "--json", "--as", as_owner(), "--title=" + title, "--with=" + ",".join(names), "--", group],
                 run)


def hide(group, hidden, run=sources.run):
    return _call(["hide" if hidden else "unhide", "--as", as_owner(), "--", group], run, parse=False)


def title_error(title):
    """Why a chat title won't do, or None: one line of 1 to MAX_TITLE bytes."""
    if not isinstance(title, str) or not title.strip():
        return "the chat needs a title"
    if len(title.encode()) > MAX_TITLE or "\n" in title or "\x00" in title:
        return "a chat title is one line of at most %d bytes" % MAX_TITLE
    return None


def rows(listed):
    """agent-group's list as the board shows it, in the snapshot and the Chats sheet alike (the Mac's ChatSummary):
    [{id, title, hidden, members (a count), posts (a count), last_post (ms epoch or None)}]. Raises ChatError when
    it is not a list of chats."""
    if not (isinstance(listed, list) and all(isinstance(c, dict) for c in listed)):
        raise ChatError("agent-group's list is not a list of chats: %s" % json.dumps(listed)[:200])
    return [{"id": c.get("id"), "title": c.get("title"), "hidden": bool(c.get("hidden")),
             "members": len(c.get("members") or []), "posts": c.get("posts") or 0, "last_post": c.get("last_post")}
            for c in listed]


def summary(run=sources.run):
    """The snapshot's view of the chats, for the Mac's Chats button: None when agent-group isn't installed, else
    {"chats": rows()}. Raises ChatError. subchats.summary adds each chat's sub-group, and the collector reads that
    every REFRESH seconds."""
    if not installed():
        return None
    return {"chats": rows(chat_list(run))}
