"""Widgets in agent mail: clickable options (```choices) and interactive plots (```plot) that the owner's Mail tab draws.

An agent writes each as a fenced block in its mail's plain-text (markdown) body, holding one JSON object (the
README's "Widgets in agent mail" gives the format and its limits). prepare checks every block, makes its text plain
(plain: as the Mail tab shows it) and turns the mail into what Gmail is sent: each block replaced by a fallback
anyone can read (the options as a numbered list, the plot as its --image or a line naming it), the fallback's HTML
wrapped in <div data-agentmail-widget="N"> so that the Mail tab, which keeps the checked blocks (Mail.widgets, the
sent row's widgets), can draw the widget in its place (mailview.safe_html). Nothing invalid is sent or kept:
prepare raises MailError naming the block, the key and the rule broken.
"""
import copy
import html as htmllib
import json
import math
import re
import secrets
import unicodedata

from .compose import FENCE, image_html, text_lines, text_to_html
from . import mailview
from .mailview import WIDGET_MARKER
from .outbound import IMAGE_NAME_MAX, MailError

KINDS = ("choices", "plot")
# The limits below are also kept by the Mail tab's renderer, agent-board's mac/Sources/AgentBoard/widgets.js, which
# draws only what keeps them: change both together.
MAX_BLOCKS = 5
MAX_BLOCK_BYTES = 64 * 1024
PLOT_KINDS = ("line", "scatter", "bar", "filled")
MAX_SERIES = 10
MAX_POINTS = 5000
MAX_CATEGORIES = 50
MAX_CATEGORY_CHARS = 80
MAX_TEXTURES = 3          # a bar plot's textures, drawn solid, hatched and cross-hatched
MAX_SEGMENTS = 6          # a filled plot's segments: each bar's parts
MAX_ROWS = 50             # and its rows: its bars
# Text holds no control character: a line break in an option's label would put lines in the owner's reply that the
# reply box shows below its fold, where they go out as their words unseen.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
NO_CONTROL = "must not hold a control character (a line break, a tab or another)"
# Nor right-to-left script: agents write English, and beside it such a character (a Hebrew geresh passing for
# an apostrophe) can show the text around it in another order than it reads.
RTL_CLASSES = ("R", "AL", "AN")   # unicodedata.bidirectional: right-to-left letters, Arabic letters, Arabic digits
NO_RTL = "must not hold right-to-left script (Hebrew, Arabic and the like): it can show in another order than it reads"

# Text is kept as the Mail tab shows it (plain), so that an answer to a question, or a quote of one, carries nothing
# into the owner's reply that they did not see. It is made plain rather than refused: agents paste no-break spaces and
# emoji with joiners in good faith, and nothing that shows is lost. agent-board makes the text the owner answers or
# quotes plain on its side too (ReplyText.plain in mac/Sources/AgentBoard/Compose.swift): change both together.
BLANK = "\u2800"          # braille pattern blank: not white space, but shown as a blank


def plain(text):
    """text as the Mail tab shows it: without the characters mailview.IGNORABLE lists, BLANK as a space, each run of
    white space (str.isspace: no-break and the other wide or narrow spaces too) one space, and no space at either
    end."""
    return " ".join(mailview.without_ignorable(text).replace(BLANK, " ").split())


def _right_to_left(text):
    return any(unicodedata.bidirectional(c) in RTL_CLASSES for c in text)


class _Bad(Exception):
    """One broken rule: where (a key path such as "series[1].y") and what."""

    def __init__(self, where, rule):
        super().__init__("%s: %s" % (where, rule))


def blocks(text):
    """The widget blocks in text as [(kind, JSON text, first line, last line)], fences of other kinds skipped, in
    order. Raises MailError for a widget block that is not closed."""
    found, lines = [], text_lines(text)
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        kind = m.group(1).strip()
        end = next((j for j in range(i + 1, len(lines)) if FENCE.match(lines[j])), None)   # any fence line ends one
        if kind in KINDS:
            if end is None or FENCE.match(lines[end]).group(1).strip():
                raise MailError("widget block %d (%s) is not closed with a ``` line" % (len(found) + 1, kind), 1)
            found.append((kind, "\n".join(lines[i + 1:end]), i, end))
        i = len(lines) if end is None else end + 1
    return found


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    dup = next((k for k in keys if keys.count(k) > 1), None)
    if dup is not None:
        raise _Bad(dup, "given twice")
    return dict(pairs)


def _refuse_constant(name):
    raise _Bad("JSON", "%s is not a number JSON allows; use null for a gap" % name)


def _parse(source):
    if len(source.encode()) > MAX_BLOCK_BYTES:
        raise _Bad("block", "over %d KiB of JSON" % (MAX_BLOCK_BYTES >> 10))
    try:
        obj = json.loads(source, object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except ValueError as e:
        raise _Bad("JSON", "not valid JSON (%s)" % e)
    if not isinstance(obj, dict):
        raise _Bad("block", "must be one JSON object")
    return obj


def _keys(obj, where, required, optional):
    extra = sorted(set(obj) - set(required) - set(optional))
    if extra:
        raise _Bad("%s%s" % (where, extra[0]), "is not a key here (allowed: %s)" % ", ".join(required + optional))
    missing = [k for k in required if k not in obj]
    if missing:
        raise _Bad("%s%s" % (where, missing[0]), "is required")


def _shown_text(value, where):
    """value, a text at key path where, made plain (plain): refused with _Bad when it holds a control character, or
    right-to-left script once plain. The caller checks its own blank and length limits on what this returns, so that
    they measure what is shown."""
    if CONTROL.search(value):
        raise _Bad(where, NO_CONTROL)
    value = plain(value)
    if _right_to_left(value):
        raise _Bad(where, NO_RTL)
    return value


def _text(obj, key, where, longest, required):
    """Check obj[key], a text, and make it plain in place (_shown_text), then non-blank if required and at most
    longest characters."""
    value = obj.get(key)
    if value is None and not required and key not in obj:
        return
    must_be = "must be %stext" % ("non-blank " if required else "")
    if not isinstance(value, str):
        raise _Bad(where + key, must_be)
    value = obj[key] = _shown_text(value, where + key)
    if required and not value:
        raise _Bad(where + key, must_be)
    if len(value) > longest:
        raise _Bad(where + key, "at most %d characters (it has %d)" % (longest, len(value)))


def _flag(obj, key, where):
    if key in obj and not isinstance(obj[key], bool):
        raise _Bad(where + key, "must be true or false")


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_choices(spec):
    """Raise _Bad unless spec is a valid choices block (the README's rules); its text is made plain in place (_text)."""
    _keys(spec, "", ["question", "options"], ["multiple", "default"])
    _text(spec, "question", "", 200, True)
    _flag(spec, "multiple", "")
    _text(spec, "default", "", 200, False)
    options = spec["options"]
    if not isinstance(options, list) or not 2 <= len(options) <= 8:
        raise _Bad("options", "must be a list of 2 to 8 options")
    for n, o in enumerate(options):
        where = "options[%d]." % n
        if not isinstance(o, dict):
            raise _Bad(where[:-1], "must be an object with a label")
        _keys(o, where, ["label"], ["detail", "recommended"])
        _text(o, "label", where, 120, True)
        _text(o, "detail", where, 300, False)
        _flag(o, "recommended", where)
    recommended = [n for n, o in enumerate(options) if o.get("recommended")]
    if len(recommended) > 1:
        raise _Bad("options[%d].recommended" % recommended[1], "at most one option is recommended")
    if recommended and spec.get("multiple"):
        raise _Bad("options[%d].recommended" % recommended[0], "only when multiple is false")


def check_plot(spec, image_names):
    """Raise _Bad unless spec is a valid plot block (the README's rules); image_names: the mail's --image NAMEs. Its
    text, the bar categories and a filled plot's names too, is made plain in place (_text)."""
    if spec.get("kind") not in PLOT_KINDS:
        raise _Bad("kind", "must be one of %s" % ", ".join(PLOT_KINDS) if "kind" in spec else "is required")
    filled = spec["kind"] == "filled"
    if filled:
        _keys(spec, "", ["title", "kind", "segments", "rows"], ["tick", "x", "image"])
    else:
        _keys(spec, "", ["title", "kind", "x", "y", "series"], ["image"])
    _text(spec, "title", "", 200, True)
    (_check_filled if filled else _check_series)(spec)
    if "image" in spec:
        _text(spec, "image", "", IMAGE_NAME_MAX, True)
    if "image" in spec and spec["image"] not in image_names:
        raise _Bad("image", "must be the NAME of an --image sent with this mail (%s)"
                   % (", ".join(sorted(image_names)) or "it has none"))


def _axis(spec, axis, optional):
    """Check spec[axis], an object of the keys `optional`: its label text (made plain) and finite min and max."""
    if not isinstance(spec[axis], dict):
        raise _Bad(axis, "must be an object")
    _keys(spec[axis], axis + ".", [], optional)
    _text(spec[axis], "label", axis + ".", 80, False)
    for k in ("min", "max"):
        if k in spec[axis] and not _number(spec[axis][k]):
            raise _Bad("%s.%s" % (axis, k), "must be a finite number")


def _check_filled(spec):
    """Raise _Bad unless spec is a valid filled plot (the README's rules), its keys checked: each row a bar cut into
    the segments in proportion to their sizes, each segment filled to a percentage, with an optional tick in each."""
    if "x" in spec:
        _axis(spec, "x", ["label"])
    segments = spec["segments"]
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
        raise _Bad("segments", "must be a list of 1 to %d segments" % MAX_SEGMENTS)
    for n, s in enumerate(segments):
        where = "segments[%d]." % n
        if not isinstance(s, dict):
            raise _Bad(where[:-1], "must be an object with a name and a size")
        _keys(s, where, ["name", "size"], [])
        _text(s, "name", where, 80, True)
        if not _number(s["size"]) or s["size"] <= 0:
            raise _Bad(where + "size", "must be a finite number above 0")
        if s["name"] in [t["name"] for t in segments[:n]]:
            raise _Bad(where + "name", "no two segments have the same name")
    if "tick" in spec:
        _text(spec, "tick", "", 80, True)
    rows = spec["rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise _Bad("rows", "must be a list of 1 to %d rows" % MAX_ROWS)
    percents = ("must be a list of %d numbers from 0 to 100, one per segment, or null for none"
                % len(segments))
    for n, r in enumerate(rows):
        where = "rows[%d]." % n
        if not isinstance(r, dict):
            raise _Bad(where[:-1], "must be an object with a label and a fill")
        _keys(r, where, ["label", "fill"], ["group", "tick"])
        _text(r, "label", where, 80, True)
        if "group" in r:
            _text(r, "group", where, 80, True)
        for key in ("fill", "tick"):
            values = r.get(key, [])
            if not isinstance(values, list) or key in r and len(values) != len(segments) \
                    or not all(v is None or _number(v) and 0 <= v <= 100 for v in values):
                raise _Bad(where + key, percents)
        if "tick" in r and "tick" not in spec:
            raise _Bad(where + "tick", "needs the plot's tick: what its ticks mark, as the legend names them")
    if "tick" in spec and not any("tick" in r for r in rows):
        raise _Bad("tick", "names the rows' ticks, and no row has any")
    groups = _all_or_none(rows, "rows", "group", "row")
    if groups[0] is not None:
        for n in range(1, len(groups)):
            if groups[n] != groups[n - 1] and groups[n] in groups[:n]:
                raise _Bad("rows[%d].group" % n, "the rows of a group come one after another")


def _all_or_none(items, where, key, noun):
    """The values of key in items, the list at key path where, each item a `noun` (None where it gives none). Raises
    _Bad unless every item gives one or none does."""
    values = [i.get(key) for i in items]
    if any(v is not None for v in values) and None in values:
        raise _Bad("%s[%d].%s" % (where, values.index(None), key), "every %s has a %s, or none does" % (noun, key))
    return values


def _check_series(spec):
    """Raise _Bad unless spec is a valid line, scatter or bar plot (the README's rules), its keys checked."""
    _axis(spec, "x", ["label"])
    _axis(spec, "y", ["label", "min", "max"])
    y = spec["y"]
    if "min" in y and "max" in y and not y["min"] < y["max"]:
        raise _Bad("y.min", "must be below y.max")
    series = spec["series"]
    if not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
        raise _Bad("series", "must be a list of 1 to %d series" % MAX_SERIES)
    bar = spec["kind"] == "bar"
    points = 0
    for n, s in enumerate(series):
        where = "series[%d]." % n
        if not isinstance(s, dict):
            raise _Bad(where[:-1], "must be an object with name, x and y")
        _keys(s, where, ["name", "x", "y"], ["texture"])
        _text(s, "name", where, 80, True)
        if "texture" in s:
            if not bar:
                raise _Bad(where + "texture", "is for bar plots only")
            _text(s, "texture", where, 80, True)
        xs, ys = s["x"], s["y"]
        if not isinstance(xs, list) or not isinstance(ys, list) or len(xs) != len(ys):
            raise _Bad(where + "y", "x and y must be lists of the same length")
        if bar:
            names = "a bar plot's x is a list of category names, each 1 to %d characters" % MAX_CATEGORY_CHARS
            if not all(isinstance(v, str) for v in xs):
                raise _Bad(where + "x", names)
            xs = s["x"] = [_shown_text(v, "%sx[%d]" % (where, i)) for i, v in enumerate(xs)]
            if not all(1 <= len(v) <= MAX_CATEGORY_CHARS for v in xs):
                raise _Bad(where + "x", names)
            if len(xs) > MAX_CATEGORIES:
                raise _Bad(where + "x", "at most %d categories" % MAX_CATEGORIES)
            if xs != series[0]["x"]:
                raise _Bad(where + "x", "every series of a bar plot has the same categories, in the same order")
        elif not all(_number(v) for v in xs):
            raise _Bad(where + "x", "must hold finite numbers")
        if not all(v is None or _number(v) for v in ys):
            raise _Bad(where + "y", "must hold finite numbers, or null for a gap")
        points += len(xs)
    if points > MAX_POINTS:
        raise _Bad("series", "at most %d points across all series (it has %d)" % (MAX_POINTS, points))
    textures = _all_or_none(series, "series", "texture", "series")
    if textures[0] is not None:
        if len(set(textures)) > MAX_TEXTURES:
            raise _Bad("series", "at most %d textures (it has %d)" % (MAX_TEXTURES, len(set(textures))))
        pairs = [(s["name"], s["texture"]) for s in series]
        for n, pair in enumerate(pairs):
            if pair in pairs[:n]:
                raise _Bad("series[%d]" % n, "no two series have the same name and texture")


def check(kind, spec, image_names):
    """Raise _Bad unless spec is valid for its kind; its text is made plain in place."""
    if kind == "choices":
        check_choices(spec)
    elif kind == "plot":
        check_plot(spec, image_names)
    else:
        raise _Bad("kind", "is %r, not one of %s" % (kind, ", ".join(KINDS)))


def _check_all(found, image_names):
    """found: [(kind, spec or JSON text)] in order. The specs, checked and with their text made plain (copies: the
    specs given are left as they are); MailError names the first broken rule."""
    if len(found) > MAX_BLOCKS:
        raise MailError("a mail carries at most %d widget blocks; this one has %d" % (MAX_BLOCKS, len(found)), 1)
    specs = []
    for n, (kind, spec) in enumerate(found, 1):
        try:
            spec = _parse(spec) if isinstance(spec, str) else copy.deepcopy(spec)
            if not isinstance(spec, dict):
                raise _Bad("block", "must be one JSON object")
            check(kind, spec, image_names)
        except _Bad as e:
            raise MailError("widget block %d (%s): %s" % (n, kind, e), 1)
        specs.append({"kind": kind, "spec": spec})
    return specs


# ---- what Gmail is sent ----

# The phrases a fallback reads with, the same in its text and its HTML (review_prompt.md quotes two of them)
PLOT_LINE = "Interactive plot: %s (open it in the Mail tab)"    # a plot without an --image
DEFAULT_LINE = "If you don't answer: %s"
# agent-board's agentboard/mailforward.py strips this from forwarded mail: change both together.
REPLY_LINE = "Reply with the number%s."                         # "s" when several may be picked
RECOMMENDED = " (recommended)"


def _reply_line(spec):
    return REPLY_LINE % ("s" if spec.get("multiple") else "")


def fallback_text(widget):
    """A widget as plain text: the options as a numbered list, or the plot's line."""
    spec = widget["spec"]
    if widget["kind"] == "plot":
        return PLOT_LINE % spec["title"]
    lines = ["**%s**" % spec["question"], ""]
    for n, o in enumerate(spec["options"], 1):
        lines.append("%d. %s%s%s" % (n, o["label"], RECOMMENDED if o.get("recommended") else "",
                                     ": " + o["detail"] if o.get("detail") else ""))
    lines.append("")
    if spec.get("default"):
        lines.append(DEFAULT_LINE % spec["default"])
    lines.append(_reply_line(spec))
    return "\n".join(lines)


def fallback_html(widget, n):
    """A widget as HTML, wrapped in <div data-agentmail-widget="n">: the options as a numbered list, the plot as its
    --image (cid:NAME) or its line."""
    spec, esc = widget["spec"], htmllib.escape
    if widget["kind"] == "plot":
        inner = (image_html(spec["image"], spec["title"]) if spec.get("image")
                 else "<p>%s</p>" % (PLOT_LINE % esc(spec["title"])))
    else:
        items = "".join("<li><b>%s</b>%s%s</li>" % (esc(o["label"]), RECOMMENDED if o.get("recommended") else "",
                                                    "<br>" + esc(o["detail"]) if o.get("detail") else "")
                        for o in spec["options"])
        inner = "<p><b>%s</b></p>\n<ol>%s</ol>\n%s<p>%s</p>" % (
            esc(spec["question"]), items,
            "<p>%s</p>\n" % (DEFAULT_LINE % esc(spec["default"])) if spec.get("default") else "", _reply_line(spec))
    return '<div %s="%d">\n%s\n</div>' % (WIDGET_MARKER, n, inner)


def prepare(mail):
    """The mail as Gmail is sent it, with its widgets checked, their text made plain, and kept (Mail.widgets). With
    blocks in its text: each block replaced by its fallback, in the text and in the HTML rendered from it
    (compose.text_to_html), the HTML's fallbacks marked for the Mail tab; a mail with blocks and its own HTML (--html)
    is refused. Without blocks: the mail as it is, but for its widgets (one prepared already, perhaps on another
    machine), checked and made plain again; its text and HTML are left as they came. Raises MailError naming the
    first broken rule."""
    image_names = {name for name, _ in mail.images}
    found = blocks(mail.text)
    if not found:
        if not mail.widgets:
            return mail
        return mail._replace(widgets=tuple(_check_all(
            [(w.get("kind") if isinstance(w, dict) else None, w.get("spec") if isinstance(w, dict) else None)
             for w in mail.widgets], image_names)))
    if mail.html is not None:
        raise MailError("a mail with ```choices or ```plot blocks cannot also have --html: put the blocks in the "
                        "plain-text body and leave out --html", 1)
    if mail.widgets:
        raise MailError("a mail cannot carry both widget blocks and prepared widgets", 1)
    widgets = _check_all([(kind, source) for kind, source, _, _ in found], image_names)
    lines = text_lines(mail.text)
    token = "agentmail-widget-%s" % secrets.token_hex(8)    # never in the agent's own text
    text_out, marked = [], []
    last = 0
    for n, ((_, _, first, end), widget) in enumerate(zip(found, widgets)):
        text_out += lines[last:first] + [fallback_text(widget)]
        marked += lines[last:first] + ["", "%s-%d" % (token, n), ""]
        last = end + 1
    text_out += lines[last:]
    marked += lines[last:]
    html = text_to_html("\n".join(marked))
    for n, widget in enumerate(widgets):
        spot = "<p>%s-%d</p>" % (token, n)
        if spot not in html:
            raise RuntimeError("widget %d's place was lost rendering the mail's HTML" % n)
        html = html.replace(spot, fallback_html(widget, n), 1)
    return mail._replace(text="\n".join(text_out), html=html, widgets=tuple(widgets))
