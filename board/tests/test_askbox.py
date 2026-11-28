"""The Mac's ask box, its desktop side (agentboard/actions.py: voice_looking and voice_ask; bin/agent-board: Acts): the
question and the box's history put to a read-only copy of the agent, a Work agent asked as any other, the box's own
window passed over, neither web page offering the actions, a Codex agent offered and asked as a Claude agent is; the
answer spoken by the desktop's voice a sentence at a time as it is written, or each sentence with why not; then the real
`agent-board act` answering a question, its parts streamed before its answer and its answer before the copy has ended,
and the stream answering the board's other actions while questions are running. The CLIs and Kokoro are the only
stand-ins (FakeClaude and FakeCodex, with Codex's model end, in this process; a claude script on PATH for `agent-board
act`; fixtures.Voice for Kokoro behind the real daemon's socket); every file goes to a temporary folder."""
import base64
import importlib.machinery
import importlib.util
import json
import os
import queue
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stand_ins  # noqa: E402
from fixtures import codex_home, mailroom_db, write_jsonl  # noqa: E402
from fixtures import Voice, until  # noqa: E402
from stand_ins import MailroomAccounts, MailroomConfig, MailroomUsage  # noqa: E402
from test_voicedesk import A, B, C, T, FakeClaude, FakeCodex, Upstream, reply, row  # noqa: E402
from agentboard import actions, groups, mailweb, sources, speech, voicedesk, voiceweb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
Q = "What is left before you can merge the loader fix?"
SPEECH = Path(tempfile.mkdtemp())          # the voice's settings and log, for these tests


def setUpModule():
    """The desktop's voice as these tests have it: settings naming a socket of their own (no daemon on it unless a
    test starts one) and a log of their own. A copy's environment is the mailroom's scrub (relaunch.claude_env): its
    accounts module stands in, at stand_ins.MAILROOM, for the mailroom's, another component's."""
    (SPEECH / "speech.json").write_text(json.dumps({"socket": str(SPEECH / "speech.sock")}))
    for name, value in (("CONFIG", SPEECH / "speech.json"), ("LOG", SPEECH / "speech.log")):
        patch = mock.patch.object(speech, name, value)
        patch.start()
        unittest.addModuleCleanup(patch.stop)
    modules = stand_ins.mailroom(accounts=MailroomAccounts, usage=MailroomUsage, config=MailroomConfig({}))
    for name, value in (("mailroom_root", lambda: str(stand_ins.MAILROOM)), ("mailroom", modules)):
        patch = mock.patch.object(sources, name, value)
        patch.start()
        unittest.addModuleCleanup(patch.stop)


def act(req, **seams):
    """One request through actions.dispatch, as `agent-board act` runs it: no board rows, no worktrees."""
    return actions.dispatch(req, lambda: [], lambda: [], **seams)


class VoiceAsk(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cwd = self.d / "repo"
        self.cwd.mkdir()
        write_jsonl(self.d / "default/projects/-repo" / (A + ".jsonl"), [reply("claude-fable-5-2", cwd=str(self.cwd))])
        self.rows = [row(A, "loader-fix", cwd=str(self.cwd)), row(B, "other")]
        self.log = self.d / "asks.jsonl"

    def ask(self, fake, on_part=None, **req):
        """A voice_ask with `req` (the agent's id and Q by default), answered by `fake` for the claude CLI; its parts to
        on_part, as the stream has them, when given."""
        return act(dict({"action": "voice_ask", "agent": A, "question": Q}, **req), popen=fake,
                   claude_bin="/bin/claude", rows=self.rows, homes=[("default", self.d / "default")], log=self.log,
                   copies_path=self.d / "copies.json", **({"on_part": on_part} if on_part else {}))

    def test_the_question_goes_to_the_agents_copy_and_its_answer_comes_back(self):
        fake = FakeClaude()
        res = self.ask(fake)
        self.assertEqual((res["ok"], res["agent"], res["answer"]), (True, "loader-fix", "I am fixing the loader."))
        self.assertEqual(sorted(res), ["agent", "answer", "ok", "parts", "seconds"])
        primer, call = fake.calls                    # voicedesk.ask: a copy made, then the question forks it
        self.assertEqual(primer["input"], voicedesk.PREAMBLE + voicedesk.PRIMER)
        self.assertEqual(call["input"], voicedesk.PREAMBLE + Q)
        self.assertEqual(call["cwd"], str(self.cwd))
        self.assertEqual([(r["target"], r["question"]) for r in sources.read_jsonl(self.log)], [(A, Q)])

    def test_a_follow_up_carries_the_boxs_last_answers(self):
        fake = FakeClaude()
        history = [{"question": "What are you working on? ", "answer": " The loader's cache. "},
                   {"question": "What are the two choices?", "answer": "Keep it in memory, or on disk."},
                   {"question": "Which do you prefer?", "answer": ""}]
        self.assertTrue(self.ask(fake, question="and the second one?", history=history)["ok"])
        self.assertEqual(fake.calls[-1]["input"], voicedesk.PREAMBLE + "\n".join([
            "[Earlier in this ask box, Alex asked a copy of you these, and it answered:]",
            "Alex: What are you working on?\nYou: The loader's cache.\n",
            "Alex: What are the two choices?\nYou: Keep it in memory, or on disk.\n",
            "Alex: Which do you prefer?\nYou: \n",
            "[Alex's question now:]", "and the second one?"]))

    def test_the_history_is_cut_to_fit_the_longest_question(self):
        pair = lambda n, size: {"question": "q%d" % n, "answer": str(n) * size}
        limit = voicedesk.MAX_QUESTION
        text = actions.ask_text(Q, [pair(1, 1500), pair(2, 1500), pair(3, 1500)])
        self.assertLessEqual(len(text), limit)
        self.assertNotIn("q1", text)                  # the oldest pair is left out first
        self.assertEqual((text.count("2" * 1500), text.count("3" * 1500)), (1, 1))
        text = actions.ask_text("x" * 1000, [pair(1, 10), pair(2, 3500)])
        self.assertEqual(len(text), limit)            # a last pair too long keeps the start of its answer
        self.assertNotIn("q1", text)
        self.assertIn("\nYou: 2222", text)
        self.assertIn("2…\n", text)
        self.assertEqual(actions.ask_text("x" * 3700, [pair(1, 500)]), "x" * 3700)   # under 200 of it: left out
        self.assertEqual(actions.ask_text(Q, []), Q)
        fake = FakeClaude()                           # the box's history at its longest: the fork gets what fits
        res = self.ask(fake, question="y" * limit, history=[pair(n, actions.HISTORY_CHARS) for n in (1, 2, 3)])
        self.assertTrue(res["ok"], res)
        self.assertEqual(fake.calls[-1]["input"], voicedesk.PREAMBLE + "y" * limit)

    def test_what_will_not_do_is_refused_before_any_fork(self):
        fake = FakeClaude()
        long = "x" * (actions.HISTORY_CHARS + 1)
        for req, said in (({"agent": None}, "empty agent"), ({"agent": " "}, "empty agent"),
                          ({"agent": "a" * 101}, "agent too long or not text"),
                          ({"agent": ["x"]}, "agent too long or not text"),
                          ({"question": ""}, "empty question"), ({"question": "x" * 4001}, "question too long"),
                          ({"question": "a\x00b"}, "question too long or not text"),
                          ({"history": "earlier"}, "history must be a list"),
                          ({"history": [{"question": "q", "answer": "a"}] * 4}, "at most 3"),
                          ({"history": [{"question": "q"}]}, "history must be"),
                          ({"history": [{"question": "q", "answer": "a", "agent": A}]}, "history must be"),
                          ({"history": [{"question": " ", "answer": "a"}]}, "history must be"),
                          ({"history": [{"question": "q", "answer": None}]}, "history must be"),
                          ({"history": [{"question": "q", "answer": long}]}, "history must be"),
                          ({"history": [{"question": "q", "answer": "a\x00"}]}, "history must be")):
            res = self.ask(fake, **req)
            self.assertFalse(res["ok"], req)
            self.assertIn(said, res["error"], req)
        self.assertEqual(fake.calls, [])
        self.assertFalse(self.log.exists())

    def test_what_goes_wrong_is_said(self):
        res = self.ask(FakeClaude(), agent="nobody")
        self.assertEqual((res["ok"], res["error"]), (False, "no live agent called 'nobody'"))
        res = self.ask(FakeClaude(code=1, stdout=""))
        self.assertFalse(res["ok"])
        self.assertRegex(res["error"], r"loader-fix's copy failed \(exit 1\): boom")
        self.assertIn("seconds", res)
        self.assertTrue(self.ask(FakeClaude(), agent="Loader fix")["ok"])     # a name, as said, is taken too

    def test_the_answer_is_spoken_a_sentence_at_a_time_as_it_is_written(self):
        voice = Voice(SPEECH)
        self.addCleanup(voice.close)
        parts, said = [], []

        def on_part(part):
            parts.append(part)
            said.append(list(voice.said))
        fake = FakeClaude(answer="I am fixing the loader. It is nearly done, then I merge it.",
                          deltas=["I am fixing the", " loader. It is nearly", " done, then I merge it."])
        res = self.ask(fake, on_part=on_part)
        self.assertEqual((res["ok"], res["parts"]), (True, 2))
        self.assertEqual([(p["seq"], p["text"]) for p in parts], [(0, "I am fixing the loader."),
                                                                  (1, "It is nearly done, then I merge it.")])
        self.assertEqual(said[0], ["I am fixing the loader."])     # the first went before the second was asked for
        for p in parts:
            self.assertEqual(base64.b64decode(p["audio"])[:4], b"RIFF")
        self.assertIn("INFO spoke an answer of 58 characters in 2 parts", (SPEECH / "speech.log").read_text())

    def test_over_an_ssh_of_its_own_the_parts_come_in_the_answer(self):
        voice = Voice(SPEECH)
        self.addCleanup(voice.close)
        res = self.ask(FakeClaude(deltas=["I am fixing the loader."]))
        self.assertEqual([(p["seq"], p["text"], "audio" in p) for p in res["parts"]],
                         [(0, "I am fixing the loader.", True)])

    def test_with_the_voice_down_each_sentence_comes_with_why(self):
        parts = []
        res = self.ask(FakeClaude(deltas=["I am fixing the loader. ", "It is nearly done, then I merge it."]),
                       on_part=parts.append)
        self.assertTrue(res["ok"])
        self.assertEqual([p["text"] for p in parts], ["I am fixing the loader.", "It is nearly done, then I merge it."])
        for p in parts:
            self.assertNotIn("audio", p)
            self.assertIn("the desktop's voice (agent-speech) is not answering on %s" % (SPEECH / "speech.sock"),
                          p["error"])
        self.assertIn("WARNING the desktop's voice (agent-speech) is not answering",
                      (SPEECH / "speech.log").read_text())

    def excluding_work(self):
        """A voice page settings file whose excluded groups are Work's (the kit's default excludes none)."""
        path = self.d / "voice.json"
        path.write_text(json.dumps({"exclude_groups": ["Work"]}))
        return path

    def test_a_work_agent_is_asked_here_though_the_voice_page_refuses_it(self):
        real = groups.GROUPS
        groups.GROUPS = self.d / "board" / "groups.json"
        try:
            groups.save_groups([{"name": "Work", "members": [A], "subgroups": []}], live_ids={A})
            cfg = voiceweb.settings(self.excluding_work())
            self.assertIn(A, voiceweb.excluded(cfg))          # nothing of it goes to Gemini
            res = self.ask(FakeClaude())                      # nothing here goes to Google
            self.assertEqual((res["ok"], res["agent"]), (True, "loader-fix"))
        finally:
            groups.GROUPS = real


class VoiceLooking(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.db = self.d / "mailroom.sqlite"
        con = mailroom_db(self.db)
        con.execute("insert into sessions values (?, ?, ?, ?, ?, ?, ?)",
                    ("ab12cd34", "claude", A, "loader-fix", "/w", 0, ""))
        con.commit()
        self.rows = [row(A, "loader-fix", cwd="/w/loader"), row(C, "survey-model", cwd="/p", status="busy")]
        self.agents = [{"name": "loader-fix", "id": A, "status": "idle", "folder": "loader"},
                       {"name": "survey-model", "id": C, "status": "busy", "folder": "p"}]

    def look(self, windows, ok=True):
        seen = {"ok": True, "windows": windows, "web": [], "afk": "not-afk"} if ok else {"ok": False,
                                                                                         "error": windows}
        return act({"action": "voice_looking"}, rows=self.rows, seen=seen, db=self.db)

    def test_the_agent_behind_the_window_before_the_boxs_own(self):
        gmail = "Re: Loader cache: two choices [#ab12cd34] - owner@example.com - Gmail"
        res = self.look([{"app": "Agent Board", "title": "Agent Front Desk — Ask loader-fix"},
                         {"app": "Google Chrome", "title": gmail}])
        self.assertTrue(res["ok"])
        self.assertEqual((res["window"]["app"], res["window"]["title"]), ("Google Chrome", gmail))
        self.assertEqual((res["agent"]["id"], res["agent"]["name"], res["agent"]["live"], res["agent"]["how"]),
                         (A, "loader-fix", True, "mail tag [#ab12cd34]"))
        self.assertEqual(res["agents"], self.agents)

    def test_with_no_agent_found_the_live_agents_are_offered(self):
        res = self.look([{"app": "Finder", "title": "Downloads"}])
        self.assertEqual((res["ok"], res["agent"]), (True, None))
        self.assertEqual(res["said"], "You're looking at Finder: Downloads. No agent found behind it.")
        self.assertEqual(res["agents"], self.agents)

    def test_a_mac_that_cannot_be_read_is_said_and_the_agents_still_offered(self):
        res = self.look("ActivityWatch is not answering", ok=False)
        self.assertEqual((res["ok"], res["error"]), (False, "ActivityWatch is not answering"))
        self.assertEqual(res["agents"], self.agents)


class CodexFromTheBox(unittest.TestCase):
    """A Codex agent from the box: voicedesk.agents lists it and voicedesk.ask picks its CLI, so the box offers and asks
    one as it does a Claude agent; eval-consolidation, in Work, among them. The live agents are read for real
    (voicedesk.agents over a Codex home whose thread's writer lock is held, as a running Codex holds it); the codex CLI
    and the model's end are the stand-ins test_voicedesk.CodexAsk uses."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cwd, self.home = self.d / "evals", self.d / "codex"
        self.cwd.mkdir()
        rollout = write_jsonl(self.d / "rollout.jsonl", [
            {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "cwd": str(self.cwd)}}])
        os.utime(rollout, (time.time() - 600, time.time() - 600))      # written 10 minutes ago: an idle agent
        held = codex_home(self.home, [{"id": T, "name": "eval-consolidation", "cwd": str(self.cwd),
                                       "model": "gpt-6.1-sol", "rollout_path": str(rollout)}], held=[T])
        for fh in held:
            self.addCleanup(fh.close)
        (self.home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-6.1-sol",
                                                                              "tool_mode": "code_mode_only"}]}))
        self.rows = voicedesk.agents(claude=lambda: [], codex=sources.CodexReader(self.home),
                                     orch_dir=str(self.d / "o"))
        self.up = Upstream()
        self.addCleanup(self.up.close)
        patch = mock.patch.object(voicedesk, "CODEX_MODELS", self.d / "models")     # the copy's model catalogs
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_picker_offers_it_and_looking_names_it(self):
        res = act({"action": "voice_looking"}, rows=self.rows, db=self.d / "none.sqlite",
                  seen={"ok": True, "windows": [{"app": "Finder", "title": "Downloads"}], "web": []})
        self.assertEqual(res["agents"], [{"name": "eval-consolidation", "id": T, "status": "idle", "folder": "evals"}])
        res = act({"action": "voice_looking"}, rows=self.rows, db=self.d / "none.sqlite",
                  seen={"ok": True, "windows": [{"app": "Code", "title": "eval-consolidation: merge plan"}], "web": []})
        self.assertEqual((res["agent"]["cli"], res["agent"]["id"], res["agent"]["live"]), ("codex", T, True))

    def test_a_codex_work_agent_is_asked_with_the_boxs_history(self):
        real = groups.GROUPS
        groups.GROUPS = self.d / "board" / "groups.json"
        self.addCleanup(setattr, groups, "GROUPS", real)
        groups.save_groups([{"name": "Work", "members": [T], "subgroups": []}], live_ids={T})
        excluding = self.d / "voice.json"           # the kit's default excludes no group
        excluding.write_text(json.dumps({"exclude_groups": ["Work"]}))
        self.assertIn(T, voiceweb.excluded(voiceweb.settings(excluding)))
        fake = FakeCodex()
        history = [{"question": "What are you consolidating?", "answer": "The three result tables."}]
        res = act({"action": "voice_ask", "agent": T, "question": "and the third?", "history": history}, popen=fake,
                  codex_bin="/bin/codex", rows=self.rows, log=self.d / "asks.jsonl", copies_path=self.d / "copies.json",
                  upstream=self.up.url)
        self.assertEqual((res["ok"], res["agent"], res["answer"]), (True, "eval-consolidation",
                                                                    "I am checking the report."), res)
        self.assertEqual([p["text"] for p in res["parts"]], ["I am checking the report."])   # whole: no pieces
        [call] = fake.calls
        self.assertEqual(call["argv"][:3] + call["argv"][-2:], ["/bin/codex", "exec", "fork", T, "-"])
        self.assertEqual(call["input"], voicedesk.CODEX_PREAMBLE + actions.ask_text("and the third?", history))


class Reach(unittest.TestCase):
    def test_neither_web_page_offers_them(self):
        box = ("voice_looking", "voice_ask", "desk_say", "desk_new")       # the desk's too: it can send as the owner
        for api in (mailweb.API, voiceweb.API):
            for action in box:
                self.assertIn(action, actions.ACTIONS)
                self.assertNotIn(action, api)
        self.assertFalse(set(box) & set(mailweb.MAIL_ACTIONS + mailweb.DRAFT_ACTIONS))

    def test_the_box_is_titled_so_that_looking_passes_over_it_and_sends_the_history_taken_here(self):
        swift = (ROOT / "mac/Sources/AgentBoard/AskBox.swift").read_text()
        rule = lambda name: re.search(r'static let %s = "?([^"\n]*)' % name, swift).group(1)
        self.assertEqual(rule("titlePrefix"), voicedesk.VOICE_TITLE)
        self.assertTrue(voicedesk.voice_page({"title": voicedesk.VOICE_TITLE + " — Ask loader-fix"}))
        self.assertEqual(int(rule("historyPairs")), actions.HISTORY_PAIRS)
        self.assertLessEqual(int(rule("historyChars")), actions.HISTORY_CHARS)


FAKE_CLAUDE = """#!%(python)s
# The claude CLI, as `claude -p --output-format stream-json --verbose` answers: its init, then its result. A question
# but the copy's primer is answered after %(seconds)s s. One asking for SENTENCES is answered with them, streamed with
# --include-partial-messages, and the fork takes %(linger)s s to end after its result, as claude takes ~0.9 s, then
# leaves a file ENDED in its folder.
import json, sys, time
prompt = sys.stdin.read()
if %(primer)r not in prompt:
    time.sleep(%(seconds)s)
sid = %(copy)r
answer = %(sentences)r if "SENTENCES" in prompt else "ok"
print(json.dumps({"type": "system", "subtype": "init", "tools": ["Glob", "Grep", "Read"], "mcp_servers": [],
                  "permissionMode": "dontAsk", "session_id": sid}), flush=True)
if "--include-partial-messages" in sys.argv:
    for word in answer.split(" "):
        print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {
            "type": "text_delta", "text": word + " "}}}), flush=True)
        time.sleep(0.05)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": answer, "session_id": sid,
                  "num_turns": 1, "permission_denials": [], "usage": {"input_tokens": 4, "output_tokens": 1}}),
      flush=True)
if "SENTENCES" in prompt:
    time.sleep(%(linger)s)
    open("ENDED", "w").close()
"""


class ActOverTheStream(unittest.TestCase):
    """`agent-board act` run for real, as the stream runs each action (run_act_parts), under a home of its own that
    holds one live agent, loader-fix (this process stands in for its claude process), with a stand-in claude CLI first
    on PATH, and a mailroom install with no modules (stand_ins.MAILROOM's agent-spawn) before the live one."""
    SECONDS = 3
    LINGER = 4
    SENTENCES = "I am fixing the loader. It is nearly done, then I merge it."

    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("agent_board_cli", str(ROOT / "bin" / "agent-board"))
        self.cli = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.cli)
        self.home = Path(tempfile.mkdtemp())
        work, bin_dir = self.home / "work", self.home / ".local" / "bin"
        work.mkdir()
        bin_dir.mkdir(parents=True)
        sock = self.home / "inbox.sock"
        sock.write_text("")
        (self.home / ".claude" / "sessions").mkdir(parents=True)
        (self.home / ".claude" / "sessions" / "1.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": A, "name": "loader-fix", "cwd": str(work), "status": "idle",
            "kind": "interactive", "entrypoint": "cli", "messagingSocketPath": str(sock),
            "statusUpdatedAt": int(time.time() * 1000)}))
        write_jsonl(self.home / ".claude/projects/-work" / (A + ".jsonl"), [reply("claude-fable-5-2", cwd=str(work))])
        claude = bin_dir / "claude"
        claude.write_text(FAKE_CLAUDE % {"python": sys.executable, "seconds": self.SECONDS, "primer": voicedesk.PRIMER,
                                         "copy": C, "linger": self.LINGER, "sentences": self.SENTENCES})
        self.work = work
        claude.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "TMUX", "AGENT_BOARD"))}
        self.env = mock.patch.dict(os.environ, dict(env, HOME=str(self.home), PATH=os.pathsep.join(
            (str(bin_dir), str(stand_ins.MAILROOM / "bin"), os.environ["PATH"]))), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_agent_board_act_answers_a_question(self):
        out, err = self.cli.run_act({"action": "voice_ask", "agent": A, "question": "Reply with the single word: ok"})
        res = json.loads(out or "{}")
        self.assertEqual((res.get("ok"), res.get("agent"), res.get("answer")), (True, "loader-fix", "ok"), err or out)
        logged = sources.read_jsonl(self.home / ".local/state/agent-board/voice-desk/asks.jsonl")
        self.assertEqual([(r["question"], r["ok"]) for r in logged], [("Reply with the single word: ok", True)])

    def test_an_answer_comes_in_parts_and_then_whole_before_the_copy_has_ended(self):
        sock = self.home / "speech.sock"            # the default, deep in a test's home, may pass a socket's 107 bytes
        (self.home / ".config/agent-board").mkdir(parents=True)
        (self.home / ".config/agent-board/speech.json").write_text(json.dumps({"socket": str(sock)}))
        voice = Voice(self.home, socket=sock)
        self.addCleanup(voice.close)
        answers, (wake_r, wake_w) = queue.Queue(), os.pipe()
        self.addCleanup(os.close, wake_r)
        self.addCleanup(os.close, wake_w)
        acts = self.cli.Acts(answers, wake_w)
        acts.submit(7, {"action": "voice_ask", "agent": A, "question": "Two SENTENCES, please."})
        got = []
        try:
            while not got or got[-1][0]["type"] != "act":
                got.append((answers.get(timeout=60), (self.work / "ENDED").exists()))
        finally:
            acts.shutdown()
        *parts, (answer, ended) = got
        self.assertEqual([(m["type"], m["rid"], m["part"]["seq"], m["part"]["text"]) for m, _ in parts],
                         [("act_part", 7, 0, "I am fixing the loader."),
                          ("act_part", 7, 1, "It is nearly done, then I merge it.")])
        for m, _ in parts:
            self.assertEqual(base64.b64decode(m["part"]["audio"])[:4], b"RIFF")
        self.assertEqual((answer["rid"], json.loads(answer["out"])["parts"]), (7, 2))
        self.assertEqual(json.loads(answer["out"])["answer"], self.SENTENCES)
        self.assertFalse(ended)                             # the copy was still ending: the answer did not wait for it
        self.assertTrue(until(lambda: (self.work / "ENDED").exists(), self.LINGER + 5))

    def answer_of(self, req, **kw):
        """What run_act_parts answers `req`: [(stdout, stderr)]."""
        got = []
        self.cli.run_act_parts(req, lambda part: None, lambda out, err: got.append((out, err)), **kw)
        return got

    def test_an_act_over_its_time_is_stopped_and_said(self):
        self.assertEqual(self.answer_of({"action": "voice_ask", "agent": A, "question": "Too slow?"}, timeout=1),
                         [("", "the action took over 1 s and was stopped")])

    def test_an_act_that_ends_with_no_answer_says_what_it_printed(self):
        said = [sys.executable, "-c", "import sys; print('Traceback: boom'); sys.stderr.write('its stderr\\n'); "
                                      "sys.exit(3)"]
        self.assertEqual(self.answer_of({"action": "sent"}, command=said), [("", "its stderr\nTraceback: boom\n")])
        quiet = [sys.executable, "-c", "import sys; sys.exit(3)"]
        self.assertEqual(self.answer_of({"action": "sent"}, command=quiet),
                         [("", "the action ended with no answer (exit 3)")])

    def test_a_failure_after_the_answer_goes_on_the_stream_as_an_error(self):
        def broke(argv, text, on_event, **kw):           # the act process answers, then its pipe breaks
            on_event({"ok": True, "answer": "ok"})
            raise BrokenPipeError(32, "Broken pipe")
        answers, (wake_r, wake_w) = queue.Queue(), os.pipe()
        self.addCleanup(os.close, wake_r)
        self.addCleanup(os.close, wake_w)
        acts = self.cli.Acts(answers, wake_w)
        with mock.patch.object(self.cli.sources, "run_json_lines", broke):
            acts.submit(5, {"action": "sent"})
            first, second = answers.get(timeout=30), answers.get(timeout=30)
        acts.shutdown()
        self.assertEqual((first["type"], first["rid"], json.loads(first["out"])),
                         ("act", 5, {"ok": True, "answer": "ok"}))
        self.assertEqual(second, {"type": "error", "error": "action 5 failed after its answer: BrokenPipeError: "
                                                            "[Errno 32] Broken pipe"})

    def test_the_boards_other_actions_are_answered_while_questions_run(self):
        answers, (wake_r, wake_w) = queue.Queue(), os.pipe()
        self.addCleanup(os.close, wake_r)
        self.addCleanup(os.close, wake_w)
        acts = self.cli.Acts(answers, wake_w)
        started = time.time()
        for rid in range(1, self.cli.ACT_WORKERS + 1):      # more questions than the other actions have workers
            acts.submit(rid, {"action": "voice_ask", "agent": A, "question": "Question %d?" % rid})
        acts.submit(99, {"action": "sent", "limit": 1})
        got = []
        try:
            while len(got) < self.cli.ACT_WORKERS + 1:
                msg = answers.get(timeout=60)
                if msg["type"] == "act":            # not the parts of an answer ("ok": one each)
                    got.append((msg, time.time() - started))
        finally:
            acts.shutdown()
        (first, at), asks = got[0], [a for a, _ in got[1:]]
        self.assertEqual(first["rid"], 99, got)
        self.assertLess(at, self.SECONDS)
        self.assertEqual(json.loads(first["out"])["messages"], [])
        self.assertEqual(sorted(a["rid"] for a in asks), [1, 2, 3, 4])
        for a in asks:
            self.assertEqual(json.loads(a["out"])["answer"], "ok", a)


if __name__ == "__main__":
    unittest.main()
