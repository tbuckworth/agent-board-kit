"""The board groups' colours: defined once, in a colours.json, and worked out here for the layout, so that the Mac's
Mail tab, Time page and usage breakdown show the same ones. The desktop sends the colours themselves; the Mac holds no
group's colour. The README's "Group colours" says how each is chosen.

The colours.json is the owner's own, ~/.config/agent-board/colours.json (AGENT_BOARD_COLOURS names another), when it
exists, else the one beside this file, which names no group. It holds:
  groups     {group name: colour}: the colours of the groups the owner has named
  reserve    [colour, ...]: the colours of the layout's other groups, in layout order (for_layout)
  ungrouped  the colour of mail whose agent is in no group, and of the Time page's Unsorted time
  shades     {"lightness", "chroma"}: how far a group's sub-groups' shades spread from its colour (shades)
Every colour is "#RRGGBB".
"""
import itertools
import json
import math
import os
import re
from pathlib import Path

PALETTE = Path(__file__).with_name("colours.json")       # the kit's: no group named, the reserve for every group
OWN = Path(os.environ.get("AGENT_BOARD_COLOURS") or Path.home() / ".config/agent-board/colours.json")
HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def path_in_use():
    """The colours.json load() reads by default: the owner's own (OWN) when it exists, else the kit's (PALETTE)."""
    return OWN if OWN.exists() else PALETTE


def load(path=None):
    """colours.json (at `path`, path_in_use()'s by default), checked. Raises ValueError for one that is not of the
    shape the module's docstring gives (the reserve needs at least one colour; lightness is from 0 to 0.5, chroma from
    0 to 1), OSError when it cannot be read."""
    path = path if path is not None else path_in_use()
    try:
        p = json.loads(Path(path).read_text())
    except ValueError as e:
        raise ValueError("%s is not valid JSON: %s" % (path, e)) from None

    def colour(v):
        return isinstance(v, str) and bool(HEX_RE.fullmatch(v))
    s = p.get("shades") if isinstance(p, dict) else None
    if not (isinstance(p, dict) and isinstance(p.get("groups"), dict) and all(colour(v) for v in p["groups"].values())
            and isinstance(p.get("reserve"), list) and p["reserve"] and all(colour(v) for v in p["reserve"])
            and colour(p.get("ungrouped")) and isinstance(s, dict)
            and all(isinstance(s.get(k), (int, float)) and not isinstance(s.get(k), bool) for k in ("lightness", "chroma"))
            and 0 <= s["lightness"] <= 0.5 and 0 <= s["chroma"] <= 1):
        raise ValueError("%s must hold groups {name: colour}, a reserve of one colour or more, ungrouped (a colour) and "
                         "shades {lightness: 0-0.5, chroma: 0-1}, each colour #RRGGBB" % path)
    return p


# ------------------------------------------------------------------ OKLab (Björn Ottosson's), for the shades

def _oklab(colour):
    """(L, a, b) of "#RRGGBB" (sRGB)."""
    r, g, b = (_linear(int(colour[i:i + 2], 16) / 255) for i in (1, 3, 5))
    lms = [(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3),
           (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3),
           (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)]
    return (0.2104542553 * lms[0] + 0.7936177850 * lms[1] - 0.0040720468 * lms[2],
            1.9779984951 * lms[0] - 2.4285922050 * lms[1] + 0.4505937099 * lms[2],
            0.0259040371 * lms[0] + 0.7827717662 * lms[1] - 0.8086757660 * lms[2])


def _rgb(lightness, a, b):
    """The linear sRGB (r, g, b) of an OKLab colour; outside 0-1 when it is out of sRGB's gamut."""
    lms = [(lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3,
           (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3,
           (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3]
    return (4.0767416621 * lms[0] - 3.3077115913 * lms[1] + 0.2309699292 * lms[2],
            -1.2684380046 * lms[0] + 2.6097574011 * lms[1] - 0.3413193965 * lms[2],
            -0.0041960863 * lms[0] - 0.7034186147 * lms[1] + 1.7076147010 * lms[2])


def _linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _encoded(c):
    c = min(1.0, max(0.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _in_gamut(lightness, chroma, hue):
    """The sRGB colour nearest to OKLCH (lightness, chroma, hue): the same lightness and hue, its chroma lowered (by
    halving the step, to a thousandth of it) until it is in sRGB's gamut."""
    def rgb(c):
        return _rgb(lightness, c * math.cos(hue), c * math.sin(hue))
    if not all(-1e-9 <= v <= 1 + 1e-9 for v in rgb(chroma)):
        low, high = 0.0, chroma
        while high - low > chroma / 1000:
            mid = (low + high) / 2
            low, high = (mid, high) if all(-1e-9 <= v <= 1 + 1e-9 for v in rgb(mid)) else (low, mid)
        chroma = low
    return "#" + "".join("%02X" % round(_encoded(v) * 255) for v in rgb(chroma))


def shades(colour, n, rule):
    """The shades of `colour` for a group's n sub-groups, in layout order, darker to lighter, none of them the colour
    itself, which stays the colour of what is in no sub-group. The first half of them (rounded up) are darker and the
    rest lighter, each side spread evenly up to its end: with k on a side, they are 1/k, 2/k, ... k/k of `rule`'s
    lightness away from the colour in OKLCH lightness, the farthest at the outside. A lone sub-group is the darker end.
    The chroma is scaled by `rule`'s chroma at either end, and by less nearer the colour; the hue is kept. A shade
    outside sRGB has its chroma lowered until it fits."""
    lightness, a, b = _oklab(colour)
    chroma, hue = math.hypot(a, b), math.atan2(b, a)
    darker = (n + 1) // 2
    lighter = n - darker
    steps = [-(darker - i) / darker for i in range(darker)] + [j / lighter for j in range(1, lighter + 1)]
    return [_in_gamut(lightness + rule["lightness"] * t, chroma * (1 - (1 - rule["chroma"]) * abs(t)), hue)
            for t in steps]


# ------------------------------------------------------------------ the layout's colours

def for_layout(layout, palette):
    """The colours of `layout` (groups.layout's form), in the board's order: {"groups": [{"name", "colour", "shades":
    [{"name", "colour"}, ...]}, ...], "ungrouped": colour}; names are values, never keys, which the Mac's decoder would
    change. A group `palette` (load's) names has its colour there; each other group, in layout order, takes the next
    colour of the reserve, which starts again from its first once all are taken. Its sub-groups take shades() of its
    colour."""
    reserve = itertools.cycle(palette["reserve"])
    out = []
    for g in layout:
        colour = palette["groups"][g["name"]] if g["name"] in palette["groups"] else next(reserve)
        out.append({"name": g["name"], "colour": colour.upper(), "shades": [
            {"name": s, "colour": c} for s, c in zip(g["subgroups"], shades(colour, len(g["subgroups"]), palette["shades"]))]})
    return {"groups": out, "ungrouped": palette["ungrouped"].upper()}


def of(coloured, place):
    """What a list shows of something placed in `place`, (group, sub-group or None) of the layout `coloured`
    (for_layout's) holds, or None for no group: {"group", "subgroup", "colour": its group's colour, "shade": its
    sub-group's shade, else its group's colour}; with no group, group and subgroup are None and both colours
    Ungrouped's."""
    if place is None:
        return {"group": None, "subgroup": None, "colour": coloured["ungrouped"], "shade": coloured["ungrouped"]}
    group, sub = place
    g = next(x for x in coloured["groups"] if x["name"] == group)
    shade = next(x["colour"] for x in g["shades"] if x["name"] == sub) if sub else g["colour"]
    return {"group": group, "subgroup": sub, "colour": g["colour"], "shade": shade}
