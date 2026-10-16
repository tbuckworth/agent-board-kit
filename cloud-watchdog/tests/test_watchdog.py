"""Unit tests: rules, ladder, policy, registration/ack maths, modes, transcripts, credential isolation,
configuration.

No network, no provider, no mail: collectors are fed synthetic results, and the notifier, address
book and live crash re-check are replaced with fakes. Run with HOME pointing at a scratch directory.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_watchdog import (check, cli, config, digest, ladder, modes, observe, owner, policy, rules,  # noqa: E402
                            spend, state, transcripts, util)
from cloud_watchdog.providers import modal_  # noqa: E402

T0 = 1_790_000_000.0
OWNER = "11111111-2222-3333-4444-555555555555"

# The configuration a user would write in ~/.config/cloud-watchdog/config.toml.
TEST_CONFIG = """
[identity]
owner_modal_users = ["alex-10001", "alexw2"]
owner_lambda_ssh_keys = ["alex-key", "alex-desktop"]
modal_shared_apps = ["inspect_modal_sandbox"]

[accounts."modal:main"]
provider = "modal"
auto_stop = true

[accounts."modal:team"]
provider = "modal"
profile = "team-workspace"
secrets = "none"
auto_stop = true

[accounts."lambda:work"]
provider = "lambda"

[accounts."lambda:main"]
provider = "lambda"

[accounts."slurm:cluster"]
provider = "slurm"
secrets = "none"
ssh_host = "cluster-login"
paid_partitions = ["gpu-paid-*"]
slow_only = true
"""
MAILROOM = {"owner_name": "Alex", "owner_email": "owner@example.com", "allowlist": ["owner@example.com"]}


def write_config(d, text=TEST_CONFIG, mailroom=MAILROOM):
    d = Path(d)
    (d / "config.toml").write_text(text)
    (d / "mailroom.json").write_text(json.dumps(mailroom))
    return d / "config.toml", d / "mailroom.json"


class FakeNotifier:
    def __init__(self):
        self.pings, self.emails, self.stops = [], [], []

    def ping(self, inc, res, text, resume=False):
        self.pings.append((res["key"], resume, text))
        return True, 0

    def email(self, subject, md, key=None, incident_id=None, digest_if_capped=True):
        self.emails.append((subject, md))
        return True

    def send(self, subject, md, key=None, incident_id=None):
        self.emails.append((subject, md))
        return True

    def stop(self, res, actor, incident_id=None):
        self.stops.append((res["key"], actor))
        return True, "stopped"


def modal_app(app_id="ap-1", name="judge", deployed_by="alex-10001", failed=0, seen=1, tasks=1, cost=None,
              rate=None, launched=T0 - 3600):
    r = {"native_id": app_id, "name": name, "kind": "app", "state": "deployed", "launched_at": launched,
         "tasks": tasks, "containers": tasks, "deployed_by": deployed_by, "git_commit": "abc1234",
         "health_detail": {"failed": failed, "seen": seen, "window_min": 120, "last_failure_at": T0, "sample": "Runner failed"}}
    if cost is not None:
        r["cost_usd"], r["cost_source"] = cost, "billing"
    if rate is not None:
        r["rate_usd_h"], r["rate_source"] = rate, "billing"
    return r


def lambda_box(iid="i-1", keys=("alex-key",), launched=T0 - 3600, rate=1.29, idle=None):
    r = {"native_id": iid, "name": "box", "kind": "instance", "state": "active", "launched_at": launched,
         "rate_usd_h": rate, "rate_source": "list price", "cost_usd": round(rate * (T0 - launched) / 3600, 2),
         "cost_source": "estimate: price x uptime", "ssh_keys": list(keys), "ip": "1.2.3.4"}
    if idle is not None:
        r["idle"] = idle
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(config, "EVENTS_PATH", Path(self.tmp.name) / "events.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path, self.mailroom_path = write_config(self.tmp.name)
        self.cfg = config.load(self.cfg_path, self.mailroom_path)
        self.db = state.DB(":memory:")
        self.n = FakeNotifier()
        self.book = {OWNER: {"id": OWNER, "name": "eval session", "cli": "claude", "live": True}}
        p1 = mock.patch.object(owner, "sessions", lambda: self.book)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(owner, "resolve", self.fake_resolve)
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch("cloud_watchdog.ladder.gather.op", self.fake_op)
        p3.start()
        self.addCleanup(p3.stop)
        self.owner_found = True
        self.live_crash = True

    def fake_resolve(self, db, cfg, res, now, book=None):
        if res["ownership"] == "foreign":
            return {"owner_session": None, "owner_source": "foreign", "owner_checked_at": now}
        if self.owner_found:
            return {"owner_session": OWNER, "owner_cli": "claude", "owner_name": "eval session",
                    "owner_source": "transcript", "owner_confidence": "high", "owner_evidence": "launched it",
                    "owner_checked_at": now}
        return {"owner_session": None, "owner_evidence": "nothing", "owner_checked_at": now}

    def fake_op(self, account, acct, name, opts=None, native_id=None):
        assert name == "crash_check"
        return True, {"failed": 4 if self.live_crash else 0, "seen": 4, "window_min": 120}

    def ingest(self, account, resources, now, slow=True):
        observe.ingest(self.db, self.cfg, account, {"resources": resources, "metrics": {}}, now, slow)

    def tick(self, now, live=True):
        ladder.Ladder(self.db, self.cfg, now, self.n, live=live).process()

    def res(self, key):
        return self.db.resource(key)


class CrashLoop(Base):
    def test_live_owner_pinged_then_auto_stopped(self):
        k = "modal:main:ap-1"
        self.ingest("modal:main", [modal_app(failed=3, seen=4)], T0)
        self.assertEqual(self.res(k)["health"], "ok")  # one check is not enough
        self.ingest("modal:main", [modal_app(failed=4, seen=5)], T0 + 600)
        self.assertEqual(self.res(k)["health"], "crash_loop")
        self.tick(T0 + 600)
        self.assertEqual(len(self.n.pings), 1)
        self.assertIn("I will stop it", self.n.pings[0][2])
        self.assertEqual(self.n.stops, [])
        self.tick(T0 + 600 + 14 * 60)  # inside the 15 min wait
        self.assertEqual(self.n.stops, [])
        self.tick(T0 + 600 + 16 * 60)
        self.assertEqual(self.n.stops, [(k, "watchdog")])
        self.assertTrue(any("Stopped crash-looping" in s for s, _ in self.n.emails))
        self.assertIsNone(self.db.open_incident(k))

    def test_no_stop_when_fresh_check_disagrees(self):
        self.ingest("modal:main", [modal_app(failed=3, seen=3)], T0)
        self.ingest("modal:main", [modal_app(failed=3, seen=3)], T0 + 600)
        self.tick(T0 + 600)
        self.live_crash = False
        self.tick(T0 + 600 + 16 * 60)
        self.assertEqual(self.n.stops, [])
        self.assertTrue(any("owner not reached" in s for s, _ in self.n.emails))

    def test_colleague_app_never_stopped_or_pinged(self):
        k = "modal:main:ap-2"
        for t in (T0, T0 + 600, T0 + 1200, T0 + 3 * 3600):
            self.ingest("modal:main", [modal_app("ap-2", deployed_by="kim-7305", failed=5, seen=5)], t)
            self.tick(t)
        self.assertEqual(self.res(k)["ownership"], "foreign")
        self.assertEqual(self.n.pings, [])
        self.assertEqual(self.n.stops, [])
        foreign = [s for s, _ in self.n.emails if "Someone else's app" in s]
        self.assertEqual(len(foreign), 1)  # once per 24 h

    def test_unknown_deployer_is_not_auto_stopped(self):
        for t in (T0, T0 + 600):
            self.ingest("modal:main", [modal_app("ap-3", deployed_by=None, failed=4, seen=4)], t)
        self.tick(T0 + 600)
        self.tick(T0 + 600 + 16 * 60)
        self.assertEqual(self.n.stops, [])
        self.assertIn("I will ask Alex", self.n.pings[0][2])

    def test_ack_given_during_crash_loop_blocks_auto_stop(self):
        k = "modal:main:ap-1"
        for t in (T0, T0 + 600):
            self.ingest("modal:main", [modal_app(failed=4, seen=4)], t)
        self.db.update_resource(k, suppress_until=T0 + 86400, suppress_health="crash_loop", ack_x_usd=0)
        self.tick(T0 + 600)
        self.tick(T0 + 2000)
        self.assertEqual(self.n.pings, [])
        self.assertEqual(self.n.stops, [])

    def test_crash_after_healthy_ack_is_reasked(self):
        k = "modal:main:ap-1"
        self.ingest("modal:main", [modal_app()], T0)
        self.db.update_resource(k, suppress_until=T0 + 86400, suppress_health="ok", ack_x_usd=0)
        for t in (T0 + 600, T0 + 1200):
            self.ingest("modal:main", [modal_app(failed=4, seen=4)], t)
        self.tick(T0 + 1200)
        self.assertEqual(len(self.n.pings), 1)


class OwnerPaths(Base):
    def test_exited_owner_resumed_and_human_emailed_together(self):
        self.book[OWNER]["live"] = False
        k = "lambda:main:i-1"
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        self.tick(T0)
        self.assertEqual(len(self.n.pings), 1)
        self.assertTrue(self.n.pings[0][1])  # resume=True
        self.assertIn("ONE question", self.n.pings[0][2])
        self.assertEqual(len(self.n.emails), 1)
        self.tick(T0 + 31 * 60)
        self.assertEqual(len(self.n.emails), 1)  # no second email after the deadline
        self.assertEqual(self.db.open_incident(k)["state"], "escalated")
        self.assertEqual(self.n.stops, [])  # never auto-terminates Lambda

    def test_live_owner_gets_second_ping_then_human(self):
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        self.tick(T0)
        self.tick(T0 + 31 * 60)
        self.assertEqual(len(self.n.pings), 2)
        self.assertEqual(self.n.emails, [])
        self.tick(T0 + 62 * 60)
        self.assertEqual(len(self.n.emails), 1)
        self.assertIn("got no answer", self.n.emails[0][1])

    def test_unknown_owner_goes_to_human(self):
        self.owner_found = False
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        self.tick(T0, live=False)  # shadow: the investigator is not run
        self.assertEqual(self.n.pings, [])
        self.assertEqual(len(self.n.emails), 1)
        self.assertIn("could not tell", self.n.emails[0][1])

    def test_foreign_lambda_is_ignored(self):
        self.ingest("lambda:work", [lambda_box(keys=("lee-key",), launched=T0 - 70 * 3600)], T0)
        self.tick(T0)
        self.assertEqual(self.res("lambda:work:i-1")["ownership"], "foreign")
        self.assertEqual((self.n.pings, self.n.emails), ([], []))

    def test_registration_suppresses_until_expected_end(self):
        self.db.x("insert into registrations(pattern, session_id, cli, session_name, expect_secs, reason, created_at) "
                  "values(?,?,?,?,?,?,?)", "lambda:main:i-*", OWNER, "claude", "eval session", 48 * 3600, "sweep", T0)
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        r = self.res("lambda:main:i-1")
        self.assertEqual(r["owner_source"], "registered")
        self.tick(T0)
        self.assertEqual(self.n.pings, [])
        self.tick(T0 + 24 * 3600)  # past launch + 48 h
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0 + 24 * 3600)
        self.tick(T0 + 24 * 3600)
        self.assertEqual(len(self.n.pings), 1)


class CostRules(Base):
    def test_unacked_tiers_email_once_each(self):
        k = "lambda:main:i-1"
        for hours in (100, 400, 420, 800):  # $129, $516, $542, $1032 at $1.29/h
            t = T0 + hours * 3600
            self.ingest("lambda:main", [lambda_box(launched=T0, rate=1.29) | {"cost_usd": round(1.29 * hours, 2)}], t)
            self.tick(t)
        cost_mails = [s for s, _ in self.n.emails if "has cost" in s]
        self.assertEqual(len(cost_mails), 2)  # $500 tier, then $1,000 tier
        self.assertEqual(self.res(k)["cost_email_tier"], 1000)

    def test_acked_threshold_is_max_500_2x(self):
        k = "lambda:main:i-1"
        self.ingest("lambda:main", [lambda_box(launched=T0) | {"cost_usd": 40.0}], T0)
        self.db.update_resource(k, suppress_until=T0 + 30 * 86400, ack_x_usd=400.0, suppress_health="ok")
        r = self.res(k)
        self.assertEqual(rules.cost_email_threshold(r, self.cfg, T0), 800)
        self.db.update_resource(k, ack_x_usd=40.0)
        self.assertEqual(rules.cost_email_threshold(self.res(k), self.cfg, T0), 500)
        self.db.update_resource(k, ack_x_usd=600.0)
        self.assertEqual(rules.cost_email_threshold(self.res(k), self.cfg, T0), 1200)
        # Acked: no owner message for cost, long running, etc.
        self.ingest("lambda:main", [lambda_box(launched=T0 - 30 * 3600) | {"cost_usd": 300.0}], T0 + 60)
        self.assertEqual([f["rule"] for f in rules.evaluate(self.res(k), self.cfg, T0 + 60)], [])

    def test_high_burn_and_long_running(self):
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600, rate=30)], T0)
        got = {f["rule"] for f in rules.evaluate(self.res("lambda:main:i-1"), self.cfg, T0)}
        self.assertTrue({"high_burn", "long_running"} <= got)

    def test_idle_needs_three_checks(self):
        k = "lambda:main:i-1"
        for i in range(3):
            self.ingest("lambda:main", [lambda_box(idle=True)], T0 + i * 600)
            self.assertEqual(self.res(k)["health"], "idle" if i == 2 else "ok")
        self.ingest("lambda:main", [lambda_box(idle=None)], T0 + 1800)  # unreachable: no change
        self.assertEqual(self.res(k)["health"], "idle")
        self.ingest("lambda:main", [lambda_box(idle=False)], T0 + 2400)
        self.assertEqual(self.res(k)["health"], "ok")


class Policy(Base):
    def row(self, **kw):
        base = {"provider": "modal", "account": "modal:main", "deployed_by": "alex-10001", "health": "crash_loop",
                "ownership": "ours", "suppress_until": None, "suppress_health": None, "owner_name": "x",
                "owner_session": OWNER}
        base.update(kw)
        return base

    def test_watchdog(self):
        ok = lambda **kw: policy.can_stop(self.row(**kw), "watchdog", self.cfg, T0, fresh_crash=True)[0]
        self.assertTrue(ok())
        self.assertTrue(ok(account="modal:team", deployed_by="alexw2"))   # the owner's second Modal user
        self.assertFalse(ok(deployed_by="kim-7305"))
        self.assertFalse(ok(deployed_by=None))
        self.assertFalse(ok(health="ok"))
        self.assertFalse(ok(provider="lambda", account="lambda:main"))
        self.assertFalse(policy.can_stop(self.row(), "watchdog", self.cfg, T0, fresh_crash=False)[0])

    def test_second_workspace_user_is_ours(self):
        self.ingest("modal:team", [modal_app("ap-tm1", "alex-tinker-plb", deployed_by="alexw2")], T0)
        self.assertEqual(self.res("modal:team:ap-tm1")["ownership"], "ours")
        self.ingest("modal:team", [modal_app("ap-tm2", "banana", deployed_by="robin1029")], T0)
        self.assertEqual(self.res("modal:team:ap-tm2")["ownership"], "foreign")

    def test_modal_run_app_ownership_comes_from_its_creator(self):
        mine = modal_app("ap-run", "sweep-cell", deployed_by=None) | {"created_by": "alex-10001", "state": "ephemeral"}
        theirs = modal_app("ap-run2", "etv-run", deployed_by=None) | {"created_by": "kim-7305", "state": "ephemeral"}
        nobody = modal_app("ap-run3", "mystery", deployed_by=None) | {"state": "ephemeral"}
        self.ingest("modal:main", [mine, theirs, nobody], T0)
        self.assertEqual(self.res("modal:main:ap-run")["ownership"], "ours")
        self.assertEqual(self.res("modal:main:ap-run2")["ownership"], "foreign")
        self.assertEqual(self.res("modal:main:ap-run3")["ownership"], "unknown")
        ok = lambda **kw: policy.can_stop(self.row(**kw), "watchdog", self.cfg, T0, fresh_crash=True)[0]
        self.assertTrue(ok(deployed_by=None, created_by="alex-10001"))
        self.assertFalse(ok(deployed_by=None, created_by="kim-7305"))
        self.assertFalse(ok(deployed_by="kim-7305", created_by="alex-10001"))   # the latest deployer decides

    def test_people(self):
        lam = self.row(provider="lambda", account="lambda:main", health="ok")
        self.assertFalse(policy.can_stop(lam, "owner", self.cfg, T0)[0])
        self.assertTrue(policy.can_stop(lam, "owner", self.cfg, T0, confirm_terminate=True)[0])
        self.assertFalse(policy.can_stop(self.row(), "agent", self.cfg, T0)[0])
        self.assertFalse(policy.can_stop(self.row(ownership="foreign"), "owner", self.cfg, T0)[0])
        self.assertTrue(policy.can_stop(self.row(ownership="foreign"), "human", self.cfg, T0)[0])
        self.assertFalse(policy.can_stop(self.row(provider="deepinfra"), "human", self.cfg, T0)[0])


class Modes(Base):
    def test_shadow_goes_live_and_resets_incidents(self):
        modes.set_mode(self.db, "shadow", T0, live_at=T0 + 7 * 86400)
        self.assertEqual(modes.effective(self.db, T0 + 86400), ("shadow", False))
        self.db.new_incident("x:y:z", ["cost"], "normal", T0)
        self.assertEqual(modes.effective(self.db, T0 + 7 * 86400 + 1), ("live", True))
        self.assertIsNone(self.db.open_incident("x:y:z"))
        self.assertEqual(modes.effective(self.db, T0 + 8 * 86400), ("live", False))

    def test_stay_in_shadow(self):
        modes.set_mode(self.db, "shadow", T0, live_at=None)
        self.assertEqual(modes.effective(self.db, T0 + 30 * 86400)[0], "shadow")



# Runs in a child: one complete check of lambda:work, then a second one that SIGKILLs itself (what the OOM
# killer sends, so no handler or `finally` runs) after writing two of its three resources.
KILLED_MID_INGEST = """
import json, os, signal, sys
sys.path.insert(0, %(repo)r)
from pathlib import Path
from cloud_watchdog import check, config, state
config.EVENTS_PATH = Path(%(tmp)r) / "events.jsonl"
cfg = config.load(Path(%(tmp)r) / "config.toml", Path(%(tmp)r) / "mailroom.json")
db = state.DB(Path(%(tmp)r) / "state.sqlite")
boxes = json.loads(%(boxes)r)
check.ingest_account(db, cfg, "lambda:work", True, True, {"resources": boxes[:2], "metrics": {}}, %(t0)r)
real, written = db.upsert_resource, []
def dying(key, fields):
    real(key, fields)
    written.append(key)
    if len(written) == 2:
        os.kill(os.getpid(), signal.SIGKILL)
db.upsert_resource = dying
check.ingest_account(db, cfg, "lambda:work", True, True, {"resources": boxes, "metrics": {}}, %(t0)r + 600)
"""


class Hardening(Base):
    def test_check_killed_mid_ingest_leaves_the_account_as_the_last_complete_check(self):
        boxes = [lambda_box("i-%d" % i) for i in (1, 2, 3)]
        code = KILLED_MID_INGEST % {"repo": str(config.REPO_DIR), "tmp": self.tmp.name, "boxes": json.dumps(boxes),
                                    "t0": T0}
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, -9, p.stderr)
        db = state.DB(Path(self.tmp.name) / "state.sqlite")
        self.assertEqual({r["native_id"]: r["last_seen"] for r in db.q("select * from resources")},
                         {"i-1": T0, "i-2": T0})
        self.assertEqual(db.one("select count(*) n from observations")["n"], 2)
        self.assertEqual(db.one("select last_ok_at from collector_health where account='lambda:work'")[0], T0)

    def test_failed_collection_counts_and_rest_of_state_untouched(self):
        self.ingest("lambda:work", [lambda_box()], T0)
        got = check.ingest_account(self.db, self.cfg, "lambda:work", False, False, "RuntimeError: modal failed (-9)",
                                   T0 + 600)
        self.assertFalse(got["ok"])
        h = self.db.one("select * from collector_health where account='lambda:work'")
        self.assertEqual(h["consecutive_failures"], 1)
        self.assertEqual(self.res("lambda:work:i-1")["last_seen"], T0)

    def test_tx_reraises_the_real_error_when_sqlite_already_rolled_back(self):
        with self.assertRaises(ValueError):
            with self.db.tx():
                self.db.x("insert into meta(k, v) values('a', '1')")
                self.db.c.execute("rollback")  # what SQLite does itself on SQLITE_FULL / SQLITE_IOERR
                raise ValueError("disk full")
        self.assertIsNone(self.db.meta("a"))
        with self.db.tx():  # and the connection is usable afterwards
            self.db.set_meta("b", 2)
        self.assertEqual(self.db.meta("b"), 2)

    def test_service_unit_lets_a_check_survive_a_bystander_oom_kill(self):
        unit = cli.render_units(python="/p", component_dir="/kit/cloud-watchdog")["cloud-watchdog.service"]
        self.assertIn("\nOOMPolicy=continue\n", unit)
        self.assertIn("\nMemoryMax=1G\n", unit)

    def test_failure_email_names_the_cause_and_the_memory_hogs(self):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd[0])
            if cmd[0] == "systemctl":
                return 0, result + "\n", ""
            if cmd[0] == "journalctl" and "-k" in cmd:
                return 0, "Oct 05 21:20:30 h kernel: Out of memory: Killed process 2837514 (modal) rss:30012kB\n", ""
            if cmd[0] == "journalctl":
                return 0, "cloud-watchdog.service: Failed with result '%s'.\n" % result, ""
            return 0, "    PID   RSS ELAPSED COMMAND\n1199765 4977000 03:10:00 pp\n", ""

        result = "oom-kill"
        subject, md = cli.failure_report("cloud-watchdog.service", run=fake_run, mem=lambda: "Memory now: 0.1 GB")
        self.assertEqual(subject, "[cloud-watchdog] Check failed (oom-kill)")
        for text in ("bystander", "Killed process 2837514 (modal)", "4977000 03:10:00 pp", "Memory now", "retries"):
            self.assertIn(text, md)
        result, calls[:] = "exit-code", []
        subject, md = cli.failure_report("cloud-watchdog.service", run=fake_run, mem=lambda: "Memory now: 0.1 GB")
        self.assertEqual(subject, "[cloud-watchdog] Check failed (exit-code)")
        self.assertIn("traceback", md)
        self.assertNotIn("Memory now", md)
        self.assertEqual(calls, ["systemctl", "journalctl"])

    def test_dry_run_clears_copies_left_by_a_killed_dry_run(self):
        d = Path(self.tmp.name)
        (d / "cw-dry-stale").mkdir()
        (d / "cw-dry-stale" / "state.sqlite").write_text("")
        with mock.patch.multiple(config, STATE_DIR=d, LOCK_PATH=d / "check.lock", DB_PATH=d / "state.sqlite"), \
                mock.patch.object(check, "_run", lambda db, *a: {"ran": True}):
            self.assertEqual(check.run(cfg=self.cfg, dry_run=True), {"ran": True})
        self.assertEqual(list(d.glob("cw-dry-*")), [])


def modal_spend(month, creators, **kw):
    return {"month": month, "through": T0 - 3600, "creators": creators, "token_user": "alex-10001",
            "lookup_errors": 0, "volumes_by": {"alex-10001": 4, "kim-7305": 230}} | kw


SPEND_MONTH = {
    "ap-mine1": ["exp-sweep-models-fixed10-001-opus", 24.0], "ap-mine2": ["exp-sweep-models-fixed10-003-fable", 19.0],
    "ap-mine3": ["exp-b2-20260101T120000Z-0a1b2c3d", 17.0], "ap-sandbox": ["inspect_modal_sandbox", 1860.0],
    "ap-mixed": ["joint-server", 50.0], "ap-kim": ["etv-htv-sandbox", 3600.0], "ap-sam": ["targets-qwen3-8b", 386.0],
    "ap-gone": ["banana-bench-target", 1.5], "ap-both": ["alex-tinker-plb", 3.0]}
SPEND_CREATORS = {
    "ap-mine1": {"created_by": "alex-10001", "state": "stopped"},
    "ap-mine2": {"created_by": "alex-10001", "state": "stopped"},
    "ap-mine3": {"created_by": "alex-10001", "deployed_by": "alex-10001", "state": "deployed"},
    "ap-sandbox": {"created_by": "sam-22506", "deployed_by": "sam-22506", "state": "deployed"},
    "ap-mixed": {"created_by": "sam-22506", "deployed_by": "lee-90937", "state": "deployed"},
    "ap-kim": {"created_by": "kim-7305", "deployed_by": "kim-7305", "state": "deployed"},
    "ap-sam": {"created_by": "sam-22506", "state": "stopped"},
    "ap-gone": {"created_by": None, "deployed_by": None, "state": "not-found"},
    "ap-both": {"created_by": "alex-10001", "deployed_by": "alexw2", "state": "deployed"}}


class Spend(Base):
    def setUp(self):
        super().setUp()
        self.cfg["identity"]["owner_modal_users"] = ["alex-10001", "alexw2"]

    def test_split_buckets(self):
        owners = {a: dict(c) for a, c in SPEND_CREATORS.items()}
        s = spend.split(SPEND_MONTH, owners, spend.your_users(self.cfg["identity"]), self.cfg["identity"], T0)
        self.assertEqual(s["yours_usd"], 63.0)          # mine1-3 + both (two of the owner's own user names)
        self.assertEqual(s["yours_apps"], 4)
        self.assertEqual(s["shared_usd"], 1910.0)       # listed shared app + created/deployed by different people
        self.assertEqual(s["others_by"], [["kim-7305", 3600.0], ["sam-22506", 386.0]])
        self.assertEqual(s["unknown_usd"], 1.5)
        self.assertAlmostEqual(s["apps_usd"], s["yours_usd"] + s["shared_usd"] + s["others_usd"] + s["unknown_usd"])
        self.assertEqual(s["yours_top"][0], ["exp-sweep-models-*", 43.0, 2])
        self.assertIn(["exp-b2-20260101T120000Z-0a1b2c3d", 17.0, 1], s["yours_top"])   # a family of one keeps its name

    def test_token_user_counts_as_yours(self):
        ident = {"owner_modal_users": ["alex-10001"]}
        owners = {"ap-w": {"created_by": "alexw2"}}
        s = spend.split({"ap-w": ["alex-tinker", 2.0]}, owners, spend.your_users(ident, "alexw2"), ident, T0)
        self.assertEqual(s["yours_usd"], 2.0)
        s = spend.split({"ap-w": ["alex-tinker", 2.0]}, owners, spend.your_users(ident), ident, T0)
        self.assertEqual(s["others_by"], [["alexw2", 2.0]])

    def test_family(self):
        self.assertEqual(spend.family("exp-sweep-models-fixed10-001-gpt-6.1-sol"), "exp-sweep-models-*")
        self.assertEqual(spend.family("exp-b2-20260101T120000Z-0a1b2c3d"), "exp-b2-*")
        self.assertEqual(spend.family("inspect_modal_sandbox"), "inspect_modal_sandbox")
        self.assertEqual(spend.family("etv-htv-sandbox"), "etv-htv-sandbox")

    def test_creators_cached_and_only_new_or_redeployable_apps_looked_up_again(self):
        res = {"resources": [], "metrics": {"month_metered_usd": 6100.0, "month_volumes_usd": 212.0},
               "spend": modal_spend(SPEND_MONTH, SPEND_CREATORS)}
        check.ingest_account(self.db, self.cfg, "modal:main", True, True, res, T0)
        self.assertEqual(self.db.one("select count(*) n from modal_app_owner")["n"], len(SPEND_CREATORS))
        known = set(spend.known_ids(self.db, "modal:main", T0 + 3600))
        self.assertEqual(known, set(SPEND_CREATORS))
        later = set(spend.known_ids(self.db, "modal:main", T0 + 2 * 86400))
        self.assertEqual(set(SPEND_CREATORS) - later, {"ap-mine3", "ap-sandbox", "ap-mixed", "ap-kim", "ap-both"})
        # Next pass: the child looked up nothing new, the split still uses the cache.
        res["spend"] = modal_spend(SPEND_MONTH, {})
        check.ingest_account(self.db, self.cfg, "modal:main", True, True, res, T0 + 3600)
        self.assertEqual(self.db.meta("spend:modal:main")["yours_usd"], 63.0)
        opts = check.collect_opts(self.db, self.cfg, "modal:main", T0 + 3600, slow=True)
        self.assertEqual(set(opts["known_creators"]), set(SPEND_CREATORS))
        self.assertNotIn("known_creators", check.collect_opts(self.db, self.cfg, "modal:main", T0 + 3600, slow=False))

    def test_digest_and_status_show_total_and_yours(self):
        res = {"resources": [], "metrics": {"day_spend_usd": 145.0, "month_metered_usd": 6100.0,
                                            "month_volumes_usd": 212.0},
               "spend": modal_spend(SPEND_MONTH, SPEND_CREATORS)}
        check.ingest_account(self.db, self.cfg, "modal:main", True, True, res, T0)
        _, md, _, _ = digest.build(self.db, self.cfg, T0)
        self.assertIn("- `modal:main`: last 24 h $145, month metered $6,100", md)
        self.assertIn("    - **This month: total $6,100, yours $63.00** (1% of app spend). Basis: apps created or "
                      "deployed by `alex-10001` or `alexw2`", md)
        self.assertIn("shared `inspect_modal_sandbox` $1,860 (created by sam-22506;", md)
        self.assertIn("others $3,986 (kim-7305 $3,600, sam-22506 $386)", md)
        self.assertIn("creator unknown $1.50", md)
        self.assertIn("Yours by app: exp-sweep-models-* (2 apps) $43.00 · exp-b2-20260101T120000Z-0a1b2c3d $17.00", md)
        self.assertIn("volume storage $212 (billed per workspace, not per volume; you created 4 of 234 volumes)", md)
        try:
            import markdown  # noqa: F401
            from cloud_watchdog.notify import md_to_html
            self.assertIn("<ul>\n<li><strong>This month: total $6,100, yours $63.00</strong>", md_to_html(md))
        except ImportError:
            pass
        out = []
        with mock.patch.object(config, "load", lambda *a: self.cfg), mock.patch.object(cli.state, "DB", lambda: self.db), \
                mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            cli.cmd_status(None)
        self.assertIn("modal:main spend:", out)
        self.assertTrue(any(line.startswith("  This month: total $6,100, yours $63.00") for line in out), out)

    def test_lookup_failure_says_unavailable_and_collection_still_succeeds(self):
        res = {"resources": [], "metrics": {"month_metered_usd": 10.0},
               "spend": {"month": {"ap-x": ["x", 10.0]}, "through": None, "error": "modal_api failed (1): boom"}}
        got = check.ingest_account(self.db, self.cfg, "modal:main", True, True, res, T0)
        self.assertTrue(got["ok"])
        self.assertEqual(spend.lines("modal:main", self.db.meta("spend:modal:main")),
                         ["    - Yours: unavailable (creator lookup failed: `modal_api failed (1): boom`)."])


class ModalOwnership(unittest.TestCase):
    def test_old_database_gains_the_created_by_column(self):
        with tempfile.TemporaryDirectory() as d:
            import sqlite3
            path = Path(d) / "state.sqlite"
            c = sqlite3.connect(path)
            c.execute("create table resources(key text primary key, deployed_by text)")
            c.commit()
            c.close()
            db = state.DB(path)
            self.assertIn("created_by", {r["name"] for r in db.q("pragma table_info(resources)")})
            state.DB(path)   # and opening it again is a no-op

    def test_fill_creators_uses_the_cache_then_one_lookup(self):
        res = [{"native_id": "ap-a", "dormant": False}, {"native_id": "ap-b", "dormant": False},
               {"native_id": "ap-c", "dormant": False, "deployed_by": "sam-22506"}, {"native_id": "ap-d", "dormant": True}]
        asked = []

        def fake(acct, ids, python):
            asked.append(list(ids))
            return {"apps": {"ap-b": {"created_by": "alex-10001"}}}

        with mock.patch.object(modal_, "_creators", fake):
            modal_._fill_creators({}, {"known_creator_of": {"ap-a": "kim-7305"}}, res)
        self.assertEqual(asked, [["ap-b"]])
        self.assertEqual([r.get("created_by") for r in res], ["kim-7305", "alex-10001", None, None])
        with mock.patch.object(modal_, "_creators", mock.Mock(side_effect=RuntimeError("down"))):
            fresh = [{"native_id": "ap-x", "dormant": False}]
            modal_._fill_creators({}, {}, fresh)
        self.assertNotIn("created_by", fresh[0])


class ModalBilling(unittest.TestCase):
    def test_month_to_date_per_app_from_daily_and_todays_hourly_rows(self):
        now = util.parse_time("2026-10-12T09:30:00Z")
        daily = [{"object_id": "ap-a", "description": "a", "interval_start": "2026-09-30T00:00:00", "cost": "100"},
                 {"object_id": "ap-a", "description": "a", "interval_start": "2026-10-01T00:00:00", "cost": "5"},
                 {"object_id": "ap-b", "description": "b", "interval_start": "2026-10-11T00:00:00", "cost": "7"},
                 {"object_id": "ap-b", "description": "b", "interval_start": "2026-10-12T00:00:00", "cost": "999"}]
        hourly = [{"object_id": "ap-b", "description": "b", "interval_start": "2026-10-11T23:00:00", "cost": "1"},
                  {"object_id": "ap-b", "description": "b", "interval_start": "2026-10-12T07:00:00", "cost": "2"},
                  {"object_id": "ap-c", "description": "c", "interval_start": "2026-10-12T08:00:00", "cost": "3"}]

        def fake(acct, args, timeout=120):
            if args[:2] == ["billing", "summary"]:
                return json.dumps({"metered_cost": "20", "metered_cost_breakdown": {"volumes": "4"}})
            return json.dumps(daily if "d" in args else hourly)

        with mock.patch.object(modal_, "_modal", fake):
            _, _, metrics, sp = modal_._billing({}, now)
        self.assertEqual(sp["month"], {"ap-a": ["a", 5.0], "ap-b": ["b", 9.0], "ap-c": ["c", 3.0]})
        self.assertEqual(sp["through"], util.parse_time("2026-10-12T09:00:00Z"))
        self.assertEqual(metrics["month_metered_usd"], 20.0)

    def test_modal_python_from_cli_shebang(self):
        with tempfile.TemporaryDirectory() as d:
            cli_path = Path(d) / "modal"
            cli_path.write_text("#!/opt/venvs/modal/bin/python3\nimport modal\n")
            with mock.patch.object(modal_.shutil, "which", lambda name: str(cli_path)):
                self.assertEqual(modal_.modal_python(), "/opt/venvs/modal/bin/python3")
            cli_path.write_text("#!/usr/bin/env python3\n")
            with mock.patch.object(modal_.shutil, "which", lambda name: str(cli_path) if name == "modal" else "/usr/bin/python3"):
                self.assertEqual(modal_.modal_python(), "/usr/bin/python3")
        self.assertEqual(modal_.modal_python("/x/python"), "/x/python")

    def test_attribution_skips_known_and_free_apps_and_survives_a_failing_helper(self):
        sp = {"month": {"ap-a": ["a", 5.0], "ap-b": ["b", 0.0], "ap-c": ["c", 9.0]}, "through": None}
        seen = {}

        def fake_creators(acct, ids, python):
            seen["ids"] = ids
            raise RuntimeError("modal_api failed (1): NotFoundError")

        with mock.patch.object(modal_, "_creators", fake_creators), \
                mock.patch.object(modal_, "_volume_creators", lambda acct: {"alex-10001": 1}):
            out = modal_._attribution({}, {"known_creators": ["ap-a"]}, sp)
        self.assertEqual(seen["ids"], ["ap-c"])
        self.assertIn("NotFoundError", out["error"])
        self.assertEqual(out["volumes_by"], {"alex-10001": 1})


class Misc(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(util.parse_duration("24h"), 86400)
        self.assertEqual(util.parse_duration("1d12h"), 129600)
        self.assertEqual(util.parse_duration("90m"), 5400)
        with self.assertRaises(ValueError):
            util.parse_duration("soon")

    def test_tiers(self):
        self.assertEqual(rules.next_tier(500, 0), 500)
        self.assertEqual(rules.next_tier(500, 500), 1000)
        self.assertEqual(rules.next_tier(800, 1000), 1600)

    def test_childexec_keeps_only_allowlist_and_remap(self):
        payload = {"argv": [sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
                   "keep": ["PATH", "HOME"], "remap": {"LAMBDA_API_KEY": "WORK_LAMBDA_API_KEY"}}
        env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "WORK_LAMBDA_API_KEY": "fake-1", "OPENAI_API_KEY": "fake-2",
               "BWS_ACCESS_TOKEN": "fake-3", "CW_CHILD": base64.b64encode(json.dumps(payload).encode()).decode()}
        out = subprocess.run([sys.executable, str(config.PKG_DIR / "childexec.py")], env=env, capture_output=True,
                             text=True, check=True).stdout
        got = json.loads(out)
        self.assertEqual(got.get("LAMBDA_API_KEY"), "fake-1")
        for k in ("WORK_LAMBDA_API_KEY", "OPENAI_API_KEY", "BWS_ACCESS_TOKEN", "CW_CHILD"):
            self.assertNotIn(k, got)

    def test_vault_retry_matches_only_bws_server_errors(self):
        from cloud_watchdog.creds import VAULT_BUSY
        self.assertTrue(VAULT_BUSY.search("0: Received error message from server: [503 Service Unavailable]"))
        self.assertFalse(VAULT_BUSY.search("RuntimeError: HTTP 503 from https://cloud.lambdalabs.com/api/v1/instances"))
        self.assertFalse(VAULT_BUSY.search("modal app logs failed (1): 429"))

    def test_lambda_clean_drops_jupyter(self):
        from cloud_watchdog.providers import clean
        self.assertNotIn("jupyter_token", clean({"native_id": "x", "jupyter_token": "t", "jupyter_url": "u"}))

    def test_transcript_ranking_prefers_launch_tool_line(self):
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d) / "-work-repo"
            proj.mkdir()
            launcher, told = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
            (proj / ("%s.jsonl" % launcher)).write_text(json.dumps({
                "timestamp": "2026-09-23T13:17:00Z", "cwd": "/work/repo", "sessionId": launcher,
                "message": {"role": "assistant", "content": [{"type": "tool_use", "input": {
                    "command": "modal deploy judge.py  # llm-judge-oss-120b"}}]}}) + "\n")
            (proj / ("%s.jsonl" % told)).write_text(json.dumps({
                "timestamp": "2026-09-29T10:00:00Z", "cwd": "/work/other", "sessionId": told,
                "message": {"role": "user", "content": "[cloud-watchdog wd-1] Is this yours? llm-judge-oss-120b; "
                                                       "you ran modal deploy"}}) + "\n")
            cfg = config.load(Path(d) / "none.toml", Path(d) / "none.json")
            cfg["transcripts"]["roots"] = [d]
            cands = transcripts.search(["llm-judge-oss-120b"], 0, cfg)
            ranked = owner.rank(cands, util.parse_time("2026-09-23T13:18:00Z"), [], set())
            self.assertEqual(ranked[0]["session_id"], launcher)
            self.assertTrue(ranked[0]["launch_hit"])
            self.assertEqual([c for c in ranked if c["session_id"] == told], [])  # marker lines are ignored


if __name__ == "__main__":
    unittest.main()
