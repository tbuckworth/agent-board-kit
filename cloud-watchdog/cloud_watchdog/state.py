"""SQLite state. One file, WAL, short transactions; safe for the timer and agent CLI calls at once."""
import contextlib
import json
import sqlite3
import time

from . import config

SCHEMA = """
create table if not exists meta(k text primary key, v text);
create table if not exists resources(
  key text primary key, provider text, account text, native_id text, name text, kind text,
  first_seen real, last_seen real, gone_at real, launched_at real, state text,
  billing_now integer default 0, active_since real, cost_basis real default 0,
  tasks integer, containers integer, rate_usd_h real, rate_source text, cost_usd real, cost_source text,
  deployed_by text, created_by text, git_commit text, ssh_keys text, extra text,
  health text default 'unknown', health_detail text, crash_streak integer default 0,
  idle_streak integer default 0,
  ownership text default 'unknown',                 -- ours | foreign | unknown
  owner_session text, owner_cli text, owner_name text, owner_source text, owner_confidence text,
  owner_evidence text, owner_checked_at real, disowned text default '[]',
  suppress_until real, suppress_reason text, suppress_by text, suppress_health text, ack_x_usd real,
  cost_email_tier real default 0, notes text default '');
create table if not exists incidents(
  id integer primary key autoincrement, key text, opened_at real, updated_at real, closed_at real,
  state text,                                       -- open | asked | escalated | resolved
  rules text, severity text, ping_count integer default 0, last_ping_at real, deadline_at real,
  email_count integer default 0, last_email_at real, last_email_sig text, resumed_at real,
  investigated_at real, auto_stopped_at real, resolution text);
create index if not exists incidents_key on incidents(key, closed_at);
create table if not exists observations(key text, ts real, state text, tasks integer, rate real, cost real,
  health text, billing integer);
create index if not exists observations_key on observations(key, ts);
create table if not exists registrations(
  id integer primary key autoincrement, pattern text, session_id text, cli text, session_name text, cwd text,
  expect_secs real, expect_cost real, reason text, created_at real, matched_key text, matched_at real);
create table if not exists actions(
  id integer primary key autoincrement, ts real, incident_id integer, key text, actor text, action text,
  detail text, shadow integer default 0, ok integer default 1, cost_usd real);
create index if not exists actions_ts on actions(ts);
create table if not exists collector_health(
  account text primary key, last_ok_at real, last_slow_at real, last_error text, last_error_at real,
  consecutive_failures integer default 0, alerted integer default 0);
create table if not exists metrics(account text, ts real, name text, value real);
create table if not exists digest_items(id integer primary key autoincrement, ts real, kind text, key text,
  text text, sent_at real);
create table if not exists modal_app_owner(account text, app_id text, name text, created_by text, deployed_by text,
  state text, checked_at real, primary key(account, app_id));   -- who started each billed app (spend.py)
"""


class DB:
    def __init__(self, path=None):
        path = path or config.DB_PATH
        if str(path) != ":memory:":
            from pathlib import Path
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("pragma journal_mode=wal")
        self.c.executescript(SCHEMA)
        # Columns added after the first install (create table if not exists leaves old tables as they were).
        cols = {r["name"] for r in self.c.execute("pragma table_info(resources)")}
        if "created_by" not in cols:
            self.c.execute("alter table resources add column created_by text")

    # ------------------------------------------------------------- generic
    def q(self, sql, *args):
        return self.c.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.c.execute(sql, args).fetchone()

    def x(self, sql, *args):
        return self.c.execute(sql, args)

    def meta(self, k, default=None):
        r = self.one("select v from meta where k=?", k)
        return json.loads(r["v"]) if r else default

    def set_meta(self, k, v):
        self.x("insert into meta(k,v) values(?,?) on conflict(k) do update set v=excluded.v", k, json.dumps(v))

    @contextlib.contextmanager
    def tx(self):
        """Commit everything inside together, or nothing. A run killed mid-way (OOM killer, SIGTERM, timeout)
        leaves the database as the last commit left it: SQLite drops an uncommitted transaction on reopen."""
        self.c.execute("begin immediate")
        try:
            yield self
        except BaseException:
            if self.c.in_transaction:  # SQLite may already have rolled back (disk full, I/O error)
                self.c.execute("rollback")
            raise
        self.c.execute("commit")

    # ------------------------------------------------------------- resources
    def resource(self, key):
        return self.one("select * from resources where key=?", key)

    def update_resource(self, key, **fields):
        if not fields:
            return
        cols = ", ".join("%s=?" % k for k in fields)
        self.x("update resources set %s where key=?" % cols, *fields.values(), key)

    def upsert_resource(self, key, fields):
        if self.resource(key) is None:
            self.x("insert into resources(key) values(?)", key)
        self.update_resource(key, **fields)

    def live_resources(self):
        return self.q("select * from resources where gone_at is null order by provider, account, name")

    # ------------------------------------------------------------- incidents
    def open_incident(self, key):
        return self.one("select * from incidents where key=? and closed_at is null order by id desc limit 1", key)

    def incident(self, iid):
        return self.one("select * from incidents where id=?", iid)

    def new_incident(self, key, rules, severity, now):
        cur = self.x("insert into incidents(key, opened_at, updated_at, state, rules, severity) values(?,?,?,?,?,?)",
                     key, now, now, "open", json.dumps(rules), severity)
        return self.incident(cur.lastrowid)

    def update_incident(self, iid, **fields):
        fields.setdefault("updated_at", time.time())
        cols = ", ".join("%s=?" % k for k in fields)
        self.x("update incidents set %s where id=?" % cols, *fields.values(), iid)

    def close_incident(self, iid, resolution, now):
        self.update_incident(iid, closed_at=now, state="resolved", resolution=resolution)

    # ------------------------------------------------------------- audit
    def log(self, action, detail, key=None, incident_id=None, actor="watchdog", shadow=False, ok=True,
            cost=None, now=None):
        now = now or time.time()
        self.x("insert into actions(ts, incident_id, key, actor, action, detail, shadow, ok, cost_usd) "
               "values(?,?,?,?,?,?,?,?,?)", now, incident_id, key, actor, action,
               detail if isinstance(detail, str) else json.dumps(detail), int(shadow), int(ok), cost)
        try:
            config.EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(config.EVENTS_PATH, "a") as f:
                f.write(json.dumps({"ts": now, "incident": incident_id, "key": key, "actor": actor, "action": action,
                                    "detail": detail, "shadow": shadow, "ok": ok, "cost": cost}) + "\n")
        except OSError:
            pass

    def model_spend_today(self, now):
        r = self.one("select coalesce(sum(cost_usd),0) s from actions where action='investigate' and ts>=?",
                     now - 86400)
        return r["s"]

    def emails_today(self, now):
        return self.one("select count(*) n from actions where action='email' and shadow=0 and ok=1 and ts>=?",
                        now - 86400)["n"]

    def add_digest(self, kind, key, text, now):
        # One line per (kind, key) per day.
        if self.one("select 1 from digest_items where kind=? and key is ? and sent_at is null and ts>=?",
                    kind, key, now - 86400):
            return
        self.x("insert into digest_items(ts, kind, key, text) values(?,?,?,?)", now, kind, key, text)
