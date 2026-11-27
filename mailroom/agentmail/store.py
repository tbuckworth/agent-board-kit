"""SQLite routing map: which session sent what, and what came back.

sessions  one row per (origin_host, cli, session_id) with its reply tag
sent      one row per outbound mail: Gmail id, thread id, real Message-ID, tag, To, Cc, the plain text, the sha256 of
          the message kept in the message store (eml; agentmail/messages.py) and its number of attachments; mail
          sent before the store existed has none of those four until `agent-mail mailbox backfill`; its widgets as
          JSON (agentmail/widgets.py), null for mail without any; mail kept for the Mail tab alone (config
          mailbox.owner_delivery "local") has no Gmail id, and a thread id of its own ("L...") unless it answers mail
          in a Gmail thread
inbound   one row per inbound mail the mailroom looked at, with what it did; a reply written in the Mail tab has
          gmail_id "mailbox:KEY" (routing.is_local), and when the owner attached files to it, the sha256 of the whole
          reply kept in the message store (eml; agentmail/replyfiles.py). in_reply_to records the direct parent
          Message-ID when one is known.
mailbox_threads  the owner's flags on a thread of their mailbox (agentmail/mailbox.py): read, marked unread, archived,
          snoozed
mailbox_fast  fast mode on a thread of the owner's mailbox (agentmail/fastmode.py): its agent's tag, since when, for how many
          minutes, and why it was ended once switched off, until the daemon ends it
researcher_modes  Researcher preference and latest dispatch outcome, one revisioned row per routing tag
fast_agents  what fast mode does to an agent's effort, one row per agent with fast mode on in any thread or whose
          effort is still being put back (agentmail/fastmode.py)
fast_alerts  a fast-keys alert to the owner that has not gone out yet (Gmail failed), one per event (its key: the agent's
          tag and when it was kept), tried at each poll (agentmail/fastmode.py)
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
from typing import Optional

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
create table if not exists mailbox_fast(
  thread_id text primary key,
  tag text not null,
  since real not null,
  minutes integer not null,
  ended text);
create table if not exists researcher_modes(
  tag text primary key,
  enabled integer not null check(enabled in (0, 1)),
  revision integer not null,
  status text not null,
  detail text,
  retry_at real,
  applied_at real,
  payload text,
  blocker_reported integer not null default 0);
create table if not exists fast_agents(
  tag text primary key,
  effort text not null,
  was text,
  level text,
  note text not null,
  pressed integer not null,
  tries integer not null,
  alert_key text);
create table if not exists fast_alerts(
  key text primary key,
  tag text not null,
  subject text not null,
  body text not null);
"""
# Columns added since their table was first made, each added to an older store when it is opened. reviews.audience:
# owner, collaborator or other; null, on rows from before the column existed, means owner.
ADDED_COLUMNS = (("inbound", "recovery_json", "text"), ("inbound", "notified_at", "integer"),
                 ("reviews", "audience", "text"), ("sent", "cc_addr", "text"), ("sent", "body", "text"),
                 ("sent", "eml", "text"), ("sent", "attachments", "integer"), ("inbound", "eml", "text"),
                 ("sent", "widgets", "text"), ("inbound", "in_reply_to", "text"))


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
            cols = {r[1] for r in self.con.execute("pragma table_info(researcher_modes)")}
            if "payload" not in cols:
                self.con.execute("alter table researcher_modes add column payload text")
            if "blocker_reported" not in cols:
                self.con.execute("alter table researcher_modes add column blocker_reported integer not null default 0")
                # Earlier payload receipts did not prove that the methods helper could run.
                self.con.execute(
                    "update researcher_modes set revision=revision+1, status='pending', detail=null, retry_at=?, "
                    "applied_at=null, payload=null where enabled=1", (time.time(),))
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
        host_id). Requeue an enabled Researcher preference for the replacement's plugin. Writes the store; returns
        rows changed: 0 if the old session never sent mail or the new one already has a tag of its own."""
        with self.transaction():
            if self.con.execute("select 1 from sessions where origin_host=? and cli=? and session_id=?",
                                (origin_host, new_cli, new_sid)).fetchone():
                return 0
            tag = self.con.execute("select tag from sessions where origin_host=? and cli=? and session_id=?",
                                   (origin_host, old_cli, old_sid)).fetchone()
            if tag is None:
                return 0
            self.con.execute(
                "update sessions set cli=?, session_id=?, session_name=coalesce(nullif(?, ''), session_name), "
                "cwd=coalesce(nullif(?, ''), cwd) where tag=?", (new_cli, new_sid, name, cwd, tag["tag"]))
            self.con.execute(
                "update researcher_modes set revision=revision+1, status='pending', detail=null, retry_at=?, "
                "applied_at=null, payload=null, blocker_reported=0 where tag=? and enabled=1", (time.time(), tag["tag"]))
            return 1

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

    def researcher_mode(self, tag: str) -> Optional[sqlite3.Row]:
        return self.con.execute("select * from researcher_modes where tag=?", (tag,)).fetchone()

    def researcher_modes(self) -> dict[str, sqlite3.Row]:
        return {r["tag"]: r for r in self.con.execute("select * from researcher_modes")}

    def set_researcher_mode(self, tag: str, on: bool, now: float) -> bool:
        """Record a changed preference and enqueue its revision in one statement. Absence means off."""
        return bool(self.con.execute(
            "insert into researcher_modes(tag, enabled, revision, status, retry_at) "
            "select ?, ?, 1, 'pending', ? where ? or exists(select 1 from researcher_modes where tag=?) "
            "on conflict(tag) do update set enabled=excluded.enabled, revision=researcher_modes.revision+1, "
            "status='pending', detail=null, retry_at=excluded.retry_at, applied_at=null, payload=null, blocker_reported=0 "
            "where enabled<>excluded.enabled",
            (tag, on, now, on, tag)).rowcount)

    def due_researcher_modes(self, now: float) -> list[sqlite3.Row]:
        return self.con.execute(
            "select * from researcher_modes where retry_at<=? or (enabled=1 and payload is null and retry_at is null) "
            "order by retry_at", (now,)).fetchall()

    def bind_researcher_payload(self, tag: str, revision: int, payload: Optional[str]) -> bool:
        """Save the verified payload for a still-pending On revision, or clear a failed probe."""
        return bool(self.con.execute(
            "update researcher_modes set payload=?, applied_at=null where tag=? and revision=? and enabled=1 "
            "and (applied_at is null or payload is null)", (payload, tag, revision)).rowcount)

    def reject_researcher_payload(self, tag: str, revision: int, payload: str, detail: str, retry_at: float) -> None:
        """Invalidate just the payload whose recheck failed and make its current On revision retry."""
        self.con.execute(
            "update researcher_modes set payload=null, applied_at=null, status='unavailable', detail=?, retry_at=? "
            "where tag=? and revision=? and payload=?", (detail, retry_at, tag, revision, payload))

    def record_researcher_blocker(self, tag: str, revision: int) -> None:
        """Record transport acceptance of the interpreter blocker for this On revision."""
        self.con.execute(
            "update researcher_modes set blocker_reported=1 where tag=? and revision=? and enabled=1 and payload is null",
            (tag, revision))

    def record_researcher_delivery(self, tag: str, revision: int, payload: Optional[str], status: str, detail: str,
                                   retry_at: Optional[float]) -> bool:
        """Save a dispatch outcome only while its revision and payload are current."""
        return bool(self.con.execute(
            "update researcher_modes set status=?, detail=?, retry_at=case when applied_at is null then ? end "
            "where tag=? and revision=? and payload is ?",
            (status, detail, retry_at, tag, revision, payload)).rowcount)

    def acknowledge_researcher_mode(self, tag: str, revision: int, now: float) -> bool:
        """Record when the owning conversation applied this revision."""
        return bool(self.con.execute(
            "update researcher_modes set applied_at=coalesce(applied_at, ?), retry_at=null where tag=? and revision=?",
            (now, tag, revision)).rowcount)

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

    # fast mode (agentmail/fastmode.py)
    def fast_rows(self):
        """{thread id: its mailbox_fast row}."""
        return {r["thread_id"]: r for r in self.con.execute("select * from mailbox_fast")}

    def fast_row(self, thread_id):
        return self.con.execute("select * from mailbox_fast where thread_id=?", (thread_id,)).fetchone()

    def start_fast(self, thread_id, tag, since, minutes):
        """Record fast mode switched on in thread thread_id for the agent of reply tag tag."""
        self.con.execute("insert or replace into mailbox_fast(thread_id, tag, since, minutes) values(?,?,?,?)",
                         (thread_id, tag, since, minutes))

    def end_fast(self, thread_id, why):
        """Record fast mode switched off in thread thread_id (why: fastmode.ENDED's key), for the daemon to end."""
        self.con.execute("update mailbox_fast set ended=? where thread_id=?", (why, thread_id))

    def reopen_fast(self, thread_id, since, minutes):
        """Fast mode switched on again in thread thread_id before the daemon ended it."""
        self.con.execute("update mailbox_fast set ended=null, since=?, minutes=? where thread_id=?",
                         (since, minutes, thread_id))

    def drop_fast(self, thread_id):
        self.con.execute("delete from mailbox_fast where thread_id=?", (thread_id,))

    def fast_threads_of(self, tag):
        """The threads where fast mode is on for the agent of reply tag tag, switched off ones not counted."""
        return [r["thread_id"] for r in self.con.execute("select thread_id from mailbox_fast where tag=? and ended is "
                                                         "null", (tag,))]

    def fast_agents(self):
        """{tag: its fast_agents row}."""
        return {r["tag"]: r for r in self.con.execute("select * from fast_agents")}

    def fast_agent(self, tag):
        return self.con.execute("select * from fast_agents where tag=?", (tag,)).fetchone()

    def start_fast_agent(self, tag, effort, was, note):
        """Record what fast mode does to the effort of the agent of reply tag tag, with no key pressed or try made."""
        self.con.execute("insert or replace into fast_agents(tag, effort, was, note, pressed, tries) "
                         "values(?,?,?,?,0,0)", (tag, effort, was, note))

    def update_fast_agent(self, tag, **cols):
        self.con.execute("update fast_agents set %s where tag=?" % ", ".join("%s=?" % k for k in cols),
                         list(cols.values()) + [tag])

    def count_fast_key(self, tag):
        """Count a key fast mode is about to press in the pane of the agent of reply tag tag."""
        self.con.execute("update fast_agents set pressed=pressed+1 where tag=?", (tag,))

    def drop_fast_agent(self, tag):
        self.con.execute("delete from fast_agents where tag=?", (tag,))

    def fast_alerts(self):
        """{key: its fast_alerts row}: the fast-keys alerts still to go out, oldest first."""
        return {r["key"]: r for r in self.con.execute("select * from fast_alerts order by rowid")}

    def keep_fast_alert(self, key, tag, subject, body):
        """Keep the fast-keys alert of one event (key) about the agent of reply tag tag until it goes out."""
        self.con.execute("insert into fast_alerts(key, tag, subject, body) values(?,?,?,?)", (key, tag, subject, body))

    def drop_fast_alert(self, key):
        self.con.execute("delete from fast_alerts where key=?", (key,))

    def newest_from(self, thread_id, addresses):
        """When the newest mail in thread thread_id from one of addresses (lower case) came in, or None."""
        if not addresses:
            return None
        return self.con.execute("select max(received_at) from inbound where thread_id=? and lower(from_addr) in (%s)"
                                % ",".join("?" * len(addresses)), [thread_id] + list(addresses)).fetchone()[0]

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
        """Gmail threads with agent mail to one of addresses (lower case) through Gmail, or mail from one in Gmail, in
        [since_ts, until_ts): mail kept for the Mail tab alone, and replies written there, are in no Gmail thread."""
        to = " or ".join(["lower(to_addr) like ?"] * len(addresses))
        frm = ",".join("?" * len(addresses))
        likes = ["%%%s%%" % a for a in addresses]
        rows = self.con.execute(
            "select thread_id from sent where thread_id is not null and gmail_id is not null and sent_at>=? "
            "and sent_at<? and (%s) union select thread_id from inbound where thread_id is not null "
            "and gmail_id not like ? and received_at>=? and received_at<? and lower(from_addr) in (%s)" % (to, frm),
            [int(since_ts), int(until_ts)] + likes + [routing.LOCAL_PREFIX + "%", int(since_ts), int(until_ts)]
            + list(addresses)).fetchall()
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
                "matched_tag", "match_rule", "action", "detail", "body", "eml", "in_reply_to"]
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

    def kept_sent(self, thread_id):
        """The agent mail in thread thread_id kept for the Mail tab alone (no Gmail id), oldest first."""
        return self.con.execute("select * from sent where thread_id=? and gmail_id is null order by id",
                                (thread_id,)).fetchall()

    def thread_in_gmail(self, thread_id):
        """Whether Gmail holds any of thread thread_id's mail: agent mail sent through it, or inbound mail but a reply
        written in the Mail tab."""
        return self.con.execute(
            "select 1 from sent where thread_id=? and gmail_id is not null union all "
            "select 1 from inbound where thread_id=? and gmail_id not like ? limit 1",
            (thread_id, thread_id, routing.LOCAL_PREFIX + "%")).fetchone() is not None

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
        """The newest agent mail in thread thread_id: of two sent in the same second, the later."""
        return self.con.execute("select * from sent where thread_id=? order by sent_at desc, id desc limit 1",
                                (thread_id,)).fetchone()

    def sent_rows_in_thread(self, thread_id):
        return self.con.execute("select * from sent where thread_id=? order by sent_at", (thread_id,)).fetchall()

    def counts(self):
        rows = self.con.execute("select action, count(*) n from inbound group by action").fetchall()
        out = {r["action"]: r["n"] for r in rows}
        out["sent"] = self.con.execute("select count(*) from sent").fetchone()[0]
        out["sessions"] = self.con.execute("select count(*) from sessions").fetchone()[0]
        return out
