"""The Mail tab's view of agent mail: a stored mail's MIME bytes as its text, HTML and other parts (parse,
attachments, part), an allowlist sanitizer for its HTML (safe_html), and a whole thread as one self-contained HTML
page (document), made to be shown with scripts off and every network load blocked. Stdlib only.

What safe_html lets through: the tags in TAGS with the attributes in ATTRS (and TAG_ATTRS on their tags); links
to http, https and mailto only, each with rel="noreferrer noopener"; images only as data: URIs of the types in
IMAGE_TYPES, made from the mail's own cid: parts or given as such; for each widget the message kept
(agentmail/widgets.py), the div its HTML marks as that widget's place (WIDGET_MARKER), as class="widget" and
data-widget="SCOPE-N", and no other data- attribute; CSS that can load nothing (_css_safe), with
<style> rules nested in the message's own scope so they cannot reach the page or another message. document puts
each message body on a card with paint containment, so not even a fixed-position element can cover the page, and
anchors its own CSS at the page's own top-level elements (its messages, the subject heading and the line under it),
so a mail cannot dress its content up as part of the page."""
import base64
import email
import email.policy
import html as htmllib
import mimetypes
import re
import time
import urllib.parse
from html.parser import HTMLParser
from typing import NamedTuple, Optional

from .compose import glance, html_to_text, text_to_html


class Part(NamedTuple):
    """One MIME leaf that is not the mail's main text/plain or text/html body."""
    index: int            # 1-based, in walk order (_leaves); the same bytes always give the same numbers
    name: str             # the filename's last path component, else part-<index>.<ext>
    type: str             # content type, lower case
    data: bytes
    cid: Optional[str]    # Content-ID without <>, or None
    inline: bool          # an image with a Content-ID (shown in the HTML), so not listed as an attachment


class Parsed(NamedTuple):
    """A stored mail, parsed (parse)."""
    headers: email.message.EmailMessage   # the parsed message (policy=email.policy.default), for headers
    text: str             # the text/plain body; if none, compose.html_to_text of the HTML body; "" if neither
    html: Optional[str]   # the text/html body, or None
    parts: list           # [Part]


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")   # C0 controls but tab, newline, form feed, return; and DEL


def _leaves(part):
    """The MIME leaves under part, depth first: every part that is not multipart/*. An attached mail (message/*)
    is one leaf; its own parts are not walked."""
    if part.get_content_maintype() == "multipart" and part.is_multipart():
        for sub in part.iter_parts():
            yield from _leaves(sub)
    else:
        yield part


def _name(leaf, index, ctype):
    """A part's file name: the last path component of its filename, without control characters; else
    part-INDEX.EXT, EXT guessed from the content type (bin when there is none)."""
    name = re.split(r"[\\/]", _CONTROL.sub("", leaf.get_filename() or ""))[-1].strip()
    if name in ("", ".", ".."):
        name = "part-%d%s" % (index, mimetypes.guess_extension(ctype) or ".bin")
    return name


def _data(leaf):
    """A leaf's content, transfer-decoded. email parses an attached mail (message/*) into a message even when it
    was sent base64-encoded, as compose sends a .eml file, so that leaf's content is the message re-serialized,
    then base64-decoded if it was sent so."""
    if not leaf.is_multipart():
        return leaf.get_payload(decode=True)
    body = b"".join(m.as_bytes() for m in leaf.get_payload())
    return base64.b64decode(body) if leaf.get("Content-Transfer-Encoding", "").lower() == "base64" else body


def parse(raw):
    """The mail in raw (its MIME bytes) as a Parsed: the main text and HTML bodies (email's get_body), and every
    other leaf as a Part, numbered from 1 in walk order."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    plain, rich = msg.get_body(("plain",)), msg.get_body(("html",))
    parts = []
    for leaf in _leaves(msg):
        if leaf is plain or leaf is rich:
            continue
        index, ctype = len(parts) + 1, leaf.get_content_type()
        cid = str(leaf["Content-ID"] or "").strip().removeprefix("<").removesuffix(">") or None
        parts.append(Part(index, _name(leaf, index, ctype), ctype, _data(leaf), cid,
                          leaf.get_content_maintype() == "image" and cid is not None))
    body = rich.get_content() if rich is not None else None
    text = plain.get_content() if plain is not None else html_to_text(body) if body else ""
    return Parsed(msg, text, body, parts)


def attachments(parsed):
    """The parts of a Parsed that are not inline images, as [{"index", "name", "type", "size"}]."""
    return [{"index": p.index, "name": p.name, "type": p.type, "size": len(p.data)} for p in parsed.parts
            if not p.inline]


def part(raw, index):
    """Part number index of the mail in raw (parse). Raises KeyError for a number that is not a part."""
    return {p.index: p for p in parse(raw).parts}[index]


# ---- the sanitizer ----

SCOPE_RE = re.compile(r"[a-z][0-9]+")   # a message key ("s123", "i45"): safe_html's scope and document's ids
TAGS = frozenset("""p br div span a b strong i em u s strike del ins mark small big sub sup code pre kbd samp var tt
    blockquote q cite abbr ul ol li dl dt dd table thead tbody tfoot tr th td caption colgroup col h1 h2 h3 h4 h5
    h6 hr img font center figure figcaption details summary section article header footer main""".split())
VOID = frozenset(("br", "hr", "img", "col"))
# Dropped with all their content; style's CSS is kept apart (_Sanitizer.css), then sanitized and scoped.
DROP = frozenset("""script style iframe frameset object applet svg math template noscript noembed noframes title
    button select textarea option canvas audio video picture""".split())
DROP_VOID = frozenset("input embed source track link meta base frame param keygen".split())   # no content to drop
ATTRS = frozenset("""style class title dir lang align valign width height bgcolor color border cellpadding
    cellspacing colspan rowspan start reversed face alt""".split())
TAG_ATTRS = {"type": ("ol", "ul", "li"), "size": ("font",), "href": ("a",), "src": ("img",)}
WIDGET_MARKER = "data-agentmail-widget"   # where a mail's HTML puts widget N's fallback (agentmail/widgets.py)
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
_DATA_IMAGE = re.compile(r"data:(%s)[;,]" % "|".join(map(re.escape, IMAGE_TYPES)), re.I)
_URL_SPACE = re.compile(r"[\x00-\x20\x7f]")      # spaces and control characters, removed from an image source

_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.S)
_CSS_BAD = re.compile(r"[{};<\\]|/\*")           # escapes, and anything that could end a block, rule or <style>
# Anything that loads a resource or runs code, matched with whitespace removed and lower-cased.
_CSS_LOADS = re.compile(r"url\(|image\(|image-set\(|cross-fade\(|element\(|src\(|attr\(|paint\(|expression\(|"
                        r"@import|javascript:|vbscript:|behavior|-moz-binding")
_CSS_STRING = re.compile(r"\"[^\"\n]*\"|'[^'\n]*'")
_CSS_DECLARATION = re.compile(r"\s*-{0,2}[a-zA-Z_][\w-]*\s*:", re.S)
_CSS_GROUP = re.compile(r"@(media|supports)\b", re.I)
CSS_DEPTH = 8                                     # blocks nested deeper than this are dropped


def _css_tree(css):
    """css (a style sheet or a style attribute) as items: a declaration or at-rule statement (str), or a block
    (prelude, [items]). Comments and control characters go first; a brace or semicolon inside a quoted string ends
    nothing; an unmatched } is ignored and blocks left open end with the text."""
    css = _CSS_COMMENT.sub(" ", _CONTROL.sub("", css))
    top, buf, quote = [], [], None
    stack = [top]

    def flush():
        piece = "".join(buf).strip()
        buf.clear()
        if piece:
            stack[-1].append(piece)

    for ch in css:
        if quote:
            quote = None if ch in (quote, "\n") else quote
            buf.append(ch)
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == "{":
            block = ("".join(buf).strip(), [])
            buf.clear()
            stack[-1].append(block)
            stack.append(block[1])
        elif ch == "}":
            flush()
            if len(stack) > 1:
                stack.pop()
        elif ch == ";":
            flush()
        else:
            buf.append(ch)
    flush()
    return top


def _css_safe(piece):
    """Whether a selector, at-rule prelude or declaration can go out as it is: no backslash escape, brace,
    semicolon, < or comment, every quote closing on its line, and nothing that loads or runs (_CSS_LOADS)."""
    return not (_CSS_BAD.search(piece) or _CSS_LOADS.search(re.sub(r"\s+", "", piece).lower())
                or re.search("[\"']", _CSS_STRING.sub("", piece)))


def _declarations(items):
    """The safe declarations among items (_css_tree), each ended with ";"; blocks and at-rules are dropped."""
    return " ".join(d + ";" for d in items if isinstance(d, str) and _CSS_DECLARATION.match(d) and _css_safe(d))


def _selector(prelude):
    """A style rule's selector list as it may be nested in a message's scope, each selector made relative to it:
    body, html and :root become & (the message's own element). None when a selector is empty, holds &, or starts
    with a combinator, any of which could match outside the message."""
    out = []
    for sel in prelude.split(","):
        sel = sel.strip()
        if not sel or "&" in sel or sel[0] in ">+~|":
            return None
        out.append("&" if sel.lower() in ("body", "html", ":root") else sel)
    return ", ".join(out)


def _rules(items, in_rule, depth):
    """Style sheet items (_css_tree) as sanitized CSS: style rules with a safe selector (_selector), @media and
    @supports groups, and declarations only inside a style rule (in_rule); every other at-rule, a block left
    empty, and anything nested deeper than CSS_DEPTH are dropped."""
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(_declarations([item]) if in_rule else "")
            continue
        prelude, inner = item
        if depth >= CSS_DEPTH or not _css_safe(prelude):
            continue
        selector = None if prelude.startswith("@") else _selector(prelude)
        if _CSS_GROUP.match(prelude):
            head, body = prelude, _rules(inner, in_rule, depth + 1)
        elif selector:
            head, body = selector, _rules(inner, True, depth + 1)
        else:
            continue
        out.append("%s { %s }" % (head, body) if body else "")
    return " ".join(o for o in out if o)


def _link(href):
    """href as a link may carry it, or None: http, https or mailto only, read as a browser reads it (the ends
    stripped of spaces and control characters, tabs and newlines removed), so " java\\tscript:" is javascript:."""
    url = _CONTROL.sub("", re.sub(r"[\t\n\r]", "", href)).strip(" \f")
    return url if url.lower().startswith(("http:", "https:", "mailto:")) else None


class _Sanitizer(HTMLParser):
    """safe_html's parser: out collects the fragment, css the text of each <style> block."""

    def __init__(self, images, scope, widgets):
        super().__init__(convert_charrefs=True)
        self.images = images
        self.scope, self.widgets, self.placed = scope, widgets, set()
        self.out, self.css = [], []
        self.open = []       # allowed tags open in the output, outermost first
        self.dropping = []   # DROP tags open in the input; nothing inside them is kept

    def handle_starttag(self, tag, attrs):
        if tag in DROP:
            if tag == "style" and not self.dropping:
                self.css.append("")
            self.dropping.append(tag)
        elif self.dropping or tag in DROP_VOID:
            return
        elif tag in TAGS or tag == "body":     # the mail's body becomes a div, keeping its style
            self.start("div" if tag == "body" else tag, attrs)
        # any other tag (html, head, form, an unknown one) is dropped and its text kept

    def start(self, tag, attrs):
        """Write an allowed start tag with the attributes it may carry, each attribute's first value as a browser
        reads it. An img with no source it may show (image_uri) is written as a short text instead."""
        kept, seen = [], set()
        for name, value in attrs:
            if name in seen:
                continue
            seen.add(name)
            value = _CONTROL.sub("", value or "")
            if tag not in TAG_ATTRS.get(name, ()) and name not in ATTRS:
                continue
            if name == "style":
                value = _declarations(_css_tree(value))
            elif name == "href":
                value = _link(value)
            elif name == "src":
                value = self.image_uri(value)
            if value is not None and (value or name != "style"):
                kept.append((name, value))
        if tag == "div":
            kept = self.widget(next((v for k, v in attrs if k == WIDGET_MARKER), None), kept)
        if tag == "img" and "src" not in dict(kept):
            alt = dict(kept).get("alt")
            self.out.append(htmllib.escape("[image not shown%s]" % (": " + alt if alt else ""), quote=False))
            return
        if tag == "a":
            kept.append(("rel", "noreferrer noopener"))
        self.out.append("<%s%s>" % (tag, "".join(' %s="%s"' % (n, htmllib.escape(v)) for n, v in kept)))
        if tag not in VOID:
            self.open.append(tag)

    def widget(self, marker, kept):
        """kept, the attributes a div keeps, for a div with this marker (WIDGET_MARKER's value, or None): the first
        div marked N, for N below self.widgets, becomes the place of widget SCOPE-N (class "widget", data-widget);
        any other marker is dropped like any attribute not allowed."""
        n = int(marker) if marker and marker.isdecimal() and len(marker) < 3 else None
        if n is None or n >= self.widgets or n in self.placed:
            return kept
        self.placed.add(n)
        return [("class", "widget"), ("data-widget", "%s-%d" % (self.scope, n))] + [a for a in kept if a[0] != "class"]

    def image_uri(self, src):
        """The data: URI an img may show for src (without its spaces and control characters), or None: a cid:
        part of this mail, or a data: URI as given, either of a type in IMAGE_TYPES."""
        src = _URL_SPACE.sub("", src)
        if src[:4].lower() == "cid:":
            ctype, data = self.images.get(urllib.parse.unquote(src[4:]), (None, None))
            return "data:%s;base64,%s" % (ctype, base64.b64encode(data).decode()) if ctype in IMAGE_TYPES else None
        return src if _DATA_IMAGE.match(src) else None

    def handle_endtag(self, tag):
        if self.dropping:
            if tag in self.dropping:
                del self.dropping[len(self.dropping) - 1 - self.dropping[::-1].index(tag):]
            return
        tag = "div" if tag == "body" else tag
        if tag in self.open:                 # close it and every tag still open inside it
            i = len(self.open) - 1 - self.open[::-1].index(tag)
            self.out.extend("</%s>" % t for t in reversed(self.open[i:]))
            del self.open[i:]

    def handle_data(self, data):
        if self.dropping == ["style"]:
            self.css[-1] += data
        elif not self.dropping:
            self.out.append(htmllib.escape(_CONTROL.sub("", data), quote=False))


def safe_html(html, images, scope, widgets):
    """html (a mail's HTML) sanitized for the Mail tab, as a <div class="mail"> fragment: allowed tags and
    attributes only (the module's docstring), every tag closed. Images: {cid: (content type, bytes)}, the mail's
    inline parts, which its cid: images become as data: URIs; any other image source gives "[image not shown: ALT]".
    widgets: how many widgets the message kept (agentmail/widgets.py); the div the mail marks as widget N's place
    (WIDGET_MARKER) becomes <div class="widget" data-widget="SCOPE-N">, for N below that.
    Its <style> blocks' safe rules come first, nested under "#SCOPE .mail", so they style only this fragment once
    it sits inside the element whose id is scope (a message key, SCOPE_RE; else ValueError). Comments, doctypes
    and processing instructions are dropped; hostile input still gives safe output."""
    if not SCOPE_RE.fullmatch(scope):
        raise ValueError("scope %r is not a message key such as s123" % scope)
    p = _Sanitizer(images, scope, widgets)
    p.feed(html)
    p.close()
    body = "".join(p.out) + "".join("</%s>" % t for t in reversed(p.open))
    css = " ".join(c for c in (_rules(_css_tree(block), False, 0) for block in p.css) if c)
    return '<div class="mail">%s%s</div>' % ("<style>#%s .mail { %s }</style>" % (scope, css) if css else "", body)


# ---- the thread document ----

CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; "
       "form-action 'none'")
PAGE_CSS = """
:root { color-scheme: light dark; --bg: #f6f8fc; --fg: #1f1f1f; --muted: #5f6368; --line: #e0e3e7;
  --head: #ffffff; --mine: #e8f0fe; --current: #1a73e8; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #131314; --fg: #e3e3e3; --muted: #9aa0a6; --line: #3c4043; --head: #1e1f20; --mine: #1b2a41;
    --current: #8ab4f8; }
}
body { margin: 0; padding: 12px; background: var(--bg); color: var(--fg);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
body > .subject { max-width: 960px; margin: 4px auto 2px; padding: 0 4px; font-size: 20px; font-weight: 600;
  line-height: 1.3; overflow-wrap: break-word; }
body > .about { max-width: 960px; margin: 0 auto 12px; padding: 0 4px; color: var(--muted); font-size: 12.5px; }
body > .msg { max-width: 960px; margin: 0 auto 8px; background: var(--head); border: 1px solid var(--line);
  border-radius: 10px; }
body > .msg.mine { background: var(--mine); }
body > .msg.current { border-color: var(--current); box-shadow: 0 0 0 1px var(--current); }
body > .msg > summary { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 8px; padding: 10px 14px;
  cursor: pointer; list-style: none; }
body > .msg > summary::-webkit-details-marker { display: none; }
body > .msg > summary > .from { font-weight: 600; }
body > .msg > summary > .to { flex: 1 1 auto; min-width: 0; color: var(--muted); font-size: 12px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
body > .msg > summary > .when { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; }
body > .msg > summary > .snip { flex: 1 0 100%; color: var(--muted); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
body > .msg[open] > summary > .snip { display: none; }
body > .msg > summary > .status { flex: 1 0 100%; color: var(--muted); font-size: 12px; font-style: italic; }
body > .msg > .card { margin: 0 10px 10px; padding: 12px 14px; border-radius: 8px; background: #ffffff;
  color: #1f2328; color-scheme: light; contain: content; overflow-x: auto; overflow-wrap: break-word; }
body > .msg.mine > .card { background: #f3f7fe; }
body > .msg > .card > .mail img { max-width: 100%; height: auto; }
body > .msg > .card > .text { white-space: pre-wrap; }
body > .msg > .card > .note { margin: 0 0 8px; color: #5f6368; font-size: 12px; }
body > .msg > .card > .atts { margin-top: 12px; padding-top: 8px; border-top: 1px solid #e0e3e7; font-size: 13px; }
body > .msg > .card > .atts a, body > .msg > .card > .note a { color: #0b57d0; }
"""
HEAD = ('<!DOCTYPE html>\n<html><head><meta charset="utf-8">\n'
        '<meta http-equiv="Content-Security-Policy" content="%s">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<style>%s</style></head>\n<body>\n' % (CSP, PAGE_CSS))
UNSTORED = "Sent before the Mail tab kept copies of agent mail, so only its text is here."
SUMMARY_CHARS = 120   # a closed message's line (snippet)
NO_SUBJECT = "(no subject)"


def _when(ts):
    """ts in this machine's time zone, as "Mon 16 Nov, 20:31", with the year ("Mon 16 Nov 2025, 20:31") when it
    is not this year."""
    t = time.localtime(ts)
    year = " %d" % t.tm_year if t.tm_year != time.localtime().tm_year else ""
    return "%s %d %s%s, %s" % (time.strftime("%a", t), t.tm_mday, time.strftime("%b", t), year,
                               time.strftime("%H:%M", t))


def snippet(text, chars):
    """text's first chars characters, whitespace folded, with … when it goes on: a one-line preview (a closed
    message's line here, a thread's in `mailbox list`)."""
    s = " ".join(text.split())
    return s if len(s) <= chars else s[:chars - 1].rstrip() + "…"


def size(n):
    """n bytes as "512 B", "14 KB" or "2.3 MB"."""
    return "%d B" % n if n < 1024 else "%.0f KB" % (n / 1024) if n < 1024 ** 2 else "%.1f MB" % (n / 1024 ** 2)


def _content(entry):
    """(the card's HTML, the text for the snippet) of one document entry."""
    key = entry["key"]
    if entry.get("raw") is not None:
        parsed = parse(entry["raw"])
        images = {p.cid: (p.type, p.data) for p in parsed.parts if p.inline}
        atts = "".join('<div><a class="att" href="mailbox-part:%s/%d">%s</a> (%s)</div>'
                       % (htmllib.escape(key), a["index"], htmllib.escape(a["name"]), size(a["size"]))
                       for a in attachments(parsed))
        html = parsed.html or text_to_html(parsed.text)
        return (safe_html(html, images, key, entry["widgets"]) + ('<div class="atts">%s</div>' % atts if atts else "")), glance(html)
    if entry.get("text") is None:
        raise ValueError("message %s has neither raw nor text" % key)
    note, url = "", entry.get("gmail_url")
    if url:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("message %s: gmail_url %r is not an http or https link" % (key, url))
        note = '<p class="note">%s <a href="%s" rel="noreferrer noopener">Open in Gmail</a></p>' % (
            UNSTORED, htmllib.escape(url))
    return note + '<div class="text">%s</div>' % htmllib.escape(entry["text"], quote=False), entry["text"]


def _message(entry):
    """One entry of document as a <details> element."""
    key = entry["key"]
    if not SCOPE_RE.fullmatch(key):
        raise ValueError("message key %r is not like s123 or i45" % key)
    card, text = _content(entry)
    to = '<span class="to">to %s</span>' % htmllib.escape(entry["recipients"]) if entry["recipients"] else ""
    status = '<span class="status">%s</span>' % htmllib.escape(entry["status"]) if entry.get("status") else ""
    return ('<details id="%s" class="msg%s"%s><summary><b class="from">%s</b>%s<span class="when">%s</span>'
            '<span class="snip">%s</span>%s</summary>\n<div class="card">%s</div></details>\n'
            % (htmllib.escape(key), " mine" if entry["mine"] else "", " open" if entry["open"] else "",
               htmllib.escape(entry["sender"]), to, htmllib.escape(_when(entry["ts"])),
               htmllib.escape(snippet(text, SUMMARY_CHARS)), status, card))


def about(count, agents):
    """The line under a thread's subject: "3 mails · alpha (claude), orchestrator", the agents in the order given."""
    return " · ".join(p for p in ("%d mail%s" % (count, "" if count == 1 else "s"), ", ".join(agents)) if p)


def document(subject, agents, entries):
    """A thread as one self-contained HTML page: a strict CSP, the page's CSS (light or dark to match the system,
    each message body on a white card), the thread's subject as its heading (NO_SUBJECT when it is empty) over a
    quiet line (about: how many mails, and agents, the names of the agents who wrote in it), so that both scroll
    with the mail, and per entry a <details id=KEY class="msg"> (open when entry["open"]) whose summary holds the
    sender, "to RECIPIENTS", the time (_when), while closed a one-line snippet, and the status line if any. The
    page marks no message as the one being read: a reader that steps through the messages gives that one's
    element the class "current", which outlines it.

    An entry is a dict: key ("s123" or "i45"), sender, recipients, ts (epoch seconds), mine (the owner's own: tinted),
    open, widgets (how many widgets the message kept: safe_html marks their places), and the optional status (such
    as "delivered" or "held: ..."), raw, text and gmail_url (each may be left out or None). With raw, the mail's bytes: its HTML through safe_html (or its text rendered by
    compose.text_to_html when it has no HTML) and its attachments as mailbox-part:KEY/INDEX links. Without raw,
    text shown as it is, and, with gmail_url, a note that the mail was sent before the tab kept copies and an
    "Open in Gmail" link. Raises ValueError on a key unlike SCOPE_RE, an entry with neither raw nor text, or a
    gmail_url that is not http or https."""
    head = '<h1 class="subject">%s</h1>\n<p class="about">%s</p>\n' % (
        htmllib.escape(subject or NO_SUBJECT, quote=False), htmllib.escape(about(len(entries), agents), quote=False))
    return HEAD + head + "".join(_message(e) for e in entries) + "</body></html>\n"
