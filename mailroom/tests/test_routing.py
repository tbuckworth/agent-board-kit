import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["AGENT_MAIL_STATE_DIR"] = tempfile.mkdtemp()
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


class Matching(unittest.TestCase):
    def setUp(self):
        self.st = Store(Path(os.environ["AGENT_MAIL_STATE_DIR"]) / "t.sqlite")
        self.sess = self.st.session_for("claude", "11111111-1111-1111-1111-111111111111", "mailtest", "/tmp")
        self.st.record_sent(self.sess["tag"], "g1", "t1", "<m1@mail.gmail.com>", "owner@example.com", "hi")

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
        again = self.st.session_for("claude", "11111111-1111-1111-1111-111111111111")
        self.assertEqual(again["tag"], self.sess["tag"])


if __name__ == "__main__":
    unittest.main()
