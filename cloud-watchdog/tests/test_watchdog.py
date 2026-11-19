"""Unit tests: rules, ladder, policy, registration/ack maths, modes, transcripts, credential isolation,
configuration.

No network, no provider, no mail: collectors are fed synthetic results, and agent-send, agent-mail, provider ops (stop,
the live crash re-check), the modal CLI, provider HTTP APIs, ssh, bws, claude -p, systemctl and journalctl are replaced
with fakes or with stub programs first on the PATH. HOME is a temp dir, so the owner search reads no real transcript.
"""
import base64
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_watchdog import (check, cli, collect, config, creds, digest, gather, investigator, ladder,  # noqa: E402
                            messages, modes, notify, observe, owner, policy, rules, spend, state, transcripts, util)
from cloud_watchdog import providers  # noqa: E402
from cloud_watchdog.providers import hf, lambda_, modal_, openrouter, slurm  # noqa: E402

T0 = 1_790_000_000.0
OWNER = "11111111-2222-3333-4444-555555555555"

# The configuration a user would write in ~/.config/cloud-watchdog/config.toml (the defaults list no accounts).
TEST_CONFIG = """
[identity]
owner_modal_users = ["alex-10001", "alexw2"]
owner_lambda_ssh_keys = ["alex-key", "alex-desktop"]
modal_shared_apps = ["inspect_modal_sandbox"]

[accounts."modal:main"]
provider = "modal"
secrets = "bws"
env = { MODAL_TOKEN_ID = "MAIN_MODAL_TOKEN_ID", MODAL_TOKEN_SECRET = "MAIN_MODAL_TOKEN_SECRET" }
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
month_cost_command = "month-cost --month"
slow_only = true

[accounts."slurm:cluster".partition_rates]
"gpu-paid-l40" = 1.00
"gpu-paid-h100" = 2.50

[accounts."hf:main"]
provider = "hf"

[accounts."deepinfra:main"]
provider = "deepinfra"

[accounts."openrouter:main"]
provider = "openrouter"
slow_only = true

[accounts."openrouter:work"]
provider = "openrouter"
slow_only = true

[rules.account_spend]
"modal:main" = 1000
"modal:team" = 50
"openrouter:main" = 50
"openrouter:work" = 50
"""
MAILROOM = {"owner_name": "Alex", "owner_email": "owner@example.com", "allowlist": ["owner@example.com"]}


def write_config(d, text=TEST_CONFIG, mailroom=MAILROOM):
    """Write config.toml and the mailroom's config.json into `d`; returns their paths."""
    d = Path(d)
    (d / "config.toml").write_text(text)
    (d / "mailroom.json").write_text(json.dumps(mailroom))
    return d / "config.toml", d / "mailroom.json"


def test_config(d):
    """The merged config of TEST_CONFIG, written into `d`."""
    return config.load(*write_config(d))


def script(bin_dir, name, body):
    """Write the program `name` into bin_dir: a /bin/sh script that runs `body`."""
    path = Path(bin_dir) / name
    path.write_text("#!/bin/sh\n%s\n" % body)
    path.chmod(0o755)


def stub(bin_dir, name, out, code=0, err=""):
    """Write the program `name` into bin_dir: it prints `out`, prints `err` on stderr and exits with `code`."""
    script(bin_dir, name, "printf '%%s' %s\nprintf '%%s' %s >&2\nexit %d" % (shlex.quote(out), shlex.quote(err), code))


class Outside:
    """What the real notify.Notifier runs, recorded instead of run: agent-send would message a real session and
    agent-mail would send real email. `run` stands in for util.run; Base.fake_op records the provider stops."""

    def __init__(self):
        self.pings, self.emails, self.stops = [], [], []
        self.ping_ok, self.email_ok = True, True

    def run(self, cmd, env=None, timeout=120, cwd=None, input_text=None):
        if cmd[0] == "agent-send":   # ... --offline resume|fail SESSION TEXT
            self.pings.append((cmd[-2], cmd[cmd.index("--offline") + 1] == "resume", cmd[-1]))
            return (0, "", "") if self.ping_ok else (1, "", "agent-send: no session %s" % cmd[-2])
        assert cmd[:2] == ["agent-mail", "send"], cmd
        self.emails.append((cmd[cmd.index("--subject") + 1], Path(cmd[cmd.index("--body-file") + 1]).read_text()))
        return (0, "", "") if self.email_ok else (1, "", "agent-mail: SMTP unavailable")


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


# The owner search drops terms under 6 characters, so lambda_box's own name, "box", is never searched for.
SEARCHABLE_BOX = "sweep-trainer"


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
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.cfg_path, self.mailroom_path = write_config(d)
        self.cfg = config.load(self.cfg_path, self.mailroom_path)
        # claude -p is a paid model call: the investigator is off, except in the tests that fake util.run for it.
        self.cfg["investigator"]["enabled"] = False
        self.db = state.DB(":memory:")
        self.n = Outside()   # what the notifier of each tick sent and stopped
        self.book = {OWNER: {"id": OWNER, "name": "eval session", "cli": "claude", "live": True}}
        self.bin = d / "bin"   # stub programs, first on the PATH
        self.bin.mkdir()
        # HOME is the temp dir, so the owner search's roots (~/.claude/projects, ~/.codex/sessions, ~/code) hold only
        # the transcripts a test writes, and no git runs over real repos. The notifier writes its outbox under
        # STATE_DIR. Local times (the digest hour) are in the system's time zone: the tests' times are Chicago times.
        self.addCleanup(time.tzset)
        for p in (mock.patch.object(config, "STATE_DIR", d), mock.patch.object(config, "MAILROOM_CONFIG", self.mailroom_path),
                  mock.patch.dict(os.environ, TZ="America/Chicago", HOME=str(d),
                                  PATH="%s:%s" % (self.bin, os.environ["PATH"]))):
            p.start()
            self.addCleanup(p.stop)
        time.tzset()
        self.address_book()
        self.live_crash = True
        self.crash_check_error = None

    def address_book(self, error=None):
        """(Re)write the agent-send that owner.sessions runs, as the real one would list the live sessions on this
        machine: its `--list --json` prints self.book, or with `error` fails with that message. Call it again after
        changing self.book."""
        if error is None:
            stub(self.bin, "agent-send", json.dumps(list(self.book.values())))
        else:
            stub(self.bin, "agent-send", "", 1, error)

    def register(self, pattern):
        """OWNER registers `pattern` with no expected end, as `cloud-watch register` does: the next ingest of a
        resource it names makes OWNER its owner."""
        self.db.add_registration(pattern, self.owner_session, None, None, "sweep", T0)

    def launch_box_transcript(self, session, box):
        """`session` launched this Lambda box through Lambda's launch API: its transcript."""
        self.launch_transcript(session, "curl -X POST $LAMBDA_API/instance-operations/launch -d name=%s" % box["name"], box["launched_at"])

    def launch_transcript(self, session, command, at):
        """Write the Claude transcript of `session`, in which it ran `command` at `at`, under ~/.claude/projects (a
        transcript root; HOME is the temp dir): what the owner search looks for."""
        d = Path(os.environ["HOME"]) / ".claude" / "projects" / "-work-repo"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%s.jsonl" % session)).write_text(json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)), "cwd": "/work/repo",
            "sessionId": session, "message": {"role": "assistant", "content": [
                {"type": "tool_use", "input": {"command": command}}]}}) + "\n")

    def fake_op(self, acct, name, opts=None, native_id=None):
        """gather.op runs a provider op in a credentialed child: a stop would stop a real resource, and the live crash
        re-check reads a real app's logs."""
        if name == "stop":
            self.n.stops.append(native_id)
            return True, {"ok": True, "message": "stopped"}
        assert name == "crash_check"
        if self.crash_check_error:
            return False, self.crash_check_error
        return True, {"failed": 4 if self.live_crash else 0, "seen": 4, "window_min": 120}

    def ingest(self, account, resources, now):
        observe.ingest(self.db, self.cfg, account, {"resources": resources, "metrics": {}}, now)

    def notifier(self, now, shadow=False, cfg=None):
        return notify.Notifier(self.db, cfg or self.cfg, shadow=shadow, now=now, run=self.n.run, op=self.fake_op)

    def tick(self, now, live=True):
        """One pass of the ladder. `live` False: the investigator is not run; the notifier still sends, so that
        tests can read what it would have sent."""
        ladder.Ladder(self.db, self.cfg, now, self.notifier(now), live=live, sessions=owner.sessions,
                      resolve_owner=owner.resolve, run_investigator=investigator.run, op=self.fake_op).process()

    def res(self, key):
        return self.db.resource(key)

    @property
    def owner_session(self):
        """OWNER as cli.caller describes the session running a command."""
        return {"session": OWNER, "cli": "claude", "name": "eval session", "label": "eval session"}


class CrashLoop(Base):
    def test_live_owner_pinged_then_auto_stopped(self):
        k = "modal:main:ap-1"
        self.register(k)
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
        inc = self.db.open_incident(k)
        self.assertEqual((inc["state"], inc["ping_count"], inc["deadline_at"]), ("asked", 1, T0 + 600 + 15 * 60))
        self.tick(T0 + 600 + 16 * 60)
        self.assertEqual(self.n.stops, ["ap-1"])
        self.assertEqual([tuple(a) for a in self.db.q("select key, actor, ok from actions where action='stop'")],
                         [(k, "watchdog", 1)])
        self.assertTrue(any("Stopped crash-looping" in s for s, _ in self.n.emails))
        self.assertIsNone(self.db.open_incident(k))

    def skips(self):
        """The ladder's auto-stop-skip rows: (reason, ok)."""
        return [tuple(a) for a in self.db.q("select detail, ok from actions where action='auto-stop-skip' order by id")]

    def test_no_stop_when_fresh_check_disagrees(self):
        k = "modal:main:ap-1"
        self.register(k)
        self.ingest("modal:main", [modal_app(failed=3, seen=3)], T0)
        self.ingest("modal:main", [modal_app(failed=3, seen=3)], T0 + 600)
        self.tick(T0 + 600)
        self.live_crash = False
        self.tick(T0 + 600 + 16 * 60)
        self.assertEqual(self.n.stops, [])
        self.assertTrue(any("owner not reached" in s for s, _ in self.n.emails))
        self.assertEqual(self.skips(), [("a fresh log check does not confirm the crash loop", 1)])
        inc = self.db.open_incident(k)
        self.assertEqual((inc["state"], inc["ping_count"], inc["email_count"]), ("escalated", 1, 1))

    def test_someone_elses_app_never_stopped_or_pinged(self):
        k = "modal:main:ap-2"
        for t in (T0, T0 + 600, T0 + 1200, T0 + 3 * 3600):
            self.ingest("modal:main", [modal_app("ap-2", deployed_by="kim-7305", failed=5, seen=5)], t)
            self.tick(t)
        self.assertEqual(self.res(k)["ownership"], "foreign")
        self.assertEqual(self.n.pings, [])
        self.assertEqual(self.n.stops, [])
        foreign = [s for s, _ in self.n.emails if "Someone else's app" in s]
        self.assertEqual(len(foreign), 1)  # once per 24 h
        self.assertEqual(self.db.q("select * from incidents"), [])   # no owner ladder for someone else's app
        self.assertEqual(self.db.meta("foreign_crash:" + k), T0 + 600)   # emailed when the crash loop was confirmed

    def test_unknown_deployer_is_not_auto_stopped(self):
        k = "modal:main:ap-3"
        app = modal_app("ap-3", "judge-sweep", deployed_by=None, failed=4, seen=4)
        self.launch_transcript(OWNER, "modal deploy judge-sweep.py", app["launched_at"])
        for t in (T0, T0 + 600):
            self.ingest("modal:main", [app], t)
        self.tick(T0 + 600)
        r = self.res(k)   # the transcript search found the owner; Modal names nobody, so it stays of unknown ownership
        self.assertEqual((r["ownership"], r["owner_session"], r["owner_source"]), ("unknown", OWNER, "transcript"))
        self.tick(T0 + 600 + 16 * 60)
        self.assertEqual(self.n.stops, [])
        self.assertIn("I will ask Alex", self.n.pings[0][2])
        self.assertEqual(self.skips(), [("deployed or created by an unknown user, not by %s"
                                         % "/".join(self.cfg["identity"]["owner_modal_users"]), 1)])
        self.assertEqual(self.db.open_incident(k)["state"], "escalated")

    def test_ack_given_during_crash_loop_blocks_auto_stop(self):
        k = "modal:main:ap-1"
        for t in (T0, T0 + 600):
            self.ingest("modal:main", [modal_app(failed=4, seen=4)], t)
        self.db.update_resource(k, suppress_until=T0 + 86400, suppress_health="crash_loop", ack_x_usd=0)
        self.tick(T0 + 600)
        self.tick(T0 + 2000)
        self.assertEqual(self.n.pings, [])
        self.assertEqual(self.n.stops, [])
        self.assertEqual(self.db.q("select * from incidents"), [])   # the ack silenced the rule: no incident at all

    def test_crash_after_healthy_ack_is_reasked(self):
        k = "modal:main:ap-1"
        self.register(k)
        self.ingest("modal:main", [modal_app()], T0)
        self.db.update_resource(k, suppress_until=T0 + 86400, suppress_health="ok", ack_x_usd=0)
        for t in (T0 + 600, T0 + 1200):
            self.ingest("modal:main", [modal_app(failed=4, seen=4)], t)
        self.tick(T0 + 1200)
        self.assertEqual(len(self.n.pings), 1)
        inc = self.db.open_incident(k)
        self.assertEqual((inc["rules"], inc["state"], inc["ping_count"]), ('["crash_loop"]', "asked", 1))

    def test_ping_promises_a_stop_only_when_the_watchdog_may_stop_it(self):
        # Registered, so ours, but Modal names nobody as its deployer or creator, so the watchdog may not stop it.
        k = "modal:main:ap-3"
        self.register(k)
        for t in (T0, T0 + 600):
            self.ingest("modal:main", [modal_app("ap-3", deployed_by=None, failed=4, seen=4)], t)
        self.assertEqual(self.res(k)["ownership"], "ours")
        self.tick(T0 + 600)
        self.assertIn("If nobody answers in 15 min I will ask Alex.", self.n.pings[0][2])
        self.tick(T0 + 600 + 16 * 60)
        self.assertEqual(self.n.stops, [])
        self.assertEqual(self.skips(), [("deployed or created by an unknown user, not by %s"
                                         % "/".join(self.cfg["identity"]["owner_modal_users"]), 1)])
        self.assertEqual(self.db.open_incident(k)["state"], "escalated")

    def test_failed_fresh_crash_check_is_logged_as_a_failure(self):
        k = "modal:main:ap-1"
        self.register(k)
        for t in (T0, T0 + 600):
            self.ingest("modal:main", [modal_app(failed=4, seen=4)], t)
        self.tick(T0 + 600)
        self.crash_check_error = "child exit 1: modal app logs failed (1): 503"
        self.tick(T0 + 600 + 16 * 60)
        self.assertEqual(self.n.stops, [])
        self.assertEqual(self.skips(), [("fresh crash check failed: child exit 1: modal app logs failed (1): 503", 0)])
        self.assertTrue(any("owner not reached" in s for s, _ in self.n.emails))
        inc = self.db.open_incident(k)
        self.assertEqual((inc["state"], inc["ping_count"], inc["email_count"]), ("escalated", 1, 1))

    def test_failing_crash_check_is_named_in_the_digest_once_a_day(self):
        app = modal_app() | {"health_detail": {"error": "modal ['app', 'logs'] failed (1): 503 Service Unavailable"}}
        for t in (T0, T0 + 600):
            self.ingest("modal:main", [app], t)
        self.assertEqual([tuple(i) for i in self.db.q("select kind, key, text from digest_items")],
                         [("crash_check_error", "modal:main:ap-1",
                           "crash check failing for judge: modal ['app', 'logs'] failed (1): 503 Service Unavailable")])

    def test_failed_someone_elses_crash_email_is_sent_again_next_check(self):
        for t, sent in ((T0, True), (T0 + 600, False), (T0 + 1200, True), (T0 + 1800, True)):
            self.n.email_ok = sent
            self.ingest("modal:main", [modal_app("ap-2", deployed_by="kim-7305", failed=5, seen=5)], t)
            self.tick(t)
        foreign = [s for s, _ in self.n.emails if "Someone else's app" in s]
        self.assertEqual(len(foreign), 2)   # failed at +10 min, sent at +20 min, then quiet for a day

    def test_someone_elses_crash_email_repeats_after_reemail_hours(self):
        self.cfg["ladder"]["reemail_hours"] = 2
        for t in (T0, T0 + 600, T0 + 600 + 3600, T0 + 600 + 2 * 3600):   # a crash loop from the second check on
            self.ingest("modal:main", [modal_app("ap-2", deployed_by="kim-7305", failed=5, seen=5)], t)
            self.tick(t)
        self.assertEqual(len([s for s, _ in self.n.emails if "Someone else's app" in s]), 2)


class OwnerPaths(Base):
    def test_exited_owner_resumed_and_human_emailed_together(self):
        self.book[OWNER]["live"] = False
        self.address_book()
        k = "lambda:main:i-1"
        self.register(k)
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        self.tick(T0)
        self.assertEqual(len(self.n.pings), 1)
        self.assertTrue(self.n.pings[0][1])  # resume=True
        self.assertIn("ONE question", self.n.pings[0][2])
        self.assertEqual(len(self.n.emails), 1)
        self.tick(T0 + 31 * 60)
        self.assertEqual(len(self.n.emails), 1)  # no second email after the deadline
        inc = self.db.open_incident(k)
        self.assertEqual((inc["state"], inc["resumed_at"], inc["email_count"]), ("escalated", T0, 1))
        self.assertEqual(self.n.stops, [])  # never auto-terminates Lambda

    def test_live_owner_gets_second_ping_then_human(self):
        k = "lambda:main:i-1"
        self.register(k)
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        self.tick(T0)
        self.tick(T0 + 31 * 60)
        self.assertEqual(len(self.n.pings), 2)
        self.assertEqual(self.n.emails, [])
        inc = self.db.open_incident(k)
        self.assertEqual((inc["state"], inc["ping_count"], inc["email_count"]), ("asked", 2, 0))
        self.tick(T0 + 62 * 60)
        self.assertEqual(len(self.n.emails), 1)
        self.assertIn("got no answer", self.n.emails[0][1])
        inc = self.db.open_incident(k)
        self.assertEqual((inc["state"], inc["ping_count"], inc["email_count"]), ("escalated", 2, 1))

    def test_live_owner_gets_max_pings_messages(self):
        k = "lambda:main:i-1"
        self.cfg["ladder"]["max_pings"] = 3
        self.register(k)
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        for m in (0, 31, 62):
            self.tick(T0 + m * 60)
        self.assertEqual((len(self.n.pings), self.n.emails), (3, []))
        self.assertEqual(self.db.open_incident(k)["ping_count"], 3)
        self.tick(T0 + 93 * 60)
        self.assertEqual(len(self.n.emails), 1)

    def test_failed_ping_goes_to_human_at_once(self):
        k = "lambda:main:i-1"
        self.n.ping_ok = False
        self.register(k)
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        self.tick(T0)
        self.assertEqual(len(self.n.emails), 1)
        self.assertIn("I could not message **eval session**: exit 1 agent-send: no session %s." % OWNER,
                      self.n.emails[0][1])
        self.assertEqual(self.db.open_incident(k)["state"], "escalated")

    def test_failed_email_is_sent_again_next_check(self):
        k = "lambda:main:i-1"
        self.book[OWNER]["live"] = False
        self.address_book()
        self.register(k)
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        self.n.email_ok = False
        self.tick(T0)
        inc = self.db.open_incident(k)
        self.assertEqual((inc["state"], inc["email_count"], inc["last_email_at"]), ("open", 0, None))
        self.cfg["max_emails_per_day"] = 0   # over the daily cap: in the digest, which counts as told
        self.tick(T0 + 600)
        self.assertEqual(len(self.n.emails), 1)   # the failed send; the second email went into the digest
        self.assertEqual([i["kind"] for i in self.db.q("select kind from digest_items")], ["capped_email"])
        self.assertEqual([resume for _, resume, _ in self.n.pings], [True])   # the owner was resumed once only
        inc = self.db.open_incident(k)
        self.assertEqual((inc["state"], inc["email_count"]), ("asked", 1))
        self.tick(T0 + 1200)
        self.assertEqual(self.db.one("select count(*) n from actions where action='email'")["n"], 2)

    def test_unreadable_address_book_goes_to_human_naming_the_cause(self):
        old = lambda_box("i-1", launched=T0 - 25 * 3600)
        self.register("lambda:main:i-*")
        self.ingest("lambda:main", [old], T0)
        self.tick(T0)   # the address book works: the first message goes out
        self.address_book(error="agent-send: mailroom socket refused")
        self.ingest("lambda:main", [old, lambda_box("i-2", launched=T0 - 25 * 3600)], T0 + 31 * 60)
        self.tick(T0 + 31 * 60)
        self.assertEqual(len(self.n.pings), 1)   # no second message and no resume: nobody can tell if it is live
        cause = ("I could not check whether **eval session** is live: agent-send --list exited 1: "
                 "agent-send: mailroom socket refused.")
        self.assertEqual(len(self.n.emails), 2)
        self.assertTrue(all(cause in md for _, md in self.n.emails))
        self.assertTrue(any("got no answer" in md for _, md in self.n.emails))   # i-1, asked once already
        self.assertEqual(self.db.one("select count(*) n from actions where action='error'")["n"], 1)

    def test_investigator_not_run_says_why(self):
        self.cfg["investigator"]["enabled"] = False   # and no transcript mentions the box: the owner is unknown
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        self.tick(T0)
        self.assertIn("investigator: investigator disabled", self.n.emails[0][1])
        logged = self.db.q("select detail, ok, cost_usd from actions where action='investigate'")
        self.assertEqual([tuple(a) for a in logged], [("investigator disabled", 0, None)])

    def test_investigator_run_of_unknown_cost_counts_as_its_cap(self):
        self.cfg["investigator"]["enabled"] = True   # util.run below stands in for claude -p
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        res, calls = self.res("lambda:main:i-1"), []

        def claude(cmd, env=None, timeout=None, cwd=None, input_text=None):   # claude -p is a paid model call
            calls.append(cmd[0])
            return 124, "half an answer", "timed out after 600s"

        with mock.patch.object(util, "run", claude):
            got = [investigator.run(self.db, self.cfg, res, [], [], T0) for _ in range(6)]
        self.assertEqual(len(calls), 5)   # $1 a run against $5 a day
        self.assertEqual(got[-1], (None, "daily model budget used up"))
        self.assertEqual(self.db.model_spend_today(T0), 5.0)
        detail = json.loads(self.db.one("select detail from actions where action='investigate' order by id")["detail"])
        self.assertEqual((detail["stdout"], detail["cost"]),
                         ("half an answer", "unknown, counted as the $1.00 per-run cap"))

    def test_investigator_reads_the_configured_transcript_and_repo_roots(self):
        self.cfg["investigator"]["enabled"] = True   # util.run below stands in for claude -p
        self.cfg["transcripts"]["roots"] = ["/srv/transcripts/claude", "/srv/transcripts/codex"]
        self.cfg["repo_roots"] = ["/srv/code"]
        self.ingest("lambda:main", [lambda_box()], T0)
        calls = []

        def claude(cmd, env=None, timeout=None, cwd=None, input_text=None):   # claude -p is a paid model call
            calls.append(cmd)
            return 0, json.dumps({"total_cost_usd": 0.05, "result": "{}"}), ""

        with mock.patch.object(util, "run", claude):
            investigator.run(self.db, self.cfg, self.res("lambda:main:i-1"), [], [], T0)
        cmd = calls[0]
        self.assertEqual([cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-dir"],
                         ["/srv/transcripts/claude", "/srv/transcripts/codex", "/srv/code"])
        self.assertIn("Grep/Read over the transcripts (.jsonl) under /srv/transcripts/claude and "
                      "/srv/transcripts/codex (look for the launch command), and `git -C <repo> log` in the repos "
                      "under /srv/code.", " ".join(cmd[-1].split()))

    def test_unknown_owner_goes_to_human(self):
        k = "lambda:main:i-1"
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)   # no transcript mentions it
        self.tick(T0, live=False)  # shadow: the investigator is not run
        self.assertEqual(self.n.pings, [])
        self.assertEqual(len(self.n.emails), 1)
        self.assertIn("could not tell", self.n.emails[0][1])
        self.assertIn("(no transcript mentions it)", self.n.emails[0][1])
        inc = self.db.open_incident(k)
        self.assertEqual((inc["state"], inc["ping_count"], inc["email_count"]), ("escalated", 0, 1))
        self.assertEqual([tuple(a) for a in self.db.q("select detail, shadow from actions where action='investigate'")],
                         [("would run the investigator (owner unknown)", 1)])

    def test_foreign_lambda_is_ignored(self):
        self.ingest("lambda:work", [lambda_box(keys=("lee-key",), launched=T0 - 70 * 3600)], T0)
        self.tick(T0)
        self.assertEqual(self.res("lambda:work:i-1")["ownership"], "foreign")
        self.assertEqual((self.n.pings, self.n.emails), ([], []))
        self.assertEqual(self.db.q("select * from incidents"), [])

    def test_incident_quiet_for_an_hour_of_check_time_closes(self):
        k = "lambda:main:i-1"
        self.register(k)
        for t in (T0, T0 + 600):
            self.ingest("lambda:main", [lambda_box(rate=30)], t)   # high_burn
            self.tick(t)
        self.assertEqual(self.db.open_incident(k)["updated_at"], T0 + 600)
        t = T0 + 600 + 3601
        self.ingest("lambda:main", [lambda_box(rate=1.29)], t)   # no rule fires, still billing
        self.tick(t)
        self.assertIsNone(self.db.open_incident(k))

    def test_owner_search_runs_again_only_after_its_max_age_and_is_logged(self):
        k = "lambda:main:i-1"
        box = lambda_box() | {"name": SEARCHABLE_BOX}
        self.ingest("lambda:main", [box], T0)
        for t in (T0, T0 + 1799, T0 + 1800):
            res, cands = owner.refresh(self.db, self.cfg, self.res(k), t, self.book, 1800, owner.resolve)
            self.assertEqual((res["owner_checked_at"], cands), (T0 + 1800 if t == T0 + 1800 else T0, None))
        self.assertEqual([tuple(a) for a in self.db.q("select ts, detail from actions where action='owner'")],
                         [(T0, "no transcript mentions it"), (T0 + 1800, "no transcript mentions it")])
        self.launch_box_transcript(OWNER, box)
        res, cands = owner.refresh(self.db, self.cfg, self.res(k), T0 + 3600, self.book, 1800, owner.resolve)
        self.assertEqual((res["owner_session"], res["owner_name"], res["owner_source"], res["owner_confidence"]),
                         (OWNER, "eval session", "transcript", "high"))
        self.assertEqual([c["session_id"] for c in cands], [OWNER])
        self.assertTrue(res["owner_evidence"].startswith("launched it %s in /work/repo: " % util.iso(T0 - 3600)),
                        res["owner_evidence"])
        res, _ = owner.refresh(self.db, self.cfg, self.res(k), T0 + 7200, self.book, 1800, None)   # has an owner
        self.assertEqual(res["owner_checked_at"], T0 + 3600)

    def test_registration_suppresses_until_expected_end(self):
        self.db.add_registration("lambda:main:i-*", self.owner_session, "48h", None, "sweep", T0)
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        r = self.res("lambda:main:i-1")
        self.assertEqual(r["owner_source"], "registered")
        self.tick(T0)
        self.assertEqual(self.n.pings, [])
        self.assertEqual(self.db.q("select * from incidents"), [])   # acked until launch + 48 h: no rule fires
        self.tick(T0 + 24 * 3600)  # past launch + 48 h
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0 + 24 * 3600)
        self.tick(T0 + 24 * 3600)
        self.assertEqual(len(self.n.pings), 1)

    def test_registration_that_closes_an_incident_logs_it(self):
        k = "lambda:main:i-1"
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0)
        self.tick(T0)
        inc = self.db.open_incident(k)
        self.db.add_registration("lambda:main:i-*", self.owner_session, "48h", None, "sweep", T0 + 60)
        self.ingest("lambda:main", [lambda_box(launched=T0 - 25 * 3600)], T0 + 600)
        self.assertIsNone(self.db.open_incident(k))
        resolved = self.db.q("select incident_id, detail from actions where action='resolve'")
        self.assertEqual([tuple(a) for a in resolved], [(inc["id"], "registered")])


class CostRules(Base):
    def test_unacked_tiers_email_once_each(self):
        k = "lambda:main:i-1"
        self.register(k)
        for hours in (100, 400, 420, 800):  # $129, $516, $542, $1032 at $1.29/h
            t = T0 + hours * 3600
            self.ingest("lambda:main", [lambda_box(launched=T0, rate=1.29) | {"cost_usd": round(1.29 * hours, 2)}], t)
            self.tick(t)
        cost_mails = [s for s, _ in self.n.emails if "has cost" in s]
        self.assertEqual(len(cost_mails), 2)  # $500 tier, then $1,000 tier
        self.assertEqual(self.res(k)["cost_email_tier"], 1000)

    def test_failed_cost_email_is_sent_again_next_check(self):
        k = "lambda:main:i-1"
        self.register(k)
        self.ingest("lambda:main", [lambda_box(launched=T0) | {"cost_usd": 516.0}], T0)
        self.n.email_ok = False
        self.tick(T0)
        self.assertEqual(self.res(k)["cost_email_tier"], 0)
        self.n.email_ok = True
        self.tick(T0 + 600)
        self.assertEqual(self.res(k)["cost_email_tier"], 500)
        self.tick(T0 + 1200)
        self.assertEqual(len([s for s, _ in self.n.emails if "has cost" in s]), 2)

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
        self.ingest("lambda:main", [lambda_box() | {"idle": None, "idle_detail": {   # the probe failed: no change
            "reachable": False, "error": "ssh failed (exit 255): Connection timed out"}}], T0 + 1800)
        self.assertEqual(self.res(k)["health"], "idle")
        self.ingest("lambda:main", [lambda_box(idle=False)], T0 + 2400)
        self.assertEqual(self.res(k)["health"], "ok")


    def test_failing_idle_check_is_named_in_the_digest_once_a_day(self):
        box = lambda_box() | {"idle": None, "idle_detail": {"reachable": True,   # as lambda_.collect sends a failed probe
                                                            "error": "remote command failed (exit 127): bc: not found"}}
        for t in (T0, T0 + 600):
            self.ingest("lambda:main", [box], t)
        self.assertEqual([tuple(i) for i in self.db.q("select kind, key, text from digest_items")],
                         [("idle_check_error", "lambda:main:i-1",
                           "idle check failing for box: remote command failed (exit 127): bc: not found")])


class Derive(Base):
    """observe.derive, the pure part of ingest: a resource's new row fields from what was collected and its row."""

    def test_cost_basis_is_set_when_a_stretch_starts_and_carried_over_while_it_bills(self):
        app = modal_app(launched=T0 - 2 * 86400, cost=40.0)   # Modal counts from the stretch; launched 2 days ago
        fields, _ = observe.derive(self.cfg, "modal:main", app, None, T0)
        self.assertEqual((fields["billing_now"], fields["active_since"], fields["cost_basis"]), (1, T0, 40.0))
        row = dict(fields)
        fields, _ = observe.derive(self.cfg, "modal:main", app | {"cost_usd": 55.0}, row, T0 + 600)
        self.assertEqual([k for k in ("active_since", "cost_basis") if k in fields], [])   # both carry over
        fields, _ = observe.derive(self.cfg, "modal:main", app | {"tasks": 0, "containers": 0}, row, T0 + 1200)
        self.assertEqual((fields["billing_now"], fields["active_since"]), (0, None))
        fresh = modal_app(launched=T0 - 3600, cost=40.0)   # launched under 3 h ago: all of its cost counts
        fields, _ = observe.derive(self.cfg, "modal:main", fresh, None, T0)
        self.assertEqual((fields["active_since"], fields["cost_basis"]), (T0 - 3600, 0.0))
        fields, _ = observe.derive(self.cfg, "lambda:main", lambda_box(launched=T0 - 2 * 86400), None, T0)
        self.assertEqual((fields["active_since"], fields["cost_basis"]), (T0 - 2 * 86400, 0.0))   # from launch

    def test_a_failed_probe_keeps_its_streak(self):
        row = {"crash_streak": 1, "idle_streak": 2, "health": "unknown", "ownership": "ours", "billing_now": 1,
               "active_since": T0}
        app = modal_app() | {"health_detail": {"error": "logs failed"}}
        fields, failures = observe.derive(self.cfg, "modal:main", app, row, T0)
        self.assertEqual([k for k in ("crash_streak", "health") if k in fields], [])   # health as it was, too
        self.assertEqual(failures, [("crash_check_error", "crash check failing for judge: logs failed")])
        box = lambda_box() | {"idle": None, "idle_detail": {"error": "ssh failed"}}
        for streak, health in ((2, "ok"), (3, "idle")):   # health follows the kept streak
            fields, failures = observe.derive(self.cfg, "lambda:main", box, dict(row, idle_streak=streak), T0)
            self.assertEqual(("idle_streak" in fields, fields["health"]), (False, health))
        self.assertEqual(failures, [("idle_check_error", "idle check failing for box: ssh failed")])


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
        theirs = modal_app("ap-run2", "team-run", deployed_by=None) | {"created_by": "kim-7305", "state": "ephemeral"}
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


class ProviderFacts(Base):
    def test_ping_close_line_says_what_the_providers_stop_does(self):
        self.ingest("lambda:main", [lambda_box()], T0)
        self.ingest("modal:main", [modal_app()], T0)
        self.ingest("deepinfra:main", [{"native_id": "d-1", "name": "llama", "kind": "deployment",
                                            "state": "running", "launched_at": T0 - 3600}], T0)
        close = {}
        for k in ("lambda:main:i-1", "modal:main:ap-1", "deepinfra:main:d-1"):
            inc = self.db.new_incident(k, ["cost"], "normal", T0)
            text = messages.ping(inc, self.res(k), [{"text": "$200 spent"}], T0, 30, False)
            close[k] = next(line.strip() for line in text.splitlines() if "close" in line)
        self.assertEqual(close, {
            "lambda:main:i-1": 'cloud-watch close wd-1 --reason "..."               # stop it now: terminate the '
                                   'instance; its local disk is lost',
            "modal:main:ap-1": 'cloud-watch close wd-2 --reason "..."               # stop it now: `modal app stop`; '
                                'volumes, secrets and images are kept',
            "deepinfra:main:d-1": "(cloud-watch cannot close deepinfra resources: stop it in the provider's "
                                      "dashboard)"})

    def test_child_refuses_an_op_its_provider_does_not_offer(self):
        def child(op, provider):
            req = {"op": op, "acct": {"provider": provider}, "opts": {"window_min": 120}, "native_id": "x-1"}
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(req))), contextlib.redirect_stdout(out):
                collect.main()
            got = json.loads(out.getvalue())
            return got["ok"], got["error"].rsplit(" (", 1)[0]

        self.assertEqual(child("crash_check", "lambda"), (False, "ValueError: the lambda provider offers no crash_check"))
        self.assertEqual(child("stop", "openrouter"), (False, "ValueError: the openrouter provider offers no stop"))

    def test_every_provider_declares_the_facts_the_core_reads(self):
        for name in sorted({a["provider"] for a in self.cfg["accounts"].values()}):
            mod = providers.get(name)
            with self.subTest(provider=name):
                if mod.HAS_RESOURCES:
                    self.assertIn(mod.COUNTS_FROM_LAUNCH, (True, False))
                    self.assertIn("%s", mod.ID_TERM)
                    self.assertTrue(callable(mod.billing_now) and callable(mod.ownership))
                    self.assertIn(name, self.cfg["rules"]["long_running"]["hours"])
                if hasattr(mod, "stop"):
                    self.assertIn(mod.STOP_KEEPS_DATA, (True, False))
                    self.assertTrue(mod.STOP_EFFECT)

    def test_collect_options_come_from_the_provider(self):
        def opts(account):
            return check.collect_opts(self.db, self.cfg, account, T0, slow=True, inventory_only=False)

        common = {"now", "slow", "inventory_only"}
        self.assertEqual(set(opts("slurm:cluster")), common)
        self.assertEqual(opts("lambda:main"), {"now": T0, "slow": True, "inventory_only": False,
                                                   "identity": self.cfg["identity"],
                                                   "idle_gpu_max": self.cfg["rules"]["idle_gpu"]["gpu_util_max_pct"],
                                                   "idle_pattern": self.cfg["rules"]["idle_gpu"]["work_pattern"]})
        self.assertEqual(set(opts("modal:main")) - common,
                         {"crash_window_min", "watch", "known_history", "known_creator_of", "known_creators"})

    def test_owner_search_terms(self):
        self.ingest("lambda:main", [lambda_box() | {"name": "trainer"}], T0)
        self.ingest("slurm:cluster", [{"native_id": "4242", "name": "train", "kind": "job", "state": "RUNNING",
                                   "launched_at": T0 - 600}], T0)
        self.ingest("modal:main", [modal_app()], T0)
        self.assertEqual(owner.search_terms(self.res("lambda:main:i-1")), ["trainer", "i-1", "1.2.3.4"])
        self.assertEqual(owner.search_terms(self.res("slurm:cluster:4242")), ["train", "Submitted batch job 4242"])
        self.assertEqual(owner.search_terms(self.res("modal:main:ap-1")), ["judge", "ap-1"])

    def test_long_running_hours_per_provider(self):
        self.cfg["rules"]["long_running"]["hours"]["deepinfra"] = 6
        self.ingest("deepinfra:main", [{"native_id": "d-1", "name": "llama", "kind": "deployment",
                                            "state": "running", "launched_at": T0 - 7 * 3600}], T0)
        self.assertEqual([f["text"] for f in rules.evaluate(self.res("deepinfra:main:d-1"), self.cfg, T0)],
                         ["running for 7h 0m (threshold 6h)"])


class Modes(Base):
    def test_shadow_goes_live_and_resets_incidents(self):
        modes.set_mode(self.db, "shadow", T0, live_at=T0 + 7 * 86400, by="test")
        self.assertEqual(modes.effective(self.db, T0 + 86400), ("shadow", False))
        self.db.new_incident("x:y:z", ["cost"], "normal", T0)
        self.assertEqual(modes.effective(self.db, T0 + 7 * 86400 + 1), ("live", True))
        self.assertIsNone(self.db.open_incident("x:y:z"))
        self.assertEqual(modes.effective(self.db, T0 + 8 * 86400), ("live", False))

    def test_stay_in_shadow(self):
        modes.set_mode(self.db, "shadow", T0, live_at=None, by="test")
        self.assertEqual(modes.effective(self.db, T0 + 30 * 86400)[0], "shadow")


class Cli(Base):
    """`cloud-watch` commands run through cli.main, with the state, lock and config paths in the test's temp dir."""

    def setUp(self):
        super().setUp()
        d = Path(self.tmp.name)
        for p in (mock.patch.multiple(config, STATE_DIR=d, DB_PATH=d / "state.sqlite", LOCK_PATH=d / "check.lock",
                                      CONFIG_PATH=d / "config.toml"),
                  mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.db = state.DB(config.DB_PATH)

    def cw(self, *argv):
        """Run `cloud-watch ARGV`; returns (exit code, stdout, stderr)."""
        out, err, code = io.StringIO(), io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class Commands(Cli):
    @contextlib.contextmanager
    def agent_mail(self, code):
        """agent-mail sends real email, so util.run, which runs it for these commands, returns its exit code instead."""
        def run(cmd, env=None, timeout=120, cwd=None, input_text=None):
            self.assertEqual(cmd[:2], ["agent-mail", "send"])
            return code, "", "" if code == 0 else "agent-mail: SMTP unavailable"
        with mock.patch.object(util, "run", run):
            yield

    def test_ack_reports_the_threshold_past_tiers_already_emailed(self):
        k = "lambda:main:i-1"
        self.ingest("lambda:main", [lambda_box() | {"cost_usd": 1100.0}], T0)
        self.db.update_resource(k, cost_email_tier=1000)
        code, out, _ = self.cw("ack", k, "--keep", "24h", "--reason", "training", "--expect-cost", "300",
                               "--by", "human")
        self.assertEqual(code, 0)
        self.assertIn("Alex is emailed if it reaches $1,200.", out)

    def test_register_applies_only_the_new_registration(self):
        other = "99999999-0000-0000-0000-000000000009"
        self.book[other] = {"id": other, "name": "judge session", "cli": "claude", "live": True}
        self.address_book()
        self.ingest("modal:main", [modal_app("ap-9", "judge-x")], T0)
        self.ingest("lambda:main", [lambda_box()], T0)
        self.cw("register", "modal:main:judge-*", "--expect", "6h", "--session", other)
        judge = dict(self.res("modal:main:ap-9"))
        self.assertEqual(judge["owner_session"], other)
        code, out, _ = self.cw("register", "lambda:main:*", "--expect", "6h", "--session", OWNER)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "registered lambda:main:i-1 (already running) to this session")
        self.assertNotIn("modal:main:ap-9", out)
        self.assertEqual(dict(self.res("modal:main:ap-9")), judge)
        self.assertEqual(self.res("lambda:main:i-1")["owner_session"], OWNER)
        self.assertEqual([tuple(a) for a in self.db.q("select actor, detail from actions where action='register'")],
                         [("judge session", "modal:main:judge-* expect 6h: "),
                          ("eval session", "lambda:main:* expect 6h: ")])

    def test_heartbeat_alert_is_retried_until_it_is_sent(self):
        self.db.set_meta("last_ok_at", util.now() - 7200)
        with self.agent_mail(1):
            code, _, err = self.cw("heartbeat")
        self.assertEqual(code, 1)
        self.assertIn("not sent", err)
        self.assertFalse(self.db.meta("heartbeat_alerted"))
        with self.agent_mail(0):
            self.assertEqual(self.cw("heartbeat")[0], 0)
        self.assertTrue(self.db.meta("heartbeat_alerted"))

    def unit_failed(self):
        """systemctl and journalctl would read this host's units and journal: stubs first on the PATH answer that the
        check's unit failed with exit-code."""
        stub(self.bin, "systemctl", "exit-code\n")
        stub(self.bin, "journalctl", "cloud-watchdog.service: Failed with result 'exit-code'.\n")

    def test_failure_alert_is_retried_until_it_is_sent(self):
        self.unit_failed()
        with self.agent_mail(1):
            code, _, err = self.cw("alert-failure", "cloud-watchdog.service")
        self.assertEqual(code, 1)
        self.assertIn("not sent", err)
        self.assertIsNone(self.db.meta("failure_alerted_at"))
        with self.agent_mail(0):
            self.assertEqual(self.cw("alert-failure", "cloud-watchdog.service")[0], 0)
        self.assertIsNotNone(self.db.meta("failure_alerted_at"))
        emails = self.db.q("select detail from actions where action='email'")
        self.assertEqual([a["detail"].split(":")[0] for a in emails], ["[cloud-watchdog] Check failed (exit-code)"] * 2)

    def test_digest_send_failure_exits_nonzero(self):
        with self.agent_mail(1):
            code, out, err = self.cw("digest", "--send")
        self.assertEqual(code, 1)
        self.assertIn("not sent", err)

    def checks(self, times):
        """Run a check at each time, as `cloud-watch check --account none`: it collects no account, so it calls no
        provider. Returns the digests it sent, by time."""
        sent = {}
        for t in times:
            def run(cmd, env=None, timeout=120, cwd=None, input_text=None):   # agent-mail sends real email
                sent[t] = Path(cmd[cmd.index("--body-file") + 1]).read_text()
                return 0, "", ""
            # The check reads the wall clock (util.now): `t` stands in for it, so the digest hour and weekday are known.
            with mock.patch.object(util, "run", run), mock.patch.object(util, "now", lambda: t):
                check.run(self.cfg, only=["none"])
        return sent

    def test_storage_line_goes_out_with_each_monday_digest_once(self):
        monday = util.parse_time("2026-11-02T14:00:00Z")   # 08:00 in Chicago: the digest hour
        self.db.add_metric("modal:main", "month_volumes_usd", 212, monday - 900)
        times = [monday - 600, monday, monday + 600, monday + 86400, monday + 7 * 86400 - 600, monday + 7 * 86400]
        sent = self.checks(times)
        self.assertEqual(sorted(sent), [monday, monday + 86400, monday + 7 * 86400])
        line = "- modal:main volume storage $212 so far this month (threshold $100/month)"
        self.assertEqual([sent[t].count(line) for t in sorted(sent)], [1, 0, 1])

    def test_daily_spend_over_its_limit_is_a_digest_line_and_flags_the_spend(self):
        t = util.parse_time("2026-11-03T14:00:00Z")
        for account, name, value in (("modal:team", "day_spend_usd", 75.0), ("modal:team", "month_metered_usd", 900.0),
                                     ("slurm:cluster", "month_estimated_usd", 1234.5)):
            self.db.add_metric(account, name, value, t - 900)
        md = self.checks([t])[t]
        self.assertIn("- modal:team spent $75.00 in the last 24 h (threshold $50.00)", md)
        self.assertIn("- `modal:team`: last 24 h $75.00 ⚠ over $50.00, month metered $900\n", md)
        self.assertIn("- `slurm:cluster`: month estimated $1,234", md)

    def test_digest_lists_the_actions_since_the_last_digest_even_when_it_is_late(self):
        day, k = util.parse_time("2026-11-03T14:00:00Z"), "lambda:main:i-1"
        self.db.log("ping", "before the first digest", key=k, shadow=False, now=day - 3600)
        first = self.checks([day])[day]
        self.db.log("ping", "after the first digest", key=k, shadow=False, now=day + 3600)
        late = day + 86400 + 3 * 3600   # no check ran from 08:00 to 11:00
        second = self.checks([late])[late]
        self.assertIn("· %s · messaged owner: before the first digest" % k, first)
        self.assertNotIn("before the first digest", second)
        self.assertIn("· %s · messaged owner: after the first digest" % k, second)

    def test_digest_marks_failed_actions_and_lists_errors(self):
        k = "modal:main:ap-1"
        self.db.log("stop", "%s: 503 Service Unavailable" % k, key=k, ok=False, shadow=False, now=T0 - 600)
        self.db.log("ping", "to eval session: exit 0", key=k, shadow=False, now=T0 - 500)
        self.db.log("error", "ladder: KeyError: 'rate_usd_h'", key=k, ok=False, shadow=False, now=T0 - 300)
        md = digest.build(self.db, self.cfg, T0)[1]
        self.assertIn("· %s · FAILED: stopped: %s: 503 Service Unavailable\n" % (k, k), md)
        self.assertIn("· %s · messaged owner: to eval session: exit 0\n" % k, md)
        at = util.iso(T0 - 300, local=True)[11:16]
        self.assertIn("\n### Errors\n- %s · %s · ladder: KeyError: 'rate_usd_h'\n" % (at, k), md)

    def test_prune_runs_once_a_day_whatever_the_time_of_the_check(self):
        noon = util.parse_time("2026-11-03T12:00:00Z")

        def old_observations_after(t):
            self.db.x("insert into observations(key, ts) values('k', ?)", noon - 40 * 86400)
            self.checks([t])
            return self.db.one("select count(*) n from observations")["n"]

        self.assertEqual([old_observations_after(t) for t in (noon, noon + 600, noon + 86400)], [0, 1, 0])

    def test_quiet_live_day_skips_the_digest_and_says_so(self):
        modes.set_mode(self.db, "live", T0, by="test")
        day = util.parse_time("2026-11-03T14:00:00Z")
        self.assertEqual(self.checks([day]), {})   # no digest sent
        self.assertEqual([tuple(a) for a in self.db.q("select action, detail from actions where action='digest'")],
                         [("digest", "skipped: nothing to report")])

    def test_failure_alerts_inside_the_configured_gap_are_held_back_and_logged(self):
        config.CONFIG_PATH.write_text("failure_alert_every_hours = 3\n" + TEST_CONFIG)
        self.unit_failed()
        with self.agent_mail(0):
            for hours_ago in (2, 4):
                self.db.set_meta("failure_alerted_at", util.now() - hours_ago * 3600)
                self.assertEqual(self.cw("alert-failure", "cloud-watchdog.service")[0], 0)
        self.assertEqual(self.db.one("select count(*) n from actions where action='email'")["n"], 1)
        held = self.db.q("select detail from actions where action='failure-alert-skip'")
        self.assertEqual(len(held), 1)
        self.assertIn("cloud-watchdog.service failed again", held[0]["detail"])
        self.assertIn("held back a check-failure email", digest.build(self.db, self.cfg, util.now())[1])

    def test_heartbeat_staleness_comes_from_the_config(self):
        config.CONFIG_PATH.write_text("heartbeat_stale_min = 120\n" + TEST_CONFIG)
        with self.agent_mail(0):
            for minutes_ago in (90, 150):
                self.db.set_meta("last_ok_at", util.now() - minutes_ago * 60)
                self.cw("heartbeat")
                self.assertEqual(bool(self.db.meta("heartbeat_alerted")), minutes_ago > 120)

    def test_refused_ack_and_close_are_logged(self):
        other = "99999999-0000-0000-0000-000000000009"
        self.book[other] = {"id": other, "name": "judge session", "cli": "claude", "live": True}
        self.address_book()
        k = "lambda:main:i-1"
        self.register(k)
        self.ingest("lambda:main", [lambda_box()], T0)
        for command in (("ack", k, "--keep", "1h", "--reason", "mine"), ("close", k, "--reason", "done")):
            self.assertEqual(self.cw(*command, "--session", other)[0], 1)
        refused = self.db.q("select action, actor, ok, detail from actions where action!='register' order by id")
        self.assertEqual([tuple(a)[:3] for a in refused], [("ack", "judge session", 0), ("close", "judge session", 0)])
        self.assertTrue(all(a["detail"].startswith("refused: ") for a in refused), [a["detail"] for a in refused])

    def test_uninstall_fails_loudly_when_systemctl_fails(self):
        # systemctl would disable this machine's real timers, so util.run stands in for it.
        fail = lambda cmd, timeout=120, **kw: (1, "", "Failed to disable unit: Access denied")  # noqa: E731
        with mock.patch.object(util, "run", fail):
            code, out, err = self.cw("uninstall")
        self.assertEqual(code, 1)
        self.assertIn("Access denied", err)
        self.assertEqual(out, "")

    def test_mode_records_one_actor(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = OWNER
        self.cw("mode", "live")
        self.assertEqual(modes.get(self.db)["set_by"], OWNER)
        self.assertEqual(self.db.one("select actor from actions where action='mode'")["actor"], OWNER)

    def test_check_logs_the_config_when_it_changes(self):
        modes.set_mode(self.db, "off", T0, by="test")   # an off check records the config and does nothing else
        cfg = config.load()
        changed = dict(cfg, max_emails_per_day=3)
        for c in (cfg, cfg, changed):
            check.run(cfg=c)
        logged = [json.loads(a["detail"])
                  for a in self.db.q("select detail from actions where action='config' order by id")]
        self.assertEqual(logged, [cfg, changed])
        events = [json.loads(line) for line in (Path(self.tmp.name) / "events.jsonl").read_text().splitlines()]
        self.assertEqual([e["detail"] for e in events if e["action"] == "config"], logged)

    def test_crashcheck_window_defaults_to_the_configured_rule(self):
        config.CONFIG_PATH.write_text("[rules.crash_loop]\nwindow_min = 90\n" + TEST_CONFIG)
        seen = []

        def op(acct, name, opts=None, native_id=None):   # the provider CLI runs in a credentialed child
            seen.append(opts["window_min"])
            return False, "no provider in tests"

        with mock.patch.object(gather, "op", op):
            self.cw("crashcheck", "modal:main", "ap-1")
            self.cw("crashcheck", "modal:main", "ap-1", "--window", "30")
        self.assertEqual(seen, [90, 30])

    def test_session_name_matching_several_sessions_is_refused(self):
        for sid in ("aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"):
            self.book[sid] = {"id": sid, "name": "worker", "cli": "codex", "live": True}
        self.address_book()
        code, _, err = self.cw("register", "modal:main:x-*", "--expect", "1h", "--session", "worker")
        self.assertEqual(code, 1)
        self.assertIn("matches 2 sessions", err)
        self.assertEqual(self.db.q("select * from registrations"), [])

    def test_unknown_session_name_is_refused(self):
        code, _, err = self.cw("register", "modal:main:x-*", "--expect", "1h", "--session", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("ghost", err)
        self.assertEqual(self.db.q("select * from registrations"), [])

    def test_without_address_book_a_claude_session_goes_on_and_a_session_name_is_refused(self):
        self.address_book(error="agent-send: mailroom socket refused")
        k = "lambda:main:i-1"
        self.ingest("lambda:main", [lambda_box()], T0)
        os.environ["CLAUDE_CODE_SESSION_ID"] = OWNER
        code, _, err = self.cw("ack", k, "--keep", "1h", "--reason", "mine")
        self.assertEqual(code, 0)
        self.assertIn("warning: agent-send --list exited 1: agent-send: mailroom socket refused", err)
        self.assertEqual(self.res(k)["suppress_by"], OWNER)
        code, _, err = self.cw("register", "modal:main:x-*", "--expect", "1h", "--session", "worker")
        self.assertEqual(code, 1)
        self.assertIn("cannot look up --session worker: agent-send --list exited 1: agent-send: mailroom socket refused",
                      err)
        self.assertEqual(self.db.q("select * from registrations"), [])

    def test_check_skips_quiet_attribution_without_address_book(self):
        self.address_book(error="agent-send: mailroom socket refused")
        self.ingest("lambda:main", [lambda_box()], T0)
        s = check.run(cfg=dict(self.cfg, accounts={}), dry_run=True)   # no collection: acts on the state it has
        self.assertIn("quiet attribution skipped: agent-send --list exited 1: agent-send: mailroom socket refused",
                      [w["detail"] for w in s["would"] if w["action"] == "error"])

    def test_check_attributes_an_owner_quietly_and_logs_it(self):
        box = lambda_box() | {"name": SEARCHABLE_BOX}
        self.launch_box_transcript(OWNER, box)
        self.ingest("lambda:main", [box], T0)   # ours and billing, owner unknown
        s = check.run(cfg=dict(self.cfg, accounts={}), dry_run=True)   # no collection: acts on the state it has
        self.assertEqual([w["detail"].split(": ")[0] for w in s["would"] if w["action"] == "owner"],
                         ["launched it %s in /work/repo" % util.iso(box["launched_at"])])

    def test_list_flags_come_from_the_loaded_config(self):
        self.ingest("lambda:main", [lambda_box(rate=1.29)], T0)
        self.assertNotIn("high_burn", self.cw("list")[1])
        config.CONFIG_PATH.write_text("[rules.high_burn]\nusd_per_hour = 1\n" + TEST_CONFIG)
        self.assertIn("high_burn", self.cw("list")[1].splitlines()[1])

    def test_disown_starts_the_ladder_afresh_for_the_next_owner(self):
        other = "99999999-0000-0000-0000-000000000009"
        self.book[OWNER]["live"] = False
        self.book[other] = {"id": other, "name": "judge session", "cli": "claude", "live": False}
        self.address_book()
        k = "lambda:main:i-1"
        box = lambda_box(launched=T0 - 25 * 3600) | {"name": SEARCHABLE_BOX}
        self.register(k)   # OWNER registered it, but the transcripts show that `other` launched it
        self.launch_box_transcript(other, box)
        self.ingest("lambda:main", [box], T0)
        self.tick(T0)   # OWNER has exited: resumed for one turn
        inc = self.db.open_incident(k)
        self.assertEqual(self.cw("disown", "wd-%d" % inc["id"], "--reason", "not mine", "--session", OWNER)[0], 0)
        self.tick(T0 + 600)   # the owner search finds `other`, which has exited too: resumed, as on a fresh incident
        self.assertEqual((self.res(k)["owner_session"], self.res(k)["owner_source"]), (other, "transcript"))
        self.assertEqual([resume for _, resume, _ in self.n.pings], [True, True])
        self.assertIn("**judge session** (`%s`), has exited. I resumed it" % other, self.n.emails[-1][1])

    def test_disown_sticks_when_the_resource_is_seen_again(self):
        k = "lambda:main:i-1"
        box = lambda_box(launched=T0 - 25 * 3600)
        self.register(k)
        self.ingest("lambda:main", [box], T0)
        self.assertEqual(self.res(k)["owner_session"], OWNER)
        self.assertEqual(self.cw("disown", k, "--reason", "not mine", "--session", OWNER)[0], 0)
        self.ingest("lambda:main", [box], T0 + 600)   # the next check sees it again: OWNER's registration must not
        self.assertIsNone(self.res(k)["owner_session"])   # hand the resource back to the session that disowned it
        self.assertIsNone(self.res(k)["owner_source"])

    def test_registering_again_after_a_disown_reclaims_the_resource(self):
        k = "lambda:main:i-1"
        box = lambda_box(launched=T0 - 25 * 3600)
        self.register(k)
        self.ingest("lambda:main", [box], T0)
        self.assertEqual(self.cw("disown", k, "--reason", "not mine", "--session", OWNER)[0], 0)
        self.assertEqual(self.cw("register", k, "--reason", "it is mine after all", "--session", OWNER)[0], 0)
        self.assertEqual((self.res(k)["owner_session"], self.res(k)["owner_source"]), (OWNER, "registered"))
        self.ingest("lambda:main", [box], T0 + 600)
        self.assertEqual((self.res(k)["owner_session"], self.res(k)["owner_source"]), (OWNER, "registered"))

    def test_blind_alert_is_retried_until_it_is_sent(self):
        modes.set_mode(self.db, "live", T0, by="test")
        for t in (T0 - 1200, T0 - 600, T0):
            self.db.collector_failed("lambda:work", "HTTP 401", t)
        cfg = dict(self.cfg, accounts={})   # no collection: the failing count is already recorded

        def alerted():
            return self.db.collector_health("lambda:work")["alerted"]

        with self.agent_mail(1):
            check.run(cfg=cfg)
        self.assertEqual(alerted(), 0)
        with self.agent_mail(0):
            check.run(cfg=cfg)
        self.assertEqual(alerted(), 1)
        sent = self.db.q("select ok from actions where action='email' and detail like '[cloud-watchdog] Can''t see%'")
        self.assertEqual([a["ok"] for a in sent], [0, 1])

    def launch(self, command, provider_cli, started, extra_args=()):
        """Run `cloud-watch launch --expect 6h --reason sweep ... -- COMMAND` through the real gather.op, collect.main
        and provider collectors. Each collector child runs in-process, because creds.run_as would start it under
        `bws run` with vault secrets; `provider_cli` patches stand in for the provider's CLI or ssh, and `started`
        (the launched command, which would really start a paid resource) runs instead of the command. Returns
        (exit code, stderr)."""
        def child(acct, argv, timeout=300, input_text=None):
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(input_text)), contextlib.redirect_stdout(out):
                collect.main()
            return 0, out.getvalue(), ""

        with contextlib.ExitStack() as stack:
            for p in [mock.patch.object(gather.creds, "run_as", child),
                      mock.patch.object(cli.subprocess, "call", started)] + provider_cli:
                stack.enter_context(p)
            code, _, err = self.cw("launch", "--expect", "6h", "--reason", "sweep", "--session", OWNER,
                                   *extra_args, "--", *command)
        return code, err

    def modal_launch(self, failing_listings=()):
        """Launch `modal run sweep.py`, which starts app ap-new beside the running ap-old. The modal CLI answers only
        the app and container listings, so a collect that probes or looks anything up fails; the main workspace's app listings
        whose number (1 before the command, 2 after) is in `failing_listings` fail too. Returns (exit code, stderr)."""
        apps = [{"app_id": "ap-old", "description": "judge", "state": "deployed", "tasks": "1",
                 "created_at": "2026-10-29 08:00:00+00:00"}]
        listings = []

        def modal(acct, args, timeout=120):   # the modal CLI needs Modal credentials and the network
            if args[:2] == ["app", "list"]:
                if acct.get("profile"):
                    return "[]"   # the team workspace has no apps here
                listings.append(args)
                if len(listings) in failing_listings:
                    raise RuntimeError("modal ['app', 'list'] failed (1): 503 Service Unavailable")
                return json.dumps(apps)
            if args[:2] == ["container", "list"]:
                return "[]"
            raise AssertionError("an inventory snapshot ran %s" % args)

        def lookup(acct, ids, python):   # the AppGetLifecycle helper, run under the modal CLI's Python: Modal's API
            raise AssertionError("an inventory snapshot looked up creators of %s" % ids)

        def deploy(command):
            apps.append({"app_id": "ap-new", "description": "sweep", "state": "ephemeral", "tasks": "1",
                         "created_at": "2026-10-30 09:30:00+00:00"})
            return 0

        return self.launch(["modal", "run", "sweep.py"], [mock.patch.object(modal_, "_modal", modal),
                                                          mock.patch.object(modal_, "_creators", lookup)], deploy)

    def registrations(self):
        return [(r["pattern"], r["session_id"]) for r in self.db.q("select * from registrations")]

    def test_launch_registers_the_modal_app_its_command_started(self):
        code, err = self.modal_launch()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.registrations(), [("modal:main:ap-new", OWNER)])
        self.assertEqual([tuple(a) for a in self.db.q("select action, actor, detail from actions")],
                         [("register", "eval session", "modal:main:ap-new expect 6h: sweep")])

    def test_launch_leaves_out_an_account_it_could_not_list_before_the_command(self):
        # The main workspace's listing fails before the command and works after: ap-old was already running, so it is not new.
        code, err = self.modal_launch(failing_listings=(1,))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.registrations(), [])
        self.assertIn("cloud-watch: could not list modal:main: RuntimeError: modal ['app', 'list'] failed (1): 503 "
                      "Service Unavailable", err)
        self.assertIn("cloud-watch: no new resource appeared, but modal:main could not be listed: register by hand "
                      "anything the command started there", err)

    def test_launch_names_an_account_it_could_not_list_after_the_command(self):
        code, err = self.modal_launch(failing_listings=(2,))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.registrations(), [])
        self.assertIn("cloud-watch: could not list modal:main: RuntimeError: modal ['app', 'list'] failed (1)", err)
        self.assertIn("cloud-watch: no new resource appeared, but modal:main could not be listed: register by hand "
                      "anything the command started there", err)

    def test_launch_registers_the_slurm_job_its_sbatch_started(self):
        jobs = ["101|gpu-paid-h100|train|RUNNING|1-02:00:00"]

        def ssh(host, command, timeout, options=()):   # ssh would log in to the cluster's login node
            self.assertTrue(command.startswith("squeue --me"))
            return 0, "\n".join(jobs) + "\n===\nEstimated total: $12.50\n", ""

        def sbatch(command):
            jobs.append("102|gpu-paid-l40|sweep|RUNNING|00:05")
            return 0

        code, err = self.launch(["sbatch", "sweep.sh"], [mock.patch.object(slurm, "ssh", ssh)], sbatch)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.registrations(), [("slurm:cluster:102", OWNER)])

    def test_launch_without_an_account_with_resources_still_runs_the_command(self):
        # An agent told to launch through cloud-watch must not be blocked by a missing account: it says so and runs it.
        ran = []
        code, err = self.launch(["true"], [], lambda command: ran.append(command) or 0,
                                extra_args=("--provider", "openrouter"))
        self.assertEqual((code, ran, self.registrations()), (0, [["true"]], []))
        self.assertIn("cloud-watch: no configured account of provider openrouter has resources to watch, so nothing "
                      "is registered", err)

    def test_unnamed_agent_is_logged_by_its_session(self):
        anon = "cccccccc-0000-0000-0000-000000000003"
        self.book[anon] = {"id": anon, "cli": "codex", "live": True}
        self.address_book()
        k = "lambda:main:i-1"
        self.ingest("lambda:main", [lambda_box()], T0)
        self.assertEqual(self.cw("ack", k, "--keep", "1h", "--reason", "mine", "--session", anon)[0], 0)
        self.assertEqual(self.db.one("select actor from actions where action='ack'")["actor"], anon)
        self.assertEqual(self.res(k)["suppress_by"], anon)


class Notifying(Base):
    """The real notify.Notifier, with self.n standing in for agent-send, agent-mail and the provider stop."""

    def owned_box(self):
        k = "lambda:main:i-1"
        self.register(k)
        self.ingest("lambda:main", [lambda_box()], T0)
        return self.db.new_incident(k, ["cost"], "normal", T0), self.res(k)

    def test_email_result_tells_sent_folded_and_failed_apart(self):
        n = self.notifier(T0)
        self.n.email_ok = False
        self.assertEqual(n.email("[cloud-watchdog] one", "body"), "failed")
        self.n.email_ok = True
        self.assertEqual(n.email("[cloud-watchdog] two", "body"), "sent")
        self.assertEqual(self.notifier(T0, cfg=dict(self.cfg, max_emails_per_day=1)).email("three", "body"), "folded")
        self.assertEqual(self.notifier(T0, shadow=True).email("four", "body"), "shadow")
        self.assertEqual([s for s, _ in self.n.emails], ["[cloud-watchdog] one", "[cloud-watchdog] two"])

    def test_every_sent_email_ends_with_the_reply_footer(self):
        n = self.notifier(T0, cfg=dict(self.cfg, shadow_days=3))
        n.email("[cloud-watchdog wd-7] x", "body", incident_id=7)
        n.send("[cloud-watchdog] Daily", "digest body")
        (_, with_incident), (_, without) = self.n.emails
        self.assertIn("`cloud-watch close wd-7 --by human", with_incident)
        self.assertIn("`cloud-watch close <id> --by human", without)
        for body in (with_incident, without):
            self.assertIn("`cloud-watch mode shadow --days 3 --by human` (stay in shadow)", body)

    def test_email_over_the_daily_cap_goes_into_the_digest(self):
        n = self.notifier(T0, cfg=dict(self.cfg, max_emails_per_day=2))
        got = [n.email("[cloud-watchdog] %d" % i, "body", key="k") for i in range(3)]
        self.assertEqual((got, len(self.n.emails)), (["sent", "sent", "folded"], 2))
        self.assertEqual([tuple(i) for i in self.db.q("select kind, key, text from digest_items")],
                         [("capped_email", "k", "[cloud-watchdog] 2 (held back: daily email cap)")])
        later = self.notifier(T0 + 86401, cfg=dict(self.cfg, max_emails_per_day=2))   # a day on: the cap is free
        self.assertEqual(later.email("[cloud-watchdog] 3", "body"), "sent")

    def test_shadow_notifier_runs_nothing_and_logs_what_it_would_do(self):
        inc, res = self.owned_box()
        n = self.notifier(T0, shadow=True)
        self.assertEqual(n.ping(inc, res, "Is this yours?\nmore"), (True, "shadow"))
        self.assertEqual(n.ping(inc, res, "Is this yours?", resume=True), (True, "shadow"))
        self.assertEqual(n.email("[cloud-watchdog] x", "body"), "shadow")
        self.assertEqual(n.stop(res, "watchdog", inc["id"]), (True, "shadow"))
        self.assertEqual((self.n.pings, self.n.emails, self.n.stops), ([], [], []))
        logged = self.db.q("select action, detail, shadow from actions where action!='register' order by id")
        self.assertEqual([tuple(a) for a in logged],
                         [("ping", "would message eval session: Is this yours?", 1),
                          ("resume", "would message eval session: Is this yours?", 1),
                          ("email", "would email Alex: [cloud-watchdog] x", 1),
                          ("stop", "would stop box (lambda:main:i-1)", 1)])
        self.assertFalse((config.STATE_DIR / "outbox").exists())

    def test_ping_result_carries_agent_sends_answer(self):
        inc, res = self.owned_box()
        self.n.ping_ok = False
        self.assertEqual(self.notifier(T0).ping(inc, res, "Is this yours?"),
                         (False, "exit 1 agent-send: no session %s" % OWNER))
        self.assertEqual(self.n.pings, [(OWNER, False, "Is this yours?")])

    def test_an_app_without_a_name_is_shown_by_its_id(self):
        self.ingest("modal:main", [modal_app("ap-1", name=None, cost=600.0),
                                    modal_app("ap-2", name=None, deployed_by="kim-7305")], T0)
        res, theirs = self.res("modal:main:ap-1"), self.res("modal:main:ap-2")
        inc = self.db.new_incident(res["key"], ["crash_loop"], "urgent", T0)
        self.notifier(T0, shadow=True).stop(res, "watchdog", inc["id"])
        shown = {"auto_stopped": messages.auto_stopped(inc, res, T0, True, "stopped"),
                 "cost_email": messages.cost_email(inc["id"], res, {"text": "$600 spent", "tier": 500}, T0),
                 "foreign_crash": messages.foreign_crash(theirs, T0),
                 "digest": digest.build(self.db, self.cfg, T0)[:2],
                 "stop log": ("", self.db.one("select detail from actions where action='stop'")["detail"])}
        for name, (subject, md) in shown.items():
            with self.subTest(name):
                self.assertNotIn("None", subject + md)
        self.assertIn("`ap-2` in `modal:main` is crash-looping", shown["foreign_crash"][1])
        self.assertIn("\n| ap-1 | modal:main |", shown["digest"][1])
        self.assertEqual(shown["stop log"][1], "would stop ap-1 (modal:main:ap-1)")

    def test_cost_email_marks_an_estimate_and_names_the_next_tier(self):
        self.ingest("lambda:main", [lambda_box(launched=T0 - 400 * 3600)], T0)   # price x uptime: an estimate
        subject, md = messages.cost_email(None, self.res("lambda:main:i-1"), {"text": "x", "tier": 500}, T0)
        self.assertIn("box has cost ≈$516", subject)
        self.assertIn("Cost so far: ≈$516 (estimate: price x uptime).", md)
        self.assertIn("Next email at $1,000 unless it stops.", md)


# Runs in a child: one complete check of lambda:work, then a second one that SIGKILLs itself (what the OOM
# killer sends, so no handler or `finally` runs) after writing two of its three resources.
KILLED_MID_INGEST = """
import json, os, signal, sys
sys.path.insert(0, %(repo)r)
from pathlib import Path
from cloud_watchdog import check, config, state
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
        self.assertEqual(db.collector_health("lambda:work")["last_ok_at"], T0)

    def test_a_request_over_128_kib_reaches_the_child(self):
        """Linux caps each argv or environment string at 128 KiB (MAX_ARG_STRLEN), and a big workspace's known_creator_of alone
        can exceed that. gather.op runs the real child here, as a check does: a plain child for an account with
        `env = {}`, and childexec under `bws run` for one that names secrets. `modal` and `bws` are stubs, because the
        real ones need Modal credentials, the network and the vault token. The bws stub runs its command as `bws run`
        2.1.0 does: through `sh -c`, with stdin inherited, the access token removed and the secrets added to the
        environment."""
        apps = [{"app_id": "ap-run", "description": "sweep", "state": "ephemeral", "tasks": "0",
                 "created_at": "2026-11-17 18:00:00+00:00"}]
        script(self.bin, "modal", 'case "$1 $2" in\n'
                                  '  "app list") printf %%s %s ;;\n'
                                  '  "container list"|"app history") printf "[]" ;;\n'
                                  '  *) echo "unexpected: modal $*" >&2; exit 1 ;;\n'
                                  'esac' % shlex.quote(json.dumps(apps)))
        script(self.bin, "bws", 'while [ "$1" != "--" ]; do shift; done; shift\n'
                                'unset BWS_ACCESS_TOKEN\n'
                                'export MAIN_MODAL_TOKEN_ID=fake-id MAIN_MODAL_TOKEN_SECRET=fake-secret\n'
                                'exec sh -c "$*"')
        vault = Path(self.tmp.name) / "bws"
        vault.mkdir()
        (vault / "token").write_text("fake-token")
        (vault / "project-id").write_text("fake-project")
        env_file = Path(self.tmp.name) / "modal-main.env"
        env_file.write_text("MAIN_MODAL_TOKEN_ID=fake-id\nMAIN_MODAL_TOKEN_SECRET=fake-secret\n")
        env_file.chmod(0o600)
        creators = {"ap-%022d" % i: {"name": "app-%d" % i, "created_by": "member-%d" % i, "deployed_by": None,
                                     "state": "stopped"} for i in range(4000)}
        creators["ap-run"] = {"name": "sweep", "created_by": "alex-10001", "deployed_by": None, "state": "ephemeral"}
        spend.cache_creators(self.db, "modal:main", creators, T0)
        opts = check.collect_opts(self.db, self.cfg, "modal:main", T0, slow=False, inventory_only=False)
        self.assertGreater(len(json.dumps(opts)), 128 * 1024)
        acct = self.cfg["accounts"]["modal:main"]
        # LOCAL_BIN leads the children's PATH (config.service_path), and the real modal and bws are installed there.
        # Each secrets backend that starts a child: none (a plain child), bws (childexec under `bws run`) and env-file
        # (childexec reading the account's file).
        with mock.patch.object(config, "LOCAL_BIN", self.bin):
            for over in ({"secrets": "none", "env": {}},
                         {"secrets": "bws", "bws_token_file": str(vault / "token"),
                          "bws_project_file": str(vault / "project-id")},
                         {"secrets": "env-file", "env_file": str(env_file)}):
                with self.subTest(secrets=over["secrets"]):
                    ok, res = gather.op(dict(acct, **over), "collect", opts)
                    self.assertTrue(ok, res)
                    self.assertEqual([(r["native_id"], r.get("created_by")) for r in res["resources"]],
                                     [("ap-run", "alex-10001")])

    def test_a_resource_seen_again_after_it_was_gone_is_logged(self):
        k = "lambda:main:i-1"
        for t, boxes in ((T0, [lambda_box()]), (T0 + 600, []), (T0 + 1200, [lambda_box()])):
            self.ingest("lambda:main", boxes, t)
        self.assertIsNone(self.res(k)["gone_at"])
        self.assertEqual([tuple(a) for a in self.db.q("select key, detail from actions where action='reappear'")],
                         [(k, "gone since %s" % util.iso(T0 + 600))])

    def test_failed_collection_counts_and_rest_of_state_untouched(self):
        self.ingest("lambda:work", [lambda_box()], T0)
        got = check.ingest_account(self.db, self.cfg, "lambda:work", False, False, "RuntimeError: modal failed (-9)",
                                   T0 + 600)
        self.assertFalse(got["ok"])
        h = self.db.collector_health("lambda:work")
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
        unit = cli.units("/p", "/r")["cloud-watchdog.service"]
        self.assertIn("\nOOMPolicy=continue\n", unit)
        self.assertIn("\nMemoryMax=1G\n", unit)

    def test_tools_run_on_the_service_path_with_no_dir_twice(self):
        service_path = "%s/.local/bin:/usr/local/bin:/usr/bin:/bin" % config.HOME
        with mock.patch.dict(os.environ, {"PATH": service_path}):   # as the systemd units set it
            self.assertEqual(creds.base_env()["PATH"], service_path)
        with mock.patch.dict(os.environ, {"PATH": "/opt/tools/bin:/usr/bin"}):   # a command run from a shell
            self.assertEqual(creds.base_env()["PATH"], "%s/.local/bin:/opt/tools/bin:/usr/bin" % config.HOME)
        for name, text in cli.units("/p", "/r").items():
            if "[Service]" in text:   # systemd expands %h to the user's home
                self.assertIn("\nEnvironment=PATH=%s\n" % service_path, text.replace("%h", str(config.HOME)), name)

    def test_failure_email_names_the_cause_and_the_memory_hogs(self):
        calls = []

        def fake_run(cmd, timeout=None):   # systemctl, journalctl and ps read this host's units, journal, processes
            calls.append(cmd[0])
            if cmd[0] == "systemctl":
                return 0, result + "\n", ""
            if cmd[0] == "journalctl" and "-k" in cmd:
                return 0, "Oct 05 21:20:30 h kernel: Out of memory: Killed process 2837514 (modal) rss:30012kB\n", ""
            if cmd[0] == "journalctl":
                return 0, "cloud-watchdog.service: Failed with result '%s'.\n" % result, ""
            return 0, "    PID   RSS ELAPSED COMMAND\n1199765 4977000 03:10:00 pp\n", ""

        result = "oom-kill"
        cfg = dict(self.cfg, failure_alert_every_hours=3, heartbeat_stale_min=90)
        subject, md = cli.failure_report("cloud-watchdog.service", cfg, run=fake_run, mem=lambda: "Memory now: 0.1 GB")
        self.assertEqual(subject, "[cloud-watchdog] Check failed (oom-kill)")
        for text in ("bystander", "Killed process 2837514 (modal)", "4977000 03:10:00 pp", "Memory now",
                     "retries every 10 minutes. Further failures in the next 3 h are not emailed; if no check succeeds "
                     "for 90 min you get \"[cloud-watchdog] Not running\"."):
            self.assertIn(text, md)
        result, calls[:] = "exit-code", []
        subject, md = cli.failure_report("cloud-watchdog.service", cfg, run=fake_run, mem=lambda: "Memory now: 0.1 GB")
        self.assertEqual(subject, "[cloud-watchdog] Check failed (exit-code)")
        self.assertIn("traceback", md)
        self.assertNotIn("Memory now", md)
        self.assertEqual(calls, ["systemctl", "journalctl"])

    def test_dry_run_clears_copies_left_by_a_killed_dry_run(self):
        d = Path(self.tmp.name)
        (d / "cw-dry-stale").mkdir()
        (d / "cw-dry-stale" / "state.sqlite").write_text("")
        with mock.patch.multiple(config, STATE_DIR=d, LOCK_PATH=d / "check.lock", DB_PATH=d / "state.sqlite"):
            s = check.run(cfg=dict(self.cfg, accounts={}), dry_run=True)   # no collection: acts on the state it has
        self.assertEqual((s["dry_run"], s["accounts"]), (True, {}))
        self.assertEqual(list(d.glob("cw-dry-*")), [])

    def test_dry_run_keeps_no_audit_log(self):
        d = Path(self.tmp.name)
        # create=True: before the fix DB.log wrote to config.EVENTS_PATH; pointing that at the real DB's log keeps
        # a run of this test against the old code off the live state, and makes it fail there as it should.
        with mock.patch.multiple(config, STATE_DIR=d, LOCK_PATH=d / "check.lock", DB_PATH=d / "state.sqlite"), \
                mock.patch.object(config, "EVENTS_PATH", d / "events.jsonl", create=True):
            db = state.DB(config.DB_PATH)
            db.add_registration("lambda:main:i-1", self.owner_session, None, None, "sweep", T0)   # a real entry
            observe.ingest(db, self.cfg, "lambda:main", {"resources": [lambda_box(launched=T0 - 25 * 3600)],
                                                             "metrics": {}}, T0)
            db.c.close()
            s = check.run(cfg=dict(self.cfg, accounts={}), dry_run=True)   # no collection: acts on the state it has
        self.assertIn("ping", [w["action"] for w in s["would"]])
        self.assertEqual(len((d / "events.jsonl").read_text().splitlines()), 1)   # only the real entry

    def test_audit_log_write_failure_raises(self):
        d = Path(self.tmp.name)
        db = state.DB(d / "state.sqlite")
        (d / "events.jsonl").mkdir()
        with self.assertRaises(OSError):
            db.log("ack", "keep", shadow=False, now=T0)


def modal_spend(month, **kw):
    return {"month": month, "through": T0 - 3600, "token_user": "alex-10001", "lookup_errors": 0,
            "volumes_by": {"alex-10001": 4, "kim-7305": 230}} | kw


SPEND_MONTH = {
    "ap-mine1": ["exp-sweep-models-fixed10-001-opus", 24.0], "ap-mine2": ["exp-sweep-models-fixed10-003-fable", 19.0],
    "ap-mine3": ["exp-b2-20260101T120000Z-0a1b2c3d", 17.0], "ap-sandbox": ["inspect_modal_sandbox", 1860.0],
    "ap-mixed": ["joint-server", 50.0], "ap-kim": ["team-eval-sandbox", 3600.0], "ap-sam": ["targets-qwen3-8b", 386.0],
    "ap-gone": ["banana-bench-target", 1.5], "ap-both": ["alex-tinker-plb", 3.0]}
SPEND_CREATORS = {   # as modal_.collect returns them: the lookups it made this pass
    a: {"name": SPEND_MONTH[a][0], "created_by": c, "deployed_by": d, "state": st} for a, c, d, st in (
        ("ap-mine1", "alex-10001", None, "stopped"), ("ap-mine2", "alex-10001", None, "stopped"),
        ("ap-mine3", "alex-10001", "alex-10001", "deployed"), ("ap-sandbox", "sam-22506", "sam-22506", "deployed"),
        ("ap-mixed", "sam-22506", "lee-90937", "deployed"), ("ap-kim", "kim-7305", "kim-7305", "deployed"),
        ("ap-sam", "sam-22506", None, "stopped"), ("ap-gone", None, None, "not-found"),
        ("ap-both", "alex-10001", "alexw2", "deployed"))}


class Spend(Cli):
    def test_split_buckets(self):
        owners = {a: dict(c) for a, c in SPEND_CREATORS.items()}
        s = spend.split(SPEND_MONTH, owners, spend.your_users(self.cfg["identity"]), self.cfg["identity"])
        self.assertEqual(s["yours_usd"], 63.0)          # mine1-3 + both (two of the owner's own user names)
        self.assertEqual(s["yours_apps"], 4)
        self.assertEqual(s["shared_usd"], 1910.0)       # listed shared app + created/deployed by different people
        self.assertEqual(s["others_by"], [["kim-7305", 3600.0], ["sam-22506", 386.0]])
        self.assertEqual(s["unknown_usd"], 1.5)
        self.assertAlmostEqual(s["apps_usd"], s["yours_usd"] + s["shared_usd"] + s["others_usd"] + s["unknown_usd"])
        self.assertEqual(s["yours_top"][0], ["exp-sweep-models-*", 43.0, 2])
        self.assertIn(["exp-b2-20260101T120000Z-0a1b2c3d", 17.0, 1], s["yours_top"])   # a family of one keeps its name

    def test_token_user_counts_as_yours(self):
        ident = dict(self.cfg["identity"], owner_modal_users=["alex-10001"])
        owners = {"ap-w": {"created_by": "alexw2"}}
        s = spend.split({"ap-w": ["alex-tinker", 2.0]}, owners, spend.your_users(ident, "alexw2"), ident)
        self.assertEqual(s["yours_usd"], 2.0)
        s = spend.split({"ap-w": ["alex-tinker", 2.0]}, owners, spend.your_users(ident), ident)
        self.assertEqual(s["others_by"], [["alexw2", 2.0]])

    def test_family(self):
        self.assertEqual(spend.family("exp-sweep-models-fixed10-001-gpt-6.1-sol"), "exp-sweep-models-*")
        self.assertEqual(spend.family("exp-b2-20260101T120000Z-0a1b2c3d"), "exp-b2-*")
        self.assertEqual(spend.family("inspect_modal_sandbox"), "inspect_modal_sandbox")
        self.assertEqual(spend.family("team-eval-sandbox"), "team-eval-sandbox")

    def test_creators_cached_and_only_new_or_redeployable_apps_looked_up_again(self):
        res = {"resources": [], "metrics": {"month_metered_usd": 6100.0, "month_volumes_usd": 212.0},
               "creators": SPEND_CREATORS, "warnings": [], "spend": modal_spend(SPEND_MONTH)}
        check.ingest_account(self.db, self.cfg, "modal:main", True, True, res, T0)
        self.assertEqual(self.db.one("select count(*) n from modal_app_owner")["n"], len(SPEND_CREATORS))
        known = set(spend.known_ids(self.db, "modal:main", T0 + 3600))
        self.assertEqual(known, set(SPEND_CREATORS))
        later = set(spend.known_ids(self.db, "modal:main", T0 + 2 * 86400))
        self.assertEqual(set(SPEND_CREATORS) - later, {"ap-mine3", "ap-sandbox", "ap-mixed", "ap-kim", "ap-both"})
        # Next pass: the child looked up nothing new, the split still uses the cache.
        res["creators"] = {}
        check.ingest_account(self.db, self.cfg, "modal:main", True, True, res, T0 + 3600)
        self.assertEqual(self.db.meta("spend:modal:main")["yours_usd"], 63.0)
        opts = check.collect_opts(self.db, self.cfg, "modal:main", T0 + 3600, slow=True, inventory_only=False)
        self.assertEqual(set(opts["known_creators"]), set(SPEND_CREATORS))
        self.assertNotIn("known_creators", check.collect_opts(self.db, self.cfg, "modal:main", T0 + 3600, slow=False,
                                                              inventory_only=False))

    def test_digest_and_status_show_total_and_yours(self):
        res = {"resources": [], "metrics": {"day_spend_usd": 145.0, "month_metered_usd": 6100.0,
                                            "month_volumes_usd": 212.0},
               "creators": SPEND_CREATORS, "warnings": [], "spend": modal_spend(SPEND_MONTH)}
        check.ingest_account(self.db, self.cfg, "modal:main", True, True, res, T0)
        _, md, _, _ = digest.build(self.db, self.cfg, T0)
        self.assertIn("- `modal:main`: last 24 h $145, month metered $6,100", md)
        self.assertIn("    - **This month: total $6,100, yours $63.00** (1% of app spend). Basis: apps created and "
                      "deployed only by `alex-10001` or `alexw2`", md)
        self.assertIn("shared `inspect_modal_sandbox` $1,860 (created by sam-22506;", md)
        self.assertIn("others $3,986 (kim-7305 $3,600, sam-22506 $386)", md)
        self.assertIn("creator unknown $1.50", md)
        self.assertIn("Yours by app: exp-sweep-models-* (2 apps) $43.00 · exp-b2-20260101T120000Z-0a1b2c3d $17.00", md)
        self.assertIn("volume storage $212 (billed per workspace, not per volume; you created 4 of 234 volumes)", md)
        self.assertIn("<ul>\n<li><strong>This month: total $6,100, yours $63.00</strong>", notify.md_to_html(md))
        code, out, _ = self.cw("status")
        self.assertEqual(code, 0)
        self.assertIn("modal:main spend:", out.splitlines())
        self.assertTrue(any(line.startswith("  This month: total $6,100, yours $63.00") for line in out.splitlines()),
                        out)

    def test_failed_lookups_are_shown_after_a_good_pass(self):
        good = {"resources": [], "metrics": {"month_metered_usd": 6100.0, "month_volumes_usd": 212.0},
                "creators": SPEND_CREATORS, "warnings": [], "spend": modal_spend(SPEND_MONTH)}
        check.ingest_account(self.db, self.cfg, "modal:main", True, True, good, T0)
        failed = modal_spend(SPEND_MONTH, error="modal_api failed (1): boom", volumes_by=None,
                             volumes_error="modal ['volume', 'list'] failed (1): 500")
        check.ingest_account(self.db, self.cfg, "modal:main", True, True, dict(good, creators={}, spend=failed), T0 + 3600)
        got = spend.lines(self.db.meta("spend:modal:main"))
        self.assertIn("yours $63.00", got[0])   # from the creators cached on the good pass
        self.assertEqual(got[-1], "    - Incomplete: creator lookup failed: `modal_api failed (1): boom`, so apps not "
                                  "looked up before count as creator unknown; volume creator lookup failed: "
                                  "`modal ['volume', 'list'] failed (1): 500`.")
        partly = dict(good, creators={}, spend=modal_spend(SPEND_MONTH, lookup_errors=2))
        check.ingest_account(self.db, self.cfg, "modal:main", True, True, partly, T0 + 7200)
        self.assertEqual(spend.lines(self.db.meta("spend:modal:main"))[-1],
                         "    - Incomplete: creator lookup failed for 2 billed apps, so they count as creator unknown.")

    def test_lookup_failure_says_unavailable_and_collection_still_succeeds(self):
        res = {"resources": [], "metrics": {"month_metered_usd": 10.0},
               "creators": {}, "warnings": [],
               "spend": {"month": {"ap-x": ["x", 10.0]}, "through": None, "error": "modal_api failed (1): boom"}}
        got = check.ingest_account(self.db, self.cfg, "modal:main", True, True, res, T0)
        self.assertTrue(got["ok"])
        self.assertEqual(spend.lines(self.db.meta("spend:modal:main")),
                         ["    - Yours: unavailable (creator lookup failed: `modal_api failed (1): boom`)."])
        res["spend"]["volumes_error"] = "modal ['volume', 'list'] failed (1): 500"
        check.ingest_account(self.db, self.cfg, "modal:main", True, True, res, T0 + 3600)
        self.assertEqual(spend.lines(self.db.meta("spend:modal:main")),
                         ["    - Yours: unavailable (creator lookup failed: `modal_api failed (1): boom`).",
                          "    - Incomplete: volume creator lookup failed: `modal ['volume', 'list'] failed (1): 500`."])


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

    def test_creator_lookup_uses_the_cache_then_one_batch_and_names_a_failure(self):
        res = [{"native_id": "ap-a", "name": "a", "dormant": False}, {"native_id": "ap-b", "name": "b", "dormant": False},
               {"native_id": "ap-c", "dormant": False, "deployed_by": "sam-22506"}, {"native_id": "ap-d", "dormant": True}]
        asked, warnings = [], []

        def fake(acct, ids, python):   # runs modal_api.py under the modal CLI's Python: Modal credentials, network
            asked.append(list(ids))
            return {"apps": {"ap-b": {"created_by": "alex-10001", "deployed_by": None, "state": "ephemeral"}},
                    "errors": {}}

        with mock.patch.object(modal_, "_creators", fake):
            found = modal_._lookup_creators({}, {"known_creator_of": {"ap-a": "kim-7305"}}, res, None, warnings)
        self.assertEqual(asked, [["ap-b"]])
        self.assertEqual(found, {"ap-b": {"name": "b", "created_by": "alex-10001", "deployed_by": None,
                                          "state": "ephemeral"}})
        self.assertEqual([r.get("created_by") for r in res], ["kim-7305", "alex-10001", None, None])
        self.assertEqual(warnings, [])
        fresh = [{"native_id": "ap-x", "name": "x", "dormant": False}]
        with mock.patch.object(modal_, "_creators", mock.Mock(side_effect=RuntimeError("down"))):
            self.assertEqual(modal_._lookup_creators({}, {"known_creator_of": {}}, fresh, None, warnings), {})
        self.assertNotIn("created_by", fresh[0])
        self.assertEqual(warnings, [{"lookup": "creators", "text": "creator lookup failed for x (ap-x): down; the owner "
                                                                   "stays unknown until a lookup works."}])

    def test_apps_the_helper_could_not_look_up_are_counted_and_named(self):
        sp, warnings = {"month": {"ap-b": ["b", 4.0], "ap-c": ["c", 9.0]}, "through": None}, []
        tracked = [{"native_id": "ap-x", "name": "x", "dormant": False}]

        def helper(acct, ids, python):   # runs modal_api.py under the modal CLI's Python: Modal credentials, network
            return {"token_user": "alex-10001", "apps": {"ap-b": {"created_by": "kim-7305", "deployed_by": None,
                                                                    "state": "stopped"}},
                    "errors": {"ap-c": "DeadlineExceeded", "ap-x": "DeadlineExceeded"}}

        with mock.patch.object(modal_, "_creators", helper):
            found = modal_._lookup_creators({}, {"known_creator_of": {}, "known_creators": []}, tracked, sp, warnings)
        self.assertEqual((list(found), sp["lookup_errors"], sp["token_user"]), (["ap-b"], 1, "alex-10001"))
        self.assertEqual(warnings, [{"lookup": "creators", "text": "creator lookup failed for x (ap-x): DeadlineExceeded; "
                                                                   "the owner stays unknown until a lookup works."}])


class ModalCrashCheck(unittest.TestCase):
    def crash_check(self, opts):
        """Run the child's crash_check op (collect.main) for ap-1; in the canned modal CLI output, three containers
        failed in the window and four others of the app are running now. Returns (result, container listings)."""
        listings = []

        def modal(acct, args, timeout=120):   # the modal CLI needs credentials and the network
            if args[:2] == ["container", "list"]:
                listings.append(args)
                return json.dumps([{"app_id": "ap-1", "container_id": "ta-live%d" % i} for i in range(4)]
                                  + [{"app_id": "ap-2", "container_id": "ta-other"}])
            line = "Runner failed" if "-s" in args else "loading weights"
            return "\n".join("2026-10-30 10:0%d:00 ta-dead%d %s" % (i, i, line) for i in range(3))

        req = {"op": "crash_check", "acct": {"provider": "modal"}, "opts": opts, "native_id": "ap-1"}
        out = io.StringIO()
        with mock.patch.object(modal_, "_modal", modal), contextlib.redirect_stdout(out), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(req))):
            collect.main()
        return json.loads(out.getvalue())["result"], listings

    def test_live_recheck_counts_the_apps_running_containers_like_the_collect_pass(self):
        rule = config.load(Path(__file__).parent / "none.toml")["rules"]["crash_loop"]
        got, listings = self.crash_check({"window_min": 120})
        self.assertEqual((got["failed"], got["seen"]), (3, 7))
        self.assertFalse(observe.crash_candidate(got, rule))   # 3 of 7 failed: under half
        got, listings = self.crash_check({"window_min": 120, "at": "2026-10-30T10:30:00Z"})
        self.assertEqual((got["seen"], listings), (3, []))   # a backtest's past window has no live containers


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

        def fake(acct, args, timeout=120):   # the modal CLI needs credentials and the network
            if args[:2] == ["billing", "summary"]:
                return json.dumps({"metered_cost": "20", "metered_cost_breakdown": {"volumes": "4"}})
            return json.dumps(daily if "d" in args else hourly)

        with mock.patch.object(modal_, "_modal", fake):
            _, _, metrics, sp = modal_._billing({}, now)
        self.assertEqual(sp["month"], {"ap-a": ["a", 5.0], "ap-b": ["b", 9.0], "ap-c": ["c", 3.0]})
        self.assertEqual(sp["through"], util.parse_time("2026-10-12T09:00:00Z"))
        self.assertEqual(metrics["month_metered_usd"], 20.0)

    def test_app_cost_counts_from_a_day_before_launch_and_its_rate_from_a_recent_hour(self):
        now = util.parse_time("2026-10-30T10:00:00Z")
        app = {"app_id": "ap-1", "description": "judge", "state": "deployed", "tasks": "1",
               "created_at": "2026-10-29 10:00:00+00:00"}
        billed = [(now - 3 * 86400, 50.0), (now - 2 * 86400 + 3600, 2.0), (now - 3600, 3.0)]
        got = lambda billed, latest, slow: modal_.app_resource(app, [], billed, latest, False, slow, now)  # noqa: E731
        self.assertEqual({k: got(billed, (now - 3600, 3.0), False)[k] for k in ("cost_usd", "rate_usd_h")},
                         {"cost_usd": 5.0, "rate_usd_h": 3.0})
        self.assertEqual(got(billed, (now - 5 * 3600, 3.0), False)["rate_usd_h"], 0.0)   # the hour is over 4 h old
        self.assertEqual({"cost_usd", "rate_usd_h"} & set(got(None, None, False)), set())   # a fast pass keeps both
        self.assertEqual(got(None, None, True)["rate_usd_h"], 0.0)   # a slow pass that billed no hour for it

    def test_a_deployed_app_doing_nothing_is_dormant(self):
        now = util.parse_time("2026-10-30T10:00:00Z")
        idle = {"app_id": "ap-1", "description": "judge", "state": "deployed", "tasks": "0",
                "created_at": "2026-10-29 10:00:00+00:00"}

        def dormant(app, live=(), billed=None, watched=False):
            return modal_.app_resource(app, list(live), billed, None, watched, False, now)["dormant"]

        self.assertTrue(dormant(idle))
        self.assertEqual([dormant(idle, watched=True), dormant(idle | {"state": "ephemeral"}),
                          dormant(idle, billed=[(now - 3600, 1.0)]), dormant(idle, live=[{"container_id": "ta-1"}]),
                          dormant(idle | {"tasks": "1"})], [False] * 5)
        self.assertIsNone(modal_.app_resource(idle | {"state": "stopped"}, [], None, None, False, False, now))

    def test_malformed_billing_row_fails_the_pass_instead_of_being_dropped(self):
        rows = [{"object_id": "ap-a", "description": "a", "interval_start": "2026-10-12 07:00 CDT", "cost": "2"}]

        def fake(acct, args, timeout=120):   # the modal CLI needs credentials and the network
            if args[:2] == ["billing", "summary"]:
                return json.dumps({"metered_cost": "20", "metered_cost_breakdown": {"volumes": "4"}})
            return json.dumps(rows)

        with mock.patch.object(modal_, "_modal", fake), self.assertRaises(ValueError):
            modal_._billing({}, util.parse_time("2026-10-12T09:30:00Z"))

    def test_modal_python_from_cli_shebang(self):
        # shutil.which would find this host's own modal CLI, if any: it finds the test's file instead.
        with tempfile.TemporaryDirectory() as d:
            cli_path = Path(d) / "modal"
            cli_path.write_text("#!/opt/venvs/modal/bin/python3\nimport modal\n")
            with mock.patch.object(modal_.shutil, "which", lambda name: str(cli_path)):
                self.assertEqual(modal_.modal_python(), "/opt/venvs/modal/bin/python3")
            cli_path.write_text("#!/usr/bin/env python3\n")
            with mock.patch.object(modal_.shutil, "which", lambda name: str(cli_path) if name == "modal" else "/usr/bin/python3"):
                self.assertEqual(modal_.modal_python(), "/usr/bin/python3")

    def test_billed_lookup_skips_known_and_free_apps_and_survives_a_failing_helper(self):
        sp = {"month": {"ap-a": ["a", 5.0], "ap-b": ["b", 0.0], "ap-c": ["c", 9.0]}, "through": None}
        seen, warnings = {}, []

        def fake_creators(acct, ids, python):   # runs modal_api.py under the modal CLI's Python: Modal credentials
            seen["ids"] = ids
            raise RuntimeError("modal_api failed (1): NotFoundError")

        with mock.patch.object(modal_, "_creators", fake_creators):
            found = modal_._lookup_creators({}, {"known_creator_of": {}, "known_creators": ["ap-a"]}, [], sp, warnings)
        self.assertEqual((seen["ids"], found, warnings), (["ap-c"], {}, []))   # no tracked app's owner depends on it
        self.assertIn("NotFoundError", sp["error"])


class Misc(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(util.parse_duration("24h"), 86400)
        self.assertEqual(util.parse_duration("1d12h"), 129600)
        self.assertEqual(util.parse_duration("90m"), 5400)
        with self.assertRaises(ValueError):
            util.parse_duration("soon")

    def test_malformed_time_raises_and_absent_time_is_none(self):
        for absent in (None, "", "Pending"):
            self.assertIsNone(util.parse_time(absent))
        self.assertEqual(util.parse_time("2026-10-12 10:00:00+01:00"), util.parse_time("2026-10-12T09:00:00Z"))
        with self.assertRaises(ValueError):
            util.parse_time("2026-10-12 10:00 CDT")
        self.assertIsNone(modal_._log_time("ta-01 Runner failed with exit code 1"))   # a log line with no timestamp

    def test_transcript_line_with_a_junk_timestamp_still_counts(self):
        with tempfile.TemporaryDirectory() as d:
            sid = "aaaaaaaa-0000-0000-0000-000000000001"
            (Path(d) / ("%s.jsonl" % sid)).write_text(json.dumps({
                "timestamp": "yesterday", "sessionId": sid, "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "input": {"command": "modal deploy judge.py  # demo-judge-app"}}]}}) + "\n")
            cfg = config.load(Path(d) / "none.toml")
            cfg["transcripts"]["roots"] = [d]
            cands, failures = transcripts.search(["demo-judge-app"], 0, cfg)
        self.assertEqual(failures, [])
        self.assertEqual([(c["session_id"], c["launch_hit"], c["first_ts"]) for c in cands], [(sid, True, None)])

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

    def test_unparseable_child_output_keeps_the_exit_code_and_stderr(self):
        cut = '{"ok": true, "result": {"resources": [{"native_id": "i-1", "ip": "1.2'   # the child died mid-print
        # creds.run_as runs the child under `bws run` with the account's vault secrets.
        with mock.patch.object(gather.creds, "run_as",
                               lambda acct, argv, timeout, input_text: (-9, cut + "\n", "Killed\n")):
            self.assertEqual(gather.op({"provider": "lambda"}, "collect"),
                             (False, "unparseable child output (exit -9): Killed"))

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
                    "command": "modal deploy judge.py  # demo-judge-app"}}]}}) + "\n")
            (proj / ("%s.jsonl" % told)).write_text(json.dumps({
                "timestamp": "2026-09-29T10:00:00Z", "cwd": "/work/other", "sessionId": told,
                "message": {"role": "user", "content": "[cloud-watchdog wd-1] Is this yours? demo-judge-app; "
                                                       "you ran modal deploy"}}) + "\n")
            cfg = config.load(Path(d) / "none.toml")
            cfg["transcripts"]["roots"] = [d]
            cands, failures = transcripts.search(["demo-judge-app"], 0, cfg)
            self.assertEqual(failures, [])
            ranked = owner.rank(cands, util.parse_time("2026-09-23T13:18:00Z"), [], set())
            self.assertEqual(ranked[0]["session_id"], launcher)
            self.assertTrue(ranked[0]["launch_hit"])
            self.assertEqual([c for c in ranked if c["session_id"] == told], [])  # marker lines are ignored


class OwnerSearch(unittest.TestCase):
    """owner.sessions, owner.worktrees and owner.resolve over real transcripts in a temp dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cfg = test_config(self.dir)
        self.cfg["transcripts"]["roots"] = [str(self.dir / "transcripts")]
        (self.dir / "transcripts").mkdir()

    def resource(self):
        """The owner's Modal app demo-judge-app, as owner.resolve reads it (no commit: no repos to search)."""
        db = state.DB(":memory:")
        app = modal_app(name="demo-judge-app") | {"git_commit": None}
        observe.ingest(db, self.cfg, "modal:main", {"resources": [app], "metrics": {}}, T0)
        return db.resource("modal:main:ap-1")

    def transcript(self, name, records):
        path = self.dir / "transcripts" / name
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path

    def launch(self, sid):
        return {"timestamp": "2026-09-23T13:17:00Z", "cwd": "/work/repo", "sessionId": sid, "message": {
            "role": "assistant", "content": [{"type": "tool_use", "input": {"command": "modal deploy judge.py  # "
                                                                                       "demo-judge-app"}}]}}

    def test_failing_agent_send_raises_with_its_cause(self):
        answers = iter([(1, "", "agent-send: mailroom socket refused"), (0, "agent-send: starting up\n", "")])
        with mock.patch.object(util, "run", lambda cmd, **kw: next(answers)):   # agent-send is another program
            with self.assertRaisesRegex(RuntimeError, "agent-send --list exited 1: agent-send: mailroom socket refused"):
                owner.sessions()
            with self.assertRaisesRegex(RuntimeError, "agent-send --list printed no JSON"):
                owner.sessions()

    def test_failing_git_worktree_list_raises(self):
        with self.assertRaisesRegex(RuntimeError, "git worktree list in .*gone exited 128"):
            owner.worktrees(str(self.dir / "gone"))

    def test_a_deploys_commit_is_found_in_the_repos_under_the_configured_roots(self):
        repo = self.dir / "code" / "judge"
        repo.mkdir(parents=True)
        (self.dir / "code" / "notes").mkdir()   # not a repo

        def git(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                   *args], input="", check=True, capture_output=True, text=True).stdout.strip()

        git("init", "-q")
        sha = git("commit-tree", "--no-gpg-sign", "-m", "deploy", git("mktree"))
        self.assertEqual(owner.repos_with_commit(sha, [str(self.dir / "code")]), [str(repo.resolve())])
        self.assertEqual(owner.repos_with_commit(sha, [str(self.dir / "elsewhere")]), [])

    def test_the_investigators_own_transcripts_are_never_searched(self):
        self.cfg["transcripts"]["exclude"] = []
        sid = "aaaaaaaa-0000-0000-0000-000000000001"
        d = self.dir / "transcripts" / ("-home-alex--local-state-cloud-watchdog-" + investigator.WORKDIR_NAME)
        d.mkdir()
        (d / (sid + ".jsonl")).write_text(json.dumps(self.launch(sid)) + "\n")
        self.assertEqual(transcripts.search(["demo-judge-app"], 0, self.cfg), ([], []))

    def test_failed_transcript_search_is_named_not_read_as_no_mention(self):
        self.transcript("aaaaaaaa-0000-0000-0000-000000000001.jsonl", [self.launch("aaaaaaaa")])
        with mock.patch.dict(os.environ, {"PATH": str(self.dir / "no-bin")}):   # no grep on the PATH: a real failure
            upd = owner.resolve(self.cfg, self.resource(), T0, {})
        self.assertIsNone(upd["owner_session"])
        self.assertTrue(upd["owner_evidence"].startswith("transcript search failed: grep exited 127: "),
                        upd["owner_evidence"])

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_partly_failed_transcript_search_is_marked_incomplete(self):
        sid = "aaaaaaaa-0000-0000-0000-000000000001"
        self.transcript(sid + ".jsonl", [self.launch(sid)])
        self.transcript("bbbbbbbb-0000-0000-0000-000000000002.jsonl", [self.launch("bbbbbbbb")]).chmod(0)
        upd = owner.resolve(self.cfg, self.resource(), T0, {})
        self.assertEqual(upd["owner_session"], sid)
        self.assertRegex(upd["owner_evidence"], r"^incomplete search \(1 failed: grep exited 2: .*Permission denied\) "
                                                 r"\| launched it ")

    def test_codex_rollout_is_recognised_by_its_first_record_under_any_root(self):
        sid = "01900000-1111-7222-8333-444444444444"
        self.transcript("rollout-2026-09-23T13-10-00-%s.jsonl" % sid, [
            {"timestamp": "2026-09-23T13:10:00Z", "type": "session_meta",
             "payload": {"id": sid, "cwd": "/work/repo"}},
            {"timestamp": "2026-09-23T13:17:00Z", "type": "response_item", "payload": {
                "type": "function_call", "arguments": "{\"cmd\": \"modal deploy judge.py  # demo-judge-app\"}"}}])
        upd = owner.resolve(self.cfg, self.resource(), T0, {})
        self.assertEqual((upd["owner_session"], upd["owner_cli"]), (sid, "codex"))
        self.assertIn(" in /work/repo: ", upd["owner_evidence"])


class ModalCollect(Base):
    """modal_.collect, then the parent's ingest. _modal stands in for the modal CLI and _creators for the AppGetLifecycle
    helper run under the CLI's Python: both need Modal credentials and the network."""

    NOW = util.parse_time("2026-10-30T10:00:00Z")
    APPS = [{"app_id": "ap-dep", "description": "judge", "state": "deployed", "tasks": "1",
             "created_at": "2026-10-30 08:00:00+00:00"},
            {"app_id": "ap-run", "description": "sweep-cell", "state": "ephemeral", "tasks": "1",
             "created_at": "2026-10-30 09:00:00+00:00"},
            {"app_id": "ap-new", "description": "sweep", "state": "ephemeral", "tasks": "1",
             "created_at": "2026-10-30 09:30:00+00:00"}]

    def setUp(self):
        super().setUp()
        self.history_error, self.lookup_error, self.lookups = None, None, []
        self.billed = [("ap-dep", "judge", "3"), ("ap-old", "old", "1")]   # (app, name, $) in the 08:00 hour

    def modal(self, acct, args, timeout=120):
        if args[:2] in (["app", "list"], ["container", "list"], ["app", "logs"], ["volume", "list"]):
            return {"app": json.dumps(self.APPS) if args[1] == "list" else "", "container": "[]",
                    "volume": json.dumps([{"name": "v", "created_by": "alex-10001"}])}[args[0]]
        if args[:2] == ["app", "history"]:
            if self.history_error and args[2] == "ap-new":
                raise RuntimeError(self.history_error)
            return json.dumps([{"version": "v3", "deployed_by": "kim-7305", "commit": "abc1234",
                                "time_deployed": "2026-10-30 08:00:00+00:00"}] if args[2] == "ap-dep" else [])
        if args[:2] == ["billing", "report"]:
            hourly = [{"object_id": a, "description": n, "interval_start": "2026-10-30T08:00:00", "cost": c}
                      for a, n, c in self.billed]
            return json.dumps(hourly if args[args.index("-r") + 1] == "h" else [])
        if args[:2] == ["billing", "summary"]:
            return json.dumps({"metered_cost": "4", "metered_cost_breakdown": {}})
        raise AssertionError(args)

    def creators(self, acct, ids, python):
        self.lookups.append(list(ids))
        if self.lookup_error:
            raise RuntimeError(self.lookup_error)
        return {"token_user": "alex-10001", "errors": {}, "apps": {a: {
            "created_by": "alex-10001", "deployed_by": "kim-7305" if a == "ap-dep" else None,
            "state": "deployed" if a == "ap-dep" else "ephemeral"} for a in ids}}

    def collect(self, now, slow, inventory_only=False):
        opts = check.collect_opts(self.db, self.cfg, "modal:main", now, slow, inventory_only)
        self.calls = []

        def modal(acct, args, timeout=120):
            self.calls.append(args[:2])
            return self.modal(acct, args, timeout)

        with mock.patch.object(modal_, "_modal", modal), mock.patch.object(modal_, "_creators", self.creators):
            res = modal_.collect(self.cfg["accounts"]["modal:main"], opts)
        self.summary = check.ingest_account(self.db, self.cfg, "modal:main", slow, True, res, now)
        return res

    def test_rate_from_the_last_slow_pass_and_zero_once_billing_has_no_hour_for_the_app(self):
        self.APPS = [{"app_id": "ap-idle", "description": "eval", "state": "ephemeral", "tasks": "0",
                      "created_at": "2026-10-30 07:00:00+00:00"}]   # no tasks or containers: only its rate bills
        self.billed = [("ap-idle", "eval", "5")]
        got = []
        for now, slow in ((self.NOW, True), (self.NOW + 600, False), (self.NOW + 3600, True)):
            if now == self.NOW + 3600:
                self.billed = []   # billing has no hour for it any more
            self.collect(now, slow)
            r = self.res("modal:main:ap-idle")
            got.append((r["rate_usd_h"], r["billing_now"]))
        self.assertEqual(got, [(5.0, 1), (5.0, 1), (0.0, 0)])   # the fast pass keeps the slow pass's rate

    def test_inventory_only_lists_the_apps_without_probes_or_lookups(self):
        res = self.collect(self.NOW, slow=False, inventory_only=True)
        self.assertEqual([r["native_id"] for r in res["resources"]], ["ap-dep", "ap-run", "ap-new"])
        self.assertEqual((self.calls, self.lookups), ([["app", "list"], ["container", "list"]], []))
        self.collect(self.NOW, slow=False)   # a check's pass probes each app and looks up the owners
        self.assertEqual({tuple(c) for c in self.calls},
                         {("app", "list"), ("container", "list"), ("app", "logs"), ("app", "history")})
        self.assertEqual(self.lookups, [["ap-run", "ap-new"]])

    def test_one_creator_lookup_per_pass_for_ownership_and_spend_with_one_cache(self):
        res = self.collect(self.NOW, slow=True)
        self.assertEqual(self.lookups, [["ap-run", "ap-new", "ap-dep", "ap-old"]])
        self.assertEqual(res["spend"]["token_user"], "alex-10001")
        self.assertEqual(self.res("modal:main:ap-new")["created_by"], "alex-10001")
        self.assertEqual(self.res("modal:main:ap-new")["ownership"], "ours")
        self.assertEqual({r["app_id"]: r["name"] for r in self.db.q("select * from modal_app_owner")},
                         {"ap-run": "sweep-cell", "ap-new": "sweep", "ap-dep": "judge", "ap-old": "old"})
        self.collect(self.NOW + 600, slow=False)   # every creator is cached: nothing to look up
        self.collect(self.NOW + 3600, slow=True)   # nothing due either; the slow pass still reads the token's user
        self.assertEqual(self.lookups[1:], [[]])
        self.assertEqual(self.db.meta("spend:modal:main")["yours_usd"], 1.0)   # ap-old; ap-dep's deployer is alex

    def test_stop_does_not_wait_for_a_confirmation_nobody_can_give(self):
        # Stands in for the modal CLI (needs credentials and stops a real app). Since modal 1.x, `modal app stop`
        # asks "Are you sure?" and aborts when no terminal is attached, which is always the case here.
        def run(cmd, env, timeout):
            if cmd[:3] == ["modal", "app", "stop"] and not {"-y", "--yes"} & set(cmd):
                return 1, "", "Are you sure you want to stop App 'ap-1'? [y/N]: Aborted: no interactive terminal detected."
            return 0, "", ""

        with mock.patch.object(modal_.util, "run", run):
            self.assertEqual(modal_.stop({"provider": "modal"}, "ap-1")[0], True)

    def test_a_failed_lookup_names_an_unnamed_app_by_its_id(self):
        w = modal_._lookup_failed("history", [{"name": None, "native_id": "ap-x"}], "503")
        self.assertEqual(w["text"], "deploy history lookup failed for ap-x: 503; the deployer stays unknown until a "
                                    "lookup works.")

    def test_failed_lookups_are_named_in_the_digest(self):
        self.history_error = "modal ['app', 'history'] failed (1): 503"
        self.lookup_error = "modal_api failed (1): boom"
        res = self.collect(self.NOW, slow=False)
        self.assertEqual(self.lookups, [["ap-run", "ap-new"]])
        texts = ["modal:main: deploy history lookup failed for sweep (ap-new): modal ['app', 'history'] failed (1): "
                 "503; the deployer stays unknown until a lookup works.",
                 "modal:main: creator lookup failed for sweep-cell (ap-run), sweep (ap-new): modal_api failed (1): boom; "
                 "the owner stays unknown until a lookup works."]
        self.assertEqual(["modal:main: " + w["text"] for w in res["warnings"]], texts)
        self.assertEqual(["modal:main: " + w for w in self.summary["warnings"]], texts)   # `check` prints these
        self.assertEqual([(i["kind"], i["text"]) for i in self.db.q("select * from digest_items order by id")],
                         [("collector_warning", t) for t in texts])
        self.assertEqual(self.res("modal:main:ap-new")["ownership"], "unknown")
        self.collect(self.NOW + 600, slow=False)   # the same failures again: no second line today
        self.assertEqual(self.db.one("select count(*) n from digest_items")["n"], 2)


class Slurm(unittest.TestCase):
    """slurm.collect over a stand-in for ssh: the remote command runs in a local sh with squeue and month-cost scripts
    first on its PATH, so which exit status reaches the collector is decided by real shell semantics."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name)
        self.cfg = test_config(self.bin)

    def collect(self):
        real = util.run

        def ssh(cmd, timeout=120, **kw):   # ssh would log in to the cluster's login node
            self.assertEqual(cmd[0], "ssh")
            return real(["sh", "-c", cmd[-1]], env={"PATH": "%s:%s" % (self.bin, os.environ["PATH"])}, timeout=timeout)

        with mock.patch.object(util, "run", ssh):
            return slurm.collect(self.cfg["accounts"]["slurm:cluster"], {"now": T0})

    def test_failed_squeue_fails_the_collection(self):
        stub(self.bin, "squeue", "", 1, "slurm_load_jobs error: Unable to contact slurm controller (connect failure)\n")
        stub(self.bin, "month-cost", "Estimated total: $12.50\n")
        with self.assertRaisesRegex(RuntimeError, "squeue failed on cluster-login.*Unable to contact slurm controller"):
            self.collect()

    def test_paid_jobs_and_month_estimate_and_a_failed_cost_command(self):
        stub(self.bin, "squeue", "101|gpu-paid-h100|train|RUNNING|1-02:00:00\n102|compute|free|RUNNING|05:00\n"
                            "103|gpu-paid-l40|queued|PENDING|0:00\n")
        stub(self.bin, "month-cost", "Estimated total: $1,234.50\n")
        got = self.collect()
        self.assertEqual([(r["native_id"], r["partition"], r["launched_at"], r["cost_usd"]) for r in got["resources"]],
                         [("101", "gpu-paid-h100", T0 - 93600, 65.0)])
        self.assertEqual(got["metrics"], {"month_estimated_usd": 1234.5})
        stub(self.bin, "month-cost", "", 1, "month-cost: sacct failed\n")
        with self.assertRaisesRegex(RuntimeError, "sacct failed"):
            self.collect()


class LambdaCollect(unittest.TestCase):
    """lambda_.collect and idle_check. http_json stands in for Lambda's API and util.run for ssh to the box: both need
    credentials and the network."""

    BOX = {"id": "i-1", "name": "trainer", "status": "active", "ip": "1.2.3.4", "first_healthy": "2026-10-30T08:00:00Z",
           "ssh_key_names": ["alex-key"], "region": {"name": "us-east-1"},
           "instance_type": {"name": "gpu_1x_h100", "price_cents_per_hour": 249}, "jupyter_token": "secret"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = test_config(tmp.name)
        self.opts = check.collect_opts(None, self.cfg, "lambda:main", util.parse_time("2026-10-30T10:00:00Z"),
                                       slow=False, inventory_only=False)

    def probe(self, code, out, err):
        calls = []

        def ssh(cmd, timeout=120, **kw):
            calls.append(cmd)
            return code, out, err

        with mock.patch.object(util, "run", ssh):
            return lambda_.idle_check("1.2.3.4", "python.*train"), calls[0]

    def test_idle_check_names_what_failed(self):
        (ok, detail), argv = self.probe(0, "2\n7\n", "")
        self.assertEqual((ok, detail), (True, {"reachable": True, "work_procs": 2, "gpu_util_sum": 7}))
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertEqual(argv[-2:-1], ["ubuntu@1.2.3.4"])
        self.assertEqual(self.probe(255, "", "ssh: connect to host 1.2.3.4 port 22: Connection timed out\r\n")[0],
                         (False, {"reachable": False, "error": "ssh failed (exit 255): ssh: connect to host 1.2.3.4 "
                                                               "port 22: Connection timed out"}))
        self.assertEqual(self.probe(127, "3\n", "bash: line 1: bc: command not found\n")[0],
                         (False, {"reachable": True, "error": "remote command failed (exit 127): bash: line 1: bc: "
                                                              "command not found"}))

    def collect(self, body):
        self.probes = []

        def ssh(cmd, timeout=120, **kw):
            self.probes.append(cmd)
            return 0, "0\n3\n", ""

        with mock.patch.dict(os.environ, {"LAMBDA_API_KEY": "k"}), \
                mock.patch.object(lambda_, "http_json", lambda url, **kw: body), mock.patch.object(util, "run", ssh):
            return lambda_.collect({}, self.opts)

    def test_our_box_is_priced_and_probed(self):
        got = self.collect({"data": [self.BOX]})["resources"]
        self.assertEqual(len(got), 1)
        self.assertEqual({k: got[0][k] for k in ("rate_usd_h", "cost_usd", "ssh_keys", "idle")},
                         {"rate_usd_h": 2.49, "cost_usd": 4.98, "ssh_keys": ["alex-key"], "idle": True})
        self.assertNotIn("jupyter_token", got[0])

    def test_inventory_only_skips_the_idle_probe(self):
        self.opts["inventory_only"] = True
        got = self.collect({"data": [self.BOX]})["resources"]
        self.assertEqual(([r["native_id"] for r in got], self.probes), (["i-1"], []))
        self.assertNotIn("idle", got[0])

    def test_changed_api_shape_fails_the_collection(self):
        for body in ({"instances": [self.BOX]},
                     {"data": [self.BOX | {"instance_type": {"name": "gpu_1x_h100"}}]},
                     {"data": [{k: v for k, v in self.BOX.items() if k != "ssh_key_names"}]}):
            with self.subTest(body=body), self.assertRaises(KeyError):
                self.collect(body)


class ProviderApis(unittest.TestCase):
    """A provider API that changes shape fails the account's collection, so the blind alert fires, instead of reading
    as nothing running or $0. http_json stands in for each provider's HTTP API (credentials and the network)."""

    def test_hf_endpoints(self):
        def api(items):
            return lambda url, **kw: {"name": "alex"} if "whoami" in url else items

        with mock.patch.dict(os.environ, {"HF_TOKEN": "t"}):
            with mock.patch.object(hf, "http_json", api({"items": [{"name": "e", "status": {"state": "running",
                                                                                              "createdAt": None}}]})):
                self.assertEqual([r["native_id"] for r in hf.collect({}, {})["resources"]], ["alex/e"])
            with mock.patch.object(hf, "http_json", api({"endpoints": []})), self.assertRaises(KeyError):
                hf.collect({}, {})

    def test_openrouter_spend(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            with mock.patch.object(openrouter, "http_json",
                                   lambda url, **kw: {"data": {"usage_daily": 1.234, "usage_monthly": 9.876}}):
                self.assertEqual(openrouter.collect({}, {})["metrics"], {"day_spend_usd": 1.23, "month_spend_usd": 9.88})
            for body in ({"key": {"usage_daily": 1.0, "usage_monthly": 2.0}}, {"data": {"usage": 3.0}}):
                with self.subTest(body=body), mock.patch.object(openrouter, "http_json", lambda url, **kw: body), \
                        self.assertRaises(KeyError):
                    openrouter.collect({}, {})

    def test_modal_billing_summary_without_metered_cost(self):
        def fake(acct, args, timeout=120):   # the modal CLI needs credentials and the network
            return json.dumps({"billed_cost": "20"} if args[:2] == ["billing", "summary"] else [])

        with mock.patch.object(modal_, "_modal", fake), self.assertRaises(KeyError):
            modal_._billing({}, util.parse_time("2026-10-12T09:30:00Z"))

    def test_http_json_sends_the_bearer_token(self):
        sent = []

        def urlopen(req, timeout):   # the network
            sent.append(req)
            return io.BytesIO(b'{"ok": 1}')

        with mock.patch.object(urllib.request, "urlopen", urlopen):
            self.assertEqual(providers.http_json("https://example.invalid/x", bearer="tok"), {"ok": 1})
        self.assertEqual(sent[0].get_header("Authorization"), "Bearer tok")


if __name__ == "__main__":
    unittest.main()


class KitConfig(unittest.TestCase):
    """The kit's configuration: owner and repos from the mailroom, first-release rule keys, an empty identity."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_owner_comes_from_the_mailroom_and_repo_roots_from_repos_root(self):
        cfg = config.load(*write_config(self.dir, mailroom={"owner_name": "Alex", "allowlist": ["a@example.com"],
                                                            "repos_root": "~/src"}))
        self.assertEqual((cfg["owner_name"], cfg["owner_email"], cfg["repo_roots"]), ("Alex", "a@example.com", ["~/src"]))
        cfg = config.load(*write_config(self.dir, 'owner_name = "Sam"\nrepo_roots = ["/a", "/b"]\n' + TEST_CONFIG))
        self.assertEqual((cfg["owner_name"], cfg["owner_email"], cfg["repo_roots"]), ("Sam", "owner@example.com",
                                                                                     ["/a", "/b"]))
        cfg = config.load(*write_config(self.dir, mailroom={}))
        self.assertEqual((cfg["owner_name"], cfg["owner_email"]), ("the owner", None))

    def test_first_release_rule_keys_still_apply(self):
        text = "[rules.long_running]\nlambda_hours = 48\nhf_hours = 30\n[rules.storage]\nmodal_volumes_usd_month = 20\n"
        cfg = config.load(*write_config(self.dir, text))
        lr = cfg["rules"]["long_running"]
        self.assertEqual((lr["hours"]["lambda"], lr["hours"]["hf"], lr["hours"]["deepinfra"], lr["hours"]["modal"]),
                         (48, 30, 30, 12))
        self.assertNotIn("lambda_hours", lr)
        self.assertEqual(cfg["rules"]["storage"], {"volumes_usd_month": 20})

    def test_no_modal_users_leaves_ownership_unknown_and_never_auto_stops(self):
        cfg = config.load(*write_config(self.dir, '[accounts."modal:main"]\nprovider = "modal"\nauto_stop = true\n'))
        self.assertEqual(observe.derive(cfg, "modal:main", modal_app(), None, T0)[0]["ownership"], "unknown")
        row = {"provider": "modal", "account": "modal:main", "deployed_by": "alex-10001", "health": "crash_loop",
               "ownership": "unknown", "suppress_until": None, "suppress_health": None}
        self.assertEqual(policy.can_stop(row, "watchdog", cfg, T0, fresh_crash=True),
                         (False, "identity.owner_modal_users is empty, so the watchdog never stops anything itself"))

    def test_the_watchdogs_own_modal_user_counts_as_yours(self):
        cfg = test_config(self.dir)
        db = state.DB(":memory:")
        app = modal_app("ap-t", deployed_by="alex-token")
        observe.ingest(db, cfg, "modal:main", {"resources": [app], "metrics": {}}, T0)
        self.assertEqual(db.resource("modal:main:ap-t")["ownership"], "foreign")
        db.set_meta(spend.meta_key("modal:main"), {"token_user": "alex-token"})
        observe.ingest(db, cfg, "modal:main", {"resources": [app], "metrics": {}}, T0 + 600)
        self.assertEqual(db.resource("modal:main:ap-t")["ownership"], "ours")

    def test_accounts_lists_where_each_accounts_keys_come_from(self):
        out = io.StringIO()
        with mock.patch.object(config, "CONFIG_PATH", self.dir / "config.toml"), contextlib.redirect_stdout(out):
            cli.cmd_accounts(None, None, test_config(self.dir), T0)
        text = out.getvalue()
        self.assertIn("owner:  Alex <owner@example.com>", text)
        self.assertRegex(text, r"modal:team +modal +none +no keys")
        self.assertRegex(text, r"lambda:main +lambda +env-file +PROBLEM: no credentials: create .*lambda-main.env")

