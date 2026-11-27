"""The owner's unsent Mail-tab replies, kept on the desktop so that the Mac app (every window of it) and the phone app
share them and a draft outlives either (README, "Drafts"): one draft per thread, the same on both, with its last few
versions kept for a while, so that a Discard can be undone and an older version brought back. Like the owner's mail,
drafts are kept here, on the desktop, never on the phone.

Under ROOT (0700), each thread holding a draft or versions has threads/<thread>.json:

  {"rev": N, "at": TS, "draft": CONTENT | null, "updated": {"at": TS, "device": D} | null, "since": TS | null,
   "versions": [{"id", "at", "device", "why", "kept", "content"}, ...]}          oldest first

CONTENT is {"text", "picks", "comments", "files", "key"} (check_content says what each holds); a version's has no key.
`rev` is the store's count of changes (ROOT/rev) at the thread's last change, so it goes up with every change and is
not given twice (a count gone missing starts again past every record's rev; one that cannot be read is an error): a
device whose base_rev is not the current one has not seen the draft as it is now, and its change is a conflict, kept
as a version, while the current draft stays (put). A thread with no record is at rev 0. A record holding nothing is
kept `version_days` after its last change (`at`), so a device that saw it emptied (a reply sent, a discard) saves on it
with no conflict; then it goes. `since` is when the content was last kept as a version, or
started. A version is kept (`why`):
  "discarded"  the draft discarded;
  "saved"      a save replacing content that was last kept more than `version_minutes` ago (or started then);
  "cleared"    a save emptying the draft (text and all deleted);
  "conflict"   a save from a device that had not seen the current draft (its content, kept; the current stays);
  "replaced"   the draft replaced by an older version brought back (restore).
`at` and `device` say when and where that content was written, `kept` when it became a version. A thread keeps at
most `versions` of them, none kept more than `version_days` ago (pruned whenever the store is written), and one of any
content: the same content kept again moves to the newest. A draft sent is not kept: the mailroom holds the mail.

Each file of a draft is kept once, at files/<thread>/<id>, its id the SHA-256 of its bytes (FILE_RE); a draft names it
by that id, with the name it is sent under and its size. A device uploads a file (add_file) before its draft names it;
a file goes once neither the draft nor a version names it and it was uploaded more than `upload_hours` ago.

Every change holds the store's lock (ROOT/lock, sources.file_lock) and writes atomically (sources.write_json); reads
take no lock. Each `agent-board act` and the phone's page (agent-board mail-web) run their own Store on the same
folder. CONFIG overrides
DEFAULTS. The Mac's self-test stands in for these rules (MailTest.DeskDrafts in mac/Sources/AgentBoard/Mail.swift):
change both together.
"""
import base64
import contextlib
import hashlib
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from . import mailbox, sources

ROOT = sources.BOARD_STATE / "mail-drafts"
CONFIG = Path(os.environ.get("AGENT_BOARD_DRAFTS_CONFIG") or sources.HOME / ".config/agent-board/drafts.json")
DEFAULTS = {
    "version_minutes": 2,     # a save replacing content last kept as a version longer ago than this keeps it as one
    "versions": 20,           # the most versions a thread keeps
    "version_days": 7,        # none kept longer than this
    "upload_hours": 24,       # a file uploaded and named by no draft or version goes after this
}
SWEEP_SECONDS = 3600          # every thread's versions are pruned at most this often (the thread written, every time)
DEVICES = ("mac", "phone")    # what may save a draft (an action's "device"): the Mac app, the phone's page
FILE_RE = re.compile(r"[0-9a-f]{64}")             # a file's id: the SHA-256 of its bytes, in hex
VERSION_RE = re.compile(r"[0-9a-f]{12}")          # a version's id
WIDGET_RE = re.compile(r"s[0-9]{1,12}-[0-9]{1,4}")   # an answer's widget (Compose.swift's DraftPick)
MESSAGE_RE = re.compile(r"s[0-9]{1,12}")           # the agent's mail a comment quotes (DraftComment)
COMMENT_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")    # a comment's id, made by the Mac (a UUID)
MAX_TEXT = 100_000            # characters of a draft's text: more than a reply's (mailbox.MAX_REPLY), so that a long
                              # paste being cut down to send is saved too
MAX_ENTRIES = 200             # answers in a draft, and comments
MAX_PICKED = 100              # options picked in one answer
MAX_WORDS = 8000              # characters of a question, an option's label or a quote
WORDS_SHOWN = 80              # characters of a version's text its summary gives


class Refused(ValueError):
    """A request the store will not take, and why (the action's error)."""


def settings(path=None):
    """DEFAULTS with CONFIG's overrides (or `path`'s). Raises ValueError for a setting that is not a number over 0
    (`versions` a whole one)."""
    path = path or CONFIG
    cfg = sources.load_config(DEFAULTS, path)
    for k in DEFAULTS:
        v = cfg[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 or (k == "versions" and not isinstance(v, int)):
            raise ValueError("%s: %s must be a %snumber over 0" % (path, k, "whole " if k == "versions" else ""))
    return cfg


def _text(v, limit, what):
    if not isinstance(v, str) or len(v) > limit or "\x00" in v:
        raise Refused("%s must be text of at most %d characters" % (what, limit))
    return v


def _whole(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def check_content(c):
    """A draft's content, checked: {"text": what the owner typed (at most MAX_TEXT characters), "picks": the owner's
    answers to an agent's options ([{"widget", "picked": [index...], "question", "labels": [...], "comment": text or
    null}]), "comments": the owner's comments on its text ([{"id", "message", "at", "quote", "comment"}]), "files":
    [{"id", "name", "size"}] (at most mailbox.MAX_FILES, mailbox.MAX_FILE_BYTES together), "key": the key of a reply
    that may be on its way, or null}; the Mac's Draft (Compose.swift) and the phone's draft hold the same. Raises
    Refused."""
    if not isinstance(c, dict) or not set(c) <= {"text", "picks", "comments", "files", "key"}:
        raise Refused("a draft is {text, picks, comments, files, key}")
    text = _text(c.get("text", ""), MAX_TEXT, "text")
    picks, comments, files = c.get("picks", []), c.get("comments", []), c.get("files", [])
    key = c.get("key")
    if key is not None and not (isinstance(key, str) and mailbox.KEY_RE.fullmatch(key)):
        raise Refused("key must be a reply key or null")
    if not isinstance(picks, list) or len(picks) > MAX_ENTRIES:
        raise Refused("picks must be a list of at most %d answers" % MAX_ENTRIES)
    for p in picks:
        if not (isinstance(p, dict) and set(p) <= {"widget", "picked", "question", "labels", "comment"}
                and isinstance(p.get("widget"), str) and WIDGET_RE.fullmatch(p["widget"])
                and isinstance(p.get("picked"), list) and 0 < len(p["picked"]) <= MAX_PICKED
                and all(_whole(i, 0, 999) for i in p["picked"])
                and isinstance(p.get("labels"), list) and len(p["labels"]) == len(p["picked"])):
            raise Refused("each answer is {widget, picked, question, labels, comment}, one label for each option picked")
        _text(p.get("question"), MAX_WORDS, "a question")
        for label in p["labels"]:
            _text(label, MAX_WORDS, "an option's label")
        if p.get("comment") is not None:
            _text(p["comment"], mailbox.MAX_REPLY, "an answer's comment")
    if len({p["widget"] for p in picks}) != len(picks):
        raise Refused("one answer to each question")
    if not isinstance(comments, list) or len(comments) > MAX_ENTRIES:
        raise Refused("comments must be a list of at most %d" % MAX_ENTRIES)
    for q in comments:
        if not (isinstance(q, dict) and set(q) == {"id", "message", "at", "quote", "comment"}
                and isinstance(q["id"], str) and COMMENT_RE.fullmatch(q["id"])
                and isinstance(q["message"], str) and MESSAGE_RE.fullmatch(q["message"]) and _whole(q["at"], 0, 10 ** 9)):
            raise Refused("each comment is {id, message, at, quote, comment}")
        _text(q["quote"], MAX_WORDS, "a quote")
        _text(q["comment"], mailbox.MAX_REPLY, "a comment")
    if not isinstance(files, list) or len(files) > mailbox.MAX_FILES:
        raise Refused("files must be a list of at most %d" % mailbox.MAX_FILES)
    for f in files:
        if not (isinstance(f, dict) and set(f) == {"id", "name", "size"} and isinstance(f["id"], str)
                and FILE_RE.fullmatch(f["id"]) and _whole(f["size"], 0, mailbox.MAX_FILE_BYTES)):
            raise Refused("each file is {id: its SHA-256, name, size}")
        if not 0 < len(_text(f["name"], mailbox.MAX_FILE_NAME, "a file's name")):
            raise Refused("a file's name must not be empty")
    if sum(f["size"] for f in files) > mailbox.MAX_FILE_BYTES:
        raise Refused("the files come to more than the %d bytes a reply carries" % mailbox.MAX_FILE_BYTES)
    return {"text": text, "picks": picks, "comments": comments, "files": files, "key": key}


def blank(c):
    """Whether content `c` holds nothing to send: no text but white space, no answer, comment or file."""
    return c is None or (not c["text"].strip() and not c["picks"] and not c["comments"] and not c["files"])


def measure(c):
    """What the record (actions.jsonl) says of content `c`: its sizes, never its words."""
    if c is None:
        return {"chars": 0, "picks": 0, "comments": 0, "files": 0}
    return {"chars": len(c["text"]), "picks": len(c["picks"]), "comments": len(c["comments"]), "files": len(c["files"])}


def _unkeyed(c):
    return None if c is None else {k: v for k, v in c.items() if k != "key"}


def summary(v):
    """A version as the Versions menu lists it: its id, when and where it was written, why it was kept and when, the
    start of its text, and how many answers, comments and files it holds."""
    c = v["content"]
    first = " ".join(c["text"].split())
    return {"id": v["id"], "at": v["at"], "device": v["device"], "why": v["why"], "kept": v["kept"],
            "words": first[:WORDS_SHOWN], "picks": len(c["picks"]), "comments": len(c["comments"]),
            "files": len(c["files"])}


class Store:
    """The drafts under `root` (ROOT), its clock `clock` (time.time; a test's own) and settings `cfg` (settings())."""

    def __init__(self, root=None, clock=None, cfg=None):
        self.root = Path(root or ROOT)
        self.clock = clock or time.time
        self.cfg = cfg or settings()

    # --- reading

    def _path(self, thread):
        return self.root / "threads" / (thread + ".json")

    def _files(self, thread):
        return self.root / "files" / thread

    def _read(self, thread):
        """Thread `thread`'s record: a new one (rev 0, nothing) when it has none. A damaged one raises ValueError
        naming it, so it is never written over."""
        rec = sources.read_json(self._path(thread))
        if rec is None:
            return {"rev": 0, "at": None, "draft": None, "updated": None, "since": None, "versions": []}
        if not (isinstance(rec, dict) and _whole(rec.get("rev"), 0, 10 ** 15) and isinstance(rec.get("versions"), list)):
            raise ValueError("%s is not a draft's record" % self._path(thread))
        return rec

    def view(self, thread, rec=None):
        """Thread `thread`'s draft as the devices get it: {"thread", "rev", "draft", "updated", "versions": the
        versions' summaries, newest first}."""
        rec = rec or self._read(thread)
        return {"thread": thread, "rev": rec["rev"], "draft": rec["draft"], "updated": rec["updated"],
                "versions": [summary(v) for v in reversed(rec["versions"])]}

    def listing(self):
        """The threads whose draft holds something to send: {thread: {"rev", "updated"}}, for the lists' Draft marks.
        A record that cannot be read is left out."""
        out = {}
        try:
            names = sorted(os.listdir(self.root / "threads"))
        except FileNotFoundError:
            return out
        for name in names:
            thread = name[:-5]
            if not (name.endswith(".json") and mailbox.THREAD_RE.fullmatch(thread)):
                continue
            try:
                rec = self._read(thread)
            except (OSError, ValueError):
                continue
            if not blank(rec["draft"]):
                out[thread] = {"rev": rec["rev"], "updated": rec["updated"]}
        return out

    def file(self, thread, fid):
        """File `fid`'s bytes, checked against its id. Raises Refused when this thread's drafts have no such file."""
        try:
            data = (self._files(thread) / fid).read_bytes()
        except FileNotFoundError:
            raise Refused("file %s… is not in this thread's drafts on the desktop; attach it again" % fid[:12]) from None
        if hashlib.sha256(data).hexdigest() != fid:
            raise Refused("file %s… on the desktop is not the file its draft names; attach it again" % fid[:12])
        return data

    # --- changing

    @contextlib.contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        with sources.file_lock(self.root / "lock"):
            yield self.clock()

    def _write(self, thread, rec, now):
        """Write thread `thread`'s record `rec` (_save), then prune every other thread too, at most every
        SWEEP_SECONDS."""
        self._save(thread, rec, now)
        self._sweep(now, thread)

    def _save(self, thread, rec, now):
        """Write `rec`, its versions pruned, or remove it when it holds nothing; the thread's files no draft or version
        names go (_collect)."""
        self._prune(rec, now)
        path = self._path(thread)
        if self._lapsed(rec, now):
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            sources.write_json(path, rec)
        self._collect(thread, rec, now)

    def _lapsed(self, rec, now):
        """Whether record `rec` holds nothing and has not changed for version_days: it can go."""
        return rec["draft"] is None and not rec["versions"] and (rec.get("at") or 0) <= now - self.cfg["version_days"] * 86400

    def _prune(self, rec, now):
        days, most = self.cfg["version_days"], self.cfg["versions"]
        rec["versions"] = [v for v in rec["versions"] if v["kept"] > now - days * 86400][-most:]

    def _collect(self, thread, rec, now):
        """Thread `thread`'s files that neither its draft nor a version names and were uploaded more than
        upload_hours ago, removed."""
        folder = self._files(thread)
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            return
        named = {f["id"] for c in [rec["draft"]] + [v["content"] for v in rec["versions"]] if c for f in c["files"]}
        for name in names:
            p = folder / name
            if name not in named and p.stat().st_mtime < now - self.cfg["upload_hours"] * 3600:
                p.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            folder.rmdir()                       # only when empty

    def _sweep(self, now, written):
        mark = self.root / "swept"
        try:
            last = float(mark.read_text())
        except (OSError, ValueError):
            last = 0.0
        if now - last < SWEEP_SECONDS:
            return
        sources.write_text_atomic(mark, repr(now))
        for name in sorted(os.listdir(self.root / "threads")) if (self.root / "threads").is_dir() else []:
            thread = name[:-5]
            if name.endswith(".json") and thread != written and mailbox.THREAD_RE.fullmatch(thread):
                try:
                    rec = self._read(thread)
                except (OSError, ValueError):
                    continue                     # a damaged record is left as it is, never written over
                n = len(rec["versions"])
                self._prune(rec, now)
                if len(rec["versions"]) != n or self._lapsed(rec, now):
                    self._save(thread, rec, now)

    def _highest(self):
        """The highest rev of any thread's record (0 for none); one that cannot be read raises."""
        folder = self.root / "threads"
        names = sorted(os.listdir(folder)) if folder.is_dir() else []
        return max([self._read(n[:-5])["rev"] for n in names if n.endswith(".json")
                    and mailbox.THREAD_RE.fullmatch(n[:-5])] or [0])

    def _keep(self, rec, content, why, at, device, now):
        """Keep `content` (its key left out) as thread record `rec`'s newest version; one already kept of the same
        content goes. Its id."""
        c = _unkeyed(content)
        rec["versions"] = [v for v in rec["versions"] if v["content"] != c]
        vid = secrets.token_hex(6)
        rec["versions"].append({"id": vid, "at": at, "device": device, "why": why, "kept": now, "content": c})
        return vid

    def _keep_current(self, rec, why, now):
        cur = rec["draft"]
        if blank(cur):
            return None
        up = rec["updated"] or {"at": now, "device": None}
        return self._keep(rec, cur, why, up["at"], up["device"], now)

    def _check_files(self, thread, content):
        for f in content["files"]:
            p = self._files(thread) / f["id"]
            try:
                size = p.stat().st_size
            except FileNotFoundError:
                raise Refused("%s is not on the desktop; attach it again" % f["name"]) from None
            if size != f["size"]:
                raise Refused("%s is %d bytes on the desktop, not %d" % (f["name"], size, f["size"]))

    def _set(self, rec, content, device, now):
        """`content` from `device` as the draft (none when it holds nothing and no key), at the store's next rev."""
        counter = self.root / "rev"
        try:
            text = counter.read_text()
        except FileNotFoundError:
            text = str(self._highest())           # a count gone missing goes on past every record's
        if not text.strip().isdigit():
            raise ValueError("%s is not the drafts' count of changes; fix it or remove it" % counter)
        rec["rev"] = max(int(text), rec["rev"]) + 1
        sources.write_text_atomic(counter, str(rec["rev"]))
        rec["at"] = now
        rec["draft"] = None if blank(content) and content.get("key") is None else content
        rec["updated"] = None if rec["draft"] is None else {"at": now, "device": device}
        if rec["draft"] is None:
            rec["since"] = None

    def put(self, thread, base_rev, content, device):
        """Save `content` as thread `thread`'s draft from `device`, which last read it at `base_rev`: the view, with
        "conflict": true when it was not taken. It is not taken when the draft has changed since base_rev (unless it is
        the same content), or when the draft holds a key (a reply that may be on its way, Draft.frozen on the Mac) and
        this changes more than that key, which may only stay, or go (the reply refused) from a device that has seen the
        draft as it is; then its content is kept as a
        version ("kept": its id, null when it holds nothing) and the draft stays. A save replacing content last kept
        more than version_minutes ago keeps that content as a version first, and one emptying it keeps what it held.
        Raises Refused for content that is not a draft's, or files it names that are not here."""
        content = check_content(content)
        with self._locked() as now:
            rec = self._read(thread)
            cur = rec["draft"]
            same = _unkeyed(content) == _unkeyed(cur) or (blank(content) and blank(cur))
            frozen = cur is not None and cur.get("key") is not None and (
                not same or content["key"] not in (None, cur["key"]) or (content["key"] is None and base_rev != rec["rev"]))
            if (base_rev != rec["rev"] and not same) or frozen:
                kept = None
                if not blank(content) and not same:
                    kept = self._keep(rec, content, "conflict", now, device, now)
                    self._write(thread, rec, now)
                return dict(self.view(thread, rec), conflict=True, kept=kept)
            if content == cur or (cur is None and blank(content) and content["key"] is None):
                return self.view(thread, rec)
            self._check_files(thread, content)
            if not same and not blank(cur):
                if blank(content):
                    self._keep_current(rec, "cleared", now)
                elif rec["since"] is None or now - rec["since"] > self.cfg["version_minutes"] * 60:
                    self._keep_current(rec, "saved", now)
                    rec["since"] = now
            elif blank(cur):
                rec["since"] = now                 # started
            self._set(rec, content, device, now)
            self._write(thread, rec, now)
            return self.view(thread, rec)

    def discard(self, thread, base_rev, device):
        """Discard thread `thread`'s draft, as `device` last read it at `base_rev`: kept as a version ("version": its
        id, for Undo; null when it held nothing), and the draft empty, its key gone. When the draft has changed since,
        nothing is discarded and the answer says "conflict"."""
        with self._locked() as now:
            rec = self._read(thread)
            if base_rev != rec["rev"]:
                return dict(self.view(thread, rec), conflict=True)
            if rec["draft"] is None:
                return dict(self.view(thread, rec), version=None)
            vid = self._keep_current(rec, "discarded", now)
            self._set(rec, check_content({}), device, now)
            self._write(thread, rec, now)
            return dict(self.view(thread, rec), version=vid)

    def restore(self, thread, version, base_rev, device):
        """Bring version `version` back as thread `thread`'s draft, from `device`, which last read it at `base_rev`;
        the draft it replaces is kept as a version first (when it held something else). When the draft has changed
        since, nothing changes and the answer says "conflict". Raises Refused for a version the thread does not keep."""
        with self._locked() as now:
            rec = self._read(thread)
            v = next((v for v in rec["versions"] if v["id"] == version), None)
            if v is None:
                raise Refused("that version of this draft is no longer kept")
            if base_rev != rec["rev"]:
                return dict(self.view(thread, rec), conflict=True)
            content = dict(v["content"], key=None)
            self._check_files(thread, content)
            if _unkeyed(rec["draft"]) != v["content"]:
                self._keep_current(rec, "replaced", now)
            self._set(rec, content, device, now)
            rec["since"] = now
            self._write(thread, rec, now)
            return self.view(thread, rec)

    def sent(self, thread, key):
        """A reply with key `key` has reached the mailroom: thread `thread`'s draft goes, not kept as a version, when it
        holds that key, which the device sending it saved first. Its new rev, or None when the draft holds another key
        or none (then it is left as it is)."""
        if not self._path(thread).exists():
            return None                                  # no draft: nothing to take the lock for
        with self._locked() as now:
            rec = self._read(thread)
            if rec["draft"] is None or rec["draft"].get("key") != key:
                return None
            self._set(rec, check_content({}), None, now)
            self._write(thread, rec, now)
            return rec["rev"]

    def add_file(self, thread, data):
        """Keep `data` (bytes) as a file of thread `thread`'s drafts, for a draft to name: {"id", "size"}. A file
        already kept is uploaded again only in that its time starts afresh. Raises Refused past
        mailbox.MAX_FILE_BYTES."""
        if len(data) > mailbox.MAX_FILE_BYTES:
            raise Refused("a reply carries at most %d bytes of files" % mailbox.MAX_FILE_BYTES)
        fid = hashlib.sha256(data).hexdigest()
        with self._locked() as now:
            folder = self._files(thread)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(folder.parent, 0o700)
            p = folder / fid
            if not p.exists():
                fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=".upload.")
                try:
                    with os.fdopen(fd, "wb") as out:
                        out.write(data)
                    os.replace(tmp, p)
                except BaseException:
                    Path(tmp).unlink(missing_ok=True)
                    raise
            os.utime(p, (now, now))
            return {"id": fid, "size": len(data)}

    def reply_files(self, thread, refs):
        """The files a reply names from thread `thread`'s drafts ([{"name", "draft": id}]), as mail_reply sends files:
        [{"name", "data": base64}]. Raises Refused for one that is not here."""
        return [{"name": r["name"], "data": base64.b64encode(self.file(thread, r["draft"])).decode()} for r in refs]

