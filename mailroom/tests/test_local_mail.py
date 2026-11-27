"""Mail to the owner kept for the Mail tab alone (config mailbox.owner_delivery "local"): transmit keeping it with its own
Message-ID, Date and thread instead of sending it, mail to anyone else still sent, which alerts still go through Gmail,
the nudge, its widgets kept as Gmail mail keeps them, its page through the sanitizer, tab replies to kept mail routed
(with files, and into a group) or refused with mailbox.ANSWER_REFUSED, the orchestrator's thread read from the store,
sync with old Gmail mail beside kept mail, switching back, and what reads Gmail ids or threads.
Gmail is the boundary (fakes.fake_gmail); the boundaries TabReplies names in test_mailbox are stood in for the same
way here: deliver.deliver_session for agent-send into a session, orchestrate.run_event for the orchestrator's claude
CLI run, recovery.recover for the transcript search."""
import argparse
import base64
import contextlib
import email
import email.policy
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from test_mailbox import INFO, KEY, OWNER, PNG, Mailbox, no_agent_env  # noqa: E402
import test_mailbox  # noqa: E402  (its Widgets' blocks; a TestCase imported by name would run here too)
from test_groups import A, OWNER as OWNER_ACTOR, Base as GroupBase  # noqa: E402
from test_remote import CFG, CFG_FILE, MAC_FILE, ORIGIN, SID  # noqa: E402
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import (alerts, config, deliver, groupmail, mailbox, mailroom, messages,  # noqa: E402
                       outbound, recovery, replyfiles, routing, rpc)
from agentmail.store import Store  # noqa: E402

LOCAL = dict(CFG, mailbox={"owner_delivery": "local", "nudge_minutes": 30})
QUIET = dict(CFG, mailbox={"owner_delivery": "local"})   # no nudge
THREAD_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")   # the board's rule for a thread id


class Local(Mailbox):
    """Mailbox's desktop, with mail to the owner kept for the Mail tab alone."""

    def setUp(self):
        super().setUp()
        self.ctx = outbound.Context(LOCAL)

    def stored(self, sent):
        return email.message_from_bytes(messages.load(sent["eml"]), policy=email.policy.default)

    def poll(self, cfg):
        self.fake.history_since.return_value = ([], "after")
        self.st.set_meta("history_id", "before")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.room.poll(self.fake, self.st, cfg, False)
        return out.getvalue()

    def tab_reply(self, thread, text):
        return self.st.inbound(int(self.answer(thread, text)["key"][1:]))


class Keeping(Local):
    def test_mail_to_the_owner_is_kept_with_its_own_message_id_date_and_thread_and_not_sent(self):
        out = outbound.send_mail(self.ctx, self.sess, "owner", None, "Run results",
                                 outbound.Mail("The run finished.", None, [], []), "test")
        sent = self.st.sent_row(self.st.max_sent_id())
        self.fake.send_raw.assert_not_called()
        self.fake.get_metadata.assert_not_called()
        self.assertIsNone(sent["gmail_id"])
        self.assertTrue(sent["thread_id"].startswith("L") and THREAD_ID.fullmatch(sent["thread_id"]), sent["thread_id"])
        self.assertRegex(sent["message_id"], r"^<[^<>@\s]+@agent-mailroom\.local>$")
        kept = self.stored(sent)
        self.assertEqual((kept["Message-ID"], kept["To"]), (sent["message_id"], OWNER))
        self.assertIsNotNone(kept["Date"].datetime)
        self.assertTrue(out.summary.startswith("sent to %s in the Mail tab | subject 'Run results [#%s]' | thread %s |"
                                               % (OWNER, self.sess["tag"], sent["thread_id"])), out.summary)
        self.assertNotEqual(self.agent_mail(subject="Other")["thread_id"], sent["thread_id"])   # a new thread each

    def test_mail_to_anyone_else_goes_through_gmail_as_before(self):
        for to, cc in (("sam@example.com", None), (OWNER, "sam@example.com")):
            with self.subTest(to=to, cc=cc):
                sent = self.agent_mail(to=to, cc=cc)
                self.assertEqual(self.raws[-1], messages.load(sent["eml"]))
                self.assertEqual(sent["gmail_id"], "g%d" % len(self.raws))
                self.assertEqual(sent["message_id"], "<g%d@mail.gmail.com>" % len(self.raws))

    def test_a_reply_stays_in_its_thread_a_gmail_one_included(self):
        first = self.agent_mail()
        answer = outbound.reply_mail(self.ctx, self.sess, self.tab_reply(first["thread_id"], "Rerun it."),
                                     outbound.Mail("Done.", None, [], []), "test", None, None)
        reply = self.st.sent_row(self.st.max_sent_id())
        self.assertEqual((reply["thread_id"], reply["gmail_id"]), (first["thread_id"], None))
        self.assertEqual(self.stored(reply)["In-Reply-To"], routing.local_message_id(KEY))
        self.assertIn("replied to %s in the Mail tab" % OWNER, answer.summary)
        old = self.agent_mail(subject="Before the switch", to="sam@example.com", cc=OWNER)   # through Gmail
        self.agent_mail(inb=self.his_mail(old["thread_id"], gid="in9"))
        later = self.st.sent_row(self.st.max_sent_id())
        self.assertEqual((later["thread_id"], later["gmail_id"]), (old["thread_id"], None))   # one thread in the tab
        self.assertEqual(len(self.raws), 1)

    def test_the_settings_are_checked(self):
        for box, said in (({"owner_delivery": "phone"}, "owner_delivery must be one of gmail, local"),
                          ({"nudge_minutes": 0}, "nudge_minutes must be null or a whole number"),
                          ({"nudge_minutes": "30"}, "nudge_minutes must be null or a whole number"),
                          ({"gmail_alerts": ["loop", "memroy"]}, "gmail_alerts must be a list of alert kinds"),
                          ({"gmail_alerts": "loop"}, "gmail_alerts must be a list of alert kinds"),
                          ({"fast_minutes": 0}, "fast_minutes must be a whole number of minutes"),
                          ({"fast_minutes": None}, "fast_minutes must be a whole number of minutes"),
                          ({"fast_minutes": True}, "fast_minutes must be a whole number of minutes")):
            with self.subTest(box=box), self.assertRaisesRegex(ValueError, said):
                outbound.mailbox_settings(dict(CFG, mailbox=box))
        self.assertEqual(outbound.mailbox_settings(CFG),
                         {"owner_delivery": "gmail", "nudge_minutes": None, "gmail_alerts": [], "fast_minutes": 5})

    def test_agent_mail_sent_says_which_mail_is_in_the_mail_tab(self):
        self.agent_mail(subject="Kept")
        self.agent_mail(subject="Emailed", to="sam@example.com")
        code, out, _ = self.main(["sent"])
        kept, = [line for line in out.splitlines() if "Kept" in line]
        emailed, = [line for line in out.splitlines() if "Emailed" in line]
        self.assertTrue(kept.endswith("Kept [#%s]  (in the Mail tab)" % self.sess["tag"]), kept)
        self.assertTrue(emailed.endswith("Emailed [#%s]" % self.sess["tag"]), emailed)

    def test_status_says_where_mail_to_the_owner_goes(self):
        homes.config_file(self, dict(CFG_FILE, mailbox={"owner_delivery": "local", "nudge_minutes": 30}))
        code, out, _ = self.main(["status"])
        self.assertIn("owner mail    kept for the Mail tab; nudge every 30 min, last -; alerts through Gmail: dkim, "
                      "collab-thread, flood, phone-enrolment, fast-mode, fast-keys, voicemail\n", out)


class AlertKinds(Local):
    """Which alerts and notices go through Gmail while mail to the owner is kept for the tab
    (outbound.alert_through_gmail):
    the security kinds and PERSONAL_MAIL always, the kinds config mailbox.gmail_alerts names, no other; each kept in
    the store all the same. The same rule for the mailroom's own alerts and for `agent-mail send --unrouted --alert KIND`; with Gmail
    delivery, every one through Gmail as before."""

    SECURITY = {"dkim", "collab-thread", "flood", "phone-enrolment", "fast-mode", "fast-keys"}
    PERSONAL = {"voicemail"}
    CHOSEN = ["loop", "reply-recovery", "extra-usage-spend", "memory", "cloud-cost"]

    def alert(self, cfg, kind):
        """alerts.send_alert of kind under cfg: whether it went through Gmail. It is kept in the store either way."""
        before = len(self.raws)
        self.assertTrue(alerts.send_alert(self.fake, self.st, cfg, kind, "key", "about " + kind, "Body."))
        sent = self.st.sent_row(self.st.max_sent_id())
        went = len(self.raws) > before
        self.assertEqual((sent["subject"], sent["gmail_id"] is not None), ("[mailroom] about " + kind, went))
        self.assertIsNotNone(messages.load(sent["eml"]))
        return went

    def notice(self, box, *flags):
        """agent-mail send --to owner with flags, under config mailbox box: (exit code, stderr, whether it went through
        Gmail, the sent row or None)."""
        homes.config_file(self, dict(CFG_FILE, mailbox=box))
        body = homes.temp_dir(self) / "body.txt"
        body.write_text("Something happened.")
        before, last = len(self.raws), self.st.max_sent_id()
        code, _out, err = self.main(["send", "--to", "owner", "--subject", "notice", "--body-file", str(body)]
                                    + list(flags))
        sent = self.st.sent_row(self.st.max_sent_id()) if self.st.max_sent_id() > last else None
        return code, err, len(self.raws) > before, sent

    def test_the_security_kinds_and_personal_mail_always_go_through_gmail_and_the_rest_is_kept(self):
        local = dict(CFG, mailbox={"owner_delivery": "local", "gmail_alerts": []})
        self.assertEqual({k for k in outbound.ALERT_KINDS if self.alert(local, k)}, self.SECURITY | self.PERSONAL)
        self.assertEqual(outbound.SECURITY_ALERTS, self.SECURITY)
        self.assertEqual(outbound.PERSONAL_MAIL, self.PERSONAL)

    def test_a_voicemail_goes_through_gmail_and_its_alerts_as_configured(self):
        for box, alerts_too in (({"owner_delivery": "local"}, False),
                                ({"owner_delivery": "local", "gmail_alerts": ["voicemail-alert"]}, True)):
            with self.subTest(box=box):
                code, err, went, sent = self.notice(box, "--unrouted", "--alert", "voicemail")
                self.assertEqual((code, err, went, sent["gmail_id"] is not None), (0, "", True, True))
                self.assertIsNotNone(messages.load(sent["eml"]))   # in the tab as well
                code, err, went, sent = self.notice(box, "--unrouted", "--alert", "voicemail-alert")
                self.assertEqual((code, err, went), (0, "", alerts_too))
                self.assertIsNotNone(messages.load(sent["eml"]))

    def test_every_kind_goes_as_configured_in_process_and_from_the_command_line(self):
        box = {"owner_delivery": "local", "gmail_alerts": self.CHOSEN}
        for kind in outbound.ALERT_KINDS:
            with self.subTest(kind=kind):
                wanted = kind in self.SECURITY or kind in self.PERSONAL or kind in self.CHOSEN
                self.assertEqual(self.alert(dict(CFG, mailbox=box), kind), wanted)
                code, err, went, sent = self.notice(box, "--unrouted", "--alert", kind)
                self.assertEqual((code, err, went, sent["gmail_id"] is not None), (0, "", wanted, wanted))
                self.assertIsNotNone(messages.load(sent["eml"]))

    def test_with_gmail_delivery_every_kind_goes_through_gmail_as_before(self):
        for kind in outbound.ALERT_KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(self.alert(CFG, kind))
                self.assertEqual(self.notice({}, "--unrouted", "--alert", kind)[:3], (0, "", True))

    def test_an_unknown_kind_or_one_without_unrouted_is_refused_and_nothing_is_sent_or_kept(self):
        for box in ({"owner_delivery": "local"}, {}):
            code, err, went, sent = self.notice(box, "--unrouted", "--alert", "cloud-cots")
            self.assertEqual((code, went, sent), (1, False, None))
            self.assertIn("--alert: 'cloud-cots' is no alert kind; the kinds are dkim, collab-thread,", err)
            code, err, went, sent = self.notice(box, "--alert", "memory")
            self.assertEqual((code, went, sent), (1, False, None))
            self.assertIn("--alert goes with --unrouted", err)
        with self.assertRaisesRegex(ValueError, "'cloud-cots' is no alert kind"):
            alerts.send_alert(self.fake, self.st, LOCAL, "cloud-cots", "key", "about", "Body.")
        self.assertEqual(self.st.max_sent_id(), 0)

    def test_a_send_with_alert_on_the_mac_is_refused_before_it_is_proxied(self):
        homes.config_file(self, MAC_FILE)
        body = homes.temp_dir(self) / "body.txt"
        body.write_text("Something happened.")
        with mock.patch.object(self.am, "resolve_session", return_value=dict(ORIGIN)), \
                mock.patch.object(rpc, "proxy", return_value=("sent", False)) as proxy:
            code, out, err = self.main(["send", "--to", "owner", "--subject", "notice", "--body-file", str(body),
                                        "--alert", "memory"])
        proxy.assert_not_called()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--alert is not supported through the proxy", err)


class Alerts(Local):
    def test_an_alert_is_kept_too_and_a_tab_reply_to_it_reaches_the_orchestrator(self):
        self.assertTrue(alerts.send_alert(self.fake, self.st, LOCAL, "process-error", "X", "mail #3 is held", "Why."))
        self.fake.send_raw.assert_not_called()
        sent = self.st.sent_row(self.st.max_sent_id())
        kept = self.stored(sent)
        self.assertEqual((sent["tag"], sent["subject"], sent["gmail_id"]), ("", "[mailroom] mail #3 is held", None))
        self.assertEqual(kept[routing.MAILROOM_HEADER], routing.ALERT_MARK)
        self.assertTrue(kept.get_body(("plain",)).get_content().endswith("Reply to this email to reach the orchestrator.\n"))
        self.assertTrue(self.st.is_alert_thread(sent["thread_id"]))
        row = self.tab_reply(sent["thread_id"], "What happened?")
        self.assertEqual(row["match_rule"], "mailroom-notice")
        with mock.patch.object(self.room.orchestrate, "run_event", return_value=INFO) as run:
            self.poll(LOCAL)
        self.assertIn("replying to or forwarding a mailroom notice", run.call_args.args[0])
        self.assertIn("Why.", run.call_args.args[0])            # the alert itself, read from the store
        self.fake.thread_messages.assert_not_called()

    def test_with_gmail_delivery_an_alert_is_sent_and_kept(self):
        alerts.send_alert(self.fake, self.st, CFG, "quota", "key", "something", "Body.")
        sent = self.st.sent_row(self.st.max_sent_id())
        self.assertEqual((sent["gmail_id"], messages.load(sent["eml"])), ("g1", self.raws[0]))
        self.assertEqual(self.st.get_meta("alert:quota:key:thread_id"), "t1")


class Nudge(Local):
    def test_one_content_free_nudge_per_period_while_mail_is_kept(self):
        self.assertEqual(alerts.nudge(self.fake, self.st, LOCAL, 10_000), 0)        # nothing kept yet
        self.agent_mail(subject="Secret plan", text="the launch codes")
        alerts.send_alert(self.fake, self.st, LOCAL, "quota", "alice", "usage limit hit by alice", "alice at 10:00")
        self.assertEqual(alerts.nudge(self.fake, self.st, LOCAL, 10_000), 2)
        (nudge,) = [email.message_from_bytes(raw, policy=email.policy.default) for raw in self.raws]
        self.assertEqual((nudge["To"], nudge["Subject"], nudge[routing.MAILROOM_HEADER]),
                         (OWNER, "[mailroom] 2 new agent messages", routing.ALERT_MARK))
        text = nudge.get_body(("plain",)).get_content()
        self.assertEqual(text, "New agent mail since the last notice: 2. It is on the Agent Board, not in Gmail: open its "
                               "Mail tab.\n\nReply to this email to reach the orchestrator.\n")
        for secret in ("Secret", "launch", "mailtab", "alice", "quota", "[#"):
            self.assertNotIn(secret, messages_text(nudge))
        self.assertEqual(self.st.max_sent_id(), 2)                                  # the nudge itself is not kept
        self.agent_mail(subject="Later")
        self.assertEqual(alerts.nudge(self.fake, self.st, LOCAL, 10_000 + 29 * 60), 0)
        self.assertEqual(alerts.nudge(self.fake, self.st, LOCAL, 10_000 + 30 * 60), 1)
        self.assertEqual(email.message_from_bytes(self.raws[-1], policy=email.policy.default)["Subject"],
                         "[mailroom] 1 new agent message")
        self.assertEqual(alerts.nudge(self.fake, self.st, LOCAL, 10_000 + 99 * 60), 0)   # nothing new since

    def test_mail_already_read_in_the_tab_is_not_nudged_about(self):
        first = self.agent_mail(subject="Read at once")
        mailbox.mark(self.st, LOCAL, [first["thread_id"]], "read", first["id"], None, 10_000)
        self.assertEqual(alerts.nudge(self.fake, self.st, LOCAL, 10_000), 0)        # he has seen it: no nudge
        self.assertEqual(self.raws, [])
        archived = self.agent_mail(subject="Archived unread")
        mailbox.mark(self.st, LOCAL, [archived["thread_id"]], "archive", archived["id"], None, 10_000)
        self.assertEqual(alerts.nudge(self.fake, self.st, LOCAL, 10_000), 0)        # out of his inbox: dealt with
        snoozed = self.agent_mail(subject="Snoozed unread")
        mailbox.mark(self.st, LOCAL, [snoozed["thread_id"]], "snooze", snoozed["id"], 20_000, 10_000)
        self.assertEqual(alerts.nudge(self.fake, self.st, LOCAL, 10_000), 0)        # put off: dealt with for now
        self.agent_mail(subject="Not read")
        self.assertEqual(alerts.nudge(self.fake, self.st, LOCAL, 10_000), 1)        # only the unread one counts

    def test_no_nudge_unless_local_and_asked_for(self):
        self.agent_mail()
        for cfg in (CFG, dict(CFG, mailbox={"owner_delivery": "local"}),
                    dict(CFG, mailbox={"owner_delivery": "gmail", "nudge_minutes": 30})):
            self.assertEqual(alerts.nudge(self.fake, self.st, cfg, 10_000), 0)
        self.assertEqual(self.raws, [])

    def test_a_gmail_reply_to_the_nudge_goes_to_the_orchestrator(self):
        self.agent_mail()
        alerts.nudge(self.fake, self.st, LOCAL, 10_000)
        self.assertEqual(routing.match(self.st, {"subject": "Re: [mailroom] 1 new agent message"}, "t9", CFG["account"]),
                         (None, "mailroom-notice"))
        self.assertEqual(routing.match(self.st, {"subject": "what is new"}, "t1", CFG["account"]),
                         (None, "mailroom-notice"))   # by its thread, whatever the subject

    def test_the_daemon_nudges_after_a_poll(self):
        self.agent_mail()
        homes.status_log(self)
        log = self.poll(LOCAL)
        self.assertIn("nudge  told Alex of 1 agent mail(s) in the Mail tab", log)


def messages_text(msg):
    """Every header and text part of msg, as one string."""
    return "\n".join("%s: %s" % kv for kv in msg.items()) + "\n" + "\n".join(
        p.get_content() for p in msg.walk() if p.get_content_maintype() == "text")


class Routing(Local):
    def test_a_tab_reply_to_kept_mail_reaches_its_session_without_gmail(self):
        first = self.agent_mail()
        row = self.tab_reply(first["thread_id"], "Ship it.")
        self.assertEqual((row["matched_tag"], row["match_rule"]), (self.sess["tag"], "in-reply-to"))
        with mock.patch.object(deliver, "deliver_session", return_value=(0, "sent")) as send:
            self.poll(QUIET)
        self.assertTrue(send.call_args.args[1].startswith('[email from Alex <%s> re "Re: %s"' % (OWNER, first["subject"])))
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")
        for call in ("get_full", "thread_messages", "send_raw", "modify"):
            getattr(self.fake, call).assert_not_called()

    def test_a_tab_reply_with_files_to_kept_mail_reaches_its_session_with_them(self):
        here = homes.temp_dir(self) / "files"
        homes.patch(self, replyfiles, "FILES_DIR", here)
        first = self.agent_mail()
        got = self.answer(first["thread_id"], "See the plot.", files=[("plot.png", PNG)])
        row = self.st.inbound(int(got["key"][1:]))
        self.assertEqual((row["thread_id"], row["matched_tag"]), (first["thread_id"], self.sess["tag"]))
        with mock.patch.object(deliver, "deliver_session", return_value=(0, "sent")) as send:
            self.poll(QUIET)
        path = here / ("i%d" % row["id"]) / "plot.png"
        self.assertEqual(path.read_bytes(), PNG)
        self.assertIn("See the plot.\n\nAlex attached 1 file", send.call_args.args[1])
        self.assertIn("- %s (image/png" % path, send.call_args.args[1])
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")
        for call in ("get_full", "thread_messages", "send_raw", "modify"):
            getattr(self.fake, call).assert_not_called()

    def test_a_tab_reply_in_a_gmail_thread_answered_by_kept_mail_goes_to_the_session_that_kept_it(self):
        other = self.st.session_for("claude", "11111111-1111-4111-8111-111111111111", "other", origin_host=CFG["host_id"])
        old = self.agent_mail(to="sam@example.com", cc=OWNER)                        # mailtab's, through Gmail
        self.agent_mail(sess=other, inb=self.his_mail(old["thread_id"]))                # other's answer, kept
        row = self.tab_reply(old["thread_id"], "Go on.")
        self.assertEqual((row["matched_tag"], row["match_rule"]), (other["tag"], "in-reply-to"))

    def test_a_reply_refused_in_a_kept_thread_exits_6_and_records_nothing(self):
        """A reply the owner can edit to pass is refused with mailbox.ANSWER_REFUSED in a kept thread as in a Gmail one,
        and once edited it goes."""
        thread = self.agent_mail()["thread_id"]
        one = {"name": "a.png", "data": base64.b64encode(PNG).decode()}
        for flags, stdin, said in (([], " \n", "the reply is empty"), ([], "x" * 20001, "the most is 20000"),
                                   (["--json"], json.dumps({"text": "x", "files": [one] * 21}),
                                    "the reply has 21 files; the most is 20")):
            with self.subTest(said=said):
                code, out, err = self.main(["mailbox", "answer", thread, "--key", KEY] + flags, stdin=stdin)
                self.assertEqual((code, out), (mailbox.ANSWER_REFUSED, ""))
                self.assertIn(said, err)
                self.assertIsNone(self.st.inbound_by_gmail_id(routing.local_gmail_id(KEY)))
        code, out, _ = self.main(["mailbox", "answer", thread, "--key", KEY], stdin="Ship it.")
        self.assertEqual((code, json.loads(out)["status"]), (0, "sending"))

    def test_the_orchestrator_reads_a_kept_thread_from_the_store_alone(self):
        first = self.agent_mail(text="The results you asked for.")
        self.tab_reply(first["thread_id"], "Rerun it.")
        with mock.patch.object(deliver, "deliver_session", return_value=(4, "no such session")), \
                mock.patch.object(self.room.orchestrate, "run_event", return_value=INFO) as run, \
                mock.patch.object(recovery, "recover") as recover:
            self.poll(QUIET)
        recover.assert_not_called()
        text = run.call_args.args[0]
        self.assertIn("has closed or exited", text)
        self.assertIn("agent (ours)]\nThe results you asked for.", text)
        self.assertTrue(text.endswith("--- latest message ---\nRerun it."), text)
        self.fake.thread_messages.assert_not_called()

    def test_a_gmail_thread_continued_here_is_read_from_both(self):
        old = self.agent_mail(text="Sent through Gmail.", to="sam@example.com", cc=OWNER, at=100)
        self.agent_mail(text="Kept here.", inb=self.his_mail(old["thread_id"], text="And?", at=150), at=200)
        row = self.tab_reply(old["thread_id"], "Thanks.")
        self.fake.thread_messages.return_value = [({"from": CFG["account"]}, "Sent through Gmail.", 100, "g1"),
                                                  ({"from": "Alex <%s>" % OWNER}, "And?", 150, "in1")]
        msgs = mailroom.thread_messages(self.fake, self.st, row)
        self.assertEqual([m[1].strip() for m in msgs], ["Sent through Gmail.", "And?", "Kept here.", "Thanks."])
        self.assertEqual([m[3] for m in msgs][2:], [None, routing.local_gmail_id(KEY)])
        self.fake.thread_messages.assert_called_once_with(self.fake, old["thread_id"])


class Sync(Local):
    """mailbox sync in a mailbox of old Gmail mail beside kept mail: Gmail never had the kept mail, so it archives or
    marks read no thread while kept mail in it waits in the tab, and is asked to change only the mail it holds."""

    def sync(self, inbox, unread):
        return mailbox.sync(self.st, CFG, {"inbox": inbox, "unread": unread}, 2e9)

    def gmail_mail(self, subject, inb=None):
        return self.agent_mail(subject=subject, to="sam@example.com", cc=OWNER, inb=inb)

    def answered_here(self, sent):
        """Agent mail kept for the tab, answering the owner's email in sent's (Gmail) thread."""
        n = self.st.con.execute("select count(*) from inbound").fetchone()[0]
        return self.agent_mail(inb=self.his_mail(sent["thread_id"], gid="in%d" % n))

    def test_kept_mail_waiting_in_the_tab_keeps_its_thread_and_gmail_is_asked_only_about_its_own(self):
        kept = self.agent_mail(subject="Kept")                                    # kept alone, unread in the tab
        old = self.gmail_mail("Old")                                           # Gmail's alone, done with there
        self.answered_here(self.gmail_mail("Went on"))                         # Gmail's, then kept mail newest
        back = self.gmail_mail("Back")                                         # kept mail unread in the tab,
        later = self.answered_here(back)                                          # then Gmail mail newest
        self.gmail_mail("x", inb=self.st.inbound(later["in_reply_to_inbound"]))
        dealt = self.gmail_mail("Dealt")                                       # the same, with the kept mail
        self.mark([dealt["thread_id"]], "read", upto=self.answered_here(dealt)["id"], now=1000)   # read and
        self.mark([dealt["thread_id"]], "archive", upto=self.st.max_sent_id(), now=1000)          # archived first
        self.gmail_mail("x", inb=self.his_mail(dealt["thread_id"], gid="in-dealt"))
        done = self.gmail_mail("Done")                                         # unread in Gmail's inbox, its
        tidied = self.agent_mail(subject="Tidied", inb=None)                      # kept mail done with in the tab
        self.answered_here(done)
        self.mark([done["thread_id"], tidied["thread_id"]], "read", upto=self.st.max_sent_id(), now=1000)
        self.mark([done["thread_id"], tidied["thread_id"]], "archive", upto=self.st.max_sent_id(), now=1000)
        got = self.sync([done["message_id"], tidied["message_id"]],               # a kept Message-ID, given as if
                        [done["message_id"], tidied["message_id"]])               # Gmail held it, comes back in none
        self.assertEqual(got, {"ok": True, "archived": 2, "read": 2,              # old and dealt
                               "gmail": {"archive": [done["message_id"]], "read": [done["message_id"]]}})
        self.assertEqual({t["subject"]: (t["box"], t["unread"]) for t in self.listed().values()},
                         {"Kept": ("inbox", True), "Old": ("archived", False), "Went on": ("inbox", True),
                          "Back": ("inbox", True), "Dealt": ("archived", False), "Done": ("archived", False),
                          "Tidied": ("archived", False)})
        self.assertEqual(sorted(self.st.mailbox_flags()), sorted([old["thread_id"], dealt["thread_id"],
                                                                  done["thread_id"], tidied["thread_id"]]))
        self.assertEqual(mailbox.unread_kept(self.st, CFG, 0, 2e9)[0], 3)         # what the nudge still tells of
        self.assertEqual(kept["gmail_id"], None)


class Widgets(Local):
    """Kept mail keeps its widgets as mail sent through Gmail does: the sent row's widgets, the fallbacks in the
    stored copy (the page the Mail tab renders) with their places marked, and show's widgets and places."""

    BODY = "Pick one, then run:\n\n```bash\nagent-mail status\n```\n\n```choices\n%s\n```\n\n```plot\n%s\n```\n" % (
        json.dumps(test_mailbox.Widgets.CHOICES), json.dumps(test_mailbox.Widgets.PLOT))

    def widget_mail(self, ctx):
        """agent-mail send --unrouted to the owner with BODY and the plot's --image through ctx: its sent row."""
        body, png = homes.temp_dir(self) / "body.txt", homes.temp_dir(self) / "acc.png"
        body.write_text(self.BODY)
        png.write_bytes(PNG)
        with contextlib.redirect_stdout(io.StringIO()) as out, mock.patch.dict(os.environ, SSH_CONNECTION=""):
            self.am.do_send(ctx, argparse.Namespace(to="owner", cc=None, subject="pick", unrouted=True,
                                                    body_file=str(body), html=None, image=["acc=%s" % png],
                                                    attach=None, alert=None))
        return self.st.sent_row(self.st.max_sent_id()), out.getvalue()

    def test_kept_mail_stores_and_shows_its_widgets_as_gmail_mail_does(self):
        emailed, _ = self.widget_mail(outbound.Context(CFG))
        kept, said = self.widget_mail(self.ctx)
        self.assertEqual((len(self.raws), kept["gmail_id"]), (1, None))              # the first alone went to Gmail
        self.assertTrue(said.startswith("sent to %s in the Mail tab | subject 'pick'" % OWNER), said)
        self.assertEqual(json.loads(kept["widgets"]), [{"kind": "choices", "spec": test_mailbox.Widgets.CHOICES},
                                                       {"kind": "plot", "spec": test_mailbox.Widgets.PLOT}])
        self.assertEqual((kept["widgets"], kept["body"]), (emailed["widgets"], emailed["body"]))
        for kind in ("plain", "html"):
            self.assertEqual(self.stored(kept).get_body((kind,)).get_content(),
                             self.stored(emailed).get_body((kind,)).get_content())
        page = self.stored(kept).get_body(("html",)).get_content()
        self.assertIn('<div data-agentmail-widget="0">', page)
        self.assertRegex(page, r"<pre [^>]*>agent-mail status</pre>")                # the command the tab can copy
        shown = mailbox.show(self.st, CFG, kept["thread_id"], 2e9)
        key = "s%d" % kept["id"]
        (m,) = shown["messages"]
        self.assertEqual(m["widgets"], [{"id": key + "-0", "kind": "choices", "spec": test_mailbox.Widgets.CHOICES},
                                        {"id": key + "-1", "kind": "plot", "spec": test_mailbox.Widgets.PLOT}])
        for n in (0, 1):
            self.assertIn('<div class="widget" data-widget="%s-%d">' % (key, n), shown["document"])


class Page(Local):
    """Kept mail reaches the Mail tab through the page's sanitizer, as mail sent through Gmail does: nothing that hides,
    moves or reorders text, from the mail, its subject or its widgets."""

    def kept_mail(self, body, html=None, subject="Report"):
        """agent-mail send --unrouted to the owner, mail to the owner kept for the tab: its sent row."""
        homes.config_file(self, dict(CFG_FILE, mailbox={"owner_delivery": "local"}))
        files = homes.temp_dir(self)
        (files / "body.txt").write_text(body)
        flags = ["--body-file", str(files / "body.txt")]
        if html is not None:
            (files / "body.html").write_text(html)
            flags += ["--html", str(files / "body.html")]
        code, out, err = self.main(["send", "--unrouted", "--to", "owner", "--subject", subject] + flags)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("in the Mail tab", out)
        return self.st.sent_row(self.st.max_sent_id())

    def test_kept_mail_is_shown_through_the_sanitizer(self):
        rlo, zwsp = "\u202e", "\u200b"
        sent = self.kept_mail("Delete it.",
                              html='<p dir="rtl" style="color: red; position: fixed; top: 0">Delete%s it.</p>'
                                   '<font face="Webdings" color="red">f</font><q>quoted</q>' % rlo,
                              subject="Re%sport" % zwsp)
        self.assertIsNone(sent["gmail_id"])
        doc = mailbox.show(self.st, CFG, sent["thread_id"], 2e9)["document"]
        self.assertIn('<p style="color: red;">Delete it.</p><font color="red">f</font>quoted', doc)
        self.assertIn("Report", doc)
        for gone in ("position", "dir=", "face=", "<q>", rlo, zwsp):
            self.assertNotIn(gone, doc)

    def test_kept_widgets_keep_their_text_as_the_tab_shows_it(self):
        choices = {"question": "Which\u200b one?", "options": [{"label": "Keep\u00a0\u00a0it"}, {"label": "Lift it"}]}
        sent = self.kept_mail("Pick.\n\n```choices\n%s\n```\n" % json.dumps(choices))
        spec = {"question": "Which one?", "options": [{"label": "Keep it"}, {"label": "Lift it"}]}
        self.assertEqual(json.loads(sent["widgets"]), [{"kind": "choices", "spec": spec}])
        (m,) = mailbox.show(self.st, CFG, sent["thread_id"], 2e9)["messages"]
        self.assertEqual(m["widgets"], [{"id": "s%d-0" % sent["id"], "kind": "choices", "spec": spec}])


class Readers(Local):
    """What reads Gmail ids and threads copes with mail kept here."""

    def test_backfill_skips_mail_kept_here(self):
        sent = self.agent_mail()
        self.st.con.execute("update sent set eml=null where id=?", (sent["id"],))   # even with no message kept
        out = []
        self.assertEqual(mailbox.backfill(self.st, "svc", None, out.append), 0)
        self.assertEqual(out, ["backfill: 0 kept, 0 failed"])
        self.fake.get_raw.assert_not_called()

    def test_owner_threads_holds_only_gmail_threads(self):
        kept = self.agent_mail()
        through = self.agent_mail(to="sam@example.com", cc=OWNER)
        self.tab_reply(kept["thread_id"], "Fine.")
        self.assertEqual(self.st.owner_threads([OWNER], 0, 3e9), [])                 # Sam's has the owner in Cc only
        self.st.record_inbound(gmail_id="in1", thread_id=through["thread_id"], from_addr=OWNER, action="delivered")
        self.assertEqual(self.st.owner_threads([OWNER], 0, 3e9), [through["thread_id"]])

    def test_a_held_tab_reply_s_notice_points_at_the_mail_tab_not_gmail(self):
        first = self.agent_mail()
        row = self.tab_reply(first["thread_id"], "Ship it.")
        recovery.hold_delivery(self.st, row, self.sess, "delivery failed (1): refused")
        self.assertTrue(recovery.notify(self.fake, self.st, LOCAL, self.st.inbound(row["id"])))
        notice = self.stored(self.st.sent_row(self.st.max_sent_id())).get_body(("plain",)).get_content()
        self.assertIn("Open email: in the Mail tab on the Agent Board (its thread is not in Gmail)", notice)
        self.assertIn("Nothing has been lost: your email is saved in the mailroom.", notice)
        self.fake.send_raw.assert_not_called()


class GroupMail(GroupBase):
    """A group's mail to the owner is kept like any other, and their tab reply to it is their post in the group, as in
    test_mailbox's GroupTabReply."""

    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, mailbox={"owner_delivery": "local"})
        self.st = Store(homes.temp_dir(self) / "mailroom.sqlite")
        self.addCleanup(self.st.con.close)
        self.room = load_script("agent-mailroom")
        self.sent = fakes.sending_gmail(self, outbound, self.room, mailroom, alerts)
        homes.patch(self, outbound, "Store", lambda: self.st)
        homes.patch(self, config, "MESSAGES_DIR", homes.temp_dir(self) / "messages")

    def test_a_tab_reply_to_a_group_s_mail_is_his_post(self):
        gid = self.group(A, "Release", "bob")
        groupmail.announce(self.room.gmail, self.st, self.cfg)
        self.assertEqual(self.sent, [])                                                # kept, not sent
        thread = self.st.sent_row(self.st.max_sent_id())["thread_id"]
        self.assertTrue(thread.startswith("L"), thread)
        self.delivered.clear()
        with no_agent_env():
            mailbox.answer(self.st, self.cfg, thread, KEY, "Ship it.", [])
        self.room.gmail.history_since.return_value = ([], "after")
        self.st.set_meta("history_id", "before")
        with contextlib.redirect_stdout(io.StringIO()):
            self.room.poll(self.room.gmail, self.st, self.cfg, False)
        post, = json.loads(self.run_(OWNER_ACTOR, "posts", group=gid, json=True))
        self.assertEqual((post["from_agent"], post["text"]), (None, "Ship it."))
        self.assertEqual(sorted(self.names_delivered()), ["alice", "bob"])

    def test_a_group_takes_no_files(self):
        self.group(A, "Release", "bob")
        groupmail.announce(self.room.gmail, self.st, self.cfg)
        thread = self.st.sent_row(self.st.max_sent_id())["thread_id"]
        with no_agent_env(), self.assertRaisesRegex(outbound.MailError,
                                                    "goes to group 'Release', which takes text only") as refused:
            mailbox.answer(self.st, self.cfg, thread, KEY, "Ship it.", [("plot.png", PNG)])
        self.assertEqual(refused.exception.exit_code, mailbox.ANSWER_REFUSED)   # his draft can be edited and sent
        self.assertIsNone(self.st.inbound_by_gmail_id(routing.local_gmail_id(KEY)))


class SwitchBack(Local):
    """Back on "gmail": a reply in a thread kept meanwhile, which Gmail holds nothing of, starts a Gmail thread."""

    def test_a_reply_in_a_kept_thread_goes_to_gmail_without_its_thread_id(self):
        first = self.agent_mail()
        row = self.tab_reply(first["thread_id"], "Rerun it.")
        outbound.reply_mail(outbound.Context(CFG), self.sess, row, outbound.Mail("Done.", None, [], []), "test", None,
                            None)
        (msg, thread), = self.sent
        self.assertIsNone(thread)                                                    # Gmail has no thread L...
        self.assertEqual((msg["In-Reply-To"], msg["References"]),
                         (routing.local_message_id(KEY), "%s %s" % (first["message_id"], routing.local_message_id(KEY))))
        reply = self.st.sent_row(self.st.max_sent_id())
        self.assertEqual((reply["gmail_id"], reply["thread_id"]), ("g1", "t1"))


if __name__ == "__main__":
    unittest.main()
