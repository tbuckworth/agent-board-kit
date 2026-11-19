import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from agentmail import routing  # noqa: E402
from agentmail.store import Store  # noqa: E402

ACCT = "agents@example.com"


class Tags(unittest.TestCase):
    def test_plus_tag_found_with_dots_and_case(self):
        self.assertEqual(routing.tag_from_address("Agents <A.Gents+ab34cd9k@example.com>", ACCT), "ab34cd9k")

    def test_plus_tag_other_domain_ignored(self):
        self.assertIsNone(routing.tag_from_address("agents+ab34cd9k@example.org", ACCT))

    def test_subject_tag(self):
        self.assertEqual(routing.tag_from_subject("Re: results [#ab34cd9k]"), "ab34cd9k")
        self.assertIsNone(routing.tag_from_subject("Re: results"))

    def test_tag_subject_idempotent(self):
        self.assertEqual(routing.tag_subject("x [#abcdef]", "abcdef"), "x [#abcdef]")
        self.assertEqual(routing.tag_subject("x", "abcdef"), "x [#abcdef]")

    def test_untag_subject_removes_only_a_tag(self):
        self.assertEqual(routing.untag_subject("results [#ab34cd9k] and [#x] [#ab34cd9kab34cd9k]"),
                         "results and [#x] [#ab34cd9kab34cd9k]")

    def test_bare_subject(self):
        self.assertEqual(routing.bare_subject(" Re: Fwd: results [#ab34cd9k] "), "results")
        self.assertEqual(routing.bare_subject("results"), "results")

    def test_reply_prefixes(self):
        for subject in ("hi", "Re: hi", "RE: Fwd: hi", " re : FW: re:hi"):
            self.assertEqual(routing.strip_reply_prefixes(subject), "hi")
        self.assertEqual(routing.strip_reply_prefixes("Regarding: hi"), "Regarding: hi")
        self.assertTrue(routing.is_reply("Re: hi"))
        self.assertTrue(routing.is_reply("RE : Fwd: hi"))
        self.assertFalse(routing.is_reply("Fwd: Re: hi"))
        self.assertFalse(routing.is_reply("Ref: hi"))

    def test_new_tag_alphabet(self):
        t = routing.new_tag()
        self.assertEqual(len(t), 8)
        self.assertTrue(all(c in routing.ALPHABET for c in t))


class Quotes(unittest.TestCase):
    def test_gmail_style(self):
        txt = "Yes, go ahead.\n\nOn Thu, 11 Sep 2026 at 10:00, Alex's agents\n<agents@example.com> wrote:\n> hello\n> world\n"
        self.assertEqual(routing.strip_quotes(txt), "Yes, go ahead.")

    def test_outlook_style(self):
        txt = "Do it\r\n\r\n-----Original Message-----\r\nFrom: x\r\n"
        self.assertEqual(routing.strip_quotes(txt), "Do it")

    def test_signature(self):
        self.assertEqual(routing.strip_quotes("ok\n-- \nAlex"), "ok")

    FORWARD = ("---------- Forwarded message ---------\nFrom: Council <tax@council.example>\n"
               "Date: Mon, 2 Nov 2026 at 14:24\nSubject: Your bill\nTo: <alex@example.com>\n\nYou owe 5 dollars.\n\n"
               "On Sun, 1 Nov 2026 at 10:00, Alex wrote:\n> earlier\n")

    def test_gmail_forward_keeps_the_forwarded_message(self):
        self.assertEqual(routing.strip_quotes("can you deal with this?\r\n-- \r\nAlex\r\n\r\n" + self.FORWARD),
                         "can you deal with this?\n\n" + self.FORWARD.strip())

    def test_apple_mail_forward_keeps_the_forwarded_message(self):
        fwd = "Begin forwarded message:\n\nFrom: Council <tax@council.example>\nDate: 2 November 2026\n\nYou owe 5 dollars."
        self.assertEqual(routing.strip_quotes("see below\n\n" + fwd), "see below\n\n" + fwd)

    def test_forward_inside_a_quoted_reply_is_still_cut(self):
        self.assertEqual(routing.strip_quotes("ok\n\n-----Original Message-----\nFrom: x\n\n" + self.FORWARD), "ok")
        self.assertEqual(routing.strip_quotes("ok\n\nOn Mon, x wrote:\n> " + self.FORWARD.replace("\n", "\n> ")), "ok")

    def test_a_long_forward_is_cut_with_a_note(self):
        out = routing.strip_quotes("note\n\n" + self.FORWARD + "x" * (2 * routing.FORWARD_MAX_CHARS))
        self.assertLess(len(out), routing.FORWARD_MAX_CHARS + 200)
        self.assertTrue(out.endswith(routing.FORWARD_CUT_NOTE))


class Matching(unittest.TestCase):
    def setUp(self):
        self.st = Store(Path(os.environ["AGENT_MAIL_STATE_DIR"]) / "t.sqlite")
        self.sess = self.st.session_for("claude", "11111111-1111-1111-1111-111111111111", "mailtest", "/tmp",
                                       origin_host="desktop")
        self.st.record_sent(self.sess["tag"], "g1", "t1", "<m1@mail.gmail.com>", "alex@example.com", "hi",
                            cc_addr=None, body=None, eml=None, attachments=None)

    def test_in_reply_to_wins(self):
        s, rule = routing.match(self.st, {"in-reply-to": "<m1@mail.gmail.com>"}, "other", ACCT)
        self.assertEqual((s["tag"], rule), (self.sess["tag"], "in-reply-to"))

    def test_thread(self):
        s, rule = routing.match(self.st, {}, "t1", ACCT)
        self.assertEqual(rule, "thread-id")

    def test_plus_tag(self):
        s, rule = routing.match(self.st, {"to": routing.tagged_address(ACCT, self.sess["tag"])}, None, ACCT)
        self.assertEqual(rule, "plus-tag")

    def test_subject_tag(self):
        s, rule = routing.match(self.st, {"subject": "Re: hi [#%s]" % self.sess["tag"]}, None, ACCT)
        self.assertEqual(rule, "subject-tag")

    def test_no_match(self):
        self.assertEqual(routing.match(self.st, {"subject": "hello"}, None, ACCT), (None, None))

    def test_same_session_same_tag(self):
        again = self.st.session_for("claude", "11111111-1111-1111-1111-111111111111", origin_host="desktop")
        self.assertEqual(again["tag"], self.sess["tag"])


if __name__ == "__main__":
    unittest.main()
