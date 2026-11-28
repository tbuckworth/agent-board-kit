"""The mailroom with Gmail off (config "gmail": false, README.md "Running without Gmail"): no token and no Gmail call
anywhere. config.load gives kept mail a From address of its own (config.LOCAL_ACCOUNT); the daemon starts with no token
and routes the owner's Mail-tab replies, at a pass and within a second between passes, in any thread; mail to the
owner, and every alert, is kept for the Mail tab, and a security alert is also recorded in security-alerts.jsonl (mode
600) for the board; mail to anyone else is refused before the pre-send review, from the command line, the library, the
Mac proxy and the held drafts alike; release, rerun, recover and the orchestrator's replies work; status says Gmail is
off; and what needs Gmail (mailbox sync and backfill, --baseline, agent-mail-auth) refuses plainly.

Gmail is the boundary: fakes.fake_gmail stands in for it, and every one of its API calls (fakes.GMAIL_API) fails the
test, which also checks at its end that none was made. The other boundaries are stood in for as in test_mailbox:
deliver.deliver_session for agent-send into a session, orchestrate.run_event for the orchestrator's claude CLI run."""
import argparse
import contextlib
import email
import email.policy
import gc
import importlib.util
import io
import json
import stat
import sys
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from test_mailbox import INFO, OWNER, Mailbox, no_agent_env  # noqa: E402
from test_remote import SID  # noqa: E402
import fakes  # noqa: E402
import homes  # noqa: E402
from agentmail import (alerts, cli, config, deliver, gmail, mailbox, messages, outbound, review, routing,  # noqa: E402
                       rpc)

SECURITY = ["dkim", "collab-thread", "flood", "phone-enrolment", "fast-mode", "fast-keys"]   # ALERT_KINDS' order
OTHER = "sam@example.com"


def load_auth():
    """agent_mail_auth.py (the agent-mail-auth wrapper's script) as a fresh module."""
    spec = importlib.util.spec_from_file_location("agent_mail_auth", str(ROOT / "agent_mail_auth.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class NoGmail(Mailbox):
    """Mailbox's desktop with "gmail": false and no `account` in config.json, the session mailtab in the registry
    too, and no Gmail token. self.cfg is what config.load makes of that file."""

    def setUp(self):
        super().setUp()
        self.file = dict(json.loads(self.homes.config.read_text()), gmail=False, account=None)
        self.homes.config.write_text(json.dumps(self.file))
        self.cfg = config.load()
        self.ctx = outbound.Context(self.cfg)
        for name in fakes.GMAIL_API:
            getattr(self.fake, name).side_effect = AssertionError("Gmail called: gmail.%s" % name)
        self.addCleanup(self.assert_no_gmail)
        homes.patch(self, config, "SECURITY_ALERTS_PATH", homes.temp_dir(self) / "security-alerts.jsonl")
        homes.status_log(self)
        self.homes.claude(SID, name="mailtab")
        self.body = homes.temp_dir(self) / "body.txt"
        self.body.write_text("The run finished.")
        self.keys = iter("key%017d" % n for n in range(1000))
        self.assertFalse(config.TOKEN_PATH.exists())

    def assert_no_gmail(self):
        self.assertEqual([c for c in self.fake.mock_calls if c[0].split(".")[0] in fakes.GMAIL_API], [])

    def configure(self, **more):
        """config.json, and self.cfg and self.ctx, with more keys."""
        self.file.update(more)
        self.homes.config.write_text(json.dumps(self.file))
        self.cfg = config.load()
        self.ctx = outbound.Context(self.cfg)

    def tab_reply(self, thread, text):
        """The owner's reply in thread, written in the Mail tab: its inbound row."""
        with no_agent_env():
            got = mailbox.answer(self.st, self.cfg, thread, next(self.keys), text, [])
        return self.st.inbound(int(got["key"][1:]))

    def stored(self, sent):
        return email.message_from_bytes(messages.load(sent["eml"]), policy=email.policy.default)

    def recorded(self):
        """The lines of security-alerts.jsonl, parsed."""
        path = config.SECURITY_ALERTS_PATH
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def sends(self, *args, env=None):
        """agent-mail send ARGS --subject S --body-file F: (exit code, stdout, stderr)."""
        return self.main(["send"] + list(args) + ["--subject", "Results", "--body-file", str(self.body)], env=env)

    def run_daemon(self, *argv):
        """(exit code, stdout, stderr) of agent-mailroom argv on self.st, its signal handlers left alone, with a lock
        file of its own (an exception caught in an earlier run can keep that run's frame, and so its lock, alive). The
        daemon never closes its lock file, which a process's exit does: the ResourceWarning of an earlier run's,
        collected now, is not this run's output."""
        homes.patch(self, config, "LOCK_PATH", homes.temp_dir(self) / "mailroom.lock")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
            return self._run_daemon(argv)

    def _run_daemon(self, argv):
        with mock.patch.object(self.room, "Store", lambda: self.st), mock.patch.object(self.room.signal, "signal"), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.room.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class Config(NoGmail):
    def test_gmail_off_needs_no_account_and_kept_mail_comes_from_the_mailroom_s_own_address(self):
        self.assertEqual((self.cfg["gmail"], self.cfg["account"]), (False, config.LOCAL_ACCOUNT))
        self.assertEqual(config.LOCAL_ACCOUNT.rsplit("@", 1)[1], routing.LOCAL_DOMAIN)
        self.assertEqual(config.require_account(self.cfg), config.LOCAL_ACCOUNT)
        self.configure(account="Agents@Example.com")
        self.assertEqual(self.cfg["account"], "agents@example.com")                # an account given is kept
        self.assertTrue(config.gmail_on(config.DEFAULTS))                          # on unless config says otherwise
        self.assertIs(self.ctx.svc, gmail.OFF)                                     # no token is looked for

    def test_gmail_must_be_true_or_false_and_tools_say_so(self):
        self.configure()
        self.homes.config.write_text(json.dumps(dict(self.file, gmail="false")))
        with self.assertRaisesRegex(config.ConfigError, r'config `gmail` in .* must be true or false .*, not "false"'):
            config.load()
        code, out, err = self.main(["status"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("must be true or false", err)
        code, _out, err = self.run_daemon("--once")
        self.assertEqual(code, 2)
        self.assertIn("must be true or false", err)

    def test_gmail_off_holds_a_stand_in_that_names_the_switch_if_anything_uses_it(self):
        with self.assertRaisesRegex(gmail.GmailOff, r'Gmail is off in config \("gmail": false in '):
            gmail.send_raw(gmail.OFF, b"x")
        with self.assertRaisesRegex(gmail.GmailOff, "Gmail is off"):
            gmail.profile(gmail.OFF)

    def test_settings_that_need_gmail_take_no_effect_and_are_named(self):
        self.configure(mailbox={"owner_delivery": "gmail", "nudge_minutes": 30, "gmail_alerts": ["memory"]},
                       alert_to="pager@example.com", collaborators={OTHER: {"name": "Sam's agent"}},
                       remarkable={"address": "agents+rm@example.com"})
        self.assertEqual(outbound.mailbox_settings(self.cfg),
                         {"owner_delivery": "local", "nudge_minutes": None, "gmail_alerts": [], "fast_minutes": 5})
        self.assertEqual(config.alert_address(self.cfg), OWNER)
        self.assertEqual([line.split(":")[0] for line in outbound.gmail_ignored(self.cfg)],
                         ["mailbox.owner_delivery \"gmail\"", "mailbox.nudge_minutes", "mailbox.gmail_alerts", "alert_to",
                          "collaborators", "remarkable.address"])
        self.assertEqual(outbound.gmail_ignored(dict(self.cfg, gmail=True)), [])
        self.agent_mail()
        self.assertEqual(alerts.nudge(gmail.OFF, self.st, self.cfg, 10_000), 0)
        with self.assertRaisesRegex(ValueError, "nudge_minutes must be null or a whole number"):   # still checked
            outbound.mailbox_settings(dict(self.cfg, mailbox={"nudge_minutes": "30"}))


class Keeping(NoGmail):
    def test_mail_to_the_owner_is_kept_whole_and_a_tab_reply_to_it_finds_its_session(self):
        out = outbound.send_mail(self.ctx, self.sess, "owner", None, "Run results",
                                 outbound.Mail("The run finished.", None, [], []), "test")
        sent = self.st.sent_row(self.st.max_sent_id())
        kept = self.stored(sent)
        self.assertIsNone(sent["gmail_id"])
        self.assertTrue(sent["thread_id"].startswith("L"), sent["thread_id"])
        (sender,), (reply_to,) = kept["From"].addresses, kept["Reply-To"].addresses
        self.assertEqual((sender.display_name, sender.addr_spec), ("Agents (mailtab (claude))", config.LOCAL_ACCOUNT))
        self.assertEqual(reply_to.addr_spec, routing.tagged_address(config.LOCAL_ACCOUNT, self.sess["tag"]))
        self.assertEqual((kept["To"], kept["Message-ID"]), (OWNER, sent["message_id"]))
        self.assertTrue(sent["message_id"].endswith("@%s>" % routing.LOCAL_DOMAIN), sent["message_id"])
        self.assertIsNotNone(kept["Date"].datetime)
        self.assertTrue(out.summary.startswith("sent to %s in the Mail tab" % OWNER), out.summary)
        row = self.tab_reply(sent["thread_id"], "Rerun it.")
        self.assertEqual((row["matched_tag"], row["match_rule"]), (self.sess["tag"], "in-reply-to"))
        answer = outbound.reply_mail(self.ctx, self.sess, row, outbound.Mail("Done.", None, [], []), "test", None, None)
        reply = self.st.sent_row(self.st.max_sent_id())
        self.assertEqual((reply["thread_id"], reply["gmail_id"]), (sent["thread_id"], None))
        self.assertIn("replied to %s in the Mail tab" % OWNER, answer.summary)

    def test_the_orchestrator_s_reply_to_the_owner_is_kept(self):
        report = self.agent_mail(subject="Daily report", sess=None)            # unrouted: replies go to the orchestrator
        row = self.tab_reply(report["thread_id"], "Close it.")
        self.assertIsNone(row["matched_tag"])
        code, out, err = self.main(["reply", str(row["id"]), "--body-file", str(self.body), "--no-review", "test"],
                                   env={"AGENT_ORCHESTRATOR": "1"})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("replied to %s in the Mail tab" % OWNER, out)
        self.assertIn("from orchestrator", out)
        reply = self.st.sent_row(self.st.max_sent_id())
        self.assertEqual((reply["thread_id"], reply["gmail_id"]), (report["thread_id"], None))


class Alerts(NoGmail):
    def check_recorded(self, kinds, subjects, threads, before):
        lines = self.recorded()
        self.assertEqual([(r["kind"], r["subject"], r["thread_id"]) for r in lines], list(zip(kinds, subjects, threads)))
        for r in lines:
            self.assertEqual(sorted(r), ["at", "kind", "subject", "thread_id"])
            self.assertIsInstance(r["at"], float)
            self.assertTrue(before <= r["at"] <= time.time(), r)
        self.assertEqual(stat.S_IMODE(config.SECURITY_ALERTS_PATH.stat().st_mode), 0o600)

    def test_every_alert_is_kept_and_a_security_one_is_also_recorded_for_the_board(self):
        self.assertEqual(outbound.SECURITY_ALERTS, set(SECURITY))
        config.SECURITY_ALERTS_PATH.write_text("")
        config.SECURITY_ALERTS_PATH.chmod(0o644)                    # a file made by hand becomes the owner's alone
        before, threads = time.time(), {}
        cfg = dict(self.cfg, alert_to="pager@example.com")         # ignored: only mail to the owner is kept
        for kind in outbound.ALERT_KINDS:
            with self.subTest(kind=kind):
                self.assertFalse(outbound.alert_through_gmail(cfg, kind))
                self.assertTrue(alerts.send_alert(gmail.OFF, self.st, cfg, kind, "key", "about " + kind, "Body."))
                sent = self.st.sent_row(self.st.max_sent_id())
                self.assertEqual((sent["to_addr"], sent["subject"], sent["gmail_id"]),
                                 (OWNER, "[mailroom] about " + kind, None))
                self.assertEqual(self.stored(sent)[routing.MAILROOM_HEADER], routing.ALERT_MARK)
                threads[kind] = sent["thread_id"]
        self.check_recorded(SECURITY, ["[mailroom] about " + k for k in SECURITY], [threads[k] for k in SECURITY],
                            before)
        self.assertEqual(len(mailbox.list_threads(self.st, self.cfg, "inbox", None, 100, time.time())["threads"]),
                         len(outbound.ALERT_KINDS))                 # each in the Mail tab

    def test_a_tool_s_alert_from_the_command_line_is_kept_and_recorded_alike(self):
        before, threads = time.time(), []
        for kind in outbound.ALERT_KINDS:
            with self.subTest(kind=kind):
                code, out, err = self.sends("--unrouted", "--alert", kind, "--to", "owner")
                self.assertEqual((code, err), (0, ""))
                sent = self.st.sent_row(self.st.max_sent_id())
                self.assertEqual((sent["gmail_id"], sent["to_addr"]), (None, OWNER))
                self.assertTrue(out.startswith("sent to %s in the Mail tab | subject 'Results'" % OWNER), out)
                if kind in SECURITY:
                    threads.append(sent["thread_id"])
        self.check_recorded(SECURITY, ["Results"] * len(SECURITY), threads, before)

    def test_an_alert_not_kept_records_nothing(self):
        self.assertFalse(alerts.send_alert(gmail.OFF, self.st, dict(self.cfg, alerts_enabled=False), "fast-keys", "k",
                                           "s", "b"))
        self.assertFalse(alerts.send_alert(gmail.OFF, self.st, dict(self.cfg, allowlist=[]), "fast-keys", "k", "s",
                                           "b"))                                              # no owner address
        self.assertEqual((self.st.max_sent_id(), self.recorded()), (0, []))

    def test_with_gmail_on_a_security_alert_goes_through_gmail_and_is_not_recorded(self):
        for name in ("send_raw", "get_metadata"):
            getattr(self.fake, name).side_effect = None
        self.fake.send_raw.side_effect = self.send
        self.fake.get_metadata.side_effect = lambda svc, gid, names: {
            "payload": {"headers": [{"name": "Message-ID", "value": "<%s@mail.gmail.com>" % gid}]}}
        self.addCleanup(self.fake.reset_mock)                       # these calls are wanted
        local = dict(self.cfg, gmail=True, account="agents@example.com", mailbox={"owner_delivery": "local"})
        self.assertTrue(alerts.send_alert(self.fake, self.st, local, "phone-enrolment", "k", "a passkey", "b"))
        self.assertEqual((len(self.sent), self.recorded()), (1, []))


class Refused(NoGmail):
    """Mail to anyone but the owner needs Gmail: refused (exit outbound.NEEDS_GMAIL) before the pre-send review runs,
    with nothing sent or kept."""

    def setUp(self):
        super().setUp()
        homes.patch(self, review, "gate", mock.Mock(side_effect=AssertionError("the pre-send review ran")))

    def nothing_kept(self):
        self.assertEqual(self.st.max_sent_id(), 0)
        self.assertFalse(config.MESSAGES_DIR.exists() and any(config.MESSAGES_DIR.rglob("*.eml")))

    def test_from_the_command_line(self):
        session = {"CLAUDE_CODE_SESSION_ID": SID}
        for args, env in ((["--to", OTHER], session), (["--to", "owner", "--cc", OTHER], session),
                          (["--unrouted", "--to", OTHER], None), (["--unrouted", "--alert", "memory", "--to", OTHER], None)):
            with self.subTest(args=args):
                code, out, err = self.sends(*args, env=env)
                self.assertEqual((code, out), (outbound.NEEDS_GMAIL, ""))
                self.assertIn('agent-mail: Gmail is off (config "gmail": false), so agent mail goes only to Alex, kept '
                              "for the board's Mail tab; sam@example.com is not an owner address (config allowlist). "
                              "Nothing was sent or kept.", err)
        self.nothing_kept()

    def test_from_the_library_and_a_reply_to_someone_else(self):
        mail = outbound.Mail("Hello.", None, [], [])
        for call in (lambda: outbound.send_mail(self.ctx, self.sess, OTHER, None, "S", mail, None),
                     lambda: outbound.send_mail(self.ctx, self.sess, OWNER, OTHER, "S", mail, None),
                     lambda: outbound.transmit(self.ctx, None, OTHER, None, "S", mail, None, through_gmail=True)):
            with self.assertRaises(outbound.MailError) as refused:
                call()
            self.assertEqual(refused.exception.exit_code, outbound.NEEDS_GMAIL)
        theirs = self.st.inbound(self.st.record_inbound(
            gmail_id="g9", thread_id="t9", message_id="<g9@mail.gmail.com>", from_addr=OTHER, subject="Data?",
            matched_tag=self.sess["tag"], action="delivered", body="Can you send the data?"))   # from before the switch
        with self.assertRaisesRegex(outbound.MailError, "sam@example.com is not an owner address"):
            outbound.reply_mail(self.ctx, self.sess, theirs, mail, None, None, None)
        self.nothing_kept()

    def test_from_the_mac_through_the_proxy(self):
        code = json.loads(rpc.serve(self.ctx, json.dumps(self.request(args={"to": OTHER, "subject": "x"}))))
        self.assertEqual(code["ok"], False)
        self.assertIn("sam@example.com is not an owner address", code["error"])
        self.nothing_kept()
        homes.patch(self, review, "gate", mock.Mock(return_value=(None, None, None)))
        done = json.loads(rpc.serve(self.ctx, json.dumps(self.request(args={"to": "owner", "subject": "x"}))))
        self.assertTrue(done["ok"], done)
        self.assertTrue(done["result"].startswith("sent to %s in the Mail tab | subject 'x [#" % OWNER), done)
        self.assertIsNone(self.st.sent_row(self.st.max_sent_id())["gmail_id"])

    def test_a_held_draft_to_someone_else_is_dropped_not_sent(self):
        draft = dict({"op": "send", "to": OTHER, "cc": None, "subject": "Data"},
                     **outbound.Mail("Here it is.", None, [], []).as_json())
        self.st.record_review(self.sess["tag"], 1, "revise", "Data", kind="other", draft=json.dumps(draft),
                              audience="other")
        self.st.con.execute("update reviews set created_at=created_at-7200")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            outbound.flush_drafts(self.ctx)
        self.assertIn("dropped held draft 'Data': Gmail is off", out.getvalue())
        row = self.st.con.execute("select * from reviews order by id desc limit 1").fetchone()
        self.assertEqual(row["verdict"], "dropped")
        self.assertEqual(self.st.stale_drafts(time.time() + 1), [])
        self.nothing_kept()


class Daemon(NoGmail):
    def test_it_starts_with_no_token_and_routes_a_tab_reply_at_its_pass(self):
        first = self.agent_mail()
        row = self.tab_reply(first["thread_id"], "Ship it.")
        with mock.patch.object(deliver, "deliver_session", return_value=(0, "sent")) as send:
            code, out, err = self.run_daemon("--once")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("mailroom up: Gmail off, agent mail to the owner kept for the Mail tab; allowlist %s" % OWNER, out)
        self.assertIn("tab    #%d from %s" % (row["id"], OWNER), out)
        self.assertIn("once: 1 new message(s)", out)
        send.assert_called_once()
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")
        self.assertIsNotNone(self.st.get_meta("last_pass"))
        self.assertIsNone(self.st.get_meta("history_id"))

    def test_between_passes_a_tab_reply_is_routed_within_a_second_in_any_thread(self):
        self.st.record_sent(self.sess["tag"], "g1", "t1", "<g1@mail.gmail.com>", OWNER, "Old [#%s]" % self.sess["tag"],
                            cc_addr=None, body="From before the switch.", eml=None, attachments=None)   # Gmail's
        row = self.tab_reply("t1", "And now?")
        slept, delivered = [], []

        def sent(*args, **kwargs):
            delivered.append(len(slept))
            return 0, "sent"
        with mock.patch.object(self.room.time, "sleep", side_effect=slept.append), \
                mock.patch.object(deliver, "deliver_session", side_effect=sent), \
                contextlib.redirect_stdout(io.StringIO()):
            self.room.between_polls(gmail.OFF, self.st, self.cfg, 30, False)
        self.assertEqual((len(slept), delivered), (30, [1]))
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")

    def test_a_tab_reply_to_a_gone_session_reaches_the_orchestrator_with_the_thread_from_the_store(self):
        first = self.agent_mail(text="The results you asked for.")
        row = self.tab_reply(first["thread_id"], "Rerun it.")
        with mock.patch.object(deliver, "deliver_session", return_value=(4, "no such session")), \
                mock.patch.object(self.room.orchestrate, "run_event", return_value=INFO) as run:
            code, out, err = self.run_daemon("--once")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("is gone (4: no such session); to the orchestrator", out)
        text = run.call_args.args[0]
        self.assertIn("agent (ours)]\nThe results you asked for.", text)
        self.assertTrue(text.endswith("--- latest message ---\nRerun it."), text)
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")

    def test_a_failed_reply_is_held_with_a_kept_notice_and_released_rerun_and_recovered_without_gmail(self):
        first = self.agent_mail()
        row = self.tab_reply(first["thread_id"], "Ship it.")
        with mock.patch.object(deliver, "deliver_session", side_effect=RuntimeError("agent-send broke")):
            code, out, _ = self.run_daemon("--once")
        self.assertEqual(code, 0)
        self.assertIn("error  mailbox:", out)
        self.assertEqual(self.st.inbound(row["id"])["action"], "held")
        notice = self.st.sent_row(self.st.max_sent_id())
        self.assertEqual((notice["subject"], notice["gmail_id"]), ("[mailroom] mail #%d hit an error and is held"
                                                                   % row["id"], None))
        with mock.patch.object(deliver, "deliver_session", return_value=(0, "sent")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.am.do_release(self.ctx, argparse.Namespace(inbound_id=row["id"], target="origin", cli=None))
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")
        self.st.update_inbound(row["id"], action="held")
        with mock.patch("agentmail.orchestrate.run_event", return_value=INFO), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.am.do_rerun(self.ctx, argparse.Namespace(inbound_id=row["id"])), 0)
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")
        with mock.patch("agentmail.recovery.recover", return_value={"status": "unresolved"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.am.do_recover(self.ctx, argparse.Namespace(inbound_id=row["id"], query=None,
                                                                             refresh=False)), 0)

    def test_baseline_needs_gmail(self):
        code, out, err = self.run_daemon("--baseline")
        self.assertEqual(code, outbound.NEEDS_GMAIL)
        self.assertIn('--baseline resets the Gmail history cursor, and Gmail is off in config ("gmail": false', err)

    def test_settings_that_need_gmail_are_warned_of_as_it_starts(self):
        self.configure(mailbox={"nudge_minutes": 30})
        code, _out, err = self.run_daemon("--once")
        self.assertEqual(code, 0)
        self.assertIn("agent-mailroom: warning: Gmail is off, so this config takes no effect: mailbox.nudge_minutes: "
                      "no nudge, which is a mail through Gmail", err)


class Researcher(NoGmail):
    """Researcher mode needs no Gmail: the switch, its control to the agent, the request lookup and the owner's direct
    reply all work on kept mail and Mail-tab replies."""

    def test_the_switch_the_control_and_an_approval_by_tab_reply_work_without_gmail(self):
        homes.patch(self, cli, "agent_ancestor", lambda pid, stop=None: None)
        root = homes.researcher_install(self.homes.default, "claude", "0.7.0")
        self.configure(review={"enabled": False})
        code, out, err = self.sends("--to", "owner", env={"CLAUDE_CODE_SESSION_ID": SID})
        self.assertEqual(code, 0, err)
        request = self.st.sent_row(self.st.max_sent_id())
        self.assertIsNone(request["gmail_id"])
        code, out, err = self.main(["mailbox", "researcher", "--expect-tag", request["tag"], "--",
                                    request["thread_id"], "on"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["thread"]["researcher"],
                         {"on": True, "agent": "mailtab (claude)", "tag": request["tag"], "delivery": "pending",
                          "delivery_detail": None})
        # agent-send into the session is the boundary; the daemon's wait between passes sends the control
        with mock.patch.object(deliver.subprocess, "run", return_value=mock.Mock(
                returncode=0, stdout="sent", stderr="")) as send, mock.patch.object(self.room.time, "sleep"):
            self.room.between_polls(gmail.OFF, self.st, self.cfg, 1, False)
        self.assertIn(str(root), send.call_args.kwargs["input"])
        self.assertIn("Send Alex every decision request", send.call_args.kwargs["input"])
        self.assertIn("agent-mail send --to owner", send.call_args.kwargs["input"])
        code, out, err = self.main(["researcher-request", "--", request["tag"], request["thread_id"]])
        self.assertEqual((code, json.loads(out)), (0, {"thread_id": request["thread_id"],
                                                        "message_id": request["message_id"]}), err)
        reply = self.tab_reply(request["thread_id"], "Approve")
        self.assertEqual(reply["in_reply_to"], request["message_id"])

    def test_a_missing_plugin_is_named_in_the_mail_tab(self):
        homes.patch(self, cli, "agent_ancestor", lambda pid, stop=None: None)
        sent = self.agent_mail()
        code, _out, err = self.main(["mailbox", "researcher", "--expect-tag", sent["tag"], "--", sent["thread_id"], "on"])
        self.assertEqual(code, 0, err)
        with mock.patch.object(deliver.subprocess, "run", side_effect=AssertionError("nothing is sent")), \
                mock.patch.object(self.room.time, "sleep"):
            self.room.between_polls(gmail.OFF, self.st, self.cfg, 1, False)
        item = self.listed()[sent["thread_id"]]["researcher"]
        self.assertEqual(item["delivery"], "unavailable")
        self.assertIn("the researcher plugin is not installed for this claude login from marketplace 'agent-kit'",
                      item["delivery_detail"])
        self.assertIn("https://github.com/tbuckworth/ai-safety-researcher", item["delivery_detail"])


class Commands(NoGmail):
    def test_status_says_gmail_is_off_and_asks_gmail_nothing(self):
        self.configure(mailbox={"nudge_minutes": 30})
        code, out, err = self.main(["status"])
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], 'gmail         off (config "gmail": false): nothing is sent through, read from or '
                                   "polled in Gmail")
        self.assertEqual(lines[1], "account       none: kept mail is from %s" % config.LOCAL_ACCOUNT)
        self.assertIn("last_pass     -", lines)
        self.assertIn("owner mail    kept for the Mail tab, every alert too; security alerts (%s) also recorded in %s"
                      % (", ".join(SECURITY), config.SECURITY_ALERTS_PATH), lines)
        self.assertIn("other mail    refused: mail to anyone but Alex needs Gmail", lines)
        self.assertIn("ignored       mailbox.nudge_minutes: no nudge, which is a mail through Gmail", lines)
        for gmail_only in ("history_id", "last_poll", "require_dkim", "mailbox "):
            self.assertFalse([line for line in lines if line.startswith(gmail_only)], gmail_only)

    def test_what_needs_gmail_refuses_plainly(self):
        for argv, what in ((["mailbox", "backfill"], "mailbox backfill"), (["mailbox", "sync"], "mailbox sync"),
                           (["remarkable", "1", "--dry-run"], "remarkable")):
            with self.subTest(argv=argv):
                code, out, err = self.main(argv, stdin='{"inbox": [], "unread": []}')
                self.assertEqual((code, out), (outbound.NEEDS_GMAIL, ""))
                self.assertIn("agent-mail: %s" % what, err)
                self.assertIn('needs Gmail, which is off in config ("gmail": false in %s)' % config.CONFIG_PATH, err)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(load_auth().main([]), 2)
        self.assertIn('Gmail is off in %s ("gmail": false), so the mailroom needs no Gmail token' % config.CONFIG_PATH,
                      err.getvalue())
        self.assertFalse(config.TOKEN_PATH.exists())

    def test_the_mailbox_commands_the_tab_uses_work(self):
        first = self.agent_mail()
        code, out, _ = self.main(["mailbox", "list"])
        self.assertEqual(code, 0)
        (listed,) = json.loads(out)["threads"]
        self.assertEqual(listed["id"], first["thread_id"])
        code, out, _ = self.main(["mailbox", "show", first["thread_id"]])
        self.assertEqual(code, 0)
        self.assertIn("The run finished.", json.loads(out)["document"])
        code, out, _ = self.main(["mailbox", "summary"])
        self.assertEqual((code, json.loads(out)["unread"]), (0, 1))

    def test_mail_sent_through_gmail_before_the_switch_shows_no_gmail_link(self):
        self.st.record_sent(self.sess["tag"], "g1", "t1", "<g1@mail.gmail.com>", OWNER, "Old [#%s]" % self.sess["tag"],
                            cc_addr=None, body="From before the store.", eml=None, attachments=None)
        shown = mailbox.show(self.st, self.cfg, "t1", time.time())
        self.assertIn("From before the store.", shown["document"])
        self.assertNotIn("Open in Gmail", shown["document"])


if __name__ == "__main__":
    unittest.main()
