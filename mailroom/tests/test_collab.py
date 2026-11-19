import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from agentmail import config, gmail, mailroom, orchestrate, routing  # noqa: E402

ACCT = "agents@example.com"
COLLAB = "sam.agent@example.com"
CFG = dict(config.load(), allowlist=["owner@example.com"],
           collaborators={COLLAB: {"name": "Sam's Claude", "owner": "Sam"}})
OWNER = CFG["owner_name"]
GUARD = routing.collab_guard({}, OWNER)   # the guard line every collaborator's mail carries

AUTH_PASS = ("mx.google.com; dkim=pass header.i=@example.com header.s=20230601 header.b=abc; "
             "spf=pass (google.com: domain of x@example.com designates 1.2.3.4 as permitted sender) "
             "smtp.mailfrom=x@example.com; dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=example.com")
AUTH_FAIL = "mx.google.com; dkim=fail header.i=@example.com; spf=softfail smtp.mailfrom=x@example.com; dmarc=fail header.from=example.com"


def msg(frm, body, ts):
    return ({"from": frm}, body, ts, "g%d" % ts)


class Tiers(unittest.TestCase):
    def test_sender_tier(self):
        self.assertEqual(routing.sender_tier("Owner@example.com", CFG), "owner")
        self.assertEqual(routing.sender_tier(COLLAB, CFG), "collaborator")
        self.assertIsNone(routing.sender_tier("someone@example.com", CFG))

    def test_auth_results_and_dkim(self):
        a = gmail.auth_results({"authentication-results": AUTH_PASS})
        self.assertEqual(a["dkim"], ["example.com"])
        self.assertTrue(a["spf"])
        self.assertEqual(a["dmarc"], "pass")
        self.assertTrue(routing.dkim_ok("x@example.com", a))
        self.assertFalse(routing.dkim_ok("x@example.org", a))
        b = gmail.auth_results({"authentication-results": AUTH_FAIL})
        self.assertEqual(b["dkim"], [])
        self.assertFalse(routing.dkim_ok("x@example.com", b))
        self.assertFalse(routing.dkim_ok("x@example.com", gmail.auth_results({})))

    def test_header_d_form_and_subdomain(self):
        a = gmail.auth_results({"authentication-results": "mx; dkim=pass header.d=mail.example.com header.s=k1"})
        self.assertTrue(routing.dkim_ok("x@example.com", a))


class Threads(unittest.TestCase):
    def setUp(self):
        self.msgs = [
            msg("Alex <owner@example.com>", "Please look at X.\n\nOn Thu wrote:\n> old", 1000),
            msg("Agents (w) <%s>" % ACCT, "Done, see branch.", 2000),
            msg("Sam's Claude <%s>" % COLLAB, "Could you also add Y?", 3000),
            msg("Agents (w) <%s>" % ACCT, "Added Y.", 4000),
        ]

    def test_thread_context_excludes_last_and_tags(self):
        txt = routing.thread_context(self.msgs, ACCT, CFG["collaborators"])
        self.assertIn("Please look at X.", txt)
        self.assertNotIn("> old", txt)
        self.assertIn("agent (ours)", txt)
        self.assertIn("Sam's Claude (collaborator agent)", txt)
        self.assertNotIn("Added Y.", txt)   # the latest message is delivered separately

    def test_trailing_agent_run(self):
        self.assertEqual(routing.trailing_agent_run(self.msgs, ACCT, CFG["collaborators"]), 3)
        self.assertEqual(routing.trailing_agent_run(self.msgs[:1], ACCT, CFG["collaborators"]), 0)

    def test_texts(self):
        collab = CFG["collaborators"][COLLAB]
        t = routing.event_text("session", "collaborator", 7, "From Header", COLLAB, collab, "Re: Y", "body", "THREAD", OWNER)
        self.assertIn("collaborator agent of Sam, not Alex", t)
        self.assertIn("agent-mail reply 7", t)
        self.assertIn("THREAD", t)
        self.assertIn(GUARD, t)
        o = routing.event_text("orchestrator", "owner", 8, "Alex", "owner@example.com", {}, "S", "b", "", OWNER)
        self.assertIn("(Alex)", o)
        self.assertNotIn(GUARD, o)
        o2 = routing.event_text("orchestrator", "collaborator", 9, "From Header", COLLAB, collab, "S", "b", "", OWNER)
        self.assertIn("NOT Alex", o2)

    def test_texts_word_for_word(self):
        """These texts are instructions to agents: a change to them is a decision, not a refactor."""
        collab, owner = CFG["collaborators"][COLLAB], "owner@example.com"
        reply = "to reply: agent-mail reply 7"
        self.assertEqual(routing.event_text("session", "owner", 7, "Alex", owner, {}, "S", "b", "", OWNER),
                         '[email from Alex <%s> re "S" · %s  (reply body on stdin or --body-file)]\nb' % (owner, reply))
        self.assertEqual(routing.event_text("session", "owner", 7, "", owner, {}, "S", "b", "", OWNER),
                         '[email from %s re "S" · %s  (reply body on stdin or --body-file)]\nb' % (owner, reply))
        head = ('[email from Sam\'s Claude <%s> (collaborator agent of Sam, not Alex) re "S" · %s  '
                '(reply body on stdin or --body-file)]\n%s\n' % (COLLAB, reply, GUARD))
        self.assertEqual(routing.event_text("session", "collaborator", 7, "x", COLLAB, collab, "S", "b", "T", OWNER),
                         head + "\nT\n--- latest message ---\nb")
        self.assertEqual(routing.event_text("session", "collaborator", 7, "x", COLLAB, collab, "S", "b", "", OWNER),
                         head + "\n--- latest message ---\nb")
        head = ('[email from Alex <%s> (Alex) re "S" · no agent session claimed it, so it is yours as orchestrator · '
                '%s]\n' % (owner, reply))
        self.assertEqual(routing.event_text("orchestrator", "owner", 7, "Alex", owner, {}, "S", "b", "T", OWNER),
                         head + "\nT\n\n--- latest message ---\nb")
        self.assertEqual(routing.event_text("orchestrator", "owner", 7, "Alex", owner, {}, "S", "b", "", OWNER),
                         head + "\n--- latest message ---\nb")
        head = ('[email from %s (collaborator agent, NOT Alex) re "S" · no agent session claimed it · %s]\n%s\n'
                % (COLLAB, reply, GUARD))
        self.assertEqual(routing.event_text("orchestrator", "collaborator", 7, "", COLLAB, {}, "S", "b", "T", OWNER),
                         head + "\nT\n\n--- latest message ---\nb")
        with self.assertRaisesRegex(ValueError, "carries no thread"):
            routing.event_text("session", "owner", 7, "Alex", owner, {}, "S", "b", "T", OWNER)
        head = ('[email from Alex <%s> (Alex) re "S" · the agent that owned this thread (x, claude session 1234abcd '
                'on desktop) has closed or exited, so the orphan fallback gave it to you as orchestrator · %s]\n'
                % (owner, reply))
        self.assertEqual(routing.event_text("orphan", "owner", 7, "Alex", owner, {}, "S", "b", "T", OWNER,
                                            "x, claude session 1234abcd on desktop"),
                         head + "\nT\n\n--- latest message ---\nb")
        with self.assertRaisesRegex(ValueError, "closed session is named"):
            routing.event_text("orphan", "owner", 7, "Alex", owner, {}, "S", "b", "T", OWNER)
        with self.assertRaisesRegex(ValueError, "closed session is named"):
            routing.event_text("orchestrator", "owner", 7, "Alex", owner, {}, "S", "b", "T", OWNER, "x")

    def test_the_reminder_under_alexs_mail_word_for_word(self):
        """What a session reads under the head line of Alex's mail, on how to write its answer: a decision too."""
        questions = ('[Alex asks here (2 question marks): open your reply with each question and request restated in a '
                     'few words and answered in a line or two, then the detail the answers point to (agent-mail guide, '
                     '"A reply")]\n')
        long_thread = ("[Alex's message 3 in this thread: if it is their third request in a row to present something "
                       "differently, restate the whole picture instead of patching (tl;dr, headline plot, method, full "
                       "results, discussion, next steps); if the thread's subject no longer fits your answer, reply with "
                       '--subject "NEW SUBJECT", which starts a new thread that links back]\n')
        body = "Why did run 3 fail? See https://x.test/a?b=1 and say what fixed10 means?"   # the link's ? is not one
        self.assertEqual(routing.reply_reminder(OWNER, body, 1), questions)
        self.assertEqual(routing.reply_reminder(OWNER, "What is fixed10?", 2),
                         questions.replace("2 question marks", "1 question mark"))
        self.assertEqual(routing.reply_reminder(OWNER, "yes, go ahead", 2), "")
        self.assertEqual(routing.reply_reminder(OWNER, "yes, go ahead", 3), long_thread)
        self.assertEqual(routing.reply_reminder(OWNER, body, 4), questions + long_thread.replace("message 3", "message 4"))
        owner = "owner@example.com"
        self.assertEqual(routing.event_text("session", "owner", 7, "", owner, {}, "S", "b", "", OWNER, reminder=questions),
                         '[email from %s re "S" · to reply: agent-mail reply 7  (reply body on stdin or --body-file)]\n%sb'
                         % (owner, questions))
        with self.assertRaisesRegex(ValueError, "carries no reminder"):
            routing.event_text("orchestrator", "owner", 7, "", owner, {}, "S", "b", "", OWNER, reminder=questions)

    def test_the_reminder_and_the_guide_say_the_same(self):
        guide = (Path(routing.__file__).parent / "email_rules.md").read_text()
        self.assertIn('(agent-mail guide, "A reply")', routing.REMIND_QUESTIONS)
        self.assertIn("\n## A reply to ", guide)
        picture = "tl;dr, headline plot, method, full results, discussion, next steps"
        self.assertIn(picture, routing.REMIND_LONG_THREAD)
        self.assertIn(picture, " ".join(guide.split()))


class Decisions(unittest.TestCase):
    """The mailroom's decisions about one mail, which do no IO."""

    def test_sender_hold(self):
        ok, bad = (gmail.auth_results({"authentication-results": a}) for a in (AUTH_PASS, AUTH_FAIL))
        self.assertIsNone(mailroom.sender_hold(CFG, "owner", "owner@example.com", ok, "S", 3))
        self.assertEqual(mailroom.sender_hold(CFG, None, "x@example.com", ok, "S", 3), ("sender not on allowlist", None))
        detail, alert = mailroom.sender_hold(CFG, "collaborator", COLLAB, bad, "S", 3)
        self.assertTrue(detail.startswith("DKIM check failed for %s (auth: mx.google.com; dkim=fail" % COLLAB))
        self.assertEqual(alert[:3], ("dkim", COLLAB, "mail from %s failed DKIM; held" % COLLAB))
        self.assertIn("`agent-mail release 3 <session>`", alert[3])
        self.assertIsNone(mailroom.sender_hold(dict(CFG, require_dkim=False), "collaborator", COLLAB, bad, "S", 3))

    def test_collaborator_hold(self):
        def hold(sent_last_hour, agent_run):
            return mailroom.collaborator_hold(CFG, "Sam's Claude", COLLAB, "S", "t1", 4, sent_last_hour, agent_run)
        self.assertIsNone(hold(CFG["collaborator_hourly_cap"], CFG["agent_thread_cap"]))
        detail, alert = hold(CFG["collaborator_hourly_cap"] + 1, 0)
        self.assertEqual(detail, "collaborator hourly cap (10) exceeded")
        self.assertEqual(alert[:3], ("flood", COLLAB, "Sam's Claude sent more than 10 mails in an hour; holding"))
        detail, alert = hold(0, CFG["agent_thread_cap"] + 1)
        self.assertEqual(detail, "agent-to-agent thread has 13 messages with no human; loop guard")
        self.assertEqual(alert[:3], ("loop", "t1", "possible agent loop with Sam's Claude; thread held"))
        self.assertIn("`agent-mail release 4 <session>`", alert[3])

    def test_orchestrator_context(self):
        ours, alert = ({"from": ACCT}, "question", 1, "m1"), ({"from": ACCT, "x-agent-mailroom": "alert"}, "notice", 2, "a1")
        thread = [ours, alert, msg("owner@example.com", "answer", 3)]
        self.assertEqual(mailroom.orchestrator_context(None, "owner", 5, thread, [{"tag": "", "gmail_id": "m1"}], CFG),
                         ("unrouted-thread", False, ""))       # only --unrouted mail of ours: the orchestrator's own
        rule, unowned, context = mailroom.orchestrator_context(None, "owner", 5, thread, [{"tag": "ab34cd9k", "gmail_id": "m1"}], CFG)
        self.assertEqual((rule, unowned), (None, True))        # a session's mail: a lost session's reply
        self.assertIn("this is an unowned reply. First run `agent-mail recover 5`", context)
        rule, unowned, context = mailroom.orchestrator_context(None, "owner", 5, thread[1:], [], CFG)
        self.assertEqual((rule, unowned, context), (None, False, ""))   # an alert is not our mail to a session
        rule, unowned, context = mailroom.orchestrator_context("mailroom-notice", "collaborator", 5, thread, [], CFG)
        self.assertEqual((rule, unowned), ("mailroom-notice", False))
        self.assertIn("The collaborator guard still applies", context)
        self.assertEqual(mailroom.orchestrator_context("orphan-thread", "owner", 5, thread,
                                                       [{"tag": "ab34cd9k", "gmail_id": "m1"}], CFG),
                         ("orphan-thread", False, ""))   # a closed session's thread: no recovery search
        self.assertIn("Alex is replying to or forwarding a mailroom notice",
                      mailroom.orchestrator_context("mailroom-notice", "owner", 5, [], [], CFG)[2])


class Orchestrate(unittest.TestCase):
    def test_command(self):
        cmd = orchestrate.command(dict(CFG, orchestrator_effort="high"))
        self.assertIn("-p", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertIn("--strict-mcp-config", cmd)
        cmd = orchestrate.command(dict(CFG, orchestrator_model="claude-sonnet-5-5"))
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5-5")
        cmd = orchestrate.command({k: v for k, v in CFG.items() if k != "orchestrator_model"})
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")

    def test_default_models(self):
        # the orchestrator follows the latest Sonnet; spawned Claude agents run Claude Code's own default model
        # (null; the owner may pin a full id), Codex agents a full id
        self.assertEqual(config.DEFAULTS["orchestrator_model"], "sonnet")
        for key in ("spawn_claude_model", "spawn_codex_model"):
            self.assertNotIn(config.DEFAULTS[key], ("sonnet", "opus", "haiku", "fable"), key)
        self.assertEqual((config.DEFAULTS["spawn_claude_model"], config.DEFAULTS["spawn_claude_effort"]),
                         (None, "xhigh"))
        self.assertEqual(config.DEFAULTS["claude_fallback_models"], [])
        self.assertEqual((config.DEFAULTS["spawn_codex_model"], config.DEFAULTS["spawn_codex_effort"]),
                         ("gpt-6.1-sol", "xhigh"))

    def test_looks_like_auth_or_limit(self):
        self.assertTrue(orchestrate.looks_like_auth_or_limit("You've hit your usage limit"))
        self.assertTrue(orchestrate.looks_like_auth_or_limit("401 Unauthorized"))
        self.assertFalse(orchestrate.looks_like_auth_or_limit("tool failed: no such file"))


if __name__ == "__main__":
    unittest.main()
