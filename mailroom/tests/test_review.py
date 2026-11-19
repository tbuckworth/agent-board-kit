import base64
import contextlib
import email
import email.policy
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import config, headless, outbound, registry, review  # noqa: E402
from agentmail.store import Store  # noqa: E402

OWNER = "owner@example.com"
OWNER_NAME = config.load()["owner_name"]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
LONG = "**Results of the run.**\n\n" + " ".join("word%d" % i for i in range(200)) + "\n"
PROSE = ("The second run finished overnight and the scores moved the way we expected, although the gap between "
         "the two models is smaller than the first run suggested. I checked the logs for every attempt and found "
         "no crashes, no retries and no missing grades, so the numbers are complete for all forty attempts. ")
# the fault in its usual shape: numbered points hard-wrapped, each continuing flush left
CUT = ("Alex asked whether we changed the settings or the data. It is both.\n\n"
       "1. We did change a setting. On 3 March, an hour before the build started, every\n"
       "config file gained this line: \"cache: off\". I checked both builds.\n\n"
       "2. The data was never cached. The service that reads it keeps nothing on disk, before the\n"
       "change or after.\n")


class FakeModel:
    """Stands in for review.ask_model, whose `claude -p` run is the boundary (a model call; AskModel tests the call
    itself): returns the queued verdicts in order, or raises."""
    def __init__(self, *verdicts):
        self.verdicts, self.calls, self.readers, self.answered = list(verdicts), [], [], []

    def __call__(self, cfg, subject, shown, notes=(), reader=None, answers=None):
        self.calls.append((subject, shown, list(notes)))
        self.readers.append(reader)
        self.answered.append(answers)
        v = self.verdicts.pop(0)
        if isinstance(v, Exception):
            raise v
        return ({"kind": "results_report", "verdict": v, "blockers": ["Embed the accuracy plot."] if v == "revise" else [],
                 "polish": ["Define 'cell'."], "predicted_reply": "where's the plot?"}, {"seconds": 1.0, "cost": 0.001})


class Render(unittest.TestCase):
    def test_html_tables_images_links(self):
        shown = review.render("", '<p>x</p><table><tr><th>a</th><td>1</td></tr></table>'
                                  '<img src="cid:p1" alt="accuracy"><a href="https://d.example/doc">doc</a>')
        self.assertIn("[TABLE]", shown)
        self.assertIn("| a | 1", shown)
        self.assertIn("[IMAGE embedded: accuracy]", shown)
        self.assertIn("doc <https://d.example/doc>", shown)

    def test_text_is_rendered_the_way_agent_mail_renders_it(self):
        shown = review.render("**Bottom line:** works\n\n- one\n- two\n")
        self.assertIn("Bottom line: works", shown)
        self.assertNotIn("**", shown)

    def test_style_and_script_dropped(self):
        self.assertEqual(review.render("", "<style>p{}</style><p>hi</p>"), "hi")

    def test_a_fenced_block_is_marked_with_its_lines_as_written(self):
        shown = review.render("On the desktop:\n\n```\ngit -C ~/code/agent-board pull\n  agent-board act\n```\n\nThen wait.")
        self.assertIn("On the desktop:\n\n[CODE]\ngit -C ~/code/agent-board pull\nagent-board act\n[/CODE]", shown)
        self.assertTrue(shown.endswith("[/CODE]\n\nThen wait."), shown)

    def test_a_shell_pipeline_in_a_block_is_not_a_broken_table(self):
        pipeline = "Run:\n\n```\nps aux | grep agent | head\nls | sort | uniq\n```\n"
        self.assertIn("[CODE]\nps aux | grep agent | head\nls | sort | uniq\n[/CODE]", review.render(pipeline))
        self.assertFalse(any("pipe-separated rows" in h for h in review.hints(pipeline, owner_name="Alex")))


class CopyRule(unittest.TestCase):
    """Commands and paste-text for the owner each in a fenced block of their own, which the Mail tab gives a Copy
    button: the guide asks for it and the review flags a breach."""

    CFG = {"owner_name": "Alex", "collaborators": {}}

    def test_the_guide_states_the_rule_with_a_one_command_example(self):
        guide = " ".join(review.read_prompt(review.RULES_PATH, self.CFG).split())
        for words in ("Every command Alex should run, and every text to paste, goes in a fenced block of its own",
                      "Copy button", "one command per block when they run separately",
                      "no `$` or `>` prompt and nothing else in the block", "where to run it"):
            self.assertIn(words, guide)
        example = review.read_prompt(review.RULES_PATH, self.CFG).split("where to run it in the line before:", 1)[1]
        block = example.split("```", 2)[1]
        self.assertEqual([line.strip() for line in block.strip().splitlines()], ["agent-mail status"])

    def test_the_review_knows_code_blocks_and_flags_a_breach(self):
        prompt = " ".join(review.read_prompt(review.PROMPT_PATH, self.CFG).split())
        self.assertIn("fenced code blocks (each of which the Mail tab gives a Copy button) between [CODE] and [/CODE]",
                      prompt)
        self.assertIn("sits alone in its own [CODE] block", prompt)
        self.assertIn('or a [CODE] block that mixes one with prose or a "$" or ">" prompt, is a blocker', prompt)


class Checks(unittest.TestCase):
    def test_lint_data_uri_local_file_and_missing_cid(self):
        out = review.lint("t", '<img src="data:image/png;base64,AA"><img src="/tmp/x.png"><img src="cid:fig">', [],
                          owner_name=OWNER_NAME)
        self.assertEqual(len(out), 3)
        self.assertEqual(review.lint("t", '<img src="cid:fig">', [("fig", PNG)], owner_name=OWNER_NAME), [])

    def test_hints(self):
        h = review.hints("The plot is at /home/alex/tmp/p.png.\n\n| a | b |\n| 1 | 2 |\n", owner_name=OWNER_NAME)
        self.assertTrue(any("/home/alex/tmp/p.png" in x for x in h))
        self.assertFalse(any("pipe-separated" in x for x in h))      # plain-text pipe tables render as tables
        h = review.hints("x", "<p>a | b | c<br>1 | 2 | 3</p>", owner_name=OWNER_NAME)    # pipe text in custom HTML
        self.assertTrue(any("pipe-separated" in x for x in h))
        self.assertEqual(review.hints("Short and clean.", owner_name=OWNER_NAME), [])
        self.assertEqual(review.hints("x", "<h2>Results</h2><pre># install\npip install x</pre><code>**kw</code>"
                                           "<table><tr><th># runs</th></tr></table>", owner_name=OWNER_NAME), [])
        self.assertTrue(review.hints("x", "<p>**Bottom line** it works</p>", owner_name=OWNER_NAME))
        self.assertEqual(review.hints("x", "<p>See <b>item</b> # 5 here</p>", owner_name=OWNER_NAME), [])

    def test_layout_wall_of_bare_paragraphs(self):
        out = review.layout(PROSE * 3)                                   # 180 words, one paragraph, no markers
        self.assertEqual(len(out), 1)
        self.assertIn("wall of bare paragraphs", out[0])
        for fine in ("**Headline:** " + PROSE * 3,                       # any marker gives it something to scan
                     "- " + PROSE * 3,
                     "\n\n".join([PROSE] * 3),                           # short paragraphs read fine
                     PROSE * 2):                                         # under 150 words
            self.assertEqual(review.layout(fine), [], fine[:40])

    def test_layout_list_item_cut_by_a_flush_left_line(self):
        out = review.layout(CUT)
        self.assertEqual(len(out), 1)
        self.assertIn('flush left ("config file gained this line', out[0])
        self.assertIn("and 1 more", out[0])
        for fine in ("- one\n  continued, indented\n- two",
                     "- Link to the PR:\nhttps://github.com/x/y/pull/1",   # a link under its item
                     "- done.\nNothing needed from you.",                  # a new paragraph, no blank line
                     "- a\n\ngarden has three helpers.",                    # after a blank line
                     "1. Reply with one of:\na. go ahead\nb. wait",        # lettered options
                     "```\n1. not a list, a\nfenced block\n```"):
            self.assertEqual(review.layout(fine), [], fine)

    def test_layout_hard_wrapped_text(self):
        wrapped = textwrap.fill(PROSE * 2, 60)
        out = review.layout("**Results**\n\n" + wrapped)
        self.assertEqual(len(out), 1)
        self.assertIn("hard-wrapped", out[0])
        self.assertEqual(review.layout("**Results**\n\n" + PROSE * 2), [])
        for lines in (["Run: 12", "Cost: $4", "Time: 3h", "GPU: H100", "Status: done", "Owner: me"],
                      ["Sources:"] + ["https://s%d.example/page" % i for i in range(6)],
                      ["| model | score |"] + ["| m%d | %d |" % (i, i) for i in range(6)],
                      ["name      recipe-site", "model     claude-opus", "effort    xhigh", "branch    main", "state     done"],
                      ["Reply with one of:", "a. go ahead,", "b. wait,", "c. stop,", "d. ask Sam,", "e. nothing"]):
            self.assertEqual(review.layout("\n".join(lines)), [], lines[0])


class AskModel(unittest.TestCase):
    """The claude -p call: the owner's own login, then the work login, then the API key; a timeout ends it."""
    GOOD = json.dumps({"total_cost_usd": 0.002, "structured_output": {
        "kind": "answer", "predicted_reply": "", "verdict": "pass", "issues": []}})
    LIMIT = json.dumps({"is_error": True, "result": "You've hit your usage limit"})

    def setUp(self):
        self.calls, self.outputs = [], []
        self.work, bws = tempfile.mkdtemp(), tempfile.mkdtemp()
        Path(self.work, ".credentials.json").write_text("{}")
        Path(bws, "token").write_text("tok\n")
        Path(bws, "project-id").write_text("proj-1\n")
        self.cfg = dict(config.load(), orchestrator_fallback_secret="AGENTS_ANTHROPIC_API_KEY",
                        bws_token_file=str(Path(bws, "token")),
                        bws_project_file=str(Path(bws, "project-id")), usage={
                            "claude_accounts": {"personal": {"config_dir": None}, "work": {"config_dir": self.work}},
                            "order": ["claude:personal", "claude:work", "codex"]})

        def run(cmd, **kw):
            self.calls.append((cmd, kw.get("env"), kw.get("input")))
            out = self.outputs.pop(0)
            if isinstance(out, Exception):
                raise out
            return subprocess.CompletedProcess(cmd, 1 if "is_error" in out else 0, out, "")
        find_bin = registry.find_bin
        # the CLIs: claude and bws are never run here (run stands in for them), so neither need be installed
        for target, attr, fake in ((registry, "claude_bin", lambda: "claude"),
                                   (registry, "find_bin", lambda name, extra: "/usr/bin/bws" if name == "bws"
                                    else find_bin(name, extra)),
                                   (subprocess, "run", run)):
            patcher = patch.object(target, attr, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def names(self):
        return [Path(c[0][0]).name for c in self.calls]

    def test_login_order_and_environments(self):
        self.outputs = [self.LIMIT, self.LIMIT, self.GOOD]
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/elsewhere", "ANTHROPIC_API_KEY": "k"}):   # the sender's own
            info = review.ask_model(self.cfg, "s", "body")[1]
        self.assertEqual((info["auth"], self.names()), (headless.API_KEY, ["claude", "claude", "bws"]))   # never Codex
        personal, work, key = [c[1] for c in self.calls]
        self.assertFalse("CLAUDE_CONFIG_DIR" in personal or "ANTHROPIC_API_KEY" in personal)
        self.assertEqual(work["CLAUDE_CONFIG_DIR"], str(Path(self.work).resolve()))
        self.assertEqual(self.calls[2][0], ["/usr/bin/bws", "run", "--project-id", "proj-1", "--",
                                            'ANTHROPIC_API_KEY="$AGENTS_ANTHROPIC_API_KEY" exec '
                                            + shlex.join(self.calls[0][0])])
        self.assertEqual(key["BWS_ACCESS_TOKEN"], "tok")

    def test_no_login_inherits_the_sending_session(self):
        session = {"CLAUDE_CODE_MESSAGING_SOCKET": "/run/s.sock", "CLAUDE_CODE_MESSAGING_TOKEN": "t", "CLAUDECODE": "1",
                   "CLAUDE_CODE_SESSION_ID": "33333333-3333-3333-3333-333333333333"}
        self.outputs = [self.LIMIT, self.LIMIT, self.GOOD]
        with patch.dict(os.environ, session):
            review.ask_model(self.cfg, "s", "body")
        for cmd, run_env, _ in self.calls:
            for k in session:
                self.assertFalse(k in run_env, k)                           # keys only: never print the environment

    def test_alex_login_first(self):
        self.outputs = [self.GOOD]
        res, info = review.ask_model(self.cfg, "s", "body")
        self.assertEqual((res["verdict"], info["auth"], len(self.calls)), ("pass", "personal", 1))

    def test_the_rows_model_names_any_login_but_the_first(self):
        label = lambda auth: review.model_label(self.cfg, "haiku", auth)   # noqa: E731
        self.assertEqual([label(a) for a in ("personal", "work", headless.API_KEY, "default", None)],
                         ["haiku", "haiku (work login)", "haiku (api key)", "haiku", "haiku"])
        self.assertEqual(review.model_label(config.load(), "haiku", "default"), "haiku")

    def test_usage_limit_moves_to_the_work_login_then_the_key(self):
        self.outputs = [self.LIMIT, self.GOOD]
        self.assertEqual(review.ask_model(self.cfg, "s", "body")[1]["auth"], "work")
        self.calls, self.outputs = [], [self.LIMIT, self.LIMIT, self.GOOD]
        res, info = review.ask_model(self.cfg, "s", "body")
        self.assertEqual((res["verdict"], info["auth"]), ("pass", headless.API_KEY))
        self.assertIn("usage limit", info["failed"])
        self.assertEqual(self.names()[2], "bws")

    def test_all_logins_failing_or_a_timeout_fails(self):
        self.outputs = [self.LIMIT, self.LIMIT, self.LIMIT]
        with self.assertRaises(RuntimeError):
            review.ask_model(self.cfg, "s", "body")
        self.calls, self.outputs = [], [subprocess.TimeoutExpired("claude", 40)]
        with self.assertRaises(subprocess.TimeoutExpired):
            review.ask_model(self.cfg, "s", "body")
        self.assertEqual(len(self.calls), 1)

    def test_outside_reader_gets_the_outside_prompt(self):
        self.outputs = [self.GOOD]
        review.ask_model(self.cfg, "s", "body", reader="Sam's team")
        cmd = self.calls[0][0]
        self.assertIn("someone other than\nAlex", cmd[cmd.index("--system-prompt") + 1])
        self.assertTrue(self.calls[0][2].startswith("To: Sam's team\nSubject: s"))

    def test_a_reply_is_read_with_the_message_it_answers_in_front(self):
        self.outputs = [self.GOOD, self.GOOD, self.GOOD]
        for answers in ("  Why did run 3 fail?\n", "x" * (review.ANSWERED_MAX + 50), " \n"):
            review.ask_model(self.cfg, "s", "body", answers=answers)
        whole, cut, blank = [c[2] for c in self.calls]
        self.assertEqual(whole, "[The message this email answers:]\nWhy did run 3 fail?\n[End of that message. The email:]"
                                "\n\nSubject: s\n\nbody")
        self.assertIn("]\n" + "x" * review.ANSWERED_MAX + "\n[", cut)
        self.assertEqual(blank, "Subject: s\n\nbody")
        cmd = self.calls[0][0]
        for frame in review.ANSWERED.split("%s"):     # the prompt names the lines the reviewer is shown
            self.assertIn(frame.strip(), cmd[cmd.index("--system-prompt") + 1])


class Gate(unittest.TestCase):
    def setUp(self):
        self.st = Store(Path(TMP) / ("gate-%s.sqlite" % self.id().rsplit(".", 1)[-1]))
        self.cfg = config.load()
        self.real = review.ask_model

    def tearDown(self):
        review.ask_model = self.real

    def gate(self, text=LONG, to=OWNER, **kw):
        return review.gate(self.cfg, self.st, "tagA", [to], "Run results", text, None, (), draft={"op": "send"}, **kw)

    def rows(self):
        return [dict(r) for r in self.st.con.execute("select * from reviews order by id")]

    def test_a_third_mail_to_alex_within_the_hour_asks_for_one_mail(self):
        now = int(time.time())
        for gid, age in (("old", 7200), ("g1", 600), ("g2", 60)):     # one sent before the hour, two inside it
            self.st.record_sent("tagA", gid, "t", "<%s@x>" % gid, OWNER, "s",
                                cc_addr=None, body=None, eml=None, attachments=None)
            self.st.con.execute("update sent set sent_at=? where gmail_id=?", (now - age, gid))
        fm = review.ask_model = FakeModel("pass")
        self.gate()
        self.assertIn("This would be mail 3 from this agent to Alex within the hour", " ".join(fm.calls[0][2]))

    def test_mail_to_others_is_reviewed_for_its_reader(self):
        self.cfg["collaborators"] = {"agent.sam@example.com": {"owner": "Sam", "share_data_with": ["agent.sam@example.com", "sam@example.com"]}}
        fm = review.ask_model = FakeModel("pass", "pass", "pass", "pass")
        self.gate(to="agent.sam@example.com")
        self.gate(to="Sam <sam@example.com>")
        self.gate(to="someone@example.com")
        self.gate(to="someone@example.com, " + OWNER)                    # Alex among them: his rules
        self.assertEqual([r["audience"] for r in self.rows()], ["collaborator", "collaborator", "other", "owner"])
        self.assertIn("Sam's team", fm.readers[0])
        self.assertIn("someone@example.com: a person Alex asked", fm.readers[2])
        self.assertIsNone(fm.readers[3])
        self.cfg["review"] = {"outside": False}
        self.assertEqual(self.gate(to="someone@example.com"), (None, None, None))
        self.cfg["review"] = {"hold_outside": False}
        review.ask_model = FakeModel("revise")
        rid, stop, tips = self.gate(to="someone@example.com")
        self.assertIsNone(stop)
        self.assertIn("for your next mail to the recipient", tips)
        self.assertEqual(self.rows()[-1]["verdict"], "advised")

    def test_the_message_a_reply_answers_goes_to_alexs_reviewer_and_no_other(self):
        fm = review.ask_model = FakeModel("pass", "pass", "pass")
        self.gate(answers="Why did run 3 fail?")
        self.gate(to="someone@example.com", answers="Why did run 3 fail?")
        self.gate()                                                          # not a reply
        self.assertEqual(fm.answered, ["Why did run 3 fail?", None, None])

    def test_a_held_mail_to_one_reader_does_not_let_the_next_to_another_through(self):
        self.cfg["collaborators"] = {"agent.sam@example.com": {"owner": "Sam", "share_data_with": ["agent.sam@example.com", "sam@example.com"]}}
        review.ask_model = FakeModel("revise", "revise", "revise")
        self.assertIn("expects them", self.gate(to="agent.sam@example.com")[1])   # held, to Sam's team
        self.assertTrue(self.gate()[1])                                      # to Alex: its own round 1, held
        rid, stop, tips = self.gate()                                        # to Alex, round 2: goes out
        self.assertIsNone(stop)
        self.st.review_sent(rid, "<m>")
        first = {r["audience"]: r["draft"] for r in self.rows() if r["round"] == 1}
        self.assertIsNotNone(first["collaborator"])                         # still waiting for its revision
        self.assertIsNone(first["owner"])
        self.assertEqual([r["audience"] for r in self.st.stale_drafts(time.time() + 1)], ["collaborator"])

    def test_a_credential_holds_the_mail_every_time(self):
        review.ask_model = FakeModel()
        key = "sk-ant-api03-" + "x" * 40
        for text in (LONG + "\nkey: " + key, LONG + "\nkey: " + key, "key: " + key):
            stop = self.gate(text=text)[1]
            self.assertIn("an Anthropic or OpenAI API key ('sk-ant...')", stop)
            self.assertNotIn(key, stop)
        self.assertEqual([(r["verdict"], r["draft"]) for r in self.rows()], [("revise", None)] * 3)
        self.assertNotIn(key, json.dumps([dict(r) for r in self.rows()]))
        self.assertIsNone(self.gate(text="key: " + key, no_review="a dummy key in a test fixture")[1])
        self.assertEqual(review.secrets("tokens look like sk-ant-..., ghp_..."), [])

    def test_short_mail_skips_the_model(self):
        review.ask_model = FakeModel()
        rid, stop, tips = self.gate(text="Done: merged PR 12. Nothing needed from you.")
        self.assertIsNone(stop)
        self.assertEqual(self.rows()[0]["verdict"], "skipped")

    def test_pass(self):
        review.ask_model = FakeModel("pass")
        rid, stop, tips = self.gate()
        self.assertIsNone(stop)
        self.assertIn("Define 'cell'.", tips)              # polish goes back as tips, the mail still goes
        self.st.review_sent(rid, "<m1>")
        row = self.rows()[0]
        self.assertEqual((row["verdict"], row["sent_message_id"], row["round"]), ("pass", "<m1>", 1))

    def test_the_pushback_names_the_owner_from_config(self):
        self.cfg["owner_name"] = "Alex"
        review.ask_model = FakeModel("revise")
        stop = self.gate()[1]
        self.assertIn("read it as Alex would and expects them to reply", stop)

    def test_revise_holds_once_then_the_next_mail_goes(self):
        review.ask_model = FakeModel("revise", "revise", "revise")
        rid, stop, tips = self.gate()
        self.assertIsNone(rid)
        self.assertTrue(stop.startswith(review.NOT_SENT))
        self.assertIn("Embed the accuracy plot.", stop)
        self.assertIn("where's the plot?", stop)
        self.assertIsNotNone(self.rows()[0]["draft"])
        rid, stop, tips = self.gate()                        # round 2 goes out whatever the verdict
        self.assertIsNone(stop)
        self.st.review_sent(rid, "<m2>")
        first, second = self.rows()
        self.assertEqual((second["round"], second["verdict"], second["sent_message_id"]), (2, "revise", "<m2>"))
        self.assertIsNone(first["draft"])              # settled: the watchdog will not send it
        rid, stop, tips = self.gate()                        # a new mail after that is round 1 again
        self.assertTrue(stop)

    def test_model_failure_fails_open(self):
        review.ask_model = FakeModel(RuntimeError("usage limit"))
        rid, stop, tips = self.gate()
        self.assertIsNone(stop)
        self.assertEqual((self.rows()[0]["verdict"], self.rows()[0]["reasons"]), ("error", "RuntimeError: usage limit"))

    def test_model_failure_still_holds_a_broken_image(self):
        review.ask_model = FakeModel(RuntimeError("timeout"))
        rid, stop, tips = review.gate(self.cfg, self.st, "tagA", [OWNER], "s", LONG, '<p>x</p><img src="cid:nope">', (),
                                draft={})
        self.assertIn("cid:nope", stop)
        # no reviewer read it, and the agent is told so: the automatic checks alone hold it
        self.assertNotIn("reviewer with no context", stop)
        self.assertIn("The reviewer could not run (RuntimeError: timeout)", stop)
        row = self.rows()[0]
        self.assertEqual((row["verdict"], json.loads(row["fixes"])["reviewer_error"]), ("revise", "RuntimeError: timeout"))

    def test_layout_holds_a_long_plain_text_mail_once(self):
        text = CUT + "\n" + textwrap.fill(PROSE * 2, 70)
        review.ask_model = FakeModel("pass", "pass")
        stop = self.gate(text=text)[1]                       # the model passes it; the layout holds it
        self.assertIn("flush left", stop)
        self.assertIn("hard-wrapped", stop)
        rid, stop, tips = self.gate(text=text)               # round 2 goes out, with the fixes as tips
        self.assertIsNone(stop)
        self.assertIn("flush left", tips)

    def test_layout_holds_with_the_model_down(self):
        review.ask_model = FakeModel(RuntimeError("timeout"))
        self.assertIn("hard-wrapped", self.gate(text=textwrap.fill(PROSE * 3, 70))[1])

    def test_layout_leaves_short_html_and_outside_mail_alone(self):
        review.ask_model = FakeModel("pass", "pass")
        self.assertIsNone(self.gate(text=CUT)[1])            # under 120 words: no model, no checks, as before
        self.assertEqual(self.rows()[-1]["verdict"], "skipped")
        text = CUT + "\n" + textwrap.fill(PROSE * 2, 70)
        self.assertIsNone(review.gate(self.cfg, self.st, "tagA", [OWNER], "s", text, "<p>x</p>" + text, (), draft={})[1])
        self.assertIsNone(self.gate(text=text, to="someone@example.com")[1])

    def test_no_review_override(self):
        review.ask_model = FakeModel()
        rid, stop, tips = self.gate(no_review="urgent")
        self.assertIsNone(stop)
        self.assertEqual((self.rows()[0]["verdict"], self.rows()[0]["reasons"]), ("override", "urgent"))

    def test_shadow_mode_records_but_sends(self):
        self.cfg["review"] = {"mode": "shadow"}
        review.ask_model = FakeModel("revise")
        rid, stop, tips = self.gate()
        self.assertIsNone(stop)
        self.assertEqual(self.rows()[0]["verdict"], "revise")
        self.assertIsNone(self.rows()[0]["draft"])

    def test_light_policy_advises_short_non_reports(self):
        self.cfg["review"] = {"hold_kinds": ["results_report"], "hold_min_words": 300}
        review.ask_model = FakeModel("revise")
        real = FakeModel.__call__
        def as_status(fm, *a, **k):
            res, info = real(fm, *a, **k)
            return dict(res, kind="status_update"), info
        FakeModel.__call__ = as_status
        try:
            rid, stop, tips = self.gate()                  # 200 words, a status update: sent, blockers as tips
        finally:
            FakeModel.__call__ = real
        self.assertIsNone(stop)
        self.assertIn("Embed the accuracy plot.", tips)
        self.assertEqual(self.rows()[0]["verdict"], "advised")
        self.cfg["review"] = {"hold_kinds": "all"}
        review.ask_model = FakeModel("revise")
        self.assertTrue(self.gate()[1])

    def test_disabled(self):
        self.cfg["review"] = {"enabled": False}
        self.assertEqual(self.gate(), (None, None, None))


class Args:
    def __init__(self, **kw):
        self.__dict__.update(dict(body_file=None, html=None, session=None, cli=None, cc=None, unrouted=False,
                                  image=None, attach=None, no_review=None, subject=None))
        self.__dict__.update(kw)


class AgentMail(unittest.TestCase):
    def setUp(self):
        self.am = load_script("agent-mail")
        self.ctx = outbound.Context(config.load())
        self.sent = fakes.sending_gmail(self, self.am, outbound)   # the Gmail API: nothing is sent
        homes.Homes(self)   # no sessions in the registry: the sending one comes from CLAUDE_CODE_SESSION_ID alone
        env = patch.dict(os.environ, CLAUDE_CODE_SESSION_ID="33333333-3333-3333-3333-333333333333")   # the sending session
        env.start()
        self.addCleanup(env.stop)
        self.real = review.ask_model
        self.body = Path(TMP) / "long.txt"
        self.body.write_text(LONG)
        self.png = Path(TMP) / "plot.png"
        self.png.write_bytes(PNG)
        Store().con.execute("delete from reviews")

    def tearDown(self):
        review.ask_model = self.real

    def last(self):
        raw, thread_id = self.sent[-1]
        return email.message_from_bytes(raw, policy=email.policy.default)

    def test_revise_sends_nothing_and_exits_3(self):
        review.ask_model = FakeModel("revise", "pass")
        self.assertEqual(self.am.do_send(self.ctx, Args(to=OWNER, subject="results", body_file=str(self.body))), 3)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.am.do_send(self.ctx, Args(to=OWNER, subject="results", body_file=str(self.body))), 0)
        self.assertEqual(len(self.sent), 1)
        row = Store().con.execute("select * from reviews order by id desc limit 1").fetchone()
        self.assertEqual(row["sent_message_id"], "<g1@mail.gmail.com>")

    def test_a_reply_is_reviewed_against_the_mail_it_answers(self):
        fm = review.ask_model = FakeModel("pass")
        inb = Store().record_inbound(gmail_id="in-q", thread_id="thr", message_id="<q@x>", from_addr=OWNER,
                                     subject="Re: results [#abc234de]", action="delivered", body="Why did run 3 fail?")
        self.assertEqual(self.am.do_reply(self.ctx, Args(inbound_id=inb, body_file=str(self.body))), 0)
        self.assertEqual((fm.answered, fm.calls[0][0]), (["Why did run 3 fail?"], "Re: results [#abc234de]"))
        self.assertEqual(self.sent[-1][1], "thr")

    def test_a_held_reply_under_a_new_subject_goes_out_later_as_that_new_thread(self):
        fm = review.ask_model = FakeModel("revise")
        st = Store()
        inb = st.record_inbound(gmail_id="in-s", thread_id="thr", message_id="<q@x>", from_addr=OWNER,
                                subject="Re: run failed [#abc234de]", action="delivered", body="Is it fixed?")
        args = Args(inbound_id=inb, body_file=str(self.body), subject="Run fixed: results", image=["fig=%s" % self.png])
        self.assertEqual(self.am.do_reply(self.ctx, args), 3)
        self.assertEqual((self.sent, fm.calls[0][0]), ([], "Run fixed: results"))    # reviewed under the new subject
        st.con.execute("update reviews set created_at=created_at-7200")
        self.am.do_flush_drafts(self.ctx, Args())
        msg = self.last()
        self.assertEqual((self.sent[-1][1], msg["In-Reply-To"]), (None, None))
        self.assertTrue(msg["Subject"].startswith("Run fixed: results [#"))
        text = msg.get_body(("plain",)).get_content()
        self.assertTrue(text.startswith("[Sent unchanged by the mailroom"))
        self.assertTrue(text.endswith('--\nContinues the thread "run failed". The mail this answers: '
                                      'https://mail.google.com/mail/?authuser=%s#search/rfc822msgid%%3Aq%%40x\n' % OWNER))
        html = msg.get_body(("html",)).get_content()
        self.assertLess(html.index("cid:fig"), html.index("Continues the thread"))

    def test_mail_to_others_is_reviewed_and_an_expired_draft_goes_without_the_note(self):
        fm = review.ask_model = FakeModel("revise")
        self.assertEqual(self.am.do_send(self.ctx, Args(to="sam@example.com", subject="data", body_file=str(self.body))), 3)
        self.assertIn("sam@example.com", fm.readers[0])
        self.assertEqual(self.sent, [])
        Store().con.execute("update reviews set created_at=created_at-7200")
        self.am.do_flush_drafts(self.ctx, Args())
        self.assertNotIn("Sent unchanged by the mailroom", self.last().get_body(("plain",)).get_content())
        self.assertEqual(self.last()["To"], "sam@example.com")

    def test_alex_by_name_is_reviewed_and_mailed_at_his_address(self):
        fm = review.ask_model = FakeModel("pass", "pass")
        self.assertEqual(self.am.do_send(self.ctx, Args(to="alex", subject="results", body_file=str(self.body))), 0)
        self.assertEqual((self.last()["To"], self.last()["Cc"]), (OWNER, None))
        self.assertEqual(self.am.do_send(self.ctx, Args(to="sam@example.com", cc="Alex", subject="data",
                                                        body_file=str(self.body))), 0)
        self.assertEqual((self.last()["To"], self.last()["Cc"]), ("sam@example.com", OWNER))
        self.assertEqual(fm.readers, [None, None])     # both reviewed as mail to Alex
        with patch.dict(os.environ), contextlib.redirect_stdout(io.StringIO()) as out:
            os.environ.pop("SSH_CONNECTION", None)
            self.assertEqual(self.am.do_send(self.ctx, Args(to="alex", subject="report", body_file=str(self.body),
                                                            unrouted=True)), 0)
        self.assertEqual(self.last()["To"], OWNER)
        self.assertTrue(out.getvalue().startswith("sent to %s | subject 'report'" % OWNER))

    def test_a_bare_name_that_is_not_alex_sends_nothing(self):
        fm = review.ask_model = FakeModel()
        with self.assertRaisesRegex(outbound.MailError, r"'jordan' is neither an address nor 'owner' \(or 'alex'"):
            self.am.do_send(self.ctx, Args(to="jordan", subject="results", body_file=str(self.body)))
        self.assertEqual((self.sent, fm.calls), ([], []))

    def test_withdraw_then_flush_sends_nothing(self):
        review.ask_model = FakeModel("revise")
        self.am.do_send(self.ctx, Args(to=OWNER, subject="results", body_file=str(self.body)))
        self.assertEqual(self.am.do_withdraw(self.ctx, Args()), 0)
        st = Store()
        st.con.execute("update reviews set created_at=created_at-7200")
        self.am.do_flush_drafts(self.ctx, Args())
        self.assertEqual(self.sent, [])
        self.assertEqual(st.latest_review(st.con.execute("select tag from reviews").fetchone()[0], "owner")["verdict"],
                         "withdrawn")

    def test_flush_sends_a_stale_draft_unchanged_with_a_note(self):
        review.ask_model = FakeModel("revise")
        self.am.do_send(self.ctx, Args(to=OWNER, subject="results", body_file=str(self.body), image=["fig=%s" % self.png]))
        st = Store()
        self.am.do_flush_drafts(self.ctx, Args())
        self.assertEqual(self.sent, [])            # not stale yet
        st.con.execute("update reviews set created_at=created_at-7200")
        self.am.do_flush_drafts(self.ctx, Args())
        msg = self.last()
        self.assertIn("Sent unchanged by the mailroom", msg.get_body(("plain",)).get_content())
        self.assertIn("[#", msg["Subject"])
        self.assertEqual([p.get_content_type() for p in msg.walk()].count("image/png"), 1)
        row = st.con.execute("select * from reviews order by id desc limit 1").fetchone()
        self.assertEqual((row["verdict"], row["sent_message_id"]), ("expired", "<g1@mail.gmail.com>"))
        self.am.do_flush_drafts(self.ctx, Args())
        self.assertEqual(len(self.sent), 1)        # sent once only

    def test_flush_drops_a_draft_whose_session_or_mail_is_gone_and_says_so(self):
        st = Store()
        inb = st.record_inbound(gmail_id="in-%d" % time.time_ns(), thread_id="thr", message_id="<q@x>", from_addr=OWNER,
                                from_name="Alex", subject="question", matched_tag=None, action="delivered", body="?")
        review.ask_model = FakeModel("revise", "revise")
        self.am.do_reply(self.ctx, Args(inbound_id=inb, body_file=str(self.body)))
        st.con.execute("delete from inbound where id=?", (inb,))
        st.con.execute("update reviews set created_at=created_at-7200")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.am.do_flush_drafts(self.ctx, Args())
        row = st.con.execute("select * from reviews order by id desc limit 1").fetchone()
        self.assertEqual((row["verdict"], row["draft"]), ("dropped", None))
        self.assertIn("#%d" % inb, row["reasons"])
        self.assertIn("dropped held draft 'question'", out.getvalue())

        self.am.do_send(self.ctx, Args(to=OWNER, subject="results", body_file=str(self.body)))
        st.con.execute("delete from sessions")
        st.con.execute("update reviews set created_at=created_at-7200")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.am.do_flush_drafts(self.ctx, Args())
            self.am.do_flush_drafts(self.ctx, Args())             # once only
        row = st.con.execute("select * from reviews order by id desc limit 1").fetchone()
        self.assertEqual(row["verdict"], "dropped")
        self.assertIn("session", row["reasons"])
        self.assertEqual(out.getvalue().count("dropped held draft 'results'"), 1)
        self.assertEqual(self.sent, [])

    def test_images_inline_and_attachments(self):
        review.ask_model = FakeModel("pass")
        html = Path(TMP) / "b.html"
        html.write_text('<html><body><p>Headline</p><img src="cid:fig"></body></html>')
        pdf = Path(TMP) / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 x")
        self.am.do_send(self.ctx, Args(to=OWNER, subject="r", body_file=str(self.body), html=str(html),
                             image=["fig=%s" % self.png, "extra=%s" % self.png], attach=[str(pdf)]))
        msg = self.last()
        types = [p.get_content_type() for p in msg.walk()]
        self.assertEqual(types[:3], ["multipart/mixed", "multipart/alternative", "text/plain"])
        self.assertIn("multipart/related", types)
        self.assertEqual(types.count("image/png"), 2)
        self.assertIn("application/pdf", types)
        h = msg.get_body(("html",)).get_content()
        self.assertIn('src="cid:extra"', h)             # unplaced image appended
        cids = sorted(p["Content-ID"] for p in msg.walk() if p.get_content_type() == "image/png")
        self.assertEqual(cids, ["<extra>", "<fig>"])

    def test_images_and_attachments_over_the_cap_together_are_refused(self):
        pdf = Path(TMP) / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 " + b"x" * 64)
        cap = max(len(PNG), pdf.stat().st_size) + 1      # each fits on its own; together they do not
        with patch.object(outbound, "MAX_IMAGES_BYTES", cap), \
                self.assertRaisesRegex(outbound.MailError, "images and attachments total over"):
            self.am.do_send(self.ctx, Args(to=OWNER, subject="r", body_file=str(self.body),
                                           image=["fig=%s" % self.png], attach=[str(pdf)]))
        self.assertEqual(self.sent, [])

    def test_bad_image_refused(self):
        bad = Path(TMP) / "x.png"
        bad.write_text("not an image")
        with self.assertRaisesRegex(outbound.MailError, "not a PNG"):
            outbound.load_images(["fig=%s" % bad])
        with self.assertRaisesRegex(outbound.MailError, "NAME=PATH"):
            outbound.load_images(["no-equals-sign"])
        with self.assertRaisesRegex(outbound.MailError, "image name 'a b'"):
            outbound.load_images(["a b=%s" % bad])


if __name__ == "__main__":
    unittest.main()
