"""Groups: a thread of up to 16 agents in which every post reaches every member, and the owner may post (README.md,
"Groups").
This module holds the records and the rules; agentmail/groupcmd.py runs the agent-group commands on them.

The records follow a format meant to be shared between agent messaging systems, so that a group could later be
exchanged with another. Everything is an envelope {id, kind, created, from, from_agent, body}: id is 16 random bytes in
base64url without padding, created is milliseconds since the epoch, from is the machine (config host_id) and
from_agent the agent's name, or null for the owner. The kinds:
  group    body: a whole record of the group {id, title, members, task, maker}, plus leaving in a leave
  message  body: a post {from_agent, reply_to, text, attachments, data: {group}}
  alerts   body: a reader's alert setting {conversation, alerts, reader}
  hide     body: whether the owner hides the group {conversation, hidden}
Nothing is changed or removed: each change is a new envelope, and fold works out the current group from the group
records. A member is {device, agent}: an agent's session name on a machine. The owner is in every group and is never
a member; an actor (who does something) is a member, or {device, agent: None} for the owner.

The store keeps each group's envelopes in records/<group id>.jsonl under the mailroom state dir, each reader's unread
posts in reads.json, and one line per post's delivery in deliveries.jsonl, all under one lock. A notice waiting for
an agent is a flag file in group-flags/ on the machine its session runs on, which the agent-group hook reads."""
import base64
import fcntl
import hashlib
import os
import re
import secrets
import string
import time
import urllib.parse
from pathlib import Path

from . import config, jsonfile

ROOT = config.STATE_DIR / "groups"
FLAGS = config.STATE_DIR / "group-flags"
KINDS = ("group", "message", "alerts", "hide")
ALERTS = ("all", "named", "never")
MAX_MEMBERS = 16
MAX_TITLE_BYTES = 200
RECORD_KEYS = ("id", "title", "members", "task", "maker")
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
    """A request agent-group refuses, with its exit code: 2 not allowed, 4 no such group, post or agent, 1 other."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- ids, members, envelopes

ID_RE = re.compile(r"[A-Za-z0-9_-]{22}")


def first_id(candidates):
    """The id of the first of candidates (16-byte strings) whose base64url without padding does not start with "-",
    which a command line would read as an option."""
    for raw in candidates:
        i = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
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
    return isinstance(text, str) and bool(ID_RE.fullmatch(text))


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
    return actor["agent"] is None


def member_label(m):
    """agent@device."""
    return "%s@%s" % (m["agent"], m["device"])


def label(actor, owner):
    """An actor as the texts name it: agent@device, or owner (config owner_name)."""
    return owner if is_owner(actor) else member_label(actor)


def sorted_members(members):
    """Sorted by device, then agent, each compared as bytes, as a record lists them."""
    return sorted(members, key=lambda m: (m["device"].encode(), m["agent"].encode()))


def envelope(kind, actor, body, env_id):
    """A new envelope of kind with body, from actor, made now. env_id: its id (new_id(), or the id a client chose for
    a post so that a retry is not stored twice)."""
    return {"id": env_id, "kind": kind, "created": now_ms(), "from": actor["device"], "from_agent": actor["agent"],
            "body": body}


def conversation(group_id):
    return {"kind": "group", "group": group_id, "topic": None}


def _check_member(m, what):
    if not (isinstance(m, dict) and set(m) == {"device", "agent"} and all(isinstance(m[k], str) and m[k]
                                                                           for k in m)):
        raise GroupError("%s must be {device, agent}, both non-empty text: %r" % (what, m), 1)


def check_record(rec):
    """Raise GroupError (1) unless rec is a well-formed group record: a title of one line, 1 to 200 bytes; 0 to 16
    members, sorted and none twice; a task that is null or text; a maker that is null or a member's form; and in a
    leave, the leaving agent, not among the members."""
    title = rec.get("title")
    if not isinstance(title, str) or "\n" in title or not 1 <= len(title.encode()) <= MAX_TITLE_BYTES:
        raise GroupError("a title is one line of 1 to %d bytes: %r" % (MAX_TITLE_BYTES, title), 1)
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


# ---------------------------------------------------------------- the fold

def applies(group, env):
    """Whether the group record env applies to the group so far (None before any record has applied), by the
    format's rule. The owner's record always applies, except one with "leaving". A leave applies when its sender is the agent
    leaving and the group so far lists it. Any other agent's record applies when its sender is its maker and, in the
    group so far, the maker and still a member; as the first record to apply, when it lists its maker."""
    rec, who = env["body"], sender(env)
    if is_owner(who):
        return "leaving" not in rec
    if "leaving" in rec:
        return group is not None and rec["leaving"] == who and who in group["members"]
    if rec["maker"] != who:
        return False
    if group is None:
        return who in rec["members"]
    return may_change(group, who)


def may_change(group, who):
    """Whether who may write a whole new record of the group so far: the owner, or its maker while a member."""
    return is_owner(who) or (group["maker"] == who and who in group["members"])


def _apply(group, env):
    rec = env["body"]
    if "leaving" in rec:   # the group so far without the agent leaving: whatever else the leave says
        return dict(group, members=[m for m in group["members"] if m != rec["leaving"]])
    return {k: rec[k] for k in RECORD_KEYS}


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
    order (order), starting from no group, each by applies. A record that does not apply changes nothing but is
    kept, so that it takes its place again if an earlier record arrives late. Raises ValueError ("mismatch") when the
    records are of more than one group."""
    records = distinct(records)
    if len({env["body"]["id"] for env in records}) > 1:
        raise ValueError("mismatch: records of more than one group")
    group, applied = None, []
    for env in records:
        if applies(group, env):
            group = _apply(group, env)
            applied.append(env["id"])
    return group, applied


def newest(envelopes):
    """The envelope that sorts last (order), or None."""
    return max(envelopes, key=order) if envelopes else None


class Group:
    """One group as its envelopes make it: the current record (fold), its posts in order, each reader's alert
    setting and whether the owner hides it. Raises ValueError when no record of it applies, which the store never
    writes."""

    def __init__(self, envelopes):
        envelopes = distinct(envelopes)
        kinds = {k: [e for e in envelopes if e["kind"] == k] for k in KINDS}
        self.record, _ = fold(kinds["group"])
        if self.record is None:
            raise ValueError("no record of the group applies")
        self.id = self.record["id"]
        self.posts = kinds["message"]
        self.created = kinds["group"][0]["created"]
        self.updated = max(e["created"] for e in envelopes)
        hide = newest(kinds["hide"])
        self.hidden = bool(hide and hide["body"]["hidden"])
        self._alerts = {}
        for env in kinds["alerts"]:   # in order: the newest setting for a reader stands
            r = env["body"]["reader"]
            self._alerts[(r["device"], r["agent"])] = env["body"]["alerts"]
        self._posts = {p["id"]: p for p in self.posts}

    @property
    def title(self):
        return self.record["title"]

    @property
    def members(self):
        return self.record["members"]

    @property
    def maker(self):
        return self.record["maker"]

    def alerts(self, reader):
        """The reader's alert setting in force: its newest, or all when it has none (the format's rule)."""
        return self._alerts.get((reader["device"], reader["agent"]), "all")

    def has_alerts(self, reader):
        return (reader["device"], reader["agent"]) in self._alerts

    def post(self, post_id):
        return self._posts.get(post_id)

    def record_with(self, members):
        """A new whole record of this group with these members: title, task and maker stay as they are."""
        return {"id": self.id, "title": self.title, "members": sorted_members(members), "task": self.record["task"],
                "maker": self.maker}


# ---------------------------------------------------------------- which posts alert a reader

RUN = re.compile(r"[\w.-]+")   # \w: Unicode letters and digits, and "_"
ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def names(text, name):
    """Whether text names the agent called name, by the format's rule: one of the text's longest runs of letters, digits,
    "_", "-" and "." is the name, alone or followed only by dots, ignoring ASCII case. So "@Ana", "Ana's" and
    "Thanks, Ana." name Ana; "Anabel", "Ana-2" and "Ana.md" do not."""
    want = name.translate(ASCII_LOWER)
    for run in RUN.findall(text):
        run = run.translate(ASCII_LOWER)
        if run.startswith(want) and not run[len(want):].strip("."):
            return True
    return False


def alerted(setting, from_owner, text, name, answers_own):
    """Whether a post alerts (wakes) a reader called name whose setting is setting: the owner's posts always do; else all
    does, never does not, and named does when the post names the reader or answers one of its posts."""
    if from_owner or setting == "all":
        return True
    if setting == "never":
        return False
    return answers_own or names(text, name)


def wakes(group, post, reader):
    """Whether post, in group, alerts reader (alerted)."""
    body = post["body"]
    answered = group.post(body["reply_to"]) if body["reply_to"] else None
    return alerted(group.alerts(reader), post["from_agent"] is None, body["text"], reader["agent"],
                   answered is not None and sender(answered) == reader)


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
        """The Group with this id. Raises GroupError (4) when there is none."""
        path = self._path(group_id)
        if not path.exists():
            raise GroupError("no group %s" % group_id, 4)
        return Group(jsonfile.read_lines(path))

    def groups(self):
        return [self.group(g) for g in self.group_ids()]

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
