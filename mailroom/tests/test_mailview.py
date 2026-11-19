import base64
import html
import random
import re
import sys
import time
import unittest
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from agentmail import config, mailview, outbound  # noqa: E402
from agentmail.compose import html_to_text  # noqa: E402

CFG = config.load()
PNG = b"\x89PNG\r\n\x1a\n" + bytes(24)
GIF = b"GIF89a" + bytes(12)
CSV = b"run,acc\n" + b"a,75.4\n" * 300
EML = b"From: a@example.com\r\nSubject: forwarded\r\n\r\nthe forwarded body\r\n"


def agent_mail(text, html_body=None, images=(), files=()):
    """The MIME bytes of an agent mail, built as transmit builds every one (outbound.compose)."""
    return outbound.compose(CFG, "tester (claude)", "alex@example.com", None, "Run A results [#ab12cd34]", text,
                            html_body, images=images, files=files).as_bytes()


def text_only_mail(text):
    """A mail with only a text/plain body, as a mailroom alert is (alerts.send_alert)."""
    msg = EmailMessage()
    msg["Subject"] = "alert"
    msg.set_content(text)
    return msg.as_bytes()


class Shape(HTMLParser):
    """What a browser would see in safe_html's or document's output: tags, attributes, the CSS of style elements
    and attributes, and whether every tag is closed in order."""

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.css, self.problems = [], [], [], []
        self.stack, self.in_style = [], False
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [(tag, n, v) for n, v in attrs]
        self.css += [v for n, v in attrs if n == "style"]
        self.in_style = tag == "style"
        if tag not in mailview.VOID and tag != "meta":
            self.stack.append(tag)

    def handle_endtag(self, tag):
        self.in_style = False
        if not self.stack or self.stack.pop() != tag:
            self.problems.append("unbalanced </%s>" % tag)

    def handle_data(self, data):
        if self.in_style:
            self.css.append(data)


def fragment_problems(out):
    """Every way safe_html's output breaks its promises (empty when it keeps them)."""
    shape = Shape(out)
    problems = shape.problems + ["unclosed <%s>" % t for t in shape.stack]
    for tag in shape.tags:
        if tag not in mailview.TAGS and tag != "style":
            problems.append("tag " + tag)
    for tag, name, value in shape.attrs:
        if name not in mailview.ATTRS and tag not in mailview.TAG_ATTRS.get(name, ()) and (tag, name) != ("a", "rel"):
            problems.append("attribute %s on %s" % (name, tag))
        if name == "href" and not re.match(r"(https?|mailto):", value):
            problems.append("href " + value)
        if name == "src" and not re.match(r"data:image/(png|jpeg|gif|webp)[;,]", value):
            problems.append("src " + value)
    css = " ".join(shape.css)
    squashed = re.sub(r"\s+", "", css).lower()
    for bad in ("url(", "@import", "\\", "<", "expression(", "javascript:", "image-set("):
        if bad in squashed:
            problems.append("CSS holds " + bad)
    return problems


def style_block(out):
    """The CSS inside the fragment's <style> element, or None."""
    m = re.match(r'<div class="mail"><style>(.*?)</style>', out, re.S)
    return m.group(1) if m else None


def scope_problem(css, scope):
    """Why css does not keep every rule inside "#SCOPE .mail { ... }" (None when it does): it must open with that
    block and close it only at its very end."""
    head = "#%s .mail {" % scope
    if not css.startswith(head):
        return "does not start with " + head
    depth = 0
    for i, ch in enumerate(css[len(head) - 1:], len(head) - 1):
        depth += {"{": 1, "}": -1}.get(ch, 0)
        if depth <= 0 and i != len(css) - 1:
            return "the scope's block closes at %d" % i
    return None if depth == 0 else "unbalanced"


class Parse(unittest.TestCase):
    def test_parts_numbered_in_walk_order_inline_images_apart(self):
        raw = agent_mail("see the plot", images=[("acc", PNG)],
                         files=[("results.csv", CSV), ("notes.txt", b"plain note"), ("fwd.eml", EML)])
        parsed = mailview.parse(raw)
        self.assertEqual([(p.index, p.name, p.type, p.cid, p.inline) for p in parsed.parts],
                         [(1, "acc.png", "image/png", "acc", True), (2, "results.csv", "text/csv", None, False),
                          (3, "notes.txt", "text/plain", None, False), (4, "fwd.eml", "message/rfc822", None, False)])
        self.assertEqual(mailview.attachments(parsed),
                         [{"index": 2, "name": "results.csv", "type": "text/csv", "size": len(CSV)},
                          {"index": 3, "name": "notes.txt", "type": "text/plain", "size": 10},
                          {"index": 4, "name": "fwd.eml", "type": "message/rfc822", "size": len(EML)}])
        self.assertEqual(parsed.text.strip(), "see the plot")
        self.assertIn('src="cid:acc"', parsed.html)
        self.assertEqual(parsed.headers["Subject"], "Run A results [#ab12cd34]")

    def test_part_returns_the_exact_bytes_and_refuses_other_numbers(self):
        raw = agent_mail("files", images=[("acc", PNG)], files=[("results.csv", CSV), ("fwd.eml", EML)])
        self.assertEqual(mailview.part(raw, 1).data, PNG)
        self.assertEqual(mailview.part(raw, 2).data, CSV)
        self.assertEqual(mailview.part(raw, 3).data, EML)    # sent base64-encoded, read back whole
        for index in (0, 4, -1):
            with self.assertRaises(KeyError):
                mailview.part(raw, index)

    def test_text_only_mail(self):
        parsed = mailview.parse(text_only_mail("The mailroom restarted.\n"))
        self.assertEqual((parsed.text, parsed.html, parsed.parts), ("The mailroom restarted.\n", None, []))

    def test_html_only_mail_takes_its_text_from_the_html(self):
        msg = EmailMessage()
        msg.set_content("<p>Hello <b>there</b></p><ul><li>one</li></ul>", subtype="html")
        parsed = mailview.parse(msg.as_bytes())
        self.assertEqual(parsed.text, html_to_text(parsed.html))
        self.assertIn("- one", parsed.text)

    def test_mail_with_no_body(self):
        msg = EmailMessage()
        msg.add_attachment(b"\x00\x01", maintype="application", subtype="octet-stream", filename="blob.bin")
        parsed = mailview.parse(msg.as_bytes())
        self.assertEqual((parsed.text, parsed.html), ("", None))
        self.assertEqual([(p.index, p.name) for p in parsed.parts], [(1, "blob.bin")])

    def test_part_names_without_a_filename_or_with_a_path(self):
        msg = EmailMessage()
        msg.set_content("body")
        msg.add_attachment(b"a,b\n", maintype="text", subtype="csv")
        msg.add_attachment(b"#!/bin/sh\n", maintype="application", subtype="x-sh", filename="../../evil.sh")
        msg.add_attachment(b"?", maintype="application", subtype="x-unknown-type", filename="..")
        self.assertEqual([p.name for p in mailview.parse(msg.as_bytes()).parts],
                         ["part-1.csv", "evil.sh", "part-3.bin"])


class SafeHtml(unittest.TestCase):
    def clean(self, markup, images=None):
        out = mailview.safe_html(markup, images or {}, "s7", 0)
        self.assertEqual(fragment_problems(out), [], out)
        return out

    def test_formatting_kept_and_text_escaped(self):
        out = self.clean('<p class="x" title=\'a"b\'>a &lt; b &amp; c <b>bold</b><br>next</p>')
        self.assertEqual(out, '<div class="mail"><p class="x" title="a&quot;b">a &lt; b &amp; c <b>bold</b><br>'
                              'next</p></div>')

    def test_cid_images_become_data_uris(self):
        out = self.clean('<img src="cid:acc" alt="accuracy" width="300"><img src="CID:dot" alt="dot">',
                         {"acc": ("image/png", PNG), "dot": ("image/gif", GIF)})
        self.assertIn('<img src="data:image/png;base64,%s" alt="accuracy" width="300">'
                      % base64.b64encode(PNG).decode(), out)
        self.assertIn('src="data:image/gif;base64,%s"' % base64.b64encode(GIF).decode(), out)

    def test_unknown_cid_and_non_image_parts_are_not_shown(self):
        out = self.clean('<img src="cid:gone" alt="lost plot"><img src="cid:vec">',
                         {"vec": ("image/svg+xml", b"<svg onload=alert(1)/>")})
        self.assertEqual(out, '<div class="mail">[image not shown: lost plot][image not shown]</div>')

    def test_remote_and_other_image_sources_are_dropped(self):
        for src in ("http://tracker.example/p.png", "https://tracker.example/p.png", "//tracker.example/p.png",
                    "file:///etc/passwd", "p.png", "data:image/svg+xml;base64,PHN2Zz4=", "data:text/html,<b>x</b>",
                    " javascript:alert(1)"):
            with self.subTest(src=src):
                self.assertEqual(self.clean('<img src="%s" alt="pic">' % src),
                                 '<div class="mail">[image not shown: pic]</div>')

    def test_data_images_stay(self):
        out = self.clean('<img src="data:image/webp;base64,UklGRg==">')
        self.assertEqual(out, '<div class="mail"><img src="data:image/webp;base64,UklGRg=="></div>')

    def test_links(self):
        for href, kept in (("https://example.com/a?b=1&c=2", 'href="https://example.com/a?b=1&amp;c=2" '),
                           ("http://example.com", 'href="http://example.com" '),
                           ("mailto:alex@example.com", 'href="mailto:alex@example.com" '),
                           ("javascript:alert(1)", ""), ("JaVaScRiPt:alert(1)", ""), (" java\tscript:alert(1)", ""),
                           ("&#106;avascript:alert(1)", ""), ("java\x00script:alert(1)", ""),
                           ("vbscript:msgbox(1)", ""), ("data:text/html,<script>alert(1)</script>", ""),
                           ("//evil.example/x", ""), ("/relative", ""), ("#top", "")):
            with self.subTest(href=href):
                out = self.clean('<a href="%s" target="_blank" rel="opener">link</a>' % href)
                self.assertEqual(out, '<div class="mail"><a %srel="noreferrer noopener">link</a></div>' % kept)

    def test_attributes_outside_the_allowlist_are_dropped(self):
        out = self.clean('<p id="i" name="n" onclick="x()" OnMouseOver=y xmlns="z" align="center">t</p>'
                         '<img src="cid:a" srcset="https://x/1.png 2x" onerror="x()">'
                         '<ol type="a" start="3" reversed><li type="i">x</li></ol><p type="x">y</p>'
                         '<font size="3" face="Arial" color="red">f</font><td size="9">c</td>',
                         {"a": ("image/png", PNG)})
        self.assertIn('<p align="center">t</p>', out)
        self.assertNotIn("srcset", out)
        self.assertIn('<ol type="a" start="3" reversed=""><li type="i">x</li></ol><p>y</p>', out)
        self.assertIn('<font size="3" face="Arial" color="red">f</font><td>c</td>', out)

    def test_bypasses(self):
        cases = {
            "mixed case": "<ScRiPt>alert(1)</sCrIpT><IMG SRC=x OnError=alert(1)>",
            "split tag": "<scr<script>ipt>alert(1)</script>",
            "unquoted": "<a href=javascript:alert(1)>x</a><img src=x onerror=alert(1)>",
            "null bytes": "<scr\x00ipt>alert(1)</scr\x00ipt><a href='java\x00script:alert(1)'>x</a>",
            "</style> in CSS": '<style>p{content:"</style><script>alert(1)</script>"}</style>',
            "svg script": "<svg><script>alert(1)</script><a xlink:href=javascript:alert(1)>x</a></svg>",
            "math style": "<math><mi><style><img src=x onerror=alert(1)></style></mi></math>",
            "tab in scheme": '<a href=" java\tscript:alert(1)">x</a>',
            "unclosed script": "<p>before</p><script>alert(1)",
            "comment": "<!-- <script>alert(1)</script> --><!--><script>alert(1)</script>-->",
            "cdata": "<![CDATA[<script>alert(1)</script>]]>",
            "marked section": "<![foo[<script>alert(1)</script>]]>",
            "processing instruction": "<?xml version='1.0'?><?php alert(1) ?>",
            "iframe srcdoc": '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
            "noscript": "<noscript><img src=x onerror=alert(1)></noscript>",
            "template": "<template><script>alert(1)</script></template>",
            "object": '<object data="javascript:alert(1)"><embed src="x.swf"></object>',
            "base and meta": '<base href="https://evil.example/"><meta http-equiv="refresh" content="0;url=x">',
            "form": '<form action="https://evil.example"><input name=x><button formaction=x>Go</button></form>',
            "self-closing script": "<script/>alert(1)",
            "entity tag": "&lt;script&gt;alert(1)&lt;/script&gt;",
        }
        for name, markup in cases.items():
            with self.subTest(name):
                out = self.clean(markup)
                self.assertNotRegex(out.lower(), r"<script|<svg|<math|<iframe|<object|<embed|<form|<input|<base|"
                                                 r"<meta|onerror|href=\"java|<!--|<\?")

    def test_dropped_elements_take_their_content(self):
        out = self.clean("<p>kept</p><title>T</title><select><option>o</option></select><textarea>t</textarea>"
                         "<button>b</button><canvas>c</canvas><video><source src=x>v</video><audio>a</audio>"
                         "<picture>p</picture><noembed>n</noembed><frameset><frame src=x>f</frameset><p>end</p>")
        self.assertEqual(out, '<div class="mail"><p>kept</p><p>end</p></div>')

    def test_unknown_and_document_tags_are_dropped_keeping_their_text(self):
        out = self.clean("<html><head><title>T</title><meta charset=x><link rel=stylesheet href=x></head>"
                         '<body style="color: #222; padding: 16px" bgcolor="#fff" onload="x()"><blink>hi</blink> '
                         "<form>in form</form></body></html>")
        self.assertEqual(out, '<div class="mail"><div style="color: #222; padding: 16px;" bgcolor="#fff">'
                              "hi in form</div></div>")

    def test_broken_markup_is_closed_and_stray_closers_dropped(self):
        self.assertEqual(self.clean("<div><b>bold <i>both"),
                         '<div class="mail"><div><b>bold <i>both</i></b></div></div>')
        self.assertEqual(self.clean("</div></details></body></html><p>x</td></p>"),
                         '<div class="mail"><p>x</p></div>')
        self.assertEqual(self.clean("<b><i>x</b>y</i>"), '<div class="mail"><b><i>x</i></b>y</div>')

    def test_scope_must_be_a_message_key(self):
        for scope in ("S1", "s", "1", "s1 x", "s1\n", "s1{", ""):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                mailview.safe_html("<p>x</p>", {}, scope, 0)

    def test_hostile_soup_stays_safe(self):
        markup_tokens = ["<", ">", "/", "=", " ", '"', "'", "&", "#", "\x00", "\t", "\n", "x", "p", ";", "\\",
                         "<!--", "-->", "<![", "]]>", "<!", "?", "<p>", "</p>", "<b>", "<div>", "</div>", "<script>",
                         "</script>", "<style>", "</style>", "<svg>", "</svg>", "<math>", "<iframe>", "</iframe>",
                         "<textarea>", "<title>", "<a href=", "<img src=", "onerror=", "style=", "javascript:",
                         "data:", "cid:a", "url(", "&lt;", "&#60;"]
        css_tokens = ["{", "}", ";", ":", ",", "(", ")", '"', "'", "\\", "\n", " ", "/*", "*/", "<", "</style>",
                      "&", "~", ">", "+", "@media x", "@supports (a:b)", "@import", "@font-face", "body", "p", ".k",
                      "color:red", "url(x)", "u\\72l(x)", "image-set(", "expression(", "!important"]
        css_tokens += ["p{", "body{", ".k, td{", "@media x{", "color:red;", "}"] * 10     # rule-shaped, mostly
        rng, styled = random.Random(16), 0
        for _ in range(3000):
            markup = "%s<style>%s</style>%s" % tuple(
                "".join(rng.choice(tokens) for _ in range(rng.randint(0, 30)))
                for tokens in (markup_tokens, css_tokens, markup_tokens))
            out = mailview.safe_html(markup, {"a": ("image/png", PNG)}, "s7", 0)
            self.assertEqual(fragment_problems(out), [], (markup, out))
            css = style_block(out)
            if css is not None:
                styled += 1
                self.assertIsNone(scope_problem(css, "s7"), (markup, css))
        self.assertGreater(styled, 100)            # the soup reached the CSS sanitizer often

    def test_deep_nesting_does_not_fail(self):
        out = mailview.safe_html("<div>" * 5000 + "<style>" + "a{" * 5000 + "color:red</style>x", {}, "s7", 0)
        self.assertTrue(out.endswith("</div>" * 5001))


class Css(unittest.TestCase):
    def style_attr(self, css):
        out = mailview.safe_html('<p style="%s">x</p>' % css, {}, "s7", 0)
        m = re.search(r'<p(?: style="([^"]*)")?>', out)
        return html.unescape(m.group(1) or "")

    def test_style_attributes_lose_every_loading_or_scripted_declaration(self):
        for css in ("background: url(https://x.example/p.png)", "background:URL( x )", "background: url('x')",
                    "background: u\\72l(x)", "background: \\75rl(x)", "background: &#117;rl(x)",
                    "background: ur\x00l(x)", "background-image: image-set('https://x.example/p.png' 1x)",
                    "background-image: -webkit-image-set(&quot;x.png&quot; 1x)", "width: expression(alert(1))",
                    "behavior: url(x.htc)", "-moz-binding: url(x.xml#x)", "background: javascript:alert(1)",
                    "list-style: src(x)", "content: attr(title url)", "content: '}'", "content: ';'"):
            with self.subTest(css=css):
                self.assertEqual(self.style_attr("color: red; %s; margin: 0" % css), "color: red; margin: 0;")

    def test_ordinary_declarations_stay(self):
        self.assertEqual(self.style_attr("font-family: 'Segoe UI', Arial; color:#333 !important; --gap: 4px"),
                         "font-family: 'Segoe UI', Arial; color:#333 !important; --gap: 4px;")
        self.assertEqual(self.style_attr("color: red /* comment */; nonsense; {x}"), "color: red;")

    def test_an_unclosed_quote_takes_the_rest_of_the_declarations(self):
        self.assertEqual(self.style_attr("color: red; font-family: 'a; margin: 0"), "color: red;")

    def test_style_blocks_are_scoped_to_the_message(self):
        out = mailview.safe_html("<style>p { color: red } body{margin:0;font-family:Georgia} td, th {padding:4px}"
                                 "</style><p>x</p>", {}, "s7", 0)
        self.assertEqual(out, '<div class="mail"><style>#s7 .mail { p { color: red; } & { margin:0; '
                              'font-family:Georgia; } td, th { padding:4px; } }</style><p>x</p></div>')

    def test_every_style_block_is_kept_and_parsed_on_its_own(self):
        out = mailview.safe_html("<style>a { color: red</style><p>x</p><style>b { color: blue }</style>", {}, "s7", 0)
        self.assertEqual(style_block(out), "#s7 .mail { a { color: red; } b { color: blue; } }")

    def test_style_blocks_drop_imports_and_loads(self):
        out = mailview.safe_html('<style>@import url(https://x.example/a.css); @import "https://x.example/b.css";'
                                 "@font-face { font-family: X; src: url(https://x.example/f.woff) }"
                                 "@namespace svg url(http://www.w3.org/2000/svg); @keyframes k { from { top: 0 } }"
                                 "p { color: red; background: url(https://x.example/p.png) }"
                                 "@media (max-width: 600px) { p { font-size: 12px } }</style>", {}, "s7", 0)
        self.assertEqual(style_block(out), "#s7 .mail { p { color: red; } "
                                           "@media (max-width: 600px) { p { font-size: 12px; } } }")

    def test_selectors_that_could_match_outside_the_message_are_dropped(self):
        for css in ("~ * { display: none }", "+ details { display: none }", "> * { display: none }",
                    "|| td { display: none }", "& ~ * { display: none }", ":root :not(&) { display: none }",
                    "p:has(&) { display: none }", "p, ~ * { display: none }", ", p { display: none }",
                    "color: red; display: none", "@media screen { display: none }"):
            with self.subTest(css=css):
                self.assertIsNone(style_block(mailview.safe_html("<style>%s</style>" % css, {}, "s7", 0)))

    def test_stray_braces_cannot_close_the_scope(self):
        for css in ("} body { color: red } p {", 'p { content: "{" } } body { color: red }', "}}}} a { color: red }",
                    "p { color: red } } } i { color: blue"):
            with self.subTest(css=css):
                block = style_block(mailview.safe_html("<style>%s</style>" % css, {}, "s7", 0))
                self.assertIsNone(scope_problem(block, "s7"), block)

    def test_closing_style_inside_css_cannot_end_the_block(self):
        out = mailview.safe_html('<style>p { color: red } a::after { content: "</style><script>alert(1)</script>" }'
                                 "</style>", {}, "s7", 0)
        self.assertEqual(out.count("</style>"), 1)
        self.assertNotIn("<script", out)
        self.assertEqual(style_block(out), "#s7 .mail { p { color: red; } }")

    def test_svg_style_is_not_collected(self):
        out = mailview.safe_html("<svg><style>p { color: red }</style></svg><p>x</p>", {}, "s7", 0)
        self.assertEqual(out, '<div class="mail"><p>x</p></div>')


def entry(key, **kw):
    """A document entry with every field document reads."""
    return dict({"key": key, "sender": "tester (claude)", "recipients": "alex@example.com",
                 "ts": time.time() - 60, "mine": False, "open": True, "status": None, "raw": None, "text": None,
                 "gmail_url": None, "widgets": 0}, **kw)


class Document(unittest.TestCase):
    def assert_self_contained(self, page):
        """No script, and nothing that could load from the network: every src a data: image, no srcset, no URL in
        any CSS."""
        shape = Shape(page)
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("srcset", page.lower())
        for tag, name, value in shape.attrs:
            if name == "src":
                self.assertTrue(value.startswith("data:image/"), value)
        for css in shape.css:
            self.assertNotRegex(css.lower(), r"https?:|url\(|@import")

    def test_head_and_an_agent_mail_with_html_images_and_attachments(self):
        html_body = ('<html><head><style>h2 { color: #0b8043 }</style></head><body><h2>Run A</h2>'
                     '<img src="cid:acc" alt="accuracy"><img src="https://tracker.example/p.png" alt="t">'
                     '<a href="https://example.com/run">the run</a></body></html>')
        raw = agent_mail("Run A finished.", html_body, images=[("acc", PNG)], files=[("results.csv", CSV)])
        page = mailview.document("Run A", [], [entry("s12", raw=raw)])
        self.assertTrue(page.startswith('<!DOCTYPE html>\n<html><head><meta charset="utf-8">\n'
                                        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                                        "img-src data:; style-src 'unsafe-inline'; font-src data:; base-uri 'none';"
                                        ' form-action \'none\'">\n<meta name="viewport" '
                                        'content="width=device-width, initial-scale=1">\n<style>'))
        self.assertIn('<details id="s12" class="msg" open>', page)
        self.assertIn('<style>#s12 .mail { h2 { color: #0b8043; } }</style>', page)
        self.assertIn('src="data:image/png;base64,%s"' % base64.b64encode(PNG).decode(), page)
        self.assertIn("[image not shown: t]", page)
        self.assertIn('<a href="https://example.com/run" rel="noreferrer noopener">the run</a>', page)
        self.assertIn('<a class="att" href="mailbox-part:s12/2">results.csv</a> (2 KB)', page)
        self.assertNotIn("mailbox-part:s12/1", page)          # the inline image is no attachment
        self.assert_self_contained(page)
        self.assertEqual(Shape(page).problems, [])

    def test_the_subject_and_who_wrote_head_the_page_before_the_first_message(self):
        page = mailview.document("Run A <done> & more", ["alpha (claude)", "orchestrator"],
                                 [entry("s1", text="a"), entry("i2", mine=True, text="b")])
        body = page.split("<body>\n", 1)[1]
        self.assertTrue(body.startswith('<h1 class="subject">Run A &lt;done&gt; &amp; more</h1>\n'
                                        '<p class="about">2 mails · alpha (claude), orchestrator</p>\n'
                                        '<details id="s1" class="msg"'), body[:200])
        self.assertIn("body > .subject {", page)
        self.assert_self_contained(page)
        one = mailview.document("", [], [entry("i1", mine=True, text="x")])
        self.assertIn('<h1 class="subject">%s</h1>\n<p class="about">1 mail</p>' % mailview.NO_SUBJECT, one)

    def test_mail_without_html_is_rendered_from_its_text(self):
        page = mailview.document("Run A", [], [entry("s3", raw=text_only_mail("Two things:\n- one\n- two\n"))])
        self.assertIn("<ul>\n<li>one</li>\n<li>two</li>\n</ul>", page)
        self.assert_self_contained(page)

    def test_sender_recipients_text_and_status_are_escaped(self):
        page = mailview.document("Run A", [], [entry("i4", sender="<b>Alex</b> & co", recipients='"x" <x@example.com>',
                                        mine=True, text="keep <b>this</b> & that\n  indented",
                                        status="held: <no session>")])
        self.assertIn('<details id="i4" class="msg mine" open><summary><b class="from">&lt;b&gt;Alex&lt;/b&gt; '
                      '&amp; co</b><span class="to">to &quot;x&quot; &lt;x@example.com&gt;</span>', page)
        self.assertIn('<div class="text">keep &lt;b&gt;this&lt;/b&gt; &amp; that\n  indented</div>', page)
        self.assertIn('<span class="status">held: &lt;no session&gt;</span>', page)
        self.assert_self_contained(page)

    def test_keys_must_be_message_keys(self):
        for key in ('s1"><script>alert(1)</script>', "S1", "s1 open", "12"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                mailview.document("Run A", [], [entry(key, text="x")])

    def test_open_and_closed_messages_and_the_snippet(self):
        long_text = "word " * 40
        page = mailview.document("Run A", [], [entry("s1", open=False, text=long_text + "\n\nmore"), entry("s2", text="short")])
        self.assertIn('<details id="s1" class="msg">', page)
        self.assertIn('<details id="s2" class="msg" open>', page)
        snippets = re.findall(r'<span class="snip">([^<]*)</span>', page)
        self.assertEqual(snippets[0], ("word " * 24).rstrip() + "…")
        self.assertEqual(snippets[1], "short")
        self.assertIn("body > .msg[open] > summary > .snip { display: none; }", page)

    def test_times_in_local_time_with_the_year_only_when_not_this_year(self):
        old = time.mktime((2025, 10, 12, 21, 37, 0, 0, 0, -1))
        year = time.localtime().tm_year
        recent = time.mktime((year, 1, 5, 9, 3, 0, 0, 0, -1))
        page = mailview.document("Run A", [], [entry("s1", ts=old, text="a"), entry("s2", ts=recent, text="b")])
        self.assertIn('<span class="when">Sun 12 Oct 2025, 21:37</span>', page)
        self.assertIn('<span class="when">%s 5 Jan, 09:03</span>' % time.strftime("%a", time.localtime(recent)),
                      page)

    def test_unstored_mail_links_to_gmail(self):
        url = "https://mail.google.com/mail/?authuser=agent%40example.com#all/18f2a9"
        page = mailview.document("Run A", [], [entry("s5", text="Old results.", gmail_url=url)])
        self.assertIn('<p class="note">%s <a href="%s" rel="noreferrer noopener">Open in Gmail</a></p>'
                      % (mailview.UNSTORED, url), page)
        self.assertIn('<div class="text">Old results.</div>', page)
        for bad in ("javascript:alert(1)", "mailto:x@example.com", "//mail.google.com"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                mailview.document("Run A", [], [entry("s5", text="x", gmail_url=bad)])

    def test_optional_fields_may_be_left_out_but_content_may_not(self):
        page = mailview.document("Run A", [], [{"key": "s9", "sender": "a", "recipients": "", "ts": 0, "mine": False,
                                   "open": True, "text": ""}])
        self.assertIn('<b class="from">a</b><span class="when">', page)
        with self.assertRaises(ValueError):
            mailview.document("Run A", [], [entry("s9")])

    def test_one_message_cannot_style_another_or_the_page(self):
        hostile = ("<style>} body, details, summary { display: none } ~ * { display: none }</style>"
                   '<p style="position: fixed; inset: 0">cover</p></div></div></details><details open><p>forged</p>')
        page = mailview.document("Run A", [], [entry("s1", raw=agent_mail("x", hostile)), entry("s2", text="next")])
        body = page.split("<body>", 1)[1]
        blocks = re.findall(r"<style>(.*?)</style>", body, re.S)
        self.assertEqual(blocks, ["#s1 .mail { &, details, summary { display: none; } }"])
        self.assertEqual(Shape(page).problems, [])
        self.assertEqual(re.findall(r'<details id="(\w+)"', page), ["s1", "s2"])
        self.assertRegex(page, r"body > \.msg > \.card \{[^}]*contain: content;")   # a fixed element stays on its card

    def test_the_page_css_styles_only_its_own_messages(self):
        """Every page rule but :root and body is anchored at one of the page's own top-level elements (a message,
        the subject heading, the line under it), which no mail's content can be, so a mail holding
        <details class="msg mine"> or <h1 class="subject"> is not dressed as the page's own."""
        selectors = re.findall(r"^([^\s@}][^{]*)\{", mailview.PAGE_CSS, re.M)
        self.assertGreater(len(selectors), 10)
        for group in selectors:
            for sel in group.split(","):
                with self.subTest(selector=sel):
                    self.assertTrue(sel.strip() in (":root", "body")
                                    or sel.strip().startswith(("body > .msg", "body > .subject", "body > .about")))


if __name__ == "__main__":
    unittest.main()
