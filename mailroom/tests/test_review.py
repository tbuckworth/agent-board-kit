import base64
import email
import email.policy
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
os.environ["AGENT_MAIL_STATE_DIR"] = TMP
os.environ["AGENT_MAIL_CONFIG_DIR"] = TMP          # defaults only: no real token, no real config
os.environ["CLAUDE_CODE_SESSION_ID"] = "33333333-3333-3333-3333-333333333333"
Path(TMP, "config.json").write_text(json.dumps({"account": "agents@example.com", "allowlist": ["owner@example.com"],
                                                "owner_name": "Alex"}))
from agentmail import config, review  # noqa: E402
from agentmail.store import Store  # noqa: E402

OWNER = "owner@example.com"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
LONG = "Results of the run.\n\n" + " ".join("word%d" % i for i in range(200)) + "\n"


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FakeModel:
    """Stands in for review.ask_model: returns the queued verdicts in order, or raises."""
    def __init__(self, *verdicts):
        self.verdicts, self.calls, self.readers = list(verdicts), [], []

    def __call__(self, cfg, subject, shown, notes=(), reader=None):
        self.calls.append((subject, shown, list(notes)))
        self.readers.append(reader)
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


class Checks(unittest.TestCase):
    def test_lint_data_uri_local_file_and_missing_cid(self):
        out = review.lint("t", '<img src="data:image/png;base64,AA"><img src="/tmp/x.png"><img src="cid:fig">', [])
        self.assertEqual(len(out), 3)
        self.assertEqual(review.lint("t", '<img src="cid:fig">', [("fig", PNG)]), [])

    def test_hints(self):
        h = review.hints("The plot is at /home/alex/tmp/p.png.\n\n| a | b |\n| 1 | 2 |\n")
        self.assertTrue(any("/home/alex/tmp/p.png" in x for x in h))
        self.assertTrue(any("pipe-separated" in x for x in h))
        self.assertEqual(review.hints("Short and clean."), [])
        self.assertEqual(review.hints("x", "<h2>Results</h2><pre># install\npip install x</pre><code>**kw</code>"
                                           "<table><tr><th># runs</th></tr></table>"), [])
        self.assertTrue(review.hints("x", "<p>**Bottom line** it works</p>"))
        self.assertEqual(review.hints("x", "<p>See <b>item</b> # 5 here</p>"), [])


class AskModel(unittest.TestCase):
    """The claude -p call: the first ready login, then the next (a work seat), then the API key; a timeout ends it."""
    GOOD = json.dumps({"total_cost_usd": 0.002, "structured_output": {
        "kind": "answer", "predicted_reply": "", "verdict": "pass", "issues": []}})
    LIMIT = json.dumps({"is_error": True, "result": "You've hit your usage limit"})

    def setUp(self):
        self.calls, self.outputs = [], []
        self.work = tempfile.mkdtemp()
        Path(self.work, ".credentials.json").write_text("{}")
        self.cfg = dict(config.load(), owner_name="Alex", usage={
            "claude_accounts": {"personal": {"config_dir": None}, "work": {"config_dir": self.work}},
            "order": ["claude:personal", "claude:work", "codex"]})
        self.saved = (review.subprocess.run, review._claude_cmd, review.orchestrate.fallback_available,
                      review.orchestrate.fallback_command, review.orchestrate._fallback_env)
        review._claude_cmd = lambda cfg, prompt: ["claude", "-p"]
        review.orchestrate.fallback_available = lambda cfg: True
        review.orchestrate.fallback_command = lambda cfg, cmd: ["bws", "run", "--"] + cmd
        review.orchestrate._fallback_env = lambda cfg, env: {"BWS_ACCESS_TOKEN": "t"}

        def run(cmd, **kw):
            self.calls.append((cmd, kw.get("env"), kw.get("input")))
            out = self.outputs.pop(0)
            if isinstance(out, Exception):
                raise out
            return review.subprocess.CompletedProcess(cmd, 1 if "is_error" in out else 0, out, "")
        review.subprocess.run = run

    def tearDown(self):
        (review.subprocess.run, review._claude_cmd, review.orchestrate.fallback_available,
         review.orchestrate.fallback_command, review.orchestrate._fallback_env) = self.saved

    def test_login_order_and_environments(self):
        os.environ["CLAUDE_CONFIG_DIR"], os.environ["ANTHROPIC_API_KEY"] = "/elsewhere", "k"   # the sender's own
        try:
            got = review.logins(self.cfg, ["claude", "-p"])
        finally:
            del os.environ["CLAUDE_CONFIG_DIR"], os.environ["ANTHROPIC_API_KEY"]
        self.assertEqual([g[0] for g in got], ["personal", "work", "api-key"])
        personal, work = got[0][2], got[1][2]
        self.assertNotIn("CLAUDE_CONFIG_DIR", personal)
        self.assertNotIn("ANTHROPIC_API_KEY", personal)
        self.assertEqual(work["CLAUDE_CONFIG_DIR"], str(Path(self.work).resolve()))
        self.assertEqual(got[2][1], ["bws", "run", "--", "claude", "-p"])

    def test_first_login_first(self):
        self.outputs = [self.GOOD]
        res, info = review.ask_model(self.cfg, "s", "body")
        self.assertEqual((res["verdict"], info["auth"], len(self.calls)), ("pass", "personal", 1))

    def test_usage_limit_moves_to_the_next_login_then_the_key(self):
        self.outputs = [self.LIMIT, self.GOOD]
        self.assertEqual(review.ask_model(self.cfg, "s", "body")[1]["auth"], "work")
        self.calls, self.outputs = [], [self.LIMIT, self.LIMIT, self.GOOD]
        res, info = review.ask_model(self.cfg, "s", "body")
        self.assertEqual((res["verdict"], info["auth"]), ("pass", "api-key"))
        self.assertIn("usage limit", info["failed"])
        self.assertEqual(self.calls[2][:2], (["bws", "run", "--", "claude", "-p"], {"BWS_ACCESS_TOKEN": "t"}))

    def test_all_logins_failing_or_a_timeout_fails(self):
        self.outputs = [self.LIMIT, self.LIMIT, self.LIMIT]
        with self.assertRaises(RuntimeError):
            review.ask_model(self.cfg, "s", "body")
        self.calls, self.outputs = [], [review.subprocess.TimeoutExpired("claude", 40)]
        with self.assertRaises(review.subprocess.TimeoutExpired):
            review.ask_model(self.cfg, "s", "body")
        self.assertEqual(len(self.calls), 1)

    def test_outside_reader_gets_the_outside_prompt(self):
        seen = []
        review._claude_cmd = lambda cfg, prompt: seen.append(prompt) or ["claude", "-p"]
        self.outputs = [self.GOOD]
        review.ask_model(self.cfg, "s", "body", reader="Sam's team")
        self.assertIn("someone other than\nAlex", seen[0])
        self.assertTrue(self.calls[0][2].startswith("To: Sam's team\nSubject: s"))


class Gate(unittest.TestCase):
    def setUp(self):
        self.st = Store(Path(TMP) / ("gate-%s.sqlite" % self.id().rsplit(".", 1)[-1]))
        self.cfg = dict(config.load(), allowlist=[OWNER], owner_name="Alex")
        self.real = review.ask_model

    def tearDown(self):
        review.ask_model = self.real

    def gate(self, text=LONG, to=OWNER, **kw):
        return review.gate(self.cfg, self.st, "tagA", [to], "Run results", text, None, (), draft={"op": "send"}, **kw)

    def rows(self):
        return [dict(r) for r in self.st.con.execute("select * from reviews order by id")]

    def test_mail_to_others_is_reviewed_for_its_reader(self):
        self.cfg["collaborators"] = {"sam.agent@example.com": {"owner": "Sam", "share_data_with": ["sam.agent@example.com", "sam@example.com"]}}
        fm = review.ask_model = FakeModel("pass", "pass", "pass", "pass")
        self.gate(to="sam.agent@example.com")
        self.gate(to="Sam <sam@example.com>")
        self.gate(to="someone@example.com")
        self.gate(to="someone@example.com, " + OWNER)                    # the owner among them: the owner's rules
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

    def test_a_held_mail_to_one_reader_does_not_let_the_next_to_another_through(self):
        self.cfg["collaborators"] = {"sam.agent@example.com": {"owner": "Sam", "share_data_with": ["sam.agent@example.com", "sam@example.com"]}}
        review.ask_model = FakeModel("revise", "revise", "revise")
        self.assertIn("expects them", self.gate(to="sam.agent@example.com")[1])   # held, to Sam's team
        self.assertTrue(self.gate()[1])                                      # to the owner: its own round 1, held
        rid, stop, tips = self.gate()                                        # to the owner, round 2: goes out
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
        self.assertEqual(self.rows()[0]["verdict"], "error")

    def test_model_failure_still_holds_a_broken_image(self):
        review.ask_model = FakeModel(RuntimeError("timeout"))
        rid, stop, tips = review.gate(self.cfg, self.st, "tagA", [OWNER], "s", LONG, '<p>x</p><img src="cid:nope">', (),
                                draft={})
        self.assertIn("cid:nope", stop)

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


class FakeGmail:
    def __init__(self):
        self.sent = []

    def service(self):
        return "svc"

    def send_raw(self, svc, raw, thread_id=None):
        self.sent.append((raw, thread_id))
        return {"id": "g%d" % len(self.sent), "threadId": thread_id or "t%d" % len(self.sent)}

    def get_metadata(self, svc, gid, names):
        return {"payload": {"headers": [{"name": "Message-ID", "value": "<%s@mail.gmail.com>" % gid}]}}


class Args:
    def __init__(self, **kw):
        self.__dict__.update(dict(body_file=None, html=None, session=None, cli=None, cc=None, unrouted=False,
                                  image=None, attach=None, no_review=None))
        self.__dict__.update(kw)


class AgentMail(unittest.TestCase):
    def setUp(self):
        self.am = load_script("agent-mail")
        self.fake = FakeGmail()
        for name in ("service", "send_raw", "get_metadata"):
            setattr(self.am.gmail, name, getattr(self.fake, name))
        self.am.deliver.registry = lambda: []
        self.real = review.ask_model
        self.body = Path(TMP) / "long.txt"
        self.body.write_text(LONG)
        self.png = Path(TMP) / "plot.png"
        self.png.write_bytes(PNG)
        self.am.Store().con.execute("delete from reviews")

    def tearDown(self):
        review.ask_model = self.real

    def last(self):
        raw, thread_id = self.fake.sent[-1]
        return email.message_from_bytes(raw, policy=email.policy.default)

    def test_revise_sends_nothing_and_exits_3(self):
        review.ask_model = FakeModel("revise", "pass")
        self.assertEqual(self.am.do_send(Args(to=OWNER, subject="results", body_file=str(self.body))), 3)
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(self.am.do_send(Args(to=OWNER, subject="results", body_file=str(self.body))), 0)
        self.assertEqual(len(self.fake.sent), 1)
        row = self.am.Store().con.execute("select * from reviews order by id desc limit 1").fetchone()
        self.assertEqual(row["sent_message_id"], "<g1@mail.gmail.com>")

    def test_mail_to_others_is_reviewed_and_an_expired_draft_goes_without_the_note(self):
        fm = review.ask_model = FakeModel("revise")
        self.assertEqual(self.am.do_send(Args(to="sam@example.com", subject="data", body_file=str(self.body))), 3)
        self.assertIn("sam@example.com", fm.readers[0])
        self.assertEqual(self.fake.sent, [])
        self.am.Store().con.execute("update reviews set created_at=created_at-7200")
        self.am.do_flush_drafts(Args())
        self.assertNotIn("Sent unchanged by the mailroom", self.last().get_body(("plain",)).get_content())
        self.assertEqual(self.last()["To"], "sam@example.com")

    def test_withdraw_then_flush_sends_nothing(self):
        review.ask_model = FakeModel("revise")
        self.am.do_send(Args(to=OWNER, subject="results", body_file=str(self.body)))
        self.assertEqual(self.am.do_withdraw(Args()), 0)
        st = self.am.Store()
        st.con.execute("update reviews set created_at=created_at-7200")
        self.am.do_flush_drafts(Args())
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(st.latest_review(st.con.execute("select tag from reviews").fetchone()[0])["verdict"],
                         "withdrawn")

    def test_flush_sends_a_stale_draft_unchanged_with_a_note(self):
        review.ask_model = FakeModel("revise")
        self.am.do_send(Args(to=OWNER, subject="results", body_file=str(self.body), image=["fig=%s" % self.png]))
        st = self.am.Store()
        self.am.do_flush_drafts(Args())
        self.assertEqual(self.fake.sent, [])            # not stale yet
        st.con.execute("update reviews set created_at=created_at-7200")
        self.am.do_flush_drafts(Args())
        msg = self.last()
        self.assertIn("Sent unchanged by the mailroom", msg.get_body(("plain",)).get_content())
        self.assertIn("[#", msg["Subject"])
        self.assertEqual([p.get_content_type() for p in msg.walk()].count("image/png"), 1)
        row = st.con.execute("select * from reviews order by id desc limit 1").fetchone()
        self.assertEqual((row["verdict"], row["sent_message_id"]), ("expired", "<g1@mail.gmail.com>"))
        self.am.do_flush_drafts(Args())
        self.assertEqual(len(self.fake.sent), 1)        # sent once only

    def test_images_inline_and_attachments(self):
        review.ask_model = FakeModel("pass")
        html = Path(TMP) / "b.html"
        html.write_text('<html><body><p>Headline</p><img src="cid:fig"></body></html>')
        pdf = Path(TMP) / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 x")
        self.am.do_send(Args(to=OWNER, subject="r", body_file=str(self.body), html=str(html),
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

    def test_bad_image_refused(self):
        bad = Path(TMP) / "x.png"
        bad.write_text("not an image")
        with self.assertRaises(SystemExit):
            self.am.load_images(["fig=%s" % bad])
        with self.assertRaises(SystemExit):
            self.am.load_images(["no-equals-sign"])


if __name__ == "__main__":
    unittest.main()
