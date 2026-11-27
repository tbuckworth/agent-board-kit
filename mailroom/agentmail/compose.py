"""Plain text to a readable HTML alternative for outgoing mail, and back (html_to_text, through flatten, which
the pre-send review's render shares). Stdlib only.

Agents write loosely markdown-ish plain text: paragraphs separated by blank lines, soft line
breaks inside a paragraph, `-`/`*` bullets, `1.` numbered items, the odd ```fenced``` block,
**bold**/*italic*/`code` spans, [links](https://...), bare https://... addresses, GitHub-style
pipe tables and, on a line of its own, ![alt](cid:NAME) for the mail's --image NAME. Gmail's
plain-text pane reflows all of that into a mess, so every mail carries an HTML part built from
the text unless the caller supplied its own with --html.
"""
import html
import re
from html.parser import HTMLParser

# The mail's body: 42em of 15px is about 600 px across, which review.MIN_IMAGE_WIDTH is twice
STYLE = ("font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 15px; "
         "line-height: 1.5; color: #1f2328; max-width: 42em; margin: 0 auto; padding: 16px;")
PRE_STYLE = ("font-family: SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; background: #f6f8fa; "
             "padding: 10px 12px; border-radius: 6px; overflow-x: auto; white-space: pre-wrap;")
CODE_STYLE = "font-family: SFMono-Regular, Menlo, Consolas, monospace; font-size: 90%; background: #f6f8fa; padding: 1px 4px; border-radius: 4px;"

_BULLET = re.compile(r"^\s{0,3}[-*•]\s+(.*\S.*)$")
_NUMBER = re.compile(r"^\s{0,3}(\d{1,3})[.)]\s+(.*\S.*)$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S.*)$")
FENCE = re.compile(r"^\s{0,3}```(.*)$")   # a fence line, and its info string (```python, ```choices)
_CONTINUATION = re.compile(r"^\s{2,}(\S.*)$")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_BOTH = re.compile(r"\*\*\*(?=[^\s*])(.+?)(?<=[^\s*])\*\*\*")
# *italic*: opens before a word and closes after one, so 2*3*4, globs such as *.py and *.txt, and a
# lone * stay as typed; never across a tag, and only around a letter (a phone code such as *61* stays)
_ITALIC = re.compile(r"(?<![\w*])\*(?=[\w(\"'\x00])([^*\n<>]+?)(?<=[\w.!?)\"'\x00])\*(?![\w*])")
_CODE = re.compile(r"`([^`\n]+)`")
# An image line: `![alt](cid:NAME)` on a line of its own, which shows the mail's --image NAME there (placed_images)
_IMAGE = re.compile(r"^\s{0,3}!\[([^\]\n]*)\]\(cid:([^\s()]+)\)\s*$")
_ESCAPE = re.compile(r"\\([*_\[\]|`])")
# A URL never holds a held span (\x00): a `code` span inside one leaves the text unlinked, not a broken href
_LINK = re.compile(r"\[([^\]\n]+)\]\(((?:https?://|mailto:)[^\s()\x00]+(?:\([^\s()\x00]*\)[^\s()\x00]*)*)\)")
# A bare http(s) address, not inside a word: it ends at a space, < or >, a quote, a backtick or a held span;
# bare_url_end then leaves out what ends the sentence around it
_BARE_URL = re.compile(r"(?<!\w)https?://[^\s<>\"`\x00]+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?'*"                # punctuation after an address, and the * of **bold** or *italic* around it
_URL_CLOSERS = {")": "(", "]": "["}
_TABLE_SEP = re.compile(r"^:?-+:?$")
TABLE_STYLE = "border-collapse: collapse; font-size: 13px; line-height: 1.35;"
TABLE_WRAP_STYLE = "overflow-x: auto; margin: 8px 0;"     # a wide table scrolls on a phone instead of clipping
CELL_STYLE = "border: 1px solid #d0d7de; padding: 4px 6px; vertical-align: top;"
HEAD_STYLE = CELL_STYLE + " background: #f6f8fa;"
LINK_STYLE = "color: #0969da;"


def inline(text):
    """Escape one line of text, then mark up `code` spans, [links](https://...), bare http(s) addresses (linked to
    themselves), **bold** and *italic* (bold and italic may wrap code or a link, never enter code). A backslash
    before * _ [ ] | or ` keeps that character as typed; any other backslash is left alone (paths, regexes)."""
    held = []

    def hold(markup):
        held.append(markup)
        return "\x00%d\x00" % (len(held) - 1)

    def bare(m):
        url = m.group(0)
        end = bare_url_end(url)
        # left as typed: an address that runs on into a held span (a `code` span), never linked to its first half,
        # and one with nothing left after its :// once its punctuation is left out
        if m.string.startswith("\x00", m.end()) or end <= url.index("//") + 2:
            return url
        return hold(anchor(url[:end], html.escape(url[:end], quote=False))) + url[end:]

    out = _CODE.sub(lambda m: hold('<code style="%s">%s</code>' % (CODE_STYLE, html.escape(m.group(1), quote=False))),
                    text)
    out = _ESCAPE.sub(lambda m: hold(html.escape(m.group(1), quote=False)), out)   # \* \_ \[ ... stay as typed
    out = _LINK.sub(lambda m: hold(anchor(m.group(2), emphasis(html.escape(m.group(1), quote=False)))), out)
    out = _BARE_URL.sub(bare, out)
    out = emphasis(html.escape(out, quote=False))
    while "\x00" in out:
        out = re.sub("\x00(\\d+)\x00", lambda m: held[int(m.group(1))], out)
    return out


def anchor(href, words):
    """A rendered mail's link to href (escaped here) around words (HTML already)."""
    return '<a href="%s" style="%s">%s</a>' % (html.escape(href, quote=True), LINK_STYLE, words)


def bare_url_end(url):
    """Where the address ends in url, a _BARE_URL match, as GitHub's markdown ends one: punctuation at its end
    (_URL_TRAILING) and a closing ) or ] that has no partner inside it are left out, so "(see https://x.y/z)."
    links https://x.y/z and https://en.wikipedia.org/wiki/Python_(programming_language) keeps its brackets."""
    unpaired = {c: url.count(c) - url.count(o) for c, o in _URL_CLOSERS.items()}   # closers without a partner
    end = len(url)
    while end:
        c = url[end - 1]
        if c in _URL_CLOSERS and unpaired[c] > 0:
            unpaired[c] -= 1
        elif c not in _URL_TRAILING:
            break
        end -= 1
    return end


def emphasis(escaped):
    """***both***, **bold** then *italic* in already escaped text."""
    out = _BOTH.sub(r"<b><i>\1</i></b>", escaped)
    out = _BOLD.sub(r"<b>\1</b>", out)
    # around a letter, or a held code span or link (\x00), so *61* stays as typed but *`code`* is italic
    return _ITALIC.sub(lambda m: "<i>%s</i>" % m.group(1) if re.search(r"[^\W\d_]|\x00", m.group(1)) else m.group(0),
                       out)


def table_cells(line):
    """The cells of a pipe-table row, or None if the line has no pipe. Pipes inside `code` or escaped as \\| do
    not split; a leading and a trailing pipe are optional."""
    s = line.strip()
    if "|" not in s:
        return None
    cells, cur, in_code, i = [], [], False, 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if c == "`":
            in_code = not in_code
        if c == "|" and not in_code:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    cells.append("".join(cur).strip())
    if s.startswith("|"):
        cells = cells[1:]
    if s.endswith("|") and not s.endswith("\\|") and cells:
        cells = cells[:-1]
    return cells


def table_at(lines, i):
    """(html, lines used) for a pipe table starting at lines[i], or None. A table is two or more rows in a row.
    With a GitHub separator row (|---|:--:|) second, the first row is the header and the colons set alignment.
    Without one, every row must have the same number of cells and either three or more cells or a leading pipe
    (so two lines of `a | b` prose or shell stay text), and the first row is still taken as the header."""
    rows = []
    while i + len(rows) < len(lines):
        line = lines[i + len(rows)]
        if not line.lstrip().startswith("|") and (_BULLET.match(line) or _NUMBER.match(line) or _HEADING.match(line)):
            break                    # a list item or heading that happens to hold pipes
        cells = table_cells(line)
        if cells is None:
            break
        rows.append(cells)
    if len(rows) < 2 or len(rows[0]) < 2:
        return None
    # a separator row has one dash cell per header cell, as GitHub requires (a lone "|" line is no separator)
    sep = len(rows[1]) == len(rows[0]) and all(_TABLE_SEP.match(c.replace(" ", "")) for c in rows[1])
    if sep:
        head, body = rows[0], rows[2:]
        align = []
        for c in rows[1]:
            c = c.replace(" ", "")
            align.append("center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "left")
    else:
        width = len(rows[0])
        keep = next((k for k, r in enumerate(rows) if len(r) != width), len(rows))
        rows = rows[:keep]
        if len(rows) < 2:
            return None
        if width < 3 and not all(lines[i + k].lstrip().startswith("|") for k in range(len(rows))):
            return None
        head, body, align = rows[0], rows[1:], ["left"] * width
    width = len(head)
    align = (align + ["left"] * width)[:width]

    def row(cells, tag, style):
        cells = (cells + [""] * width)[:width]
        return "<tr>%s</tr>" % "".join('<%s style="%s text-align: %s;">%s</%s>' % (tag, style, a, inline(c), tag)
                                       for c, a in zip(cells, align))

    out = ['<div style="%s"><table style="%s">' % (TABLE_WRAP_STYLE, TABLE_STYLE), row(head, "th", HEAD_STYLE)]
    out += [row(r, "td", CELL_STYLE) for r in body]
    out.append("</table></div>")
    return "\n".join(out), len(rows)


# A paragraph that is the mailroom's word, not the sender's, set small and grey: takes its HTML.
# agent-board's agentboard/mailforward.py strips this from forwarded mail: change both together.
NOTE = '<p style="color:#6e7781;font-size:13px">%s</p>\n'


def text_lines(text):
    """text's lines, whichever line endings it has (None: one empty line)."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def image_html(name, alt):
    """The mail's --image NAME (cid:NAME) as a paragraph of its own, as wide as the mail at most, described by alt."""
    return '<p><img src="cid:%s" alt="%s" style="max-width:100%%;height:auto"></p>' % (html.escape(name, quote=True),
                                                                                        html.escape(alt, quote=True))


def placed_images(text):
    """The --image NAMEs that text's image lines (_IMAGE) place, in order: those outside fenced blocks, which
    text_to_html shows as images."""
    names, fenced = [], False
    for line in text_lines(text):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            m = _IMAGE.match(line)
            if m:
                names.append(m.group(2))
    return names


def text_to_html(text):
    """Return a complete HTML document rendering `text`. The source is escaped first.

    Lists stay open across blank lines when the next line is another item (or an indented
    paragraph of the last one), an ordered list that opens at N carries start="N", and an item
    indented two or more columns past its list's items nests inside the previous item."""
    parts = []
    kind = None      # open block other than a list: "p", "pre" or None
    buf = []         # lines of the open paragraph / lines of the fence
    lists = []       # open lists, outermost first: {"tag", "indent", "start", "items"}; an item is [html, ends_in_list]
    gap = False      # blank line(s) seen inside an open list, not yet resolved

    def render(lst):
        start = ' start="%d"' % lst["start"] if lst["tag"] == "ol" and lst["start"] != 1 else ""
        return "<%s%s>\n%s\n</%s>" % (lst["tag"], start, "\n".join("<li>%s</li>" % i[0] for i in lst["items"]), lst["tag"])

    def pop_list():
        """Close the innermost open list into its parent item, or into the page if it is outermost."""
        lst = lists.pop()
        if lists:
            item = lists[-1]["items"][-1]
            item[0] += "\n" + render(lst)
            item[1] = True
        else:
            parts.append(render(lst))

    def close():
        nonlocal kind, buf, gap
        while lists:
            pop_list()
        if kind == "p":
            parts.append("<p>%s</p>" % "<br>\n".join(buf))
        elif kind == "pre":
            parts.append('<pre style="%s">%s</pre>' % (PRE_STYLE, html.escape("\n".join(buf), quote=False)))
        kind, buf, gap = None, [], False

    def add_item(tag, indent, number, content):
        while lists and indent < lists[-1]["indent"] + 2:      # not deeper than the innermost list
            top = lists[-1]
            if len(lists) > 1 and indent <= top["indent"] - 2:
                pop_list()                   # shallower: this nested list is finished
            elif top["tag"] == tag:
                top["items"].append([content, False])
                return
            elif len(lists) > 1:
                pop_list()                   # the other kind beside a nested list: a sibling list
            else:
                close()                      # the other kind at the outer level: a new list
        if not lists:
            close()
        lists.append({"tag": tag, "indent": indent, "start": number, "items": [[content, False]]})

    def add_to_item(text, paragraph):
        item = lists[-1]["items"][-1]
        item[0] += ("\n" if item[1] else "<br>\n<br>" if paragraph else " ") + text
        item[1] = False

    lines = text_lines(text)
    skip = 0         # lines already rendered as part of a table
    for n, raw in enumerate(lines):
        if skip:
            skip -= 1
            continue
        line = raw.rstrip()
        if kind == "pre":
            if FENCE.match(line):
                close()
            else:
                buf.append(raw.rstrip("\n"))
            continue
        table = table_at(lines, n) if "|" in line else None
        if table:
            close()
            parts.append(table[0])
            skip = table[1] - 1
            continue
        if not line.strip():
            if lists:
                gap = True
            else:
                close()
            continue
        after_gap, gap = gap, False
        if FENCE.match(line):
            close()
            kind = "pre"
            continue
        m = _HEADING.match(line)
        if m:
            close()
            level = min(len(m.group(1)) + 2, 6)          # '#' -> h3: a mail is not a web page
            parts.append("<h%d>%s</h%d>" % (level, inline(m.group(2)), level))
            continue
        m = _IMAGE.match(line)
        if m:
            close()
            parts.append(image_html(m.group(2), m.group(1).strip() or m.group(2)))
            continue
        expanded = line.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip())
        m = _BULLET.match(line.lstrip())
        if m:
            add_item("ul", indent, 1, inline(m.group(1)))
            continue
        m = _NUMBER.match(line.lstrip())
        if m:
            add_item("ol", indent, int(m.group(1)), inline(m.group(2)))
            continue
        m = _CONTINUATION.match(expanded)
        if m and lists:
            if not after_gap:
                add_to_item(inline(m.group(1)), False)            # wrapped list item
                continue
            while len(lists) > 1 and indent <= lists[-1]["indent"]:
                pop_list()
            if indent > lists[-1]["indent"]:
                add_to_item(inline(m.group(1)), True)             # an indented paragraph of the last item
                continue
        if kind == "p":
            buf.append(inline(line.strip()))
        else:
            close()
            kind, buf = "p", [inline(line.strip())]
    close()
    body = "\n".join(parts)
    return ('<!DOCTYPE html>\n<html><head><meta charset="utf-8"></head>\n'
            '<body style="%s">\n%s\n</body></html>\n' % (STYLE, body))


def cut_items(text):
    """(last line of the item, next line) for each list item text_to_html cuts short: the line straight after the
    item or its indented continuation starts flush left, so the list ends there and that line opens a paragraph."""
    out, last, fence = [], None, False      # last: the open item's latest line, until a blank line
    for raw in text_lines(text):
        line = raw.rstrip()
        if fence:
            fence = not FENCE.match(line)
            continue
        if not line.strip() or FENCE.match(line) or _HEADING.match(line):
            fence, last = bool(FENCE.match(line)), None
            continue
        if _BULLET.match(line.lstrip()) or _NUMBER.match(line.lstrip()) or (
                last and _CONTINUATION.match(line.expandtabs(4))):
            last = line.strip()
            continue
        if last:
            out.append((last, line.strip()))
        last = None
    return out


MARKED = {"table": "TABLE", "pre": "CODE"}   # the blocks flatten's mark_blocks puts between [NAME] and [/NAME]


class _Flatten(HTMLParser):
    """HTML to plain text (flatten says what each option does)."""

    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol",
             "blockquote", "pre", "hr", "section"}
    SKIP = ("style", "script", "head", "title")

    def __init__(self, image, link, mark_blocks, keep_pre, rule):
        super().__init__(convert_charrefs=True)
        self.image, self.link, self.mark_blocks, self.keep_pre, self.rule = image, link, mark_blocks, keep_pre, rule
        self.out, self.skip, self.href, self.pre = [], 0, [], 0
        # Joined cells: no " | " before the first cell of a row (first_cell). Marked tables: " | " before every cell
        # once the first row has begun (rows, never reset), the form the reviewer has always read.
        self.first_cell, self.rows = False, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag in self.BLOCK:
            self.out.append("\n")
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self.out.append("#" * int(tag[1]) + " ")
            elif tag == "li":
                self.out.append("- ")
            elif tag == "hr":
                self.out.append(self.rule)
            elif tag in MARKED and self.mark_blocks:
                self.out.append("[%s]\n" % MARKED[tag])
            elif tag == "tr":
                self.first_cell = self.rows = True
        if tag == "pre" and self.keep_pre:
            self.pre += 1
        elif tag in ("td", "th"):
            if self.rows if self.mark_blocks else not self.first_cell:
                self.out.append(" | ")
            self.first_cell = False
        elif tag == "a":
            self.href.append((dict(attrs).get("href") or "", len(self.out)))
        elif tag == "img":
            self.out.append(self.image(dict(attrs)))

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
        elif tag == "a" and self.href:
            href, start = self.href.pop()
            self.out.append(self.link(href, "".join(self.out[start:]).strip()))
        if tag in self.BLOCK:
            self.out.append("\n[/%s]\n" % MARKED[tag] if tag in MARKED and self.mark_blocks else "\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def flatten(body, image, link, mark_blocks, keep_pre, rule):
    """HTML to plain text, stripped: block tags break lines, a heading keeps its #s and a list item "- ", and style,
    script, head and title are dropped. html_to_text (a mail's plain-text part), glance (its words for a snippet) and
    review.render (what the pre-send reviewer reads) differ in the rest:
      image(attrs)  the text an <img> becomes, from its attributes (a dict)
      link(href, words)  the text after a link, from its target and its words as flattened ("" for none)
      mark_blocks   [TABLE] and [/TABLE] around each table, with " | " before every cell (else " | " between cells),
                    and [CODE] and [/CODE] around each <pre> (a fenced block)
      keep_pre      text in <pre> keeps its spacing; else spacing collapses as everywhere else
      rule          the text an <hr> becomes"""
    p = _Flatten(image, link, mark_blocks, keep_pre, rule)
    p.feed(body)
    p.close()
    t = "".join(p.out)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def html_to_text(body):
    """The plain-text part for a mail sent with only its HTML (agent-mail send --html, no body): table cells joined
    with " | ", <pre> kept as written, <hr> a ---- line, a link followed by its http(s) target, and an image as
    [image: its alt text]."""
    return flatten(body or "", image=lambda a: "[image: %s]" % (a.get("alt") or a.get("title") or "picture"),
                   link=lambda href, words: " <%s>" % href if href.startswith(("http://", "https://")) else "",
                   mark_blocks=False, keep_pre=True, rule="----\n") + "\n"


def glance(body):
    """The words of HTML body as its reader sees them, for a one-line snippet: html_to_text without link targets,
    images or rules, and with <pre> spacing collapsed."""
    return flatten(body or "", image=lambda a: "", link=lambda href, words: "", mark_blocks=False, keep_pre=False,
                   rule="")
