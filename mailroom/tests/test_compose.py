import email
import email.policy
import html
import os
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import config, outbound, piped  # noqa: E402
from agentmail.compose import CODE_STYLE, LINK_STYLE, cut_items, html_to_text, placed_images, text_to_html  # noqa: E402
from agentmail.store import Store  # noqa: E402


def strip_styles(fragment):
    """The fragment without style attributes, so a test reads the structure."""
    return re.sub(r' style="(?!text-align)[^"]*"', "", re.sub(r'style="[^"]*?(text-align: \w+;)"', r'style="\1"', fragment))


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

    def test_cut_items_are_the_lines_that_end_a_list_early(self):
        text = ("1. first point, wrapped\nhere\n2. second\n   continued\nflush\n- a\n\nnew para\n```\n- x\ny\n```\n"
                "- b\n# Heading")
        self.assertEqual(cut_items(text), [("1. first point, wrapped", "here"), ("continued", "flush")])
        self.assertIn("</ol>\n<p>here</p>", text_to_html(text))

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

    def test_pipe_table_with_separator_row(self):
        b = strip_styles(body_of(text_to_html(
            "Intro:\n| Person | Score | Note |\n|---|--:|:-:|\n| Robin | 12.5 | **big** |\n| Jo | 5 | `a|b` |\nafter")))
        self.assertEqual(b, "<p>Intro:</p>\n<div><table>\n"
                            '<tr><th style="text-align: left;">Person</th><th style="text-align: right;">Score</th>'
                            '<th style="text-align: center;">Note</th></tr>\n'
                            '<tr><td style="text-align: left;">Robin</td><td style="text-align: right;">12.5</td>'
                            '<td style="text-align: center;"><b>big</b></td></tr>\n'
                            '<tr><td style="text-align: left;">Jo</td><td style="text-align: right;">5</td>'
                            '<td style="text-align: center;"><code>a|b</code></td></tr>\n</table></div>\n<p>after</p>')

    def test_pipe_table_without_separator_or_outer_pipes(self):
        b = body_of(text_to_html("Role | Level | Billing\nAdmin | org | yes\nMember | org | no"))
        self.assertEqual(b.count("<tr>"), 3)
        self.assertEqual(b.count("<th "), 3)
        self.assertIn(">Member</td>", b)
        self.assertNotIn("|", b)

    def test_pipe_table_rows_escaped_padded_and_cut(self):
        b = body_of(text_to_html("| a<b | c&d | x |\n|---|---|---|\n| <s> | \\| pipe |\n| 1 | 2 | 3 | 4 |"))
        self.assertIn(">a&lt;b</th>", b)
        self.assertIn(">&lt;s&gt;</td>", b)
        self.assertIn(">| pipe</td>", b)                       # an escaped pipe is a character, not a cell break
        self.assertEqual(b.count("<td "), 6)                   # short row padded, long row cut to the header's width

    def test_table_without_separator_ends_at_a_row_of_another_width(self):
        self.assertNotIn("<table", text_to_html("a | b | c\nd | e"))
        b = strip_styles(body_of(text_to_html("a | b | c\nd | e | f\ng | h")))
        self.assertEqual(b.count("<tr>"), 2)
        self.assertTrue(b.endswith("</table></div>\n<p>g | h</p>"), b)

    def test_separator_must_match_the_header(self):
        self.assertNotIn("<table", text_to_html("a | b\n|"))
        self.assertNotIn("<table", text_to_html("| a | b | c |\n|---|---|\n| 1 | 2 | 3 |"))   # as on GitHub

    def test_pipe_text_that_is_not_a_table(self):
        for text in ("use a | b here", "cost | time\nfoo | bar", "ls | grep x\nps | wc",
                     "- a | b | c\n- d | e | f", "```\n| a | b | c |\n| 1 | 2 | 3 |\n```"):
            self.assertNotIn("<table", text_to_html(text), text)

    def test_table_ends_a_list_and_a_paragraph(self):
        b = strip_styles(body_of(text_to_html("- item\n| a | b | c |\n| 1 | 2 | 3 |\n- next")))
        self.assertTrue(b.startswith("<ul>\n<li>item</li>\n</ul>\n<div><table>"), b)
        self.assertTrue(b.endswith("</table></div>\n<ul>\n<li>next</li>\n</ul>"), b)

    def test_pipe_lines_under_a_list_item_become_a_table(self):
        b = strip_styles(body_of(text_to_html("- item\n  a | b | c\n  d | e | f")))
        self.assertTrue(b.startswith("<ul>\n<li>item</li>\n</ul>\n<div><table>"), b)

    def test_links(self):
        b = strip_styles(body_of(text_to_html("see [the docs](https://x.y/a_(b)?q=1&r=2) and [x](javascript:alert(1))")))
        self.assertIn('<a href="https://x.y/a_(b)?q=1&amp;r=2">the docs</a>', b)
        self.assertIn("[x](javascript:alert(1))", b)             # only http(s) and mailto become links
        self.assertIn('<code>[k](https://a.b)</code>', strip_styles(body_of(text_to_html("`[k](https://a.b)`"))))
        b = strip_styles(body_of(text_to_html("[a](https://x/`y`)")))
        self.assertEqual(b, "<p>[a](https://x/<code>y</code>)</p>")      # code in a URL: left unlinked, never a broken href

    def test_italic(self):
        b = body_of(text_to_html("see *this* and *that one*, 2*3*4, a * b * c, *.py and *.txt"))
        self.assertIn("<i>this</i> and <i>that one</i>", b)
        self.assertIn("2*3*4, a * b * c, *.py and *.txt", b)
        self.assertIn("dial *61* now", body_of(text_to_html("dial *61* now")))
        b = strip_styles(body_of(text_to_html("*`code`* and *[x](https://a.b)*")))
        self.assertEqual(b, '<p><i><code>code</code></i> and <i><a href="https://a.b">x</a></i></p>')
        self.assertIn(r"\\server\share and grep \(a\)", body_of(text_to_html(r"\\server\share and grep \(a\)")))
        # the escaped [ is no link markup; the address inside is linked as a bare one (BareAddresses)
        self.assertEqual(body_of(text_to_html("\\*not italic\\* and \\[x](https://a.b) and `\\*`")),
                         '<p>*not italic* and [x](<a href="https://a.b" style="%s">https://a.b</a>) and '
                         '<code style="%s">\\*</code></p>' % (LINK_STYLE, CODE_STYLE))
        self.assertEqual(body_of(text_to_html("***both*** and **bold *it* inside**")),
                         "<p><b><i>both</i></b> and <b>bold <i>it</i> inside</b></p>")

    def test_empty(self):
        doc = text_to_html("")
        self.assertIn("<body", doc)
        self.assertEqual(body_of(doc), "")

    def test_document_has_styles(self):
        doc = text_to_html("x")
        self.assertTrue(doc.startswith("<!DOCTYPE html>"))
        self.assertIn("font-family", doc)
        self.assertIn("line-height", doc)


def links(text):
    """The paragraph text_to_html makes of one line of text, without styles."""
    return strip_styles(body_of(text_to_html(text)))


def a(url):
    """The link a bare url becomes (url as typed; the href and the words escaped)."""
    return '<a href="%s">%s</a>' % (html.escape(url, quote=True), html.escape(url, quote=False))


class PlacedImages(unittest.TestCase):
    """A line of its own `![alt](cid:NAME)` in a plain-text body shows the mail's --image NAME there; one not placed
    goes at the end, as before; a NAME the mail has no image of is refused before anything is sent."""

    def test_an_image_line_shows_the_image_in_its_place(self):
        b = body_of(text_to_html("# Result\nThe point.\n  ![Fixes by prompt & model](cid:fill)\nAfter it."))
        self.assertEqual(strip_styles(b), '<h3>Result</h3>\n<p>The point.</p>\n'
                         '<p><img src="cid:fill" alt="Fixes by prompt &amp; model"></p>\n<p>After it.</p>')
        self.assertIn('style="max-width:100%;height:auto"', b)
        self.assertIn('alt="fill"', body_of(text_to_html("![](cid:fill)")))      # no alt text: the name

    def test_only_a_line_of_its_own_and_never_in_code(self):
        for text in ("See ![chart](cid:fill) here.", "```\n![chart](cid:fill)\n```", "![chart](https://x.test/a.png)"):
            with self.subTest(text=text):
                self.assertNotIn("<img", text_to_html(text))
                self.assertEqual(placed_images(text), [])

    def test_placed_images_are_not_repeated_at_the_end_and_the_rest_go_there(self):
        text = "Headline.\n\n![The chart](cid:fill)\n\nMore."
        doc = outbound.place_images(text_to_html(text), [("fill", b""), ("extra", b"")])
        self.assertEqual(doc.count("cid:fill"), 1)
        self.assertLess(doc.index("cid:fill"), doc.index("More."))
        self.assertGreater(doc.index("cid:extra"), doc.index("More."))
        self.assertEqual(placed_images(text), ["fill"])

    def test_a_name_with_no_image_is_refused(self):
        mail = outbound.Mail("Headline.\n\n![The chart](cid:fil)\n", None, [("fill", b"")], [])
        with self.assertRaisesRegex(outbound.MailError, r"!\[\.\.\.\]\(cid:fil\) names no --image fil \(the mail's: fill\)"):
            outbound.check_mail(mail)
        outbound.check_mail(mail._replace(text="Headline.\n\n![The chart](cid:fill)\n"))   # its own: fine


class BareAddresses(unittest.TestCase):
    """An http or https address typed without [text](...) markup is a link, as GitHub's markdown links one."""

    def test_a_bare_address_is_the_same_link_as_one_marked_up_with_itself(self):
        url = "https://claude.ai/artifact/0b9e1c2d-aa31-4f2e-9d7c-5e6f7a8b9c0d"
        self.assertEqual(links("The page: %s" % url), "<p>The page: %s</p>" % a(url))
        self.assertEqual(body_of(text_to_html(url)), body_of(text_to_html("[%s](%s)" % (url, url))))
        self.assertEqual(links("http://localhost:8080/x and https://b.example"),
                         "<p>%s and %s</p>" % (a("http://localhost:8080/x"), a("https://b.example")))
        self.assertEqual(links("HTTPS://Example.com/A"), "<p>%s</p>" % a("HTTPS://Example.com/A"))
        b = links("# Results at https://x.y/r\n\n- see https://x.y/a\n\n"
                  "| run | page |\n|---|---|\n| e6 | https://x.y/e6 |")
        for url in ("https://x.y/r", "https://x.y/a", "https://x.y/e6"):     # in a heading, a list item, a table cell
            self.assertIn(a(url), b)

    def test_punctuation_after_an_address_stays_outside_it(self):
        for p in ".,;:!?'*":
            with self.subTest(p=p):
                self.assertEqual(links("go to https://x.y/z%s" % p), "<p>go to %s%s</p>" % (a("https://x.y/z"), p))
        self.assertEqual(links("Is it https://x.y/z?!"), "<p>Is it %s?!</p>" % a("https://x.y/z"))
        self.assertEqual(links("'https://x.y/z'"), "<p>'%s'</p>" % a("https://x.y/z"))
        self.assertEqual(links("**https://x.y/z**, and *https://x.y/w*."),
                         "<p><b>%s</b>, and <i>%s</i>.</p>" % (a("https://x.y/z"), a("https://x.y/w")))
        self.assertEqual(links("http://localhost:8080/run?id=3:"), "<p>%s:</p>" % a("http://localhost:8080/run?id=3"))
        for bare in ("https://", "https://.", "see https://?!"):          # no address left once its punctuation goes
            with self.subTest(bare=bare):
                self.assertNotIn("<a", links(bare))

    def test_brackets_inside_an_address_stay_and_a_closing_one_without_its_pair_goes(self):
        wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
        self.assertEqual(links(wiki), "<p>%s</p>" % a(wiki))
        self.assertEqual(links("(see https://x.y/z)"), "<p>(see %s)</p>" % a("https://x.y/z"))
        self.assertEqual(links("(see %s)." % wiki), "<p>(see %s).</p>" % a(wiki))
        self.assertEqual(links("[see https://x.y/z]"), "<p>[see %s]</p>" % a("https://x.y/z"))
        self.assertEqual(links("https://x.y/a[0]/b"), "<p>%s</p>" % a("https://x.y/a[0]/b"))
        self.assertEqual(links("http://[::1]:8080/"), "<p>%s</p>" % a("http://[::1]:8080/"))
        self.assertEqual(links("<https://x.y/z>"), "<p>&lt;%s&gt;</p>" % a("https://x.y/z"))
        self.assertEqual(links('"https://x.y/z"'), '<p>"%s"</p>' % a("https://x.y/z"))
        self.assertEqual(links("https://x.y/z)].)"), "<p>%s)].)</p>" % a("https://x.y/z"))
        start = time.monotonic()                     # a line of closing brackets is trimmed in one pass
        self.assertEqual(links("https://x.y/z" + ")" * 200000), "<p>%s%s</p>" % (a("https://x.y/z"), ")" * 200000))
        self.assertLess(time.monotonic() - start, 2)

    def test_no_link_in_code(self):
        self.assertEqual(links("`https://x.y/z`"), "<p><code>https://x.y/z</code></p>")
        self.assertEqual(links("run `curl https://x.y/z` now"), "<p>run <code>curl https://x.y/z</code> now</p>")
        self.assertEqual(links("Run:\n\n```\ncurl https://x.y/z\n```"), "<p>Run:</p>\n<pre>curl https://x.y/z</pre>")
        # an address that runs on into a code span is left as typed, never linked to its first half
        self.assertEqual(links("https://x.y/`z`"), "<p>https://x.y/<code>z</code></p>")

    def test_a_marked_up_link_gets_no_second_anchor(self):
        for text, href, words in (("[the docs](https://x.y/z)", "https://x.y/z", "the docs"),
                                  ("[https://x.y/z](https://x.y/z)", "https://x.y/z", "https://x.y/z"),
                                  ("[see https://a.b/c](https://x.y/z)", "https://x.y/z", "see https://a.b/c"),
                                  ("*[x](https://a.b)*", "https://a.b", "x")):
            with self.subTest(text=text):
                b = links(text)
                self.assertEqual(b.count("<a "), 1, b)
                self.assertIn('<a href="%s">%s</a>' % (href, words), b)
        # an escaped [ is no link markup, so the address in it is linked as a bare one
        self.assertEqual(links("\\[x](https://a.b)"), "<p>[x](%s)</p>" % a("https://a.b"))

    def test_an_address_is_escaped_in_its_href_and_its_words(self):
        url = "https://x.y/p?a=1&b=2&it's=3#frag"
        self.assertEqual(links("open %s now" % url),
                         '<p>open <a href="https://x.y/p?a=1&amp;b=2&amp;it&#x27;s=3#frag">'
                         "https://x.y/p?a=1&amp;b=2&amp;it's=3#frag</a> now</p>")
        self.assertEqual(links("https://x.y/<script>"), "<p>%s&lt;script&gt;</p>" % a("https://x.y/"))

    def test_only_http_and_https_addresses_are_linked(self):
        for text in ("javascript:alert(1)", "data:text/html,<b>x</b>", "data:text/html;base64,PGI+eDwvYj4=",
                     "mailto:owner@example.com", "www.example.com", "example.com/x", "ftp://x.y/z", "file:///etc/x",
                     "xhttps://x.y/z", "[x](javascript:alert(1))", "[x](data:text/html,hi)"):
            with self.subTest(text=text):
                self.assertNotIn("<a", links(text))
        # whatever comes before it, a link's href is the http(s) address itself
        b = links("javascript:https://x.y/z and data:text/html,https://x.y/w")
        self.assertEqual(re.findall(r'href="([^"]*)"', b), ["https://x.y/z", "https://x.y/w"])


class Args:
    def __init__(self, **kw):
        self.__dict__.update(dict(body_file=None, html=None, session=None, cli=None, cc=None, unrouted=False,
                                  alert=None, image=None, attach=None, no_review=None, subject=None))
        self.__dict__.update(kw)


class SendAndReply(unittest.TestCase):
    def setUp(self):
        self.am = load_script("agent-mail")
        self.ctx = outbound.Context(config.load())
        self.sent = fakes.sending_gmail(self, self.am, outbound)   # the Gmail API: nothing is sent
        homes.Homes(self)   # no sessions in the registry: the sending one comes from CLAUDE_CODE_SESSION_ID alone
        env = patch.dict(os.environ, CLAUDE_CODE_SESSION_ID="22222222-2222-2222-2222-222222222222")   # the sending session
        env.start()
        self.addCleanup(env.stop)
        self.body = Path(TMP) / "body.txt"
        self.body.write_text("Hi,\n\n- one & two\n- three\n")
        self.html = Path(TMP) / "body.html"
        self.html.write_text("<p>hand-written</p>")
        st = Store()
        self.inb = st.record_inbound(gmail_id="in1", thread_id="thr1", message_id="<orig@x>", from_addr="alex@example.com",
                                     from_name="Alex", subject="results", matched_tag=None, action="delivered", body="?")

    def last(self):
        raw, thread_id = self.sent[-1]
        return email.message_from_bytes(raw, policy=email.policy.default), thread_id

    def assert_alternative(self, msg, html_contains):
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        types = [p.get_content_type() for p in msg.iter_parts()]
        self.assertEqual(types, ["text/plain", "text/html"])
        self.assertEqual(msg.get_body(("plain",)).get_content(), self.body.read_text())
        self.assertIn(html_contains, msg.get_body(("html",)).get_content())

    def test_send_attaches_html_by_default(self):
        self.am.do_send(self.ctx, Args(to="alex@example.com", subject="hello", body_file=str(self.body)))
        msg, _ = self.last()
        self.assert_alternative(msg, "<li>one &amp; two</li>")
        self.assertIn("[#", msg["Subject"])
        self.assertIn("+", msg["Reply-To"])

    def test_send_html_override(self):
        self.am.do_send(self.ctx, Args(to="alex@example.com", subject="hello", body_file=str(self.body), html=str(self.html)))
        msg, _ = self.last()
        self.assert_alternative(msg, "<p>hand-written</p>")
        self.assertNotIn("<li>", msg.get_body(("html",)).get_content())

    def test_send_unrouted_also_html(self):
        with patch.dict(os.environ, SSH_CONNECTION=""):
            self.am.do_send(self.ctx, Args(to="alex@example.com", subject="hello", body_file=str(self.body), unrouted=True))
        msg, _ = self.last()
        self.assert_alternative(msg, "<li>three</li>")
        self.assertIsNone(msg["Reply-To"])

    def test_reply_attaches_html_by_default_and_threads(self):
        self.am.do_reply(self.ctx, Args(inbound_id=self.inb, body_file=str(self.body)))
        msg, thread_id = self.last()
        self.assert_alternative(msg, "<li>one &amp; two</li>")
        self.assertEqual(thread_id, "thr1")
        self.assertEqual(msg["In-Reply-To"], "<orig@x>")
        self.assertEqual(msg["References"].split()[-1], "<orig@x>")   # after the thread's earlier mail (test_mailbox)
        self.assertTrue(msg["Subject"].startswith("Re: results [#"))

    def test_reply_html_override(self):
        self.am.do_reply(self.ctx, Args(inbound_id=self.inb, body_file=str(self.body), html=str(self.html)))
        msg, thread_id = self.last()
        self.assert_alternative(msg, "<p>hand-written</p>")
        self.assertEqual(thread_id, "thr1")
        self.assertEqual(msg["In-Reply-To"], "<orig@x>")

    def test_reply_under_a_new_subject_starts_a_thread_that_links_back(self):
        self.am.do_reply(self.ctx, Args(inbound_id=self.inb, body_file=str(self.body), subject="The fix is in"))
        msg, thread_id = self.last()
        self.assertEqual((thread_id, msg["In-Reply-To"], msg["References"]), (None, None, None))
        self.assertTrue(msg["Subject"].startswith("The fix is in [#"))
        link = "https://mail.google.com/mail/?authuser=alex@example.com#search/rfc822msgid%3Aorig%40x"
        self.assertEqual(msg.get_body(("plain",)).get_content(), self.body.read_text().rstrip()
                         + '\n\n--\nContinues the thread "results". The mail this answers: %s\n' % link)
        html = msg.get_body(("html",)).get_content()
        self.assertIn("<li>one &amp; two</li>", html)
        self.assertIn('Continues the thread &quot;results&quot;. <a href="%s">Open the mail this answers in Gmail</a></p>'
                      % link, html)
        sent = Store().con.execute("select * from sent order by id desc limit 1").fetchone()
        self.assertEqual(sent["in_reply_to_inbound"], self.inb)   # recorded as the answer to that mail all the same

    def test_reply_with_a_blank_subject_is_refused(self):
        for blank in ("", "  "):        # an empty shell variable, not a wish for a thread with no subject
            with self.assertRaisesRegex(outbound.MailError, "--subject is empty"):
                self.am.do_reply(self.ctx, Args(inbound_id=self.inb, body_file=str(self.body), subject=blank))
        self.assertEqual(self.sent, [])

    def test_the_link_back_comes_after_the_images_and_names_the_bare_subject(self):
        inb = {"subject": "Re: Fwd: results [#ab34cd9k]", "from_addr": "alex@example.com", "message_id": None}
        mail = outbound.continued(outbound.Mail("the fix\n", "<html><body><p>the fix</p></body></html>",
                                                [("plot", b"")], []), inb)
        self.assertEqual(mail.text, 'the fix\n\n--\nContinues the thread "results".\n')   # no Message-ID: no link
        self.assertTrue(mail.html.endswith(
            '<p><img src="cid:plot" alt="plot" style="max-width:100%;height:auto"></p>\n<hr><p style="color:#6e7781;'
            'font-size:13px">Continues the thread &quot;results&quot;.</p>\n</body></html>'), mail.html)

    def test_reply_cli_accepts_html_flag(self):
        import subprocess
        out = subprocess.run([sys.executable, str(ROOT / "bin" / "agent-mail"), "reply", "--help"],
                             capture_output=True, text=True, env=dict(os.environ)).stdout
        self.assertIn("--html", out)


class HtmlToText(unittest.TestCase):
    def test_blocks_lists_tables_links_images(self):
        doc = ("<html><head><style>p{color:red}</style></head><body><h2>Result</h2><p>One <b>bold</b>\n  line.</p>"
               "<ul><li>first</li><li>second</li></ul><table><tr><th>What</th><th>Before</th></tr>"
               "<tr><td>CPU</td><td>101%</td></tr></table><p><a href=\"https://x.test/pr\">PR</a></p>"
               "<img src=\"cid:cpu\" alt=\"CPU chart\"><hr><pre>a  b\n c</pre></body></html>")
        self.assertEqual(html_to_text(doc), "## Result\n\nOne bold line.\n\n- first\n\n- second\n\nWhat | Before\n\n"
                         "CPU | 101%\n\nPR <https://x.test/pr>\n[image: CPU chart]\n----\n\na  b\nc\n")

    def test_empty(self):
        self.assertEqual(html_to_text(""), "\n")


class PipedInput(unittest.TestCase):
    """Only input that was really piped or redirected in is read; an open socket (Claude Code's Bash tool, in a
    command holding a heredoc) never ends and must never be waited on."""

    def test_socket_terminal_and_devnull_are_no_input(self):
        import socket
        a, b = socket.socketpair()
        try:
            with a.makefile("r") as fh:
                self.assertIsNone(piped.piped_text(fh, idle=0.2))         # returns at once, never blocks
        finally:
            a.close()
            b.close()
        with open(os.devnull) as fh:
            self.assertIsNone(piped.piped_text(fh))

    def test_pipe_and_file_are_read(self):
        r, w = os.pipe()
        os.write(w, "body ✓\n".encode())
        os.close(w)
        with os.fdopen(r) as fh:
            self.assertEqual(piped.piped_text(fh), "body ✓\n")
        f = Path(TMP) / "redirected.txt"
        f.write_text("from a file\n")
        with open(f) as fh:
            self.assertEqual(piped.piped_text(fh), "from a file\n")

    def test_silent_pipe_gives_up(self):
        r, w = os.pipe()
        try:
            with os.fdopen(r) as fh, self.assertRaises(TimeoutError):
                piped.piped_text(fh, idle=0.2)
        finally:
            os.close(w)


class SendWithHtmlOnly(SendAndReply):
    """`cat > mail.html <<EOF ... EOF; agent-mail send --html mail.html` in Claude Code: no body file, and the
    command's input is the tool's socket, which is never read: the text comes from the HTML."""

    def with_socket_stdin(self, fn):
        import socket
        a, b = socket.socketpair()
        real = sys.stdin
        try:
            sys.stdin = a.makefile("r")
            return fn()
        finally:
            sys.stdin.close()
            sys.stdin = real
            a.close()
            b.close()

    def test_html_alone_sends_with_text_made_from_it(self):
        self.html.write_text("<p>Decision needed</p><ul><li>A</li><li>B</li></ul>")
        self.with_socket_stdin(lambda: self.am.do_send(self.ctx, Args(to="alex@example.com", subject="pick", html=str(self.html))))
        msg, _ = self.last()
        self.assertEqual(msg.get_body(("plain",)).get_content(), "Decision needed\n\n- A\n\n- B\n")
        self.assertIn("<li>A</li>", msg.get_body(("html",)).get_content())

    def test_no_body_at_all_stops_at_once(self):
        with self.assertRaises(SystemExit):
            self.with_socket_stdin(lambda: self.am.do_send(self.ctx, Args(to="alex@example.com", subject="nothing")))
        self.assertEqual(self.sent, [])

    def test_piped_text_still_wins_over_html(self):
        r, w = os.pipe()
        os.write(w, b"my own plain text\n")
        os.close(w)
        real = sys.stdin
        try:
            sys.stdin = os.fdopen(r)
            self.am.do_send(self.ctx, Args(to="alex@example.com", subject="both", html=str(self.html)))
        finally:
            sys.stdin.close()
            sys.stdin = real
        msg, _ = self.last()
        self.assertEqual(msg.get_body(("plain",)).get_content(), "my own plain text\n")


if __name__ == "__main__":
    unittest.main()
