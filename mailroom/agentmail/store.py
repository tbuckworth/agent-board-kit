"""SQLite routing map: which session sent what, and what came back.

sessions  one row per (origin_host, cli, session_id) with its reply tag
sent      one row per outbound mail: Gmail id, thread id, real Message-ID, tag, To, Cc, the plain text, the sha256 of
          the message kept in the message store (eml; agentmail/messages.py) and its number of attachments; mail
          sent before the store existed has none of those four until `agent-mail mailbox backfill`; and its widgets
          as JSON (agentmail/widgets.py), null for mail without any
inbound   one row per inbound mail the mailroom looked at, with what it did; a reply written in the Mail tab has
          gmail_id "mailbox:KEY" (routing.is_local), and when the owner attached files to it, the sha256 of the whole
          reply kept in the message store (eml; agentmail/replyfiles.py)
mailbox_threads  the owner's flags on a thread of their mailbox (agentmail/mailbox.py): read, marked unread, archived,
          snoozed
meta      history_id, last_poll (when the last successful poll began), ...
proxy_requests  one row per send or reply another machine's agent-mail asked this one to make, with its status
          and printed result, so a retried request never sends twice
reviews   one row per pre-send review decision (agentmail/review.py); verdict is pass, revise, advised, skipped,
          override, error, withdrawn, expired or dropped; sent_message_id is set once that mail went out
"""
import contextlib
import os
import sqlite3
import time

from . import routing
from .config import DB_PATH, STATE_DIR

SCHEMA = """
create table if not exists meta(key text primary key, value text);
create table if not exists sessions(
  tag text primary key,
  cli text not null,
  session_id text not null,
  session_name text,
  cwd text,
  created_at integer not null,
  unique(cli, session_id));
create table if not exists sent(
  id integer primary key,
  tag text not null,
  gmail_id text,
  thread_id text,
  message_id text,
  to_addr text,
  subject text,
  sent_at integer not null,
  in_reply_to_inbound integer);
create table if not exists inbound(
  id integer primary key,
  gmail_id text unique not null,
  thread_id text,
  message_id text,
  from_addr text,
  from_name text,
  subject text,
  received_at integer not null,
  matched_tag text,
  match_rule text,
  action text,
  detail text,
  body text,
  released_to text,
  released_at integer);
create index if not exists sent_message_id on sent(message_id);
create index if not exists sent_thread_id on sent(thread_id);
create table if not exists proxy_requests(
  request_id text primary key, fingerprint text not null,
  status text not null, result text);
create table if not exists reviews(
  id integer primary key,
  tag text not null,
  created_at integer not null,
  round integer not null,
  verdict text not null,
  subject text,
  kind text,
  sent_message_id text,
  reasons text,
  fixes text,
  words integer,
  model text,
  seconds real,
  cost real,
  draft text,
  audience text);
create index if not exists reviews_created_at on reviews(created_at);
create table if not exists mailbox_threads(
  thread_id text primary key,
  seen_id integer,
  seen_at real,
  unread_at real,
  archived_id integer,
  snooze_id integer,
  snooze_until real);
"""
# Columns added since their table was first made, each added to an older store when it is opened. reviews.audience:
# owner, collaborator or other; null, on rows from before the column existed, means owner.
ADDED_COLUMNS = (("inbound", "recovery_json", "text"), ("inbound", "notified_at", "integer"),
                 ("reviews", "audience", "text"), ("sent", "cc_addr", "text"), ("sent", "body", "text"),
                 ("sent", "eml", "text"), ("sent", "attachments", "integer"), ("inbound", "eml", "text"),
                 ("sent", "widgets", "text"))


class Store:
    def __init__(self, path=DB_PATH):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(STATE_DIR, 0o700)
        except OSError:
            pass
        self.con = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("pragma journal_mode=wal")
        self.con.executescript(SCHEMA)
        # A sessions table without origin_host gets it, as 'desktop' on every row, keeping each tag and the mail
        # that refers to it. DDL is transactional; concurrent senders serialize before checking again.
        with self.transaction():
            cols = {r[1] for r in self.con.execute("pragma table_info(sessions)")}
            if "origin_host" not in cols:
                self.con.execute("""create table sessions_hosted(
                    tag text primary key, cli text not null, session_id text not null,
                    session_name text, cwd text, created_at integer not null,
                    origin_host text not null, unique(origin_host, cli, session_id))""")
                self.con.execute("insert into sessions_hosted select *, 'desktop' from sessions")
                self.con.execute("drop table sessions")
                self.con.execute("alter table sessions_hosted rename to sessions")
        for table, col, typ in ADDED_COLUMNS:
            try:
                self.con.execute("alter table %s add column %s %s" % (table, col, typ))
            except sqlite3.OperationalError as e:
                if not str(e).startswith("duplicate column name"):   # anything but "already there", a lock say
                    raise
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @contextlib.contextmanager
    def transaction(self):
        """The statements inside run as one write transaction (begin immediate): committed when the block ends,
        rolled back when it raises, and the exception raised again."""
        self.con.execute("begin immediate")
        try:
            yield
        except BaseException:
            self.con.execute("rollback")
            raise
        self.con.execute("commit")

    # meta
    def get_meta(self, key, default=None):
        r = self.con.execute("select value from meta where key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key, value):
        self.con.execute("insert or replace into meta(key, value) values(?,?)", (key, str(value)))

    def is_alert_thread(self, thread_id):
        return self.con.execute("select 1 from meta where key like 'alert:%:thread_id' and value=? limit 1",
                                (thread_id,)).fetchone() is not None

    # sessions
    def session_for(self, cli, session_id, name="", cwd="", *, origin_host):
        """The sessions row of session_id on machine origin_host (a config host_id), made with a new reply tag if
        there is none; a new name replaces the stored one. Writes the store."""
        r = self.con.execute("select * from sessions where origin_host=? and cli=? and session_id=?",
                             (origin_host, cli, session_id)).fetchone()
        if r:
            if name and name != r["session_name"]:
                self.con.execute("update sessions set session_name=? where tag=?", (name, r["tag"]))
                r = self.session_by_tag(r["tag"])
            return r
        for _ in range(5):
            tag = routing.new_tag()
            try:
                self.con.execute(
                    "insert into sessions(tag, cli, session_id, session_name, cwd, created_at, origin_host) values(?,?,?,?,?,?,?)",
                    (tag, cli, session_id, name or "", cwd or "", int(time.time()), origin_host))
                return self.session_by_tag(tag)
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("could not allocate a unique tag")

    def rehome(self, old_cli, old_sid, new_cli, new_sid, name="", cwd="", *, origin_host):
        """An agent carried on in a new session (agent-switch between Claude Code and Codex): the old session's
        tags now reach the new one, so replies to mail it sent are delivered there. Both run on origin_host (a config
        host_id). Writes the store; returns rows changed: 0 if the old session never sent mail or the new one already
        has a tag of its own."""
        if self.con.execute("select 1 from sessions where origin_host=? and cli=? and session_id=?",
                            (origin_host, new_cli, new_sid)).fetchone():
            return 0
        return self.con.execute(
            "update sessions set cli=?, session_id=?, session_name=coalesce(nullif(?, ''), session_name), "
            "cwd=coalesce(nullif(?, ''), cwd) where origin_host=? and cli=? and session_id=?",
            (new_cli, new_sid, name, cwd, origin_host, old_cli, old_sid)).rowcount

    def session_by_tag(self, tag):
        return self.con.execute("select * from sessions where tag=?", (tag,)).fetchone()

    def session_by_message_ids(self, message_ids):
        if not message_ids:
            return None
        q = ",".join("?" * len(message_ids))
        return self.con.execute(
            "select s.* from sent t join sessions s on s.tag=t.tag where t.message_id in (%s) "
            "order by t.sent_at desc limit 1" % q, list(message_ids)).fetchone()

    def session_by_thread(self, thread_id):
        return self.con.execute(
            "select s.* from sent t join sessions s on s.tag=t.tag where t.thread_id=? "
            "order by t.sent_at desc limit 1", (thread_id,)).fetchone()

    # sent
    def record_sent(self, tag, gmail_id, thread_id, message_id, to_addr, subject, *, cc_addr, body, eml, attachments,
                    widgets=None, in_reply_to_inbound=None):
        """Record one sent mail: body is its plain text, eml the sha256 of the message kept in the message store,
        attachments how many it carries, widgets its widgets as JSON (agentmail/widgets.py) or None."""
        self.con.execute(
            "insert into sent(tag, gmail_id, thread_id, message_id, to_addr, subject, sent_at, in_reply_to_inbound, "
            "cc_addr, body, eml, attachments, widgets) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tag, gmail_id, thread_id, message_id, to_addr, subject, int(time.time()), in_reply_to_inbound, cc_addr,
             body, eml, attachments, widgets))

    def thread_message_ids(self, thread_id, until_ts):
        """The Message-IDs of the mail of thread thread_id the store holds, sent and inbound, up to until_ts, oldest
        first, each once."""
        rows = self.con.execute(
            "select message_id from (select message_id, sent_at t, 0 k, id from sent where thread_id=? union all "
            "select message_id, received_at t, 1 k, id from inbound where thread_id=?) "
            "where message_id is not null and t<=? order by t, k, id", (thread_id, thread_id, int(until_ts))).fetchall()
        return list(dict.fromkeys(r[0] for r in rows))

    def sent_row(self, sent_id):
        return self.con.execute("select * from sent where id=?", (sent_id,)).fetchone()

    def max_sent_id(self):
        return self.con.execute("select coalesce(max(id), 0) from sent").fetchone()[0]

    def sent_to_backfill(self, limit):
        """The sent rows with a Gmail id and no kept message, newest first: at most limit, or all for None."""
        return self.con.execute("select * from sent where eml is null and gmail_id is not null order by id desc "
                                "limit ?", (-1 if limit is None else limit,)).fetchall()

    def backfill_sent(self, sent_id, eml, body, cc_addr, attachments):
        """Fill in what a sent row from before the message store lacks; a row that has its message already is left
        as it is. Returns whether it was filled."""
        return self.con.execute("update sent set eml=?, body=?, cc_addr=?, attachments=? where id=? and eml is null",
                                (eml, body, cc_addr, attachments, sent_id)).rowcount == 1

    # the mailbox (agentmail/mailbox.py)
    def mailbox_sent(self):
        """Every sent row in a thread, without its text, with its session's cli, session_name, session_id and
        origin_host (null for unrouted mail), oldest first."""
        return self.con.execute(
            "select t.id, t.tag, t.gmail_id, t.thread_id, t.message_id, t.to_addr, t.cc_addr, t.subject, t.sent_at, "
            "t.eml, t.attachments, t.widgets, s.cli, s.session_name, s.session_id, s.origin_host from sent t "
            "left join sessions s on s.tag=t.tag where t.thread_id is not null order by t.id").fetchall()

    def inbound_from(self, addresses):
        """Every inbound row in a thread from one of addresses (lower case), without its text, oldest first."""
        return self.con.execute(
            "select id, gmail_id, thread_id, message_id, from_addr, subject, received_at, matched_tag, action, detail, "
            "eml from inbound where thread_id is not null and lower(from_addr) in (%s) order by id"
            % ",".join("?" * len(addresses)), list(addresses)).fetchall()

    def texts(self, table, ids):
        """{id: plain text} of the rows of table ("sent" or "inbound") with those ids."""
        if table not in ("sent", "inbound"):
            raise ValueError("no texts in table %r" % table)
        ids, out = list(ids), {}
        for i in range(0, len(ids), 500):   # under SQLite's limit on bound values
            chunk = ids[i:i + 500]
            out.update(self.con.execute("select id, body from %s where id in (%s)" % (table, ",".join("?" * len(chunk))),
                                        chunk).fetchall())
        return out

    def mailbox_flags(self):
        """{thread id: its mailbox_threads row}."""
        return {r["thread_id"]: r for r in self.con.execute("select * from mailbox_threads")}

    def set_mailbox_flags(self, changes):
        """Set the flags changes gives, {thread id: {column: value}}, in one transaction, making a thread's row first if
        it has none."""
        with self.transaction():
            for thread_id, cols in changes.items():
                self.con.execute("insert or ignore into mailbox_threads(thread_id) values(?)", (thread_id,))
                self.con.execute("update mailbox_threads set %s where thread_id=?"
                                 % ", ".join("%s=?" % k for k in cols), list(cols.values()) + [thread_id])

    # reviews
    def record_review(self, tag, round_, verdict, subject, kind=None, reasons=None, fixes=None, words=None,
                      model=None, seconds=None, cost=None, draft=None, audience=None):
        cur = self.con.execute(
            "insert into reviews(tag, created_at, round, verdict, subject, kind, reasons, fixes, words, model, "
            "seconds, cost, draft, audience) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tag, int(time.time()), round_, verdict, subject, kind, reasons, fixes, words, model, seconds, cost, draft,
             audience))
        return cur.lastrowid

    def reviews_between(self, since_ts, until_ts):
        return self.con.execute("select * from reviews where created_at>=? and created_at<? order by id",
                                (int(since_ts), int(until_ts))).fetchall()

    # A tag holds at most one draft per audience (owner/collaborator/other; null, on rows from before the column existed, means owner).
    AUD = "coalesce(audience, 'owner')"

    def latest_review(self, tag, audience):
        return self.con.execute("select * from reviews where tag=? and %s=? order by id desc limit 1" % self.AUD,
                                (tag, audience)).fetchone()

    def held_draft(self, tag):
        """The newest draft this tag still has waiting on a revision, whoever it is to."""
        return self.con.execute("select * from reviews where tag=? and verdict='revise' and draft is not null "
                                "order by id desc limit 1", (tag,)).fetchone()

    def review_sent(self, review_id, message_id):
        """The reviewed mail went out: link it, and settle any draft this tag still had held for the same audience."""
        row = self.con.execute("select tag, %s as aud from reviews where id=?" % self.AUD, (review_id,)).fetchone()
        self.con.execute("update reviews set sent_message_id=? where id=?", (message_id, review_id))
        if row:
            self.con.execute("update reviews set draft=null where tag=? and %s=? and draft is not null and id<?"
                             % self.AUD, (row["tag"], row["aud"], review_id))

    def clear_draft(self, review_id):
        """Drop the mail a review kept back, so the watchdog never sends it."""
        self.con.execute("update reviews set draft=null where id=?", (review_id,))

    def stale_drafts(self, before_ts):
        """Held drafts (verdict revise) whose session neither re-sent nor withdrew them before before_ts."""
        return self.con.execute(
            "select r.* from reviews r where r.verdict='revise' and r.draft is not null and r.created_at<? "
            "and r.id=(select max(id) from reviews x where x.tag=r.tag and coalesce(x.audience, 'owner')="
            "coalesce(r.audience, 'owner')) order by r.id", (int(before_ts),)).fetchall()

    def list_sent(self, limit=30):
        return self.con.execute(
            "select t.*, s.cli, s.session_name, s.session_id, s.origin_host from sent t left join sessions s on s.tag=t.tag "
            "order by t.sent_at desc limit ?", (limit,)).fetchall()

    def sent_count_since(self, tag, since_ts):
        return self.con.execute("select count(*) from sent where tag=? and sent_at>?",
                                (tag, int(since_ts))).fetchone()[0]

    def gmail_ids_by_message_id(self):
        """{Message-ID: Gmail id} of every sent mail."""
        return dict(self.con.execute("select message_id, gmail_id from sent where message_id is not null"))

    def owner_threads(self, addresses, since_ts, until_ts):
        """Gmail threads with agent mail to one of addresses (lower case), or mail from one, in [since_ts, until_ts)."""
        to = " or ".join(["lower(to_addr) like ?"] * len(addresses))
        frm = ",".join("?" * len(addresses))
        likes = ["%%%s%%" % a for a in addresses]
        rows = self.con.execute(
            "select thread_id from sent where thread_id is not null and sent_at>=? and sent_at<? and (%s) "
            "union select thread_id from inbound where thread_id is not null and received_at>=? and received_at<? "
            "and lower(from_addr) in (%s)" % (to, frm),
            [int(since_ts), int(until_ts)] + likes + [int(since_ts), int(until_ts)] + list(addresses)).fetchall()
        return sorted(r[0] for r in rows)

    # proxy_requests
    def proxy_request(self, request_id):
        return self.con.execute("select * from proxy_requests where request_id=?", (request_id,)).fetchone()

    def claim_proxy_request(self, request_id, fingerprint):
        """In one transaction: return the recorded row if request_id is already in the ledger (recording nothing),
        else record it as pending and return None."""
        self.con.execute("begin immediate")
        old = self.proxy_request(request_id)
        if old:
            self.con.execute("rollback")
            return old
        self.con.execute("insert into proxy_requests values(?,?,?,?)", (request_id, fingerprint, "pending", None))
        self.con.execute("commit")
        return None

    def complete_proxy_request(self, request_id, result):
        self.con.execute("update proxy_requests set status='complete',result=? where request_id=?", (result, request_id))

    # inbound
    def seen(self, gmail_id):
        return self.con.execute("select 1 from inbound where gmail_id=?", (gmail_id,)).fetchone() is not None

    def record_inbound(self, **f):
        cols = ["gmail_id", "thread_id", "message_id", "from_addr", "from_name", "subject", "received_at",
                "matched_tag", "match_rule", "action", "detail", "body", "eml"]
        vals = [f.get(c) for c in cols]
        if vals[6] is None:
            vals[6] = int(time.time())
        cur = self.con.execute(
            "insert or replace into inbound(%s) values(%s)" % (",".join(cols), ",".join("?" * len(cols))), vals)
        return cur.lastrowid

    def record_inbound_once(self, **f):
        """record_inbound, unless a row with f's gmail_id is there already; in one transaction, so that two callers
        never both record it. Returns (the row, whether this call recorded it)."""
        with self.transaction():
            row = self.inbound_by_gmail_id(f["gmail_id"])
            new_id = None if row else self.record_inbound(**f)
        return (row, False) if row else (self.inbound(new_id), True)

    def local_replies_waiting(self):
        """Replies written in the Mail tab that the daemon has not taken up yet (action "local"), oldest first."""
        return self.con.execute("select * from inbound where action='local' order by id").fetchall()

    def take_local_reply(self, inbound_id):
        """Mark a reply written in the Mail tab pending, if it still waits (action "local"). Returns whether it did,
        so that it is routed once."""
        return self.con.execute("update inbound set action='pending' where id=? and action='local'",
                                (inbound_id,)).rowcount == 1

    def local_replies(self, thread_id):
        """The replies written in the Mail tab in thread thread_id, oldest first."""
        return self.con.execute("select * from inbound where thread_id=? and gmail_id like ? order by id",
                                (thread_id, routing.LOCAL_PREFIX + "%")).fetchall()

    def update_inbound(self, inbound_id, **f):
        sets = ", ".join("%s=?" % k for k in f)
        self.con.execute("update inbound set %s where id=?" % sets, list(f.values()) + [inbound_id])

    def inbound(self, inbound_id):
        return self.con.execute("select * from inbound where id=?", (inbound_id,)).fetchone()

    def inbound_by_gmail_id(self, gmail_id):
        return self.con.execute("select * from inbound where gmail_id=?", (gmail_id,)).fetchone()

    def list_inbound(self, action=None, limit=50):
        if action:
            return self.con.execute("select * from inbound where action=? order by received_at desc limit ?",
                                    (action, limit)).fetchall()
        return self.con.execute("select * from inbound order by received_at desc limit ?", (limit,)).fetchall()

    def count_from_since(self, from_addr, since_ts):
        return self.con.execute("select count(*) from inbound where from_addr=? and received_at>=?",
                                (from_addr, int(since_ts))).fetchone()[0]

    def nth_in_thread(self, inbound_id):
        """Which of its sender's mails in its thread inbound mail #inbound_id is, counting from 1 in the order the
        mailroom saw them."""
        return self.con.execute(
            "select count(*) from inbound a join inbound b on b.from_addr=a.from_addr and b.id<=a.id "
            "and (b.id=a.id or b.thread_id=a.thread_id) where a.id=?", (inbound_id,)).fetchone()[0]

    def thread_seen_before(self, thread_id, gmail_id):
        return self.con.execute("select 1 from inbound where thread_id=? and gmail_id<>? limit 1",
                                (thread_id, gmail_id)).fetchone() is not None

    def unnotified_recoveries(self, limit):
        """Held mail with a saved recovery whose notice to the owner has not gone out, oldest first."""
        return self.con.execute("select * from inbound where action='held' and recovery_json is not null "
                                "and notified_at is null order by id limit ?", (limit,)).fetchall()

    def sent_in_thread(self, thread_id):
        return self.con.execute("select * from sent where thread_id=? order by sent_at desc limit 1",
                                (thread_id,)).fetchone()

    def sent_rows_in_thread(self, thread_id):
        return self.con.execute("select * from sent where thread_id=? order by sent_at", (thread_id,)).fetchall()

    def counts(self):
        rows = self.con.execute("select action, count(*) n from inbound group by action").fetchall()
        out = {r["action"]: r["n"] for r in rows}
        out["sent"] = self.con.execute("select count(*) from sent").fetchone()[0]
        out["sessions"] = self.con.execute("select count(*) from sessions").fetchone()[0]
        return out
