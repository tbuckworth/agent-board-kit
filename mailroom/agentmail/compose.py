"""Plain text to a readable HTML alternative for outgoing mail. Stdlib only.

Agents write loosely markdown-ish plain text: paragraphs separated by blank lines, soft line
breaks inside a paragraph, `-`/`*` bullets, `1.` numbered items, the odd ```fenced``` block and
**bold**/`code` spans. Gmail's plain-text pane reflows all of that into a mess, so every mail
carries an HTML part built from the text unless the caller supplied its own with --html.
"""
import html
import re

STYLE = ("font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 15px; "
         "line-height: 1.5; color: #1f2328; max-width: 42em; margin: 0 auto; padding: 16px;")
PRE_STYLE = ("font-family: SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; background: #f6f8fa; "
             "padding: 10px 12px; border-radius: 6px; overflow-x: auto; white-space: pre-wrap;")
CODE_STYLE = "font-family: SFMono-Regular, Menlo, Consolas, monospace; font-size: 90%; background: #f6f8fa; padding: 1px 4px; border-radius: 4px;"

_BULLET = re.compile(r"^\s{0,3}[-*•]\s+(.*\S.*)$")
_NUMBER = re.compile(r"^\s{0,3}(\d{1,3})[.)]\s+(.*\S.*)$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S.*)$")
_FENCE = re.compile(r"^\s{0,3}```")
_CONTINUATION = re.compile(r"^\s{2,}(\S.*)$")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_CODE = re.compile(r"`([^`\n]+)`")


def inline(text):
    """Escape one line of text, then mark up `code` spans and **bold** (bold may wrap code, never enter it)."""
    codes = []

    def stash(m):
        codes.append('<code style="%s">%s</code>' % (CODE_STYLE, html.escape(m.group(1), quote=False)))
        return "\x00%d\x00" % (len(codes) - 1)

    out = _CODE.sub(stash, text)
    out = _BOLD.sub(r"<b>\1</b>", html.escape(out, quote=False))
    return re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], out)


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

    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if kind == "pre":
            if _FENCE.match(line):
                close()
            else:
                buf.append(raw.rstrip("\n"))
            continue
        if not line.strip():
            if lists:
                gap = True
            else:
                close()
            continue
        after_gap, gap = gap, False
        if _FENCE.match(line):
            close()
            kind = "pre"
            continue
        m = _HEADING.match(line)
        if m:
            close()
            level = min(len(m.group(1)) + 2, 6)          # '#' -> h3: a mail is not a web page
            parts.append("<h%d>%s</h%d>" % (level, inline(m.group(2)), level))
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
