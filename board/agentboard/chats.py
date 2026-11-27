"""Group chats: threads between the owner and some of the agents, kept by the mailroom's agent-group (its README,
"Groups"). The board reads and writes them as the owner, `agent-group ... --as owner` (as_owner(): the alias the
mailroom takes for the owner whatever their name), which agent-group refuses inside an agent session; the board's
collector and `agent-board act` run from the Mac's SSH login, outside any.

The owner's post ends with a footer like a board message's: its ref on sent.py's record and the command one member
runs once it is dealt with. posts() takes the footer off again, so the Mac shows what was typed, with the ref beside it.

A chat's posts can be in topics: a topic is {id, title} on a post, exists from its first post, and a reply stays in
the topic of the post it answers.

Every id (a chat's, a post's or a topic's) goes to agent-group after "--" or in --opt=ID form, never where it could be
read as an option.
"""
import json
import re
import unicodedata

from . import config, sent, sources

ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")                  # a chat's, post's or topic's id: 16 bytes base64url
NEW_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{21}$")  # an id the Mac makes for a post: agent-group's own, no -
NAME_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,63}$")  # a session's name, as agent-group takes it
# Bytes in a title, and agents in a chat: agent-group's limits (its groups.MAX_TITLE_BYTES and MAX_MEMBERS), checked
# here so a bad request never reaches it. Chats.swift's chatMaxMembers is the Mac's copy of the second.
MAX_TITLE = 200
MAX_MEMBERS = 16
# Characters a chat's or a topic's title may not hold, by Unicode category: control characters (tab and NEL among
# them) and line and paragraph separators: agent-group's groups.NOT_IN_A_LINE.
NOT_IN_A_LINE = ("Cc", "Zl", "Zp")
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
    """A group chat could not be read or changed: agent-group failed, is missing or gave something malformed, or
    (subchats.state) the sub-group chats' state file cannot be read; the message says why, for the Mac to show."""


class ChatMadeError(ChatError):
    """new(): agent-group made the chat, but its answer does not check out (_shown). `group` is the chat's id when
    the answer gives one, else None, for the caller to keep so that the chat is not made again."""

    def __init__(self, message, group):
        super().__init__(message)
        self.group = group


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


def _shown(chat):
    """agent-group's object for a chat, as its show, new and set --json give it to the owner (its details), checked and
    reshaped, so the Mac reads one shape from every action that answers with a chat: it must list its members. A
    member's per-topic alert settings, {topic id: setting} from agent-group, become [{topic, alerts}] in topic id
    order: the Mac's decoder rewrites snake_case keys, and a topic id can hold "_". Raises ChatError otherwise."""
    if not (isinstance(chat, dict) and isinstance(chat.get("members"), list)):
        raise ChatError("agent-group's chat is not a chat with members: %s" % json.dumps(chat)[:200])
    alerts = chat.get("member_alerts")
    if alerts is not None and not (isinstance(alerts, list) and all(isinstance(m, dict) for m in alerts)):
        raise ChatError("agent-group's member alert settings are not a list: %s" % json.dumps(alerts)[:200])
    for m in alerts or []:
        per_topic = m.get("topic_alerts")
        if per_topic is None:
            continue
        if not isinstance(per_topic, dict):
            raise ChatError("agent-group's member's alert setting for each topic is not a map: %s"
                            % json.dumps(m)[:200])
        m["topic_alerts"] = [{"topic": t, "alerts": a} for t, a in sorted(per_topic.items())]
    return chat


def show(group, run=sources.run):
    """The chat as agent-group's show --json gives it, checked and reshaped (_shown). Raises ChatError."""
    return _shown(_call(["show", "--json", "--as", as_owner(), "--", group], run))


def is_owners(p):
    """Whether post p is the owner's own: agent-group's from_owner when it gives one, else a post with no agent."""
    return p["from_owner"] if isinstance(p.get("from_owner"), bool) else p.get("from_agent") is None


def posts(group, since=None, run=sources.run):
    """The chat's posts in order (after `since`), each as agent-group gives it (its topic, {id, title} or null, and
    sender_label, agent-group's name for the sender, among the rest); the owner's (is_owners) without the board's
    footer and with their `ref`. Anyone else's post is shown as it is, a footer it quotes included, with no ref.
    Raises ChatError when agent-group fails or its answer is not a list of posts."""
    args = ["posts", "--json", "--as", as_owner()] + (["--since=" + since] if since else []) + ["--", group]
    listed = _call(args, run)
    if not (isinstance(listed, list) and all(isinstance(p, dict) for p in listed)):
        raise ChatError("agent-group's posts are not a list of posts: %s" % json.dumps(listed)[:200])
    for p in listed:
        if is_owners(p) and isinstance(p.get("text"), str):
            p["text"], p["ref"] = split_footer(p["text"])
    return listed


def post(group, post_id, text, ref, topic=None, new_topic=None, reply_to=None, run=sources.run):
    """The owner's post, with the board's footer, on agent-group's stdin; post_id is its idempotency key. It goes in
    the topic whose id is `topic`, or opens a topic titled `new_topic`; with neither, a reply (`reply_to`, the id of the
    post it answers) stays in that post's topic, and any other post is in no topic. The ids and the title go in
    --opt=VALUE form, so one starting with "-" is not read as an option."""
    args = ["post", "--as", as_owner(), "--id", post_id]
    for flag, value in (("--topic=", topic), ("--new-topic=", new_topic), ("--reply-to=", reply_to)):
        if value is not None:
            args.append(flag + value)
    return _call(args + ["--", group], run, stdin=text + footer(ref), parse=False)


def new(title, names, run=sources.run):
    """Make the chat; agent-group's object for it, as show() gives it (_shown). Raises ChatError; when agent-group
    made the chat but its answer does not check out, ChatMadeError, which says the chat was made and carries its id
    when the answer has one, so that the caller keeps it and does not make the chat again."""
    made = _call(["new", "--json", "--as", as_owner(), "--with", ",".join(names), "--", title], run)
    try:
        return _shown(made)
    except ChatError as e:
        gid = made.get("id") if isinstance(made, dict) else None
        gid = gid if isinstance(gid, str) and ID_RE.fullmatch(gid) else None
        raise ChatMadeError("agent-group made %s, but its answer is not understood: %s"
                            % ("chat " + gid if gid else "the chat", e), gid) from None


def members(group, op, names, run=sources.run):
    return _call([op, "--as", as_owner(), "--", group, ",".join(names)], run, parse=False)


def set_chat(group, title, names, run=sources.run):
    """Give the chat exactly this title and these members (subchats.py); agent-group changes nothing when they are
    already so. Its object for the chat, as show() gives it (_shown). The values go in --opt=VALUE form, so a title
    starting with "-" is not read as an option. Raises ChatError."""
    args = ["set", "--json", "--as", as_owner(), "--title=" + title, "--with=" + ",".join(names), "--", group]
    return _shown(_call(args, run))


def hide(group, hidden, run=sources.run):
    return _call(["hide" if hidden else "unhide", "--as", as_owner(), "--", group], run, parse=False)


def title_error(title, what="chat"):
    """Why a chat's title (or, by `what`, a topic's) won't do, or None: one line of 1 to MAX_TITLE bytes with no
    control characters or line separators (NOT_IN_A_LINE), as agent-group takes it."""
    if not isinstance(title, str) or not title.strip():
        return "the %s needs a title" % what
    if len(title.encode()) > MAX_TITLE or any(unicodedata.category(c) in NOT_IN_A_LINE for c in title):
        return "a %s title is one line of at most %d bytes, with no tabs or other control characters" % (what,
                                                                                                         MAX_TITLE)
    return None


def _topics(c):
    """Chat c's topics as the board shows them, [{id, title, posts, last_post}]; [] when agent-group lists none.
    Raises ChatError when they are not a list of topics."""
    topics = c.get("topics")
    if topics is None:
        return []
    if not (isinstance(topics, list) and all(isinstance(t, dict) for t in topics)):
        raise ChatError("agent-group's chat %s does not list its topics as topics" % c.get("id"))
    return [{k: t.get(k) for k in ("id", "title", "posts", "last_post")} for t in topics]


def rows(listed):
    """agent-group's list as the board shows it, in the snapshot and the Chats sheet alike (the Mac's ChatSummary):
    [{id, title, hidden, members (a count), posts (a count), last_post (ms epoch or None), topics ([{id, title, posts,
    last_post}], as agent-group lists them)}]. Raises ChatError when it is not a list of chats, or a chat's topics are
    not a list of topics."""
    if not (isinstance(listed, list) and all(isinstance(c, dict) for c in listed)):
        raise ChatError("agent-group's list is not a list of chats: %s" % json.dumps(listed)[:200])
    return [{"id": c.get("id"), "title": c.get("title"), "hidden": bool(c.get("hidden")),
             "members": len(c.get("members") or []), "posts": c.get("posts") or 0, "last_post": c.get("last_post"),
             "topics": _topics(c)} for c in listed]


def summary(run=sources.run):
    """The snapshot's view of the chats, for the Mac's Chats button: None when agent-group isn't installed, else
    {"chats": rows()}. Raises ChatError. subchats.summary adds each chat's sub-group, and the collector reads that
    every REFRESH seconds."""
    if not installed():
        return None
    return {"chats": rows(chat_list(run))}
