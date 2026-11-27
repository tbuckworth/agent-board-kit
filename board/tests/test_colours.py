"""The board groups' colours (agentboard/colours.py and its colours.json): python3 -m unittest discover -s tests"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
from agentboard import colours  # noqa: E402

# An owner's own colours.json, naming four groups, and an example layout of them; the groups' colours and the ends of
# their shades (the middle of Work's three lies between the darker end and the colour)
OWN = {"groups": {"Work": "#0E8A84", "Research": "#3E9B4F", "Admin": "#6A5ACD", "Personal": "#CF4F7E"},
       "reserve": ["#2E5C8A", "#A85551", "#9750A7", "#3A9BC9", "#AD78B9", "#B08A1E"], "ungrouped": "#8C877F",
       "shades": {"lightness": 0.10, "chroma": 0.92}}
LAYOUT = [{"name": "Work", "subgroups": ["Parser", "Release", "Docs"]},
          {"name": "Research", "subgroups": ["Evals", "Reading"]},
          {"name": "Admin", "subgroups": ["Agent Board"]},
          {"name": "Personal", "subgroups": []}]
NAMED = {"Work": ("#0E8A84", ("#0A6A65", "#45ABA5")), "Research": ("#3E9B4F", ("#2C7A3B", "#63B771")),
         "Admin": ("#6A5ACD", None), "Personal": ("#CF4F7E", None)}


def own_file(palette=OWN):
    """`palette` written to a colours.json of its own in a new folder: its path."""
    path = Path(tempfile.mkdtemp()) / "colours.json"
    path.write_text(json.dumps(palette))
    return path


def distance(x, y):
    """Two colours' distance in OKLab: under about 0.02 the eye cannot tell them apart."""
    return math.dist(colours._oklab(x), colours._oklab(y))


def lightness(c):
    return colours._oklab(c)[0]


def by_name(coloured):
    """for_layout's answer as {group: (colour, {sub-group: shade})}."""
    return {g["name"]: (g["colour"], {s["name"]: s["colour"] for s in g["shades"]}) for g in coloured["groups"]}


class Palette(unittest.TestCase):
    def setUp(self):
        self.palette = colours.load(own_file())
        self.coloured = colours.for_layout(LAYOUT, self.palette)
        self.groups = by_name(self.coloured)

    def test_the_groups_the_owner_named_have_their_colours(self):
        self.assertEqual([g["name"] for g in self.coloured["groups"]], [g["name"] for g in LAYOUT])   # in its order
        for name, (colour, _) in NAMED.items():
            self.assertEqual(self.groups[name][0], colour, name)
        self.assertEqual(self.coloured["ungrouped"], "#8C877F")

    def test_sub_groups_are_shades_darker_to_lighter_in_layout_order_none_the_group_s_colour(self):
        for name, (colour, ends) in NAMED.items():
            got = list(self.groups[name][1].values())
            self.assertEqual(sorted(got, key=lightness), got, name)              # darker to lighter
            self.assertNotIn(colour, got, name)                                  # time in no sub-group keeps it
            self.assertTrue(all(distance(g, colour) > 0.045 for g in got), name)    # told apart from it
            if ends:
                for g, a in zip((got[0], got[-1]), ends):
                    self.assertLess(distance(g, a), 0.01, (name, g, a))          # the table's, to the eye
        work = self.groups["Work"][1]
        self.assertAlmostEqual(lightness(work["Release"]), lightness("#0E8A84") - 0.05, delta=0.002)  # halfway down
        self.assertLess(lightness(self.groups["Admin"][1]["Agent Board"]), lightness("#6A5ACD") - 0.09)   # a lone one: darker
        self.assertEqual(self.groups["Personal"][1], {})

    def test_each_side_spreads_evenly_to_its_end_skipping_the_group_s_colour_and_stays_in_srgb(self):
        base = lightness("#2E5C8A")
        for n, want in ((4, [-0.1, -0.05, 0.05, 0.1]), (5, [-0.1, -0.1 * 2 / 3, -0.1 / 3, 0.05, 0.1])):
            got = [lightness(c) - base for c in colours.shades("#2E5C8A", n, self.palette["shades"])]
            self.assertTrue(all(abs(g - w) < 0.003 for g, w in zip(got, want)), (n, got))   # three darker, two lighter
        self.assertEqual(colours.shades("#FFFFFF", 2, self.palette["shades"])[1], "#FFFFFF")   # lighter than white: white

    def test_a_group_the_palette_does_not_name_takes_the_reserve_s_next_colour_in_layout_order(self):
        reserve = self.palette["reserve"]
        layout = [{"name": "New one", "subgroups": []}] + LAYOUT[:2] + [{"name": "New two", "subgroups": ["a", "b"]}] + [
            {"name": "Extra %d" % i, "subgroups": []} for i in range(len(reserve))]
        got = by_name(colours.for_layout(layout, self.palette))
        self.assertEqual((got["New one"][0], got["New two"][0]), (reserve[0], reserve[1]))
        self.assertEqual(got["Work"][0], "#0E8A84")                              # a named group takes none of them
        self.assertEqual(list(got["New two"][1].values()), colours.shades(reserve[1], 2, self.palette["shades"]))
        self.assertEqual([got["Extra %d" % i][0] for i in range(len(reserve))],
                         reserve[2:] + reserve[:2])                               # used up: from the start again
        self.assertTrue(all(c != self.palette["ungrouped"] for c in reserve))   # never the grey of no group

    def test_what_a_list_shows_of_a_placement(self):
        self.assertEqual(colours.of(self.coloured, ("Work", "Docs")),
                         {"group": "Work", "subgroup": "Docs", "colour": "#0E8A84",
                          "shade": self.groups["Work"][1]["Docs"]})
        self.assertEqual(colours.of(self.coloured, ("Personal", None)),
                         {"group": "Personal", "subgroup": None, "colour": "#CF4F7E", "shade": "#CF4F7E"})
        self.assertEqual(colours.of(self.coloured, None),
                         {"group": None, "subgroup": None, "colour": "#8C877F", "shade": "#8C877F"})

    def test_a_palette_of_another_shape_is_refused(self):
        good = {"groups": {"Work": "#0E8A84"}, "reserve": ["#2E5C8A"], "ungrouped": "#8C877F",
                "shades": {"lightness": 0.1, "chroma": 0.92}}
        d = Path(tempfile.mkdtemp())
        for bad in ([], dict(good, reserve=[]), dict(good, groups={"Work": "teal"}), dict(good, ungrouped="#8C877"),
                    dict(good, shades={"lightness": 0.6, "chroma": 0.9}), dict(good, shades={"lightness": True,
                                                                                               "chroma": 0.9}),
                    {k: v for k, v in good.items() if k != "shades"}):
            (d / "c.json").write_text(json.dumps(bad))
            with self.assertRaises(ValueError, msg=bad):
                colours.load(d / "c.json")
        (d / "c.json").write_text("{")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            colours.load(d / "c.json")
        (d / "c.json").write_text(json.dumps(good))
        self.assertEqual(colours.load(d / "c.json"), good)


class KitPalette(unittest.TestCase):
    """The kit's own colours.json names no group: every group takes the reserve's colours in layout order, until the
    owner writes a colours.json of their own (colours.OWN), which then replaces it whole."""

    def setUp(self):
        self.saved = colours.OWN
        colours.OWN = Path(tempfile.mkdtemp()) / "colours.json"            # none written yet

    def tearDown(self):
        colours.OWN = self.saved

    def test_the_kit_s_palette_names_no_group_and_colours_every_group_from_its_reserve(self):
        kit = colours.load()
        self.assertEqual(colours.path_in_use(), colours.PALETTE)
        self.assertEqual(kit["groups"], {})
        self.assertGreaterEqual(len(kit["reserve"]), 8)
        self.assertEqual(len(set(kit["reserve"])), len(kit["reserve"]))
        self.assertNotIn(kit["ungrouped"], kit["reserve"])                 # never the grey of no group
        got = by_name(colours.for_layout(LAYOUT, kit))
        self.assertEqual([got[g["name"]][0] for g in LAYOUT], kit["reserve"][:4])

    def test_the_owner_s_colours_json_replaces_the_kit_s(self):
        colours.OWN.write_text(json.dumps(OWN))
        self.assertEqual(colours.path_in_use(), colours.OWN)
        self.assertEqual(colours.load(), OWN)
        self.assertEqual(by_name(colours.for_layout(LAYOUT, colours.load()))["Research"][0], "#3E9B4F")
        colours.OWN.write_text("{")                                       # a damaged one is an error, not the kit's
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            colours.load()


if __name__ == "__main__":
    unittest.main()
