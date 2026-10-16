"""SQLite routing map: which session sent what, and what came back.

sessions  one row per (cli, session_id) with its reply tag
sent      one row per outbound mail: Gmail id, thread id, real Message-ID, tag
inbound   one row per inbound mail the mailroom looked at, with what it did
meta      history_id, last_poll, ...
"""
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
"""


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
        # Preserve old tags and mail references, but key identity by machine too.
        # DDL is transactional; concurrent senders serialize before checking again.
        self.con.execute("begin immediate")
        try:
            cols = {r[1] for r in self.con.execute("pragma table_info(sessions)")}
            if "origin_host" not in cols:
                self.con.execute("""create table sessions_hosted(
                    tag text primary key, cli text not null, session_id text not null,
                    session_name text, cwd text, created_at integer not null,
                    origin_host text not null, unique(origin_host, cli, session_id))""")
                self.con.execute("insert into sessions_hosted select *, 'desktop' from sessions")
                self.con.execute("drop table sessions")
                self.con.execute("alter table sessions_hosted rename to sessions")
            self.con.execute("commit")
        except Exception:
            self.con.execute("rollback")
            raise
        for col, typ in (("ack_deadline", "integer"), ("acked", "integer default 0"),
                         ("delivered_session", "text"), ("delivered_at", "integer"),
                         ("recovery_json", "text"), ("notified_at", "integer")):
            try:
                self.con.execute("alter table inbound add column %s %s" % (col, typ))
            except sqlite3.OperationalError:
                pass  # already there
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

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
    def session_for(self, cli, session_id, name="", cwd="", origin_host="desktop"):
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

    def rehome(self, old_cli, old_sid, new_cli, new_sid, name="", cwd="", origin_host="desktop"):
        """An agent carried on in a new session (agent-switch between Claude Code and Codex): the old session's
        tags now reach the new one, so replies to mail it sent are delivered there. Returns rows changed; 0 if
        the old session never sent mail or the new one already has a tag of its own."""
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
    def record_sent(self, tag, gmail_id, thread_id, message_id, to_addr, subject, in_reply_to_inbound=None):
        self.con.execute(
            "insert into sent(tag, gmail_id, thread_id, message_id, to_addr, subject, sent_at, in_reply_to_inbound) "
            "values(?,?,?,?,?,?,?,?)",
            (tag, gmail_id, thread_id, message_id, to_addr, subject, int(time.time()), in_reply_to_inbound))

    def list_sent(self, limit=30):
        return self.con.execute(
            "select t.*, s.cli, s.session_name, s.session_id, s.origin_host from sent t left join sessions s on s.tag=t.tag "
            "order by t.sent_at desc limit ?", (limit,)).fetchall()

    # inbound
    def seen(self, gmail_id):
        return self.con.execute("select 1 from inbound where gmail_id=?", (gmail_id,)).fetchone() is not None

    def record_inbound(self, **f):
        cols = ["gmail_id", "thread_id", "message_id", "from_addr", "from_name", "subject", "received_at",
                "matched_tag", "match_rule", "action", "detail", "body"]
        vals = [f.get(c) for c in cols]
        if vals[6] is None:
            vals[6] = int(time.time())
        cur = self.con.execute(
            "insert or replace into inbound(%s) values(%s)" % (",".join(cols), ",".join("?" * len(cols))), vals)
        return cur.lastrowid

    def update_inbound(self, inbound_id, **f):
        sets = ", ".join("%s=?" % k for k in f)
        self.con.execute("update inbound set %s where id=?" % sets, list(f.values()) + [inbound_id])

    def inbound(self, inbound_id):
        return self.con.execute("select * from inbound where id=?", (inbound_id,)).fetchone()

    def list_inbound(self, action=None, limit=50):
        if action:
            return self.con.execute("select * from inbound where action=? order by received_at desc limit ?",
                                    (action, limit)).fetchall()
        return self.con.execute("select * from inbound order by received_at desc limit ?", (limit,)).fetchall()

    def count_from_since(self, from_addr, since_ts):
        return self.con.execute("select count(*) from inbound where from_addr=? and received_at>=?",
                                (from_addr, int(since_ts))).fetchone()[0]

    def thread_seen_before(self, thread_id, gmail_id):
        return self.con.execute("select 1 from inbound where thread_id=? and gmail_id<>? limit 1",
                                (thread_id, gmail_id)).fetchone() is not None

    def sent_in_thread(self, thread_id):
        return self.con.execute("select * from sent where thread_id=? order by sent_at desc limit 1",
                                (thread_id,)).fetchone()

    def sent_rows_in_thread(self, thread_id):
        return self.con.execute("select * from sent where thread_id=? order by sent_at", (thread_id,)).fetchall()

    def pending_acks(self, now):
        return self.con.execute(
            "select * from inbound where acked=0 and ack_deadline is not null and ack_deadline < ?", (now,)).fetchall()

    def counts(self):
        rows = self.con.execute("select action, count(*) n from inbound group by action").fetchall()
        out = {r["action"]: r["n"] for r in rows}
        out["sent"] = self.con.execute("select count(*) from sent").fetchone()[0]
        out["sessions"] = self.con.execute("select count(*) from sessions").fetchone()[0]
        return out
