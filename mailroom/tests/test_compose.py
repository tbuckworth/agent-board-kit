import email
import email.policy
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
os.environ["AGENT_MAIL_STATE_DIR"] = TMP
os.environ["AGENT_MAIL_CONFIG_DIR"] = TMP          # no real token, no real config: only the required identity
Path(TMP, "config.json").write_text('{"account": "agents@example.com", "allowlist": ["owner@example.com"]}')
os.environ["CLAUDE_CODE_SESSION_ID"] = "22222222-2222-2222-2222-222222222222"
from agentmail.compose import text_to_html  # noqa: E402


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def body_of(html_doc):
    return html_doc.split("<body", 1)[1].split(">", 1)[1].rsplit("</body>", 1)[0].strip()


class TextToHtml(unittest.TestCase):
    def test_escapes_source(self):
        b = body_of(text_to_html("a < b & c > d <script>x</script>"))
        self.assertEqual(b, "<p>a &lt; b &amp; c &gt; d &lt;script&gt;x&lt;/script&gt;</p>")

    def test_paragraphs_and_soft_breaks(self):
        b = body_of(text_to_html("line one\nline two\n\n\nsecond para\n"))
        self.assertEqual(b, "<p>line one<br>\nline two</p>\n<p>second para</p>")

    def test_crlf(self):
        self.assertEqual(body_of(text_to_html("a\r\nb\r\n\r\nc")), "<p>a<br>\nb</p>\n<p>c</p>")

    def test_bullets_become_ul(self):
        b = body_of(text_to_html("Findings:\n- one\n* two\n  continued\n- three\nafter"))
        self.assertEqual(b, "<p>Findings:</p>\n<ul>\n<li>one</li>\n<li>two continued</li>\n<li>three</li>\n</ul>\n<p>after</p>")

    def test_numbers_become_ol(self):
        b = body_of(text_to_html("1. first\n2) second\n\n- bullet"))
        self.assertEqual(b, "<ol>\n<li>first</li>\n<li>second</li>\n</ol>\n<ul>\n<li>bullet</li>\n</ul>")

    def test_inline_bold_and_code_after_escaping(self):
        b = body_of(text_to_html("**bold** and `a < b` and 2*3*4"))
        self.assertIn("<b>bold</b>", b)
        self.assertIn(">a &lt; b</code>", b)
        self.assertIn("2*3*4", b)
        self.assertNotIn("<b>3</b>", b)

    def test_bold_not_applied_inside_code(self):
        b = body_of(text_to_html("use `a**b` and **bold** here, `x**2 + y**2`"))
        self.assertIn(">a**b</code> and <b>bold</b> here", b)
        self.assertIn(">x**2 + y**2</code>", b)
        b = body_of(text_to_html("**run `make` now** and `**lit**`"))
        self.assertIn("<b>run <code", b)
        self.assertIn("</code> now</b>", b)
        self.assertIn(">**lit**</code>", b)
        self.assertNotIn("\x00", b)

    def test_indented_bullets_are_items_not_continuations(self):
        b = body_of(text_to_html("- item\n    - nested\n    3. numbered\n- next"))
        self.assertEqual(b, "<ul>\n<li>item\n<ul>\n<li>nested</li>\n</ul>\n<ol start=\"3\">\n<li>numbered</li>\n</ol></li>\n"
                            "<li>next</li>\n</ul>")

    def test_loose_numbered_list_is_one_list(self):
        b = body_of(text_to_html("1. one\n\n2. two\n\n\n3. three"))
        self.assertEqual(b, "<ol>\n<li>one</li>\n<li>two</li>\n<li>three</li>\n</ol>")

    def test_numbered_list_split_by_a_paragraph_keeps_counting(self):
        b = body_of(text_to_html("1. one\n\nWhy it matters.\n\n2. two\n3. three"))
        self.assertEqual(b, "<ol>\n<li>one</li>\n</ol>\n<p>Why it matters.</p>\n"
                            "<ol start=\"2\">\n<li>two</li>\n<li>three</li>\n</ol>")
        b = body_of(text_to_html("1. Run:\n```\nmake\n```\n2. Then check"))
        self.assertTrue(b.endswith("<ol start=\"2\">\n<li>Then check</li>\n</ol>"), b)

    def test_nested_bullets_stay_inside_numbered_item(self):
        b = body_of(text_to_html("Plan:\n1. one\n   - sub a\n   - sub b\n2. two\n\n3. three"))
        self.assertEqual(b, "<p>Plan:</p>\n<ol>\n<li>one\n<ul>\n<li>sub a</li>\n<li>sub b</li>\n</ul></li>\n"
                            "<li>two</li>\n<li>three</li>\n</ol>")
        self.assertEqual(b.count("<ol"), 1)

    def test_nested_numbered_list(self):
        b = body_of(text_to_html("1. a\n   1. a1\n   2. a2\n2. b"))
        self.assertEqual(b, "<ol>\n<li>a\n<ol>\n<li>a1</li>\n<li>a2</li>\n</ol></li>\n<li>b</li>\n</ol>")

    def test_indented_paragraph_after_blank_belongs_to_item(self):
        b = body_of(text_to_html("1. one\n\n   more about one\n\n2. two"))
        self.assertEqual(b, "<ol>\n<li>one<br>\n<br>more about one</li>\n<li>two</li>\n</ol>")
        b = body_of(text_to_html("- a\n  - x\n\n  back to a\n- b"))
        self.assertEqual(b, "<ul>\n<li>a\n<ul>\n<li>x</li>\n</ul>\nback to a</li>\n<li>b</li>\n</ul>")

    def test_list_ends_at_unindented_text_or_other_kind(self):
        b = body_of(text_to_html("- a\n\nafter\n- b"))
        self.assertEqual(b, "<ul>\n<li>a</li>\n</ul>\n<p>after</p>\n<ul>\n<li>b</li>\n</ul>")
        b = body_of(text_to_html("1. a\n\n- b"))
        self.assertEqual(b, "<ol>\n<li>a</li>\n</ol>\n<ul>\n<li>b</li>\n</ul>")

    def test_list_items_still_escaped(self):
        b = body_of(text_to_html("1. <b>x</b>\n   - a & b\n\n   <script>"))
        self.assertNotIn("<b>x", b)
        self.assertNotIn("<script>", b)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", b)
        self.assertIn("a &amp; b", b)

    def test_fenced_code(self):
        b = body_of(text_to_html("run:\n```\nls <dir>\n  indented\n```\ndone"))
        self.assertIn("<pre", b)
        self.assertIn("ls &lt;dir&gt;\n  indented</pre>", b)
        self.assertTrue(b.endswith("<p>done</p>"))

    def test_heading(self):
        self.assertEqual(body_of(text_to_html("# Title\ntext")), "<h3>Title</h3>\n<p>text</p>")

    def test_empty(self):
        doc = text_to_html("")
        self.assertIn("<body", doc)
        self.assertEqual(body_of(doc), "")

    def test_document_has_styles(self):
        doc = text_to_html("x")
        self.assertTrue(doc.startswith("<!DOCTYPE html>"))
        self.assertIn("font-family", doc)
        self.assertIn("line-height", doc)


class FakeGmail:
    """Captures what agent-mail would send; no network."""
    def __init__(self):
        self.sent = []

    def service(self):
        return "svc"

    def send_raw(self, svc, raw, thread_id=None):
        self.sent.append((raw, thread_id))
        return {"id": "g%d" % len(self.sent), "threadId": thread_id or "t%d" % len(self.sent)}

    def get_metadata(self, svc, gid, names):
        return {"payload": {"headers": [{"name": "Message-ID", "value": "<%s@mail.gmail.com>" % gid}]}}


class Args:
    def __init__(self, **kw):
        self.__dict__.update(dict(body_file=None, html=None, session=None, cli=None, cc=None, unrouted=False))
        self.__dict__.update(kw)


class SendAndReply(unittest.TestCase):
    def setUp(self):
        self.am = load_script("agent-mail")
        self.fake = FakeGmail()
        for name in ("service", "send_raw", "get_metadata"):
            setattr(self.am.gmail, name, getattr(self.fake, name))
        self.am.deliver.registry = lambda: []
        self.body = Path(TMP) / "body.txt"
        self.body.write_text("Hi,\n\n- one & two\n- three\n")
        self.html = Path(TMP) / "body.html"
        self.html.write_text("<p>hand-written</p>")
        st = self.am.Store()
        self.inb = st.record_inbound(gmail_id="in1", thread_id="thr1", message_id="<orig@x>", from_addr="owner@example.com",
                                     from_name="Alex", subject="results", matched_tag=None, action="delivered", body="?")

    def last(self):
        raw, thread_id = self.fake.sent[-1]
        return email.message_from_bytes(raw, policy=email.policy.default), thread_id

    def assert_alternative(self, msg, html_contains):
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        types = [p.get_content_type() for p in msg.iter_parts()]
        self.assertEqual(types, ["text/plain", "text/html"])
        self.assertEqual(msg.get_body(("plain",)).get_content(), self.body.read_text())
        self.assertIn(html_contains, msg.get_body(("html",)).get_content())

    def test_send_attaches_html_by_default(self):
        self.am.do_send(Args(to="owner@example.com", subject="hello", body_file=str(self.body)))
        msg, _ = self.last()
        self.assert_alternative(msg, "<li>one &amp; two</li>")
        self.assertIn("[#", msg["Subject"])
        self.assertIn("+", msg["Reply-To"])

    def test_send_html_override(self):
        self.am.do_send(Args(to="owner@example.com", subject="hello", body_file=str(self.body), html=str(self.html)))
        msg, _ = self.last()
        self.assert_alternative(msg, "<p>hand-written</p>")
        self.assertNotIn("<li>", msg.get_body(("html",)).get_content())

    def test_send_unrouted_also_html(self):
        with patch.dict(os.environ, SSH_CONNECTION=""):
            self.am.do_send(Args(to="owner@example.com", subject="hello", body_file=str(self.body), unrouted=True))
        msg, _ = self.last()
        self.assert_alternative(msg, "<li>three</li>")
        self.assertIsNone(msg["Reply-To"])

    def test_reply_attaches_html_by_default_and_threads(self):
        self.am.do_reply(Args(inbound_id=self.inb, body_file=str(self.body)))
        msg, thread_id = self.last()
        self.assert_alternative(msg, "<li>one &amp; two</li>")
        self.assertEqual(thread_id, "thr1")
        self.assertEqual(msg["In-Reply-To"], "<orig@x>")
        self.assertEqual(msg["References"], "<orig@x>")
        self.assertTrue(msg["Subject"].startswith("Re: results [#"))

    def test_reply_html_override(self):
        self.am.do_reply(Args(inbound_id=self.inb, body_file=str(self.body), html=str(self.html)))
        msg, thread_id = self.last()
        self.assert_alternative(msg, "<p>hand-written</p>")
        self.assertEqual(thread_id, "thr1")
        self.assertEqual(msg["In-Reply-To"], "<orig@x>")

    def test_reply_cli_accepts_html_flag(self):
        import subprocess
        out = subprocess.run([sys.executable, str(ROOT / "bin" / "agent-mail"), "reply", "--help"],
                             capture_output=True, text=True, env=dict(os.environ)).stdout
        self.assertIn("--html", out)


if __name__ == "__main__":
    unittest.main()
