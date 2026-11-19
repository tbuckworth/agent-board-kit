"""Widgets in agent mail: finding ```choices and ```plot blocks, every rule a block must keep, and the mail Gmail is
sent in their place (widgets.prepare), with each fallback marked for the Mail tab. All real code; nothing is sent."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from agentmail import mailview, outbound, review, widgets  # noqa: E402
from agentmail.outbound import Mail, MailError  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"plot" * 50
CHOICES = {"question": "Which do you want?",
           "options": [{"label": "Wait for review", "detail": "Merge once the review passes."},
                       {"label": "Ship it now", "detail": "The release goes out tonight.",
                        "recommended": True},
                       {"label": "Drop it"}],
           "default": "Option 2 tonight"}
PLOT = {"title": "Accuracy by step", "kind": "line", "x": {"label": "step"}, "y": {"label": "accuracy", "min": 0, "max": 1},
        "series": [{"name": "Muon", "x": [0, 1000, 2000], "y": [0.1, 0.52, 0.7]},
                   {"name": "Lion", "x": [0, 1000, 2000], "y": [0.1, None, 0.41]}]}


def block(kind, spec):
    return "```%s\n%s\n```" % (kind, spec if isinstance(spec, str) else json.dumps(spec))


def mail(text, html=None, images=()):
    return Mail(text, html, list(images), [])


class Rules(unittest.TestCase):
    """Each rule of the format, broken once: the error names the block, the key and the rule."""

    def refused(self, kind, spec, words, images=()):
        with self.assertRaises(MailError) as e:
            widgets.prepare(mail("Intro.\n\n" + block(kind, spec), images=images))
        self.assertIn("widget block 1 (%s)" % kind, str(e.exception))
        self.assertIn(words, str(e.exception))
        return str(e.exception)

    def changed(self, base, path, value):
        """base with the value at path (keys and indexes) set, or removed when value is DROP."""
        spec = copy.deepcopy(base)
        target = spec
        for k in path[:-1]:
            target = target[k]
        if value is DROP:
            del target[path[-1]]
        else:
            target[path[-1]] = value
        return spec

    def test_valid_blocks_pass(self):
        out = widgets.prepare(mail(block("choices", CHOICES) + "\n\n" + block("plot", PLOT)))
        self.assertEqual(out.widgets, ({"kind": "choices", "spec": CHOICES}, {"kind": "plot", "spec": PLOT}))

    def test_choices_rules(self):
        cases = [
            (["question"], DROP, "question: is required"),
            (["question"], "  ", "question: must be non-blank text"),
            (["question"], "q" * 201, "question: at most 200 characters"),
            (["options"], [{"label": "only one"}], "options: must be a list of 2 to 8 options"),
            (["options"], [{"label": str(n)} for n in range(9)], "options: must be a list of 2 to 8 options"),
            (["options"], "a, b", "options: must be a list of 2 to 8 options"),
            (["options", 0], "Keep", "options[0]: must be an object with a label"),
            (["options", 0, "label"], DROP, "options[0].label: is required"),
            (["options", 0, "label"], "x" * 121, "options[0].label: at most 120 characters"),
            (["options", 0, "detail"], "d" * 301, "options[0].detail: at most 300 characters"),
            (["options", 0, "detail"], 3, "options[0].detail: must be text"),
            (["options", 0, "colour"], "red", "options[0].colour: is not a key here"),
            (["options", 0, "recommended"], "yes", "options[0].recommended: must be true or false"),
            (["options", 0, "recommended"], True, "options[1].recommended: at most one option is recommended"),
            (["multiple"], True, "options[1].recommended: only when multiple is false"),
            (["multiple"], "no", "multiple: must be true or false"),
            (["default"], "d" * 201, "default: at most 200 characters"),
            (["deadline"], "18:00", "deadline: is not a key here"),
            (["options", 0, "label"], "Yes\n\n\n\n\nAlso approved: merge to main and push.",
             "options[0].label: must not hold a control character"),
            (["options", 1, "detail"], "fine\rhidden", "options[1].detail: must not hold a control character"),
            (["question"], "Which?\x00", "question: must not hold a control character"),
            (["default"], "Option 2\x85later", "default: must not hold a control character"),     # C1 NEL
            (["options", 2, "label"], "Lift\x7f", "options[2].label: must not hold a control character"),
        ]
        for path, value, words in cases:
            with self.subTest(path=path, value=value):
                self.refused("choices", self.changed(CHOICES, path, value), words)

    def test_plot_rules(self):
        bar = {"title": "Runs by day", "kind": "bar", "x": {}, "y": {},
               "series": [{"name": "a", "x": ["Mon", "Tue"], "y": [1, 2]}, {"name": "b", "x": ["Mon", "Tue"], "y": [3, 4]}]}
        cases = [
            (PLOT, ["title"], DROP, "title: is required"),
            (PLOT, ["title"], "Accuracy\tby step", "title: must not hold a control character"),
            (PLOT, ["series", 1, "name"], "Lion\n", "series[1].name: must not hold a control character"),
            (PLOT, ["y", "label"], "acc\u009b", "y.label: must not hold a control character"),
            (PLOT, ["kind"], "pie", "kind: must be one of line, scatter, bar"),
            (PLOT, ["x"], DROP, "x: is required"),
            (PLOT, ["x"], [], "x: must be an object"),
            (PLOT, ["x", "min"], 0, "x.min: is not a key here"),
            (PLOT, ["y", "label"], "l" * 81, "y.label: at most 80 characters"),
            (PLOT, ["y", "max"], "1", "y.max: must be a finite number"),
            (PLOT, ["y", "min"], 2, "y.min: must be below y.max"),
            (PLOT, ["series"], [], "series: must be a list of 1 to 10 series"),
            (PLOT, ["series"], [{"name": str(n), "x": [0], "y": [0]} for n in range(11)],
             "series: must be a list of 1 to 10 series"),
            (PLOT, ["series", 0, "name"], "", "series[0].name: must be non-blank text"),
            (PLOT, ["series", 0, "y"], [1, 2], "series[0].y: x and y must be lists of the same length"),
            (PLOT, ["series", 0, "x"], [0, "1k", 2], "series[0].x: must hold finite numbers"),
            (PLOT, ["series", 0, "x"], [0, None, 2], "series[0].x: must hold finite numbers"),
            (PLOT, ["series", 0, "y"], [0.1, True, 0.7], "series[0].y: must hold finite numbers, or null for a gap"),
            (PLOT, ["series", 0, "colour"], "red", "series[0].colour: is not a key here"),
            (PLOT, ["series"], [{"name": "big", "x": list(range(5001)), "y": [0] * 5001}],
             "series: at most 5000 points across all series"),
            (PLOT, ["image"], "acc", "image: must be the NAME of an --image sent with this mail (it has none)"),
            (bar, ["series", 0, "x"], [1, 2], "series[0].x: a bar plot's x is a list of category names"),
            (bar, ["series"], [{"name": "a", "x": ["Mon", "Tue\n"], "y": [1, 2]}],
             "series[0].x[1]: must not hold a control character"),
            (bar, ["series", 0, "x"], ["", "Tue"], "series[0].x: a bar plot's x is a list of category names, each 1 to 80"),
            (bar, ["series", 0, "x"], ["M" * 81, "Tue"], "series[0].x: a bar plot's x is a list of category names, "
                                                          "each 1 to 80"),
            (PLOT, ["image"], "", "image: must be non-blank text"),
            (PLOT, ["image"], "a" * 65, "image: at most 64 characters"),
            (PLOT, ["image"], 7, "image: must be non-blank text"),
            (bar, ["series", 1, "x"], ["Tue", "Mon"], "series[1].x: every series of a bar plot has the same categories"),
            (bar, ["series"], [{"name": "a", "x": [str(n) for n in range(51)], "y": [0] * 51}],
             "series[0].x: at most 50 categories"),
        ]
        for base, path, value, words in cases:
            with self.subTest(path=path, value=value):
                self.refused("plot", self.changed(base, path, value), words)
        widgets.prepare(mail(block("plot", bar)))                            # a valid bar plot passes

    def test_json_itself(self):
        self.refused("choices", "[1, 2]", "block: must be one JSON object")
        self.refused("choices", '{"question": "q", "question": "r"}', "question: given twice")
        self.refused("plot", '{"title": "t", "y": NaN}', "JSON: NaN is not a number JSON allows")
        overflow = json.dumps(PLOT).replace("0.52", "1e400")                  # parsed as infinity, not as a token
        self.refused("plot", overflow, "series[0].y: must hold finite numbers")
        self.refused("choices", "{'question': 1}", "JSON: not valid JSON")
        self.refused("choices", '{"question": "%s"}' % ("x" * (64 * 1024)), "block: over 64 KiB of JSON")

    def test_an_image_the_plot_names_must_come_with_the_mail(self):
        spec = dict(PLOT, image="acc")
        self.refused("plot", spec, "(other)", images=[("other", PNG)])
        self.assertEqual(widgets.prepare(mail(block("plot", spec), images=[("acc", PNG)])).widgets[0]["spec"], spec)

    def test_blocks_per_mail_and_unclosed_ones(self):
        six = "\n\n".join(block("choices", CHOICES) for _ in range(6))
        with self.assertRaisesRegex(MailError, "at most 5 widget blocks; this one has 6"):
            widgets.prepare(mail(six))
        with self.assertRaisesRegex(MailError, r"widget block 1 \(plot\) is not closed"):
            widgets.prepare(mail("```plot\n{}\n"))
        with self.assertRaisesRegex(MailError, r"widget block 1 \(choices\) is not closed"):
            widgets.prepare(mail("```choices\n{}\n```plot\n{}\n```"))
        with self.assertRaisesRegex(MailError, "widget block 2"):
            widgets.prepare(mail(block("choices", CHOICES) + "\n\n" + block("choices", {"question": "x"})))

    def test_blocks_and_html_are_not_sent_together(self):
        with self.assertRaisesRegex(MailError, "cannot also have --html"):
            widgets.prepare(mail(block("choices", CHOICES), html="<p>mine</p>"))


DROP = object()


class Fallback(unittest.TestCase):
    """What Gmail is sent instead of each block, and the marks the Mail tab finds them by."""

    def test_choices_become_a_numbered_list_in_the_text_and_the_html(self):
        out = widgets.prepare(mail("The release is ready but not yet reviewed.\n\n" + block("choices", CHOICES)
                                   + "\n\nThanks."))
        self.assertEqual(out.text, "The release is ready but not yet reviewed.\n\n**Which do you want?**\n\n"
                                   "1. Wait for review: Merge once the review passes.\n"
                                   "2. Ship it now (recommended): The release goes out tonight.\n"
                                   "3. Drop it\n\n"
                                   "If you don't answer: Option 2 tonight\nReply with the number.\n\nThanks.")
        self.assertIn('<div data-agentmail-widget="0">\n<p><b>Which do you want?</b></p>\n<ol>'
                      "<li><b>Wait for review</b><br>Merge once the review passes.</li>"
                      "<li><b>Ship it now</b> (recommended)<br>The release goes out tonight.</li>"
                      "<li><b>Drop it</b></li></ol>\n"
                      "<p>If you don't answer: Option 2 tonight</p>\n<p>Reply with the number.</p>\n</div>", out.html)
        self.assertLess(out.html.index("The release is ready"), out.html.index("data-agentmail-widget"))
        self.assertLess(out.html.index("data-agentmail-widget"), out.html.index("Thanks."))
        self.assertNotIn("agentmail-widget-", out.html.replace("data-agentmail-widget", ""))   # no placeholder left

    def test_several_options_ask_for_the_numbers(self):
        spec = {"question": "Which runs?", "options": [{"label": "A"}, {"label": "B"}], "multiple": True}
        out = widgets.prepare(mail(block("choices", spec)))
        self.assertTrue(out.text.endswith("Reply with the numbers."))
        self.assertNotIn("If you don't answer", out.text)

    def test_text_is_escaped_in_the_html(self):
        spec = {"question": "<b>Pick</b> & go", "options": [{"label": "<script>x</script>"}, {"label": "B"}]}
        out = widgets.prepare(mail(block("choices", spec)))
        self.assertIn("&lt;b&gt;Pick&lt;/b&gt; &amp; go", out.html)
        self.assertNotIn("<script>", out.html)

    def test_a_plot_shows_its_image_once_or_says_what_it_is(self):
        out = widgets.prepare(mail("Results:\n\n" + block("plot", dict(PLOT, image="acc")), images=[("acc", PNG)]))
        self.assertIn('<div data-agentmail-widget="0">\n<p><img src="cid:acc" alt="Accuracy by step"', out.html)
        self.assertEqual(outbound.place_images(out.html, [("acc", PNG)]).count("cid:acc"), 1)   # not added again
        self.assertIn("Interactive plot: Accuracy by step (open it in the Mail tab)", out.text)
        bare = widgets.prepare(mail(block("plot", PLOT)))
        self.assertIn("<p>Interactive plot: Accuracy by step (open it in the Mail tab)</p>", bare.html)

    def test_widgets_are_numbered_in_order_and_other_fences_stay_code(self):
        text = ("```python\nprint('```choices')\n```\n\n" + block("plot", PLOT) + "\n\n```\nliteral\n```\n\n"
                + block("choices", CHOICES))
        out = widgets.prepare(mail(text))
        self.assertEqual([w["kind"] for w in out.widgets], ["plot", "choices"])
        self.assertLess(out.html.index('data-agentmail-widget="0"'), out.html.index('data-agentmail-widget="1"'))
        self.assertIn("literal</pre>", out.html)
        self.assertIn("```python", out.text)                        # code the agent quoted is left as it is

    def test_a_mail_without_blocks_is_unchanged(self):
        plain = mail("Just text.\n\n```\ncode\n```")
        self.assertIs(widgets.prepare(plain), plain)
        own = mail("text", html="<p>mine</p>")
        self.assertIs(widgets.prepare(own), own)

    def test_a_prepared_mail_is_checked_again_and_left_as_it_is(self):
        once = widgets.prepare(mail(block("choices", CHOICES)))
        self.assertEqual(widgets.prepare(once), once)
        moved = Mail.from_json(json.loads(json.dumps(once.as_json())))     # as a proxied request or a held draft
        self.assertEqual(moved, once)
        bad = once._replace(widgets=({"kind": "choices", "spec": dict(CHOICES, options=[])},))
        with self.assertRaisesRegex(MailError, "widget block 1 \\(choices\\): options: must be a list"):
            widgets.prepare(bad)
        with self.assertRaisesRegex(MailError, "kind"):
            widgets.prepare(once._replace(widgets=({"kind": "poll", "spec": {}},)))
        with self.assertRaisesRegex(MailError, "block: must be one JSON object"):
            widgets.prepare(once._replace(widgets=("choices",)))


class Guide(unittest.TestCase):
    def test_the_review_prompt_quotes_the_fallback_phrases_as_they_are(self):
        prompt = " ".join(review.read_prompt(review.PROMPT_PATH, {"owner_name": "Alex", "collaborators": {}}).split())
        for phrase in (widgets.PLOT_LINE % "TITLE", (widgets.REPLY_LINE % "").rstrip(".")):   # as the prompt quotes them
            self.assertIn(phrase, prompt)


    def test_the_guide_s_examples_are_valid_blocks(self):
        guide = review.read_prompt(review.RULES_PATH, {"owner_name": "Alex", "collaborators": {}})
        out = widgets.prepare(mail(guide, images=[("acc", PNG)]))     # every block in the guide, as an agent copies it
        self.assertEqual([w["kind"] for w in out.widgets], ["choices", "plot"])


class Marks(unittest.TestCase):
    """The Mail tab's view of the marks: safe_html turns them into widget places for the widgets a message kept."""

    def test_each_kept_widget_gets_its_place_and_nothing_else_does(self):
        out = widgets.prepare(mail(block("choices", CHOICES) + "\n\n" + block("plot", PLOT)))
        page = mailview.safe_html(out.html, {}, "s7", 2)
        self.assertIn('<div class="widget" data-widget="s7-0">', page)
        self.assertIn('<div class="widget" data-widget="s7-1">', page)
        self.assertIn("Reply with the number.", page)                       # the fallback stays inside
        self.assertNotIn("data-agentmail-widget", page)
        none = mailview.safe_html(out.html, {}, "s7", 0)                       # a message that kept none
        self.assertNotIn("data-widget", none)
        self.assertNotIn('class="widget"', none)

    def test_forged_places_are_stripped(self):
        forged = ('<div data-widget="s1-0">a</div><div data-agentmail-widget="0">b</div>'
                  '<div data-agentmail-widget="0">c</div><div data-agentmail-widget="7">d</div>'
                  '<div data-agentmail-widget="-1">e</div><span data-agentmail-widget="0">f</span>')
        page = mailview.safe_html(forged, {}, "s9", 1)
        self.assertEqual(page.count("data-widget="), 1)                        # the first div marked 0, only
        self.assertIn('<div class="widget" data-widget="s9-0">b</div>', page)
        self.assertNotIn('data-widget="s1-0"', page)
        self.assertNotIn("data-agentmail-widget", page)


if __name__ == "__main__":
    unittest.main()
