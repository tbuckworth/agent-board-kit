"""Groups: a thread of up to 16 agents in which every post reaches every member, and the owner may post (README.md,
"Groups").
This module holds the records and the rules; agentmail/groupcmd.py runs the agent-group commands on them.

The records follow a format meant to be shared between agent messaging systems, so that a group could later be
exchanged with another. Everything is an envelope {id, kind, created, from, from_agent, body}: id is 16 random bytes in
base64url without padding, created is milliseconds since the epoch, from is the machine (config host_id) and
from_agent the agent's name, or null for the owner. The kinds:
  group    body: a whole record of the group {id, title, members, task, maker}, plus leaving in a leave, or added_by
           in an addition, an agent's adding agents to a group it did not make (check_record, step)
  message  body: a post {from_agent, reply_to, text, attachments, data: {group, and topic: {id, title} in a topic}}
  alerts   body: a reader's alert setting {conversation: {kind, group, topic id or null}, alerts, reader}
  hide     body: whether the owner hides the group {conversation, hidden}
hide is the owner's own: only the owner's counts.

Who is who. A member is {device, agent}: an agent's session name on a machine. The owner is an actor with agent None
(is_owner), on any of the owner's machines; the owner is in every group and is never a member.

Nothing is changed or removed: each change is a new envelope, and fold works out the current group from the group
records (step).

The store keeps each group's envelopes in records/<group id>.jsonl under the mailroom state dir, each reader's unread
posts in reads.json and one line per post's delivery in deliveries.jsonl, all under one lock. A notice waiting for an
agent is a flag file in group-flags/ on the machine its session runs on, which the agent-group hook reads."""
import base64
import collections
import fcntl
import hashlib
import os
import re
import secrets
import string
import time
import unicodedata
import urllib.parse
from pathlib import Path

from . import config, jsonfile

ROOT = config.STATE_DIR / "groups"
FLAGS = config.STATE_DIR / "group-flags"
KINDS = ("group", "message", "alerts", "hide")
OWNER_KINDS = ("hide",)   # the owner's own: an envelope of these counts only from the owner
ALERTS = ("all", "addressed", "named", "joined", "never")
MAX_MEMBERS = 16
MAX_TITLE_BYTES = 200
RECORD_KEYS = ("id", "title", "members", "task", "maker")
POST_KEYS = {"from_agent", "reply_to", "text", "attachments", "data"}
POST_DATA_KEYS = {"group", "topic"}   # what a post's data may hold: its group, and its topic in a topic
NOT_IN_A_LINE = ("Cc", "Zl", "Zp")   # control characters (tab, NEL, ...) and line and paragraph separators
# Config key "groups": default_alerts is the alert setting an agent gets when it joins a group (named; the record
# format's own default, when a reader has no setting, is all).
DEFAULTS = {"default_alerts": "named"}


def settings(cfg):
    """DEFAULTS with config "groups" over them. Raises ValueError on an alert setting that does not exist."""
    s = dict(DEFAULTS, **(cfg.get("groups") or {}))
    if s["default_alerts"] not in ALERTS:
        raise ValueError("config groups.default_alerts must be one of %s, not %r" % (", ".join(ALERTS),
                                                                                    s["default_alerts"]))
    return s


class GroupError(Exception):
    """A request agent-group refuses, with its exit code: 2 not allowed, 4 no such group, post, topic or agent,
    1 other."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class Unmade(GroupError):
    """A group whose records are kept but none of which applies (step): there is no such group yet."""

    def __init__(self, group_id):
        super().__init__("no group %s: none of its records applies (they are kept)" % group_id, 4)


# ---------------------------------------------------------------- ids, members, envelopes

ID_RE = re.compile(r"[A-Za-z0-9_-]{22}")


def encode_id(raw):
    """16 bytes as an id: base64url without padding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def first_id(candidates):
    """The id of the first of candidates (16-byte strings) whose encoding (encode_id) does not start with "-", which a
    command line would read as an option."""
    for raw in candidates:
        i = encode_id(raw)
        if not i.startswith("-"):
            return i
    raise ValueError("no candidate makes an id")


def new_id():
    """An id of 16 random bytes (first_id)."""
    return first_id(iter(lambda: secrets.token_bytes(16), None))


def derived_id(seed):
    """The id made from seed (text), the same every time: the first 16 bytes of its sha256, or of the sha256 of that,
    and so on (first_id). What one mail makes (groupmail) is made once, however often the mail is handled."""
    def hashes(digest):
        while True:
            digest = hashlib.sha256(digest).digest()
            yield digest[:16]
    return first_id(hashes(seed.encode()))


def is_id(text):
    """Whether text is an id: 16 bytes in base64url without padding, written as that encoding writes them (a last
    character with spare bits set is not)."""
    return isinstance(text, str) and bool(ID_RE.fullmatch(text)) and encode_id(id_bytes(text)) == text


def id_bytes(i):
    return base64.urlsafe_b64decode(i + "==")


def order(env):
    """Where an envelope sorts: by created, then by its id's 16 bytes compared as unsigned numbers."""
    return (env["created"], id_bytes(env["id"]))


def now_ms():
    return int(time.time() * 1000)


def member(device, agent):
    """An agent on a machine as the record format writes a member, or with agent None, the owner as an actor."""
    return {"device": device, "agent": agent}


def sender(env):
    return member(env["from"], env["from_agent"])


def is_owner(actor):
    """Whether actor is the owner: agent None, on whichever of the owner's machines."""
    return actor["agent"] is None


def member_label(m):
    """agent@device."""
    return "%s@%s" % (m["agent"], m["device"])


def label(actor, owner):
    """An actor as the texts and the board name it: agent@device, or owner (config owner_name)."""
    return owner if is_owner(actor) else member_label(actor)


def sorted_members(members):
    """Sorted by device, then agent, each compared as bytes, as a record lists them."""
    return sorted(members, key=lambda m: (m["device"].encode(), m["agent"].encode()))


def envelope(kind, actor, body, env_id):
    """A new envelope of kind with body, from actor, made now. env_id: its id (new_id(), or the id a client chose for
    a post so that a retry is not stored twice)."""
    return {"id": env_id, "kind": kind, "created": now_ms(), "from": actor["device"], "from_agent": actor["agent"],
            "body": body}


def post_body(from_agent, reply_to, text, group_id, topic):
    """A post's body (check_post): with no files, its data naming the group and, in a topic, the topic {id, title};
    a post outside any topic leaves the key out."""
    return {"from_agent": from_agent, "reply_to": reply_to, "text": text, "attachments": [],
            "data": dict({"group": group_id}, **({"topic": topic} if topic else {}))}


def conversation(group_id, topic_id):
    """What an alert setting or one of the owner's own envelopes is about: a group, or one topic in it (topic_id)."""
    return {"kind": "group", "group": group_id, "topic": topic_id}


def topic_of(post):
    """The topic {id, title} a post is in, or None."""
    return post["body"]["data"].get("topic")


def group_of(env):
    """The id of the group an envelope belongs to."""
    body = env["body"]
    if env["kind"] == "group":
        return body["id"]
    if env["kind"] == "message":
        return body["data"]["group"]
    return body["conversation"]["group"]


# ---------------------------------------------------------------- what is well-formed

def _check_member(m, what):
    if not (isinstance(m, dict) and set(m) == {"device", "agent"} and all(isinstance(m[k], str) and m[k]
                                                                           for k in m)):
        raise GroupError("%s must be {device, agent}, both non-empty text: %r" % (what, m), 1)


def check_title(title, what):
    """Raise GroupError (1) unless title is one line of 1 to MAX_TITLE_BYTES bytes, with no control characters (tab
    and NEL among them) or line or paragraph separators: the rule for a group's and a topic's title."""
    if not (isinstance(title, str) and 1 <= len(title.encode()) <= MAX_TITLE_BYTES
            and not any(unicodedata.category(c) in NOT_IN_A_LINE for c in title)):
        raise GroupError("%s is one line of 1 to %d bytes, with no control characters or line separators: %r"
                         % (what, MAX_TITLE_BYTES, title), 1)


def check_record(rec):
    """Raise GroupError (1) unless rec is a well-formed group record: the keys RECORD_KEYS, and at most one of
    leaving and added_by beside them; a title (check_title); 0 to 16 members, sorted and none twice; a task that is
    null or text; a maker that is null or a member's form; in a leave, the leaving agent, not among the members; and
    in an addition, the adding agent, among them."""
    if not isinstance(rec, dict):
        raise GroupError("a group record is an object: %r" % (rec,), 1)
    if not (set(RECORD_KEYS) <= set(rec) <= set(RECORD_KEYS) | {"leaving", "added_by"}):
        raise GroupError("a group record has the keys %s, and leaving in a leave or added_by in an addition: %s"
                         % (", ".join(RECORD_KEYS), ", ".join(sorted(rec))), 1)
    if "leaving" in rec and "added_by" in rec:
        raise GroupError("a group record is not both a leave and an addition", 1)
    check_title(rec.get("title"), "a title")
    members = rec.get("members")
    if not isinstance(members, list) or len(members) > MAX_MEMBERS:
        raise GroupError("a group has 0 to %d members" % MAX_MEMBERS, 1)
    for m in members:
        _check_member(m, "a member")
    if members != sorted_members(members) or len({(m["device"], m["agent"]) for m in members}) < len(members):
        raise GroupError("members must be sorted, none twice", 1)
    if rec.get("task") is not None and not isinstance(rec["task"], str):
        raise GroupError("a task is null or text", 1)
    if rec.get("maker") is not None:
        _check_member(rec["maker"], "the maker")
    if "leaving" in rec:
        _check_member(rec["leaving"], "the leaving agent")
        if rec["leaving"] in members:
            raise GroupError("a leave does not list the agent leaving", 1)
    if "added_by" in rec:
        _check_member(rec["added_by"], "the adding agent")
        if rec["added_by"] not in members:
            raise GroupError("an addition lists the agent adding", 1)


def check_topic(topic):
    """Raise GroupError (1) unless topic is {id, title} and nothing else: an id of 16 bytes and a title (check_title).
    A post outside any topic leaves the key out: a null topic is malformed."""
    if not (isinstance(topic, dict) and set(topic) == {"id", "title"} and is_id(topic["id"])):
        raise GroupError("a topic is {id, title} and nothing else, its id 16 bytes in base64url: %r" % (topic,), 1)
    check_title(topic["title"], "a topic's title")


def check_post(body, from_agent):
    """Raise GroupError (1) unless body is a well-formed post by from_agent: with no files (attachments []), and data
    naming the group and, in a topic, the topic (check_topic)."""
    if not (isinstance(body, dict) and set(body) == POST_KEYS and body["from_agent"] == from_agent
            and isinstance(body["text"], str) and (body["reply_to"] is None or is_id(body["reply_to"]))):
        raise GroupError("a post is {from_agent (its sender), reply_to (null or an id), text, attachments, data}", 1)
    if body["attachments"] != []:
        raise GroupError("a post has no files", 1)
    data = body["data"]
    if not (isinstance(data, dict) and "group" in data and set(data) <= POST_DATA_KEYS and is_id(data["group"])):
        raise GroupError("a post's data is {group, and topic in a topic}: %r" % (data,), 1)
    if "topic" in data:
        check_topic(data["topic"])


# ---------------------------------------------------------------- the fold

def _record(rec):
    """The group a record leaves: its RECORD_KEYS alone, so that no group as it stands names a leaving or an adding
    agent."""
    return {k: rec[k] for k in RECORD_KEYS}


Rights = collections.namedtuple("Rights", "whole by_agent add leave")
Rights.__doc__ = """What an actor may change of a group as it stands (rights): whole, replace it whole; by_agent, the
actor is an agent; add, add agents to it, changing nothing else, by an addition (added_by); leave, leave it."""


def rights(group, who):
    """What who (an actor) may change of group, the group as it stands. The fold decides every record of a group
    that stands by it (step), and agent-group what it may write (groupcmd.as_changer), so that the two cannot drift.

    - The owner may replace any group whole.
    - An agent the group lists may add agents to it, by an addition, and leave it; and, while it is the group's
      maker, replace it whole. An agent it does not list may do nothing."""
    if is_owner(who):
        return Rights(True, False, False, False)
    listed = who in group["members"]
    return Rights(listed and group["maker"] == who, True, listed, listed)


def allows(r, group, rec):
    """Whether rights r (rights) allow rec, a record of group that is not a leave: with whole, any record but an
    addition, an agent's keeping the group's maker. Otherwise, and for every addition (added_by), only a record that
    keeps the group's title, task and maker and adds members, taking none out, under add: a record that changes
    nothing is allowed only under whole."""
    if r.whole and "added_by" not in rec:
        return not r.by_agent or rec["maker"] == group["maker"]
    if any(rec[k] != group[k] for k in ("title", "task", "maker")):
        return False
    added = [m for m in rec["members"] if m not in group["members"]]
    out = [m for m in group["members"] if m not in rec["members"]]
    return bool(added) and not out and r.add


def written_by(group, who, rec):
    """rec, a whole new record of the group that is not a leave, as who (an actor) writes it: an addition, naming who
    in added_by, when who is an agent that may not replace the group whole (rights); otherwise as it is."""
    if is_owner(who) or rights(group, who).whole:
        return rec
    return dict(rec, added_by=who)


def step(group, env):
    """What the group record env does to the group so far: the group after it, or None when it does not apply.
    group: the group so far, None before any record applied.

    - The record that makes the group is the owner's, naming as maker no one or any agent, or an agent's, naming
      itself its maker and listing itself. A leave or an addition makes none.
    - Every later record is decided by the rights of its sender (rights, allows), who must be the agent it names as
      leaving, as adding or, naming neither, as its maker, or the owner's, naming no leaving or adding agent:
      - a leave applies when the group lists the agent leaving: the group so far without it, whatever else it says;
      - an addition, the record less added_by, when it adds members and changes nothing else, whoever made the
        group; one made from the group as it stood before a record sorting earlier is judged against the group as
        that record left it: passed over when it would take out a member added since;
      - an agent's other record replaces the group while the agent is its maker and listed, keeping the maker;
      - the owner's record replaces the group."""
    rec, who = env["body"], sender(env)
    if group is None:
        if "leaving" in rec or "added_by" in rec:
            return None
        if is_owner(who) or (rec["maker"] == who and who in rec["members"]):
            return _record(rec)
        return None
    if is_owner(who):
        return None if "leaving" in rec or "added_by" in rec else _record(rec)
    if (rec.get("leaving") or rec.get("added_by") or rec["maker"]) != who:   # the agent that must have sent it
        return None
    r = rights(group, who)
    if "leaving" in rec:
        if not r.leave:
            return None
        return dict(group, members=[m for m in group["members"] if m != rec["leaving"]])
    # An agent's record that is no addition needs whole (allows refuses it without too).
    if not allows(r, group, rec) or ("added_by" not in rec and not r.whole):
        return None
    return _record(rec)


def distinct(envelopes):
    """envelopes in order, each id once."""
    seen, out = set(), []
    for env in sorted(envelopes, key=order):
        if env["id"] not in seen:
            seen.add(env["id"])
            out.append(env)
    return out


def fold(records):
    """(the current group or None, the ids of the records applied, in order), from one group's records: applied in
    order (order), starting from no group, each by step. A record that does not apply changes nothing but is kept, so
    that it takes its place again if an earlier record arrives late. Raises ValueError ("mismatch") when the records
    are of more than one group."""
    records = distinct(records)
    if len({env["body"]["id"] for env in records}) > 1:
        raise ValueError("mismatch: records of more than one group")
    group, applied = None, []
    for env in records:
        after = step(group, env)
        if after is not None:
            group = after
            applied.append(env["id"])
    return group, applied


def newest(envelopes):
    """The envelope that sorts last (order), or None."""
    return max(envelopes, key=order) if envelopes else None


class Group:
    """One group as its envelopes make it: the current record (fold), its posts in order and their topics, each
    reader's alert settings and whether the owner hides it. Raises Unmade when no record of it applies."""

    def __init__(self, envelopes):
        envelopes = distinct(envelopes)
        kinds = {k: [e for e in envelopes if e["kind"] == k] for k in KINDS}
        self.record, applied = fold(kinds["group"])
        if self.record is None:
            raise Unmade(group_of(envelopes[0]))
        self.id = self.record["id"]
        self.posts = kinds["message"]
        self.making = next(e for e in kinds["group"] if e["id"] == applied[0])   # the record that made it
        self.created = self.making["created"]
        self.updated = max(e["created"] for e in envelopes)
        owners = {k: [e for e in kinds[k] if is_owner(sender(e))] for k in OWNER_KINDS}
        hide = newest(owners["hide"])
        self.hidden = bool(hide and hide["body"]["hidden"])
        self._alerts = {}
        for env in kinds["alerts"]:   # in order: the newest setting stands
            body = env["body"]
            r = body["reader"]
            if sender(env) == r or is_owner(sender(env)):   # the reader, or the owner
                self._alerts[(r["device"], r["agent"], body["conversation"]["topic"])] = env
        self._posts = {p["id"]: p for p in self.posts}

    @property
    def title(self):
        return self.record["title"]

    @property
    def members(self):
        return self.record["members"]

    @property
    def maker(self):
        """The agent that made the group, or None when the owner did."""
        return self.record["maker"]

    def has(self, m):
        """Whether m (an agent) is a member."""
        return m in self.members

    def _setting(self, reader, topic_id):
        return self._alerts.get((reader["device"], reader["agent"], topic_id))

    def alerts(self, reader, topic_id):
        """The reader's alert setting in force for a post in topic topic_id (None: outside any topic): the newer of
        its newest setting for the whole group and its newest for that topic, or all when it has neither (the record
        format's rule)."""
        found = [s for s in (self._setting(reader, None), topic_id and self._setting(reader, topic_id)) if s]
        return newest(found)["body"]["alerts"] if found else "all"

    def topic_alerts(self, reader):
        """{topic id: setting} for each topic where the reader's own setting for it is in force, being newer than its
        newest for the whole group."""
        whole = self._setting(reader, None)
        return {key[2]: env["body"]["alerts"] for key, env in self._alerts.items()
                if key[:2] == (reader["device"], reader["agent"]) and key[2] is not None
                and (whole is None or order(env) > order(whole))}

    def setting_for(self, post, reader):
        """The reader's setting in force for post (alerts, for the post's topic)."""
        topic = topic_of(post)
        return self.alerts(reader, topic["id"] if topic else None)

    def wrote_in(self, reader, post):
        """Whether the store holds a post of reader's in post's part of the group: its topic, or the general
        conversation when it is in none."""
        part = (topic_of(post) or {}).get("id")
        return any(sender(p) == reader and (topic_of(p) or {}).get("id") == part for p in self.posts)

    def has_alerts(self, reader):
        """Whether the reader has a setting for the whole group."""
        return self._setting(reader, None) is not None

    def post(self, post_id):
        return self._posts.get(post_id)

    def topics(self):
        """[{id, title, posts, last_post}] for each topic the posts are in, in order of their last post. A topic's
        title is the one on its newest post."""
        found = {}
        for p in self.posts:   # in order
            t = topic_of(p)
            if t:
                n = found.pop(t["id"], {"posts": 0})["posts"]
                found[t["id"]] = {"id": t["id"], "title": t["title"], "posts": n + 1, "last_post": p["created"]}
        return list(found.values())

    def topic(self, topic_id):
        """The topic {id, title} with this id, its title from its newest post, or None."""
        return next(({"id": t["id"], "title": t["title"]} for t in self.topics() if t["id"] == topic_id), None)

    def record_with(self, members):
        """A new whole record of this group with these members: title, task and maker stay as they are."""
        return {"id": self.id, "title": self.title, "members": sorted_members(members), "task": self.record["task"],
                "maker": self.maker}


# ---------------------------------------------------------------- which posts alert a reader

RUN = re.compile(r"[\w.-]+")   # \w: Unicode letters and digits, and "_"
ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def names(text, name):
    """Whether text names the agent called name, by the format's rule: one of the text's longest runs of letters,
    digits, "_", "-" and "." is the name, alone or followed only by dots, ignoring ASCII case. So "@Ana", "Ana's" and
    "Thanks, Ana." name Ana; "Anabel", "Ana-2" and "Ana.md" do not."""
    want = name.translate(ASCII_LOWER)
    for run in RUN.findall(text):
        run = run.translate(ASCII_LOWER)
        if run.startswith(want) and not run[len(want):].strip("."):
            return True
    return False


def alerted(setting, from_owner, text, name, answers_own, answers_nothing, posted):
    """Whether a post alerts (wakes) the reader called name, whose setting is setting. from_owner: the owner wrote
    it; answers_own: it answers one of the reader's posts; answers_nothing: it is not a reply; posted: the reader has
    written in the post's part of the group (its topic, or the general conversation), which counts under joined alone.

    The owner's posts always wake. Otherwise never wakes on none; all on every post; named on a post that names the
    reader or answers one of its posts; addressed on those and on any post that answers nothing, that is, a new
    thread; joined on every post in a part the reader has written in, and elsewhere as named."""
    if from_owner:
        return True
    if setting == "never":
        return False
    if setting == "all" or (setting == "joined" and posted):
        return True
    return answers_own or names(text, name) or (setting == "addressed" and answers_nothing)


def wakes(group, post, reader):
    """Whether post, in group, alerts reader (alerted, with the reader's setting in force for it, and whether the
    reader has written in the post's part: Group.wrote_in)."""
    body = post["body"]
    answered = group.post(body["reply_to"]) if body["reply_to"] else None
    return alerted(group.setting_for(post, reader), is_owner(sender(post)), body["text"], reader["agent"],
                   answered is not None and sender(answered) == reader, body["reply_to"] is None,
                   posted=group.wrote_in(reader, post))


# ---------------------------------------------------------------- unread posts

class Reads:
    """Each reader's unread posts per group, and which of them a notice has mentioned. reads.json holds
    {device: {agent: {group id: {"unread": [post ids], "mentioned": [post ids]}}}}, each list in the posts' order;
    a post leaves both lists once read."""

    def __init__(self, data):
        self.data = data

    def _entry(self, reader, group_id):
        return (self.data.setdefault(reader["device"], {}).setdefault(reader["agent"], {})
                .setdefault(group_id, {"unread": [], "mentioned": []}))

    def add(self, group_id, readers, post_id):
        for r in readers:
            self._entry(r, group_id)["unread"].append(post_id)

    def unread(self, reader, group_id):
        return list(self._entry(reader, group_id)["unread"])

    def read(self, reader, group_id, post_ids):
        e, done = self._entry(reader, group_id), set(post_ids)
        e["unread"] = [p for p in e["unread"] if p not in done]
        e["mentioned"] = [p for p in e["mentioned"] if p not in done]

    def unmentioned(self, reader):
        """{group id: [unread post ids no notice has mentioned]} for reader, leaving out groups with none."""
        out = {}
        for gid, e in self.data.get(reader["device"], {}).get(reader["agent"], {}).items():
            seen = set(e["mentioned"])
            fresh = [p for p in e["unread"] if p not in seen]
            if fresh:
                out[gid] = fresh
        return out

    def mention(self, reader, group_id, post_ids):
        e = self._entry(reader, group_id)
        unread = set(e["unread"])
        e["mentioned"] += [p for p in post_ids if p in unread and p not in e["mentioned"]]

    def unmention(self, reader, group_id, post_ids):
        e, undo = self._entry(reader, group_id), set(post_ids)
        e["mentioned"] = [p for p in e["mentioned"] if p not in undo]


# ---------------------------------------------------------------- the store

def store():
    """The store on this central mailroom (ROOT)."""
    return Store(ROOT)


class Store:
    """The groups on the central mailroom, under root (ROOT): records/<group id>.jsonl, reads.json and
    deliveries.jsonl. Use it as `with Store(root) as st:`, which holds root/.lock exclusively for the block, so that
    what a command reads cannot change before it writes."""

    def __init__(self, root):
        self.root = Path(root)
        self._lock = None

    def __enter__(self):
        (self.root / "records").mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._lock = open(self.root / ".lock", "a")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()
        self._lock = None

    def _path(self, group_id):
        if not is_id(group_id):
            raise GroupError("not a group id: %r" % group_id, 4)
        return self.root / "records" / (group_id + ".jsonl")

    def group_ids(self):
        return sorted(p.stem for p in (self.root / "records").glob("*.jsonl"))

    def group(self, group_id):
        """The Group with this id. Raises GroupError (4) when there is none: Unmade when its records are kept but
        none applies."""
        path = self._path(group_id)
        if not path.exists():
            raise GroupError("no group %s" % group_id, 4)
        return Group(jsonfile.read_lines(path))

    def groups(self):
        """Every group, leaving out those whose records are kept but none applies (Unmade): they are no group yet."""
        out = []
        for gid in self.group_ids():
            try:
                out.append(self.group(gid))
            except Unmade:
                continue
        return out

    def append(self, group_id, env):
        """Add one envelope to the group's records. Its created is moved, if need be, to 1 ms after the group's newest
        envelope, so that what is written later sorts later: writes here are one at a time, under the lock."""
        path = self._path(group_id)
        newest_ms = max((e["created"] for e in jsonfile.read_lines(path)), default=0)
        env["created"] = max(env["created"], newest_ms + 1)
        jsonfile.append_line(path, env, jsonfile.PRIVATE)

    def reads(self):
        return Reads(jsonfile.read(self.root / "reads.json"))

    def save_reads(self, reads):
        jsonfile.write(self.root / "reads.json", reads.data, jsonfile.PRIVATE, None)

    def log_delivery(self, entry):
        """One line in deliveries.jsonl: what a post's delivery did, member by member."""
        jsonfile.append_line(self.root / "deliveries.jsonl", dict(entry, ts=time.time()), jsonfile.PRIVATE)


# ---------------------------------------------------------------- notice flags

def _flag(agent):
    return FLAGS / urllib.parse.quote(agent, safe="")


def raise_flags(agents):
    """Mark that a notice may be waiting for each agent named (a session on this machine): create its file in
    FLAGS."""
    FLAGS.mkdir(parents=True, exist_ok=True)
    for agent in agents:
        os.close(os.open(str(_flag(agent)), os.O_WRONLY | os.O_CREAT, jsonfile.PRIVATE))


def any_flag():
    """Whether any notice flag is raised on this machine: the hook's first, cheap check."""
    try:
        return any(FLAGS.iterdir())
    except FileNotFoundError:
        return False


def take_flag(agent):
    """Lower the agent's flag, deleting its file in FLAGS; whether it was raised."""
    try:
        _flag(agent).unlink()
        return True
    except FileNotFoundError:
        return False
