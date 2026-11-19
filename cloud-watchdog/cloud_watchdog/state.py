"""SQLite state. One file, WAL, short transactions; safe for the timer and agent CLI calls at once."""
import contextlib
import json
import sqlite3
from pathlib import Path

from . import config, util

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
  investigated_at real, resolution text);
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
  state text, checked_at real, primary key(account, app_id));   -- who started each app looked up (spend.py)
"""


class DB:
    def __init__(self, path=None):
        path = path or config.DB_PATH
        self.events_path = None
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            # The audit log sits next to the database, so a copy of the state (a dry run's) logs into its own dir.
            self.events_path = Path(path).with_name("events.jsonl")
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

    def ours_billing(self):
        """The live resources billing now that are not someone else's, by provider, account and name."""
        return self.q("select * from resources where gone_at is null and billing_now=1 and ownership!='foreign' "
                      "order by provider, account, name")

    # ------------------------------------------------------------- incidents
    def open_incident(self, key):
        return self.one("select * from incidents where key=? and closed_at is null order by id desc limit 1", key)

    def incident(self, iid):
        return self.one("select * from incidents where id=?", iid)

    def new_incident(self, key, rules, severity, now):
        cur = self.x("insert into incidents(key, opened_at, updated_at, state, rules, severity) values(?,?,?,?,?,?)",
                     key, now, now, "open", json.dumps(rules), severity)
        return self.incident(cur.lastrowid)

    def update_incident(self, iid, now, **fields):
        fields["updated_at"] = now
        cols = ", ".join("%s=?" % k for k in fields)
        self.x("update incidents set %s where id=?" % cols, *fields.values(), iid)

    def close_incident(self, iid, resolution, now):
        self.update_incident(iid, now, closed_at=now, state="resolved", resolution=resolution)

    # ------------------------------------------------------------- registrations
    def add_registration(self, pattern, who, expect, expect_cost, reason, now):
        """Register `pattern` to the session `who` (cli.caller's dict), expected to run for `expect` (a duration such
        as "6h", or None), and log it as a `register` action by who["label"]. Returns the registration's row."""
        cur = self.x("insert into registrations(pattern, session_id, cli, session_name, cwd, expect_secs, expect_cost, "
                     "reason, created_at) values(?,?,?,?,?,?,?,?,?)", pattern, who["session"], who["cli"], who["name"],
                     who.get("cwd"), util.parse_duration(expect) if expect else None, expect_cost, reason, now)
        self.log("register", "%s expect %s: %s" % (pattern, expect, reason), actor=who["label"], shadow=False, now=now)
        return self.one("select * from registrations where id=?", cur.lastrowid)

    # ------------------------------------------------------------- audit
    def log(self, action, detail, *, shadow, now, key=None, incident_id=None, actor="watchdog", ok=True, cost=None):
        """Record an action in the actions table and append it to events.jsonl next to the database file (an
        in-memory database has no events file). A failed append raises."""
        self.x("insert into actions(ts, incident_id, key, actor, action, detail, shadow, ok, cost_usd) "
               "values(?,?,?,?,?,?,?,?,?)", now, incident_id, key, actor, action,
               detail if isinstance(detail, str) else json.dumps(detail), int(shadow), int(ok), cost)
        if self.events_path is not None:
            with open(self.events_path, "a") as f:
                f.write(json.dumps({"ts": now, "incident": incident_id, "key": key, "actor": actor, "action": action,
                                    "detail": detail, "shadow": shadow, "ok": ok, "cost": cost}) + "\n")

    def model_spend_today(self, now):
        r = self.one("select coalesce(sum(cost_usd),0) s from actions where action='investigate' and ts>=?",
                     now - 86400)
        return r["s"]

    def emails_today(self, now):
        return self.one("select count(*) n from actions where action='email' and shadow=0 and ok=1 and ts>=?",
                        now - 86400)["n"]

    # ------------------------------------------------------------- collector health and metrics
    def collector_health(self, account):
        """The account's collector_health row, or None before its first collection."""
        return self.one("select * from collector_health where account=?", account)

    def collectors(self, min_failures):
        """The collector_health rows, by account, of the accounts whose last `min_failures` or more collections
        failed in a row (0: every account)."""
        return self.q("select * from collector_health where consecutive_failures>=? order by account", min_failures)

    def collector_ok(self, account, now, slow):
        """Record a collection that worked (`slow`: a slow pass): the failure streak and its blind alert reset."""
        self.x("insert into collector_health(account, last_ok_at, last_slow_at, consecutive_failures, alerted) "
               "values(?,?,?,0,0) on conflict(account) do update set last_ok_at=excluded.last_ok_at, "
               "last_slow_at=coalesce(?, collector_health.last_slow_at), consecutive_failures=0, alerted=0",
               account, now, now if slow else None, now if slow else None)

    def collector_failed(self, account, error, now):
        """Record a failed collection and its error: one more failure in a row."""
        self.x("insert into collector_health(account, last_error, last_error_at, consecutive_failures) values(?,?,?,1) "
               "on conflict(account) do update set last_error=excluded.last_error, "
               "last_error_at=excluded.last_error_at, consecutive_failures=collector_health.consecutive_failures+1",
               account, error, now)

    def collector_alerted(self, account):
        """Mark the account's failing streak as reported to the human, until a collection works again."""
        self.x("update collector_health set alerted=1 where account=?", account)

    def add_metric(self, account, name, value, now):
        self.x("insert into metrics(account, ts, name, value) values(?,?,?,?)", account, now, name, value)

    def latest_metric(self, account, name):
        """The account's most recent value of this metric, or None when it has none."""
        r = self.one("select value from metrics where account=? and name=? order by ts desc limit 1", account, name)
        return r["value"] if r else None

    def prune(self, now):
        """Delete observations and metrics over 30 days old, and Modal app owners not looked up for 62 days (an app
        still billed is looked up again)."""
        self.x("delete from observations where ts < ?", now - 30 * 86400)
        self.x("delete from metrics where ts < ?", now - 30 * 86400)
        self.x("delete from modal_app_owner where checked_at < ?", now - 62 * 86400)

    def add_digest(self, kind, key, text, now):
        # One line per (kind, key) per day.
        if self.one("select 1 from digest_items where kind=? and key is ? and sent_at is null and ts>=?",
                    kind, key, now - 86400):
            return
        self.x("insert into digest_items(ts, kind, key, text) values(?,?,?,?)", now, kind, key, text)
