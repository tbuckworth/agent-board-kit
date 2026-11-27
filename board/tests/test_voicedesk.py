"""The voice desk (agentboard/voicedesk.py, mac/voice-desk/aw_front.py): the read-only fork's command and
environment, the agent and transcript it copies, its answer and log, the front window mapped to an agent (mail tags and
subjects in a real mailroom database, pull requests against real git worktrees), and the Mac's script reading a real
HTTP stand-in for ActivityWatch. The claude CLI, gh and ssh are stand-ins: a real fork costs allowance, gh needs GitHub
and ssh needs the Mac. Every file goes to a temporary folder."""
import http.server
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (first: a scratch HOME and config, an example owner)
import fixtures  # noqa: E402
import stand_ins  # noqa: E402
from fixtures import git, mailroom_db, repo_with_worktree, write_jsonl  # noqa: E402
from agentboard import sources, voicedesk  # noqa: E402

# fixtures.until, kept here until fixtures.py carries it (the ask box's suites add it there)
if not hasattr(fixtures, "until"):
    def until(check, seconds=10):
        """check() once it is true, or after `seconds`: whether it came true, for what another thread or process
        does."""
        ends = time.time() + seconds
        while not check() and time.time() < ends:
            time.sleep(0.01)
        return check()
    fixtures.until = until
until = fixtures.until

MAILROOM = (sources.mailroom_root, sources.mailroom)


def setUpModule():
    """A fork's environment is the mailroom's scrub of the board's (relaunch.claude_env): its accounts module stands in
    here, at stand_ins.MAILROOM, for the live install, another repo's, with its own tests."""
    sources.mailroom_root = lambda: str(stand_ins.MAILROOM)
    sources.mailroom = stand_ins.mailroom(accounts=stand_ins.MailroomAccounts, usage=stand_ins.MailroomUsage,
                                          config=stand_ins.MailroomConfig({}))


def tearDownModule():
    sources.mailroom_root, sources.mailroom = MAILROOM

A, B, C = ("a1b2c3d4-0000-4000-8000-00000000000a", "b2c3d4e5-0000-4000-8000-00000000000b",
           "c3d4e5f6-0000-4000-8000-00000000000c")       # session ids
T, F = "01a00000-0000-7000-8000-00000000000d", "01a00000-0000-7000-8000-00000000000f"   # a Codex thread, its fork
MAC_SCRIPT = voicedesk.MAC_SCRIPT
# the Mac runs /usr/bin/python3, 3.9: its script is run under the oldest Python here that is no newer
PY39 = next((p for p in (shutil.which("python3.9"),
                         str(Path.home() / ".local/share/uv/python/cpython-3.9-linux-x86_64-gnu/bin/python3.9"),
                         "/usr/bin/python3.8") if p and os.path.exists(p)), sys.executable)


def row(sid, name, cwd="/x", **kw):
    """An agent as voicedesk.agents() gives it."""
    return dict({"name": name, "id": sid, "status": "idle", "cwd": cwd, "folder": os.path.basename(cwd),
                 "account": "default", "config_dir": str(sources.DEFAULT_CLAUDE_HOME)}, **kw)


def registry(sid, name, **kw):
    """A row as sources.claude_sessions_all gives it: a live interactive agent."""
    return dict({"cli": "claude", "id": sid, "name": name, "cwd": "/x", "raw_status": "idle", "kind": "interactive",
                 "live": True, "pid": 1, "entrypoint": "cli", "status_at": 100, "account": "default",
                 "config_dir": "/h/.claude"}, **kw)


def reply(model, inp=3, read=90000, wrote=500, cwd="/w/repo"):
    return {"type": "assistant", "cwd": cwd, "message": {"model": model, "role": "assistant", "usage": {
        "input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": wrote, "output_tokens": 9}}}


class FakeClaude:
    """Stands in for subprocess.Popen running the claude CLI: keeps each call's argv, keywords and what it was given on
    stdin ("input"), and prints what `claude -p --output-format stream-json --verbose` prints: its init event (its
    `tools`, MCP `servers` and permission `mode`) and its result (`answer`, as session `session`), then exits `code`;
    with `code` it prints "boom" too (its stderr, which the fork merges in). `before` are events printed ahead of the
    init; `deltas`, pieces of the answer printed between the init and the result as `--include-partial-messages` prints
    them (after a thinking delta); `stdout` replaces the events; `hang` prints nothing until the fork is killed; with
    `linger`, it keeps running after its last event until `ended` is set (as claude takes ~0.9 s to exit)."""

    def __init__(self, answer="I am fixing the loader.", code=0, stdout=None, tools=("Glob", "Grep", "Read"),
                 servers=(), mode="dontAsk", usage=None, session=C, hang=False, before=(), deltas=(), linger=False):
        self.calls, self.code, self.hang, self.linger, self.ended = [], code, hang, linger, threading.Event()
        usage = usage or {"input_tokens": 4, "cache_creation_input_tokens": 120000, "cache_read_input_tokens": 0,
                          "output_tokens": 40, "iterations": [{"cache_creation_input_tokens": 119000,
                                                               "cache_read_input_tokens": 0}]}
        init = {"type": "system", "subtype": "init", "tools": None if tools is None else list(tools),
                "mcp_servers": list(servers),
                "permissionMode": mode, "session_id": session}
        result = {"type": "result", "subtype": "success", "is_error": False, "result": answer, "session_id": session,
                  "num_turns": 1, "duration_api_ms": 8100, "total_cost_usd": 0.5, "permission_denials": [],
                  "usage": usage}
        partial = [{"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                       "delta": {"type": "thinking_delta", "thinking": "Hm."}}}] + [
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
                                               "delta": {"type": "text_delta", "text": d}}} for d in deltas]
        self.stdout = stdout if stdout is not None else "".join(json.dumps(e) + "\n" for e in (*before, init, *partial,
                                                                                                result))

    def __call__(self, argv, **kw):
        call = dict(kw, argv=argv, input="")
        self.calls.append(call)
        call["fork"] = FakeFork(self, call)
        return call["fork"]


class FakeFork:
    """One fork FakeClaude started: its stdin, its output and its exit."""

    def __init__(self, fake, call):
        self.fake, self.call, self.killed, self.returncode = fake, call, threading.Event(), None
        self.stdin, self.stdout = self, self._lines()

    def write(self, text):
        self.call["input"] += text

    def close(self):
        pass

    def _lines(self):
        if self.fake.hang:
            self.killed.wait(30)
            return
        yield from self.fake.stdout.splitlines(keepends=True)
        if self.fake.code:
            yield "boom\n"
        ends = time.time() + 30
        while self.fake.linger and not (self.fake.ended.is_set() or self.killed.is_set()) and time.time() < ends:
            time.sleep(0.01)

    def kill(self):
        self.killed.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = -9 if self.killed.is_set() else self.fake.code
        return self.returncode


TOOLS = [{"type": "namespace", "name": "functions", "description": "", "tools": [
    {"type": "function", "name": "request_user_input", "parameters": {}}]}]     # what a locked-down copy is offered


class FakeCodex:
    """Stands in for subprocess.Popen running `codex exec fork --json`: keeps each call as FakeClaude does, and, as
    Codex does, asks its model address (its provider's base_url: the Gate) for its model list, then posts one model
    request offering `tools` with what it was given on stdin, with its login's headers; on a 200 it prints its events
    (thread `fork`, the `items` given, then `answer`) and exits 0; on any other status, turn.failed with the error
    and exit 1. `request=False` answers without asking the model; `code` and `stderr` fail it before any request;
    `hang` prints nothing until it is killed. `tools_id` is its tool list's id (a compacted conversation's own list
    keeps the id it has in the rollout)."""

    def __init__(self, answer="I am checking the report.", tools=TOOLS, items=(), request=True, code=0, stderr="",
                 hang=False, fork=F, tools_id=None):
        self.answer, self.tools, self.items, self.request, self.code = answer, tools, items, request, code
        self.tools_id = tools_id
        self.err, self.hang, self.fork, self.calls, self.got = stderr, hang, fork, [], []

    def __call__(self, argv, **kw):
        call = dict(kw, argv=argv, input="")
        self.calls.append(call)
        return CodexRun(self, call)


class CodexRun(FakeFork):
    """One run FakeCodex started."""

    def _lines(self):
        fake = self.fake
        if fake.hang:
            self.killed.wait(30)
            return
        if fake.code:
            self.call["stderr"].write(fake.err)
            return
        url = fake.base = next(json.loads(re.search(r"base_url=(\"[^\"]+\")", a).group(1)) for a in self.call["argv"]
                               if a.startswith("model_providers.voice-desk="))
        yield json.dumps({"type": "thread.started", "thread_id": fake.fork}) + "\n"
        yield json.dumps({"type": "turn.started"}) + "\n"
        if fake.request:
            try:
                urllib.request.urlopen(url + "/models?client_version=0.171.0", timeout=10)
            except urllib.error.HTTPError as e:
                fake.got.append(("GET", e.code))
            body = json.dumps({"model": "gpt-6.1-sol", "input": [
                dict({"type": "additional_tools", "role": "developer", "tools": fake.tools},
                     **({"id": fake.tools_id} if fake.tools_id else {})),
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": self.call["input"]}]}]})
            req = urllib.request.Request(url + "/responses", data=body.encode(), headers={
                "Authorization": "Bearer its-login", "Content-Type": "application/json", "Accept-Encoding": "gzip"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    fake.got.append(("POST", r.status, r.read().decode()))
            except urllib.error.HTTPError as e:
                fake.got.append(("POST", e.code))
                yield json.dumps({"type": "error", "message": "unexpected status %d" % e.code}) + "\n"
                yield json.dumps({"type": "turn.failed", "error": {"message": json.loads(e.read())["error"]
                                                                   ["message"]}}) + "\n"
                self.status = 1
                return
        for item in fake.items:
            yield json.dumps({"type": "item.started", "item": item}) + "\n"
        yield json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "Combined "
                                                             "user instructions use 33053 bytes"}}) + "\n"
        yield json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
                                                             "text": fake.answer}}) + "\n"
        yield json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12602017}}) + "\n"

    def wait(self, timeout=None):
        self.returncode = -9 if self.killed.is_set() else getattr(self, "status", self.fake.code)
        return self.returncode


class Upstream:
    """The model's end, standing in for CODEX_UPSTREAM: keeps each request (its path, headers and body) and answers
    `status` with a stream that ends in response.completed and its usage (`completed`, that event's data as sent)."""

    def __init__(self, status=200, completed=None):
        self.got, self.status, self.completed = [], status, completed
        up = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                up.got.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if up.status != 200:
                    data = json.dumps({"error": {"message": "usage limit reached"}}).encode()
                    self.send_response(up.status)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                done = {"type": "response.completed", "response": {"id": "r", "usage": {
                    "input_tokens": 217236, "input_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 0},
                    "output_tokens": 5}}}
                for e in ({"type": "response.created", "response": {"id": "r"}}, done):
                    data = up.completed if up.completed and e is done else json.dumps(e)
                    self.wfile.write(("event: %s\ndata: %s\n\n" % (e["type"], data)).encode())

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/backend-api/codex" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ForkCommand(unittest.TestCase):
    def setUp(self):
        self.argv = voicedesk.fork_argv("/bin/claude", A, "loader-fix", "claude-opus-5-5")

    def value(self, flag):
        return self.argv[self.argv.index(flag) + 1]

    def test_it_copies_the_session_and_keeps_no_transcript(self):
        self.assertEqual(self.argv[:6], ["/bin/claude", "-p", "--resume", A, "--fork-session", "--no-session-persistence"])
        self.assertEqual((self.value("--model"), self.value("--effort")), ("claude-opus-5-5", "low"))

    def test_only_the_reading_tools_exist_and_nothing_else_is_allowed(self):
        self.assertEqual(self.value("--tools"), "Read,Grep,Glob")
        # not allowed outright: Claude Code then lets them read only in the session's folder, and dontAsk refuses the
        # rest, so a fork cannot read the desktop's other files (its keys) for an answer that goes to Google
        self.assertFalse([a for a in self.argv if a.startswith("--allowed")])
        denied = self.value("--disallowedTools").split(",")
        for tool in ("Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "Agent", "Task", "SendMessage", "mcp__*",
                     "ListAgents", "ToolSearch", "ScheduleWakeup", "SendFeedback"):
            self.assertIn(tool, denied)
        self.assertEqual((self.value("--permission-mode"), self.value("--permission-prompts")), ("dontAsk", "none"))
        self.assertIn("--strict-mcp-config", self.argv)
        self.assertEqual(json.loads(self.value("--mcp-config")), {"mcpServers": {}})

    def test_never_a_bypass(self):
        text = " ".join(self.argv)
        for bad in ("dangerously", "bypass", "acceptEdits", "auto"):
            self.assertNotIn(bad, text)

    def test_it_cannot_pass_for_the_agent(self):
        self.assertEqual(self.value("--name"), "voice-copy-of-loader-fix")
        settings = json.loads(self.value("--settings"))
        self.assertEqual((settings["disableAllHooks"], settings["crossSessionInbound"]), (True, "refuse"))
        self.assertEqual(voicedesk.fork_argv("/bin/claude", A, "")[voicedesk.fork_argv("/bin/claude", A, "").index(
            "--name") + 1], "voice-copy-of-" + A[:8])

    def test_no_model_or_effort_leaves_the_logins_own(self):
        argv = voicedesk.fork_argv("/bin/claude", A, "x", None, None)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)

    def test_its_environment_is_no_other_sessions(self):
        # and none of a Gemini agent's OpenRouter settings, which Claude Code puts in every command the agent runs
        leaked = {"CLAUDE_CODE_SESSION_ID": B, "CLAUDE_CODE_MESSAGING_SOCKET": "/run/s.sock",
                  "CLAUDE_CODE_MESSAGING_TOKEN": "t", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                  "TMUX": "/tmp/tmux-1000/default,1,0", "TMUX_PANE": "%3", "SSH_CONNECTION": "1 2 3 4",
                  "CLAUDE_CONFIG_DIR": "/elsewhere", "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
                  "ANTHROPIC_MODEL": "google/gemini-model", "CLAUDE_CODE_SUBAGENT_MODEL": "google/gemini-model",
                  "LANG": "en_US.UTF-8"}
        with mock.patch.dict(os.environ, leaked):
            env = voicedesk.fork_env(sources.DEFAULT_CLAUDE_HOME)
            other = voicedesk.fork_env("/home/alex/.claude-work")
        for k in leaked:
            if k != "LANG":
                self.assertNotIn(k, env)
        self.assertEqual((env["LANG"], env["PATH"]), ("en_US.UTF-8", sources.LOGIN_PATH))
        self.assertEqual(other["CLAUDE_CONFIG_DIR"], "/home/alex/.claude-work")

    def test_no_copy_starts_when_the_mailrooms_scrub_fails_to_load(self):
        def broken(module):
            raise RuntimeError("the mailroom's agentmail is broken")
        with mock.patch.object(sources, "mailroom", broken):
            with self.assertRaisesRegex(voicedesk.VoiceError, "mailroom's scrub failed: RuntimeError: the mailroom"):
                voicedesk.fork_env(None)


class Agents(unittest.TestCase):
    def test_only_live_agents_are_listed_not_probes_copies_or_the_orchestrator(self):
        rows = voicedesk.agents(lambda: [
            registry(A, "loader-fix", cwd="/w/loader"),
            registry(B, "gone", live=False),
            registry(C, "probe", entrypoint="sdk-cli"),
            registry(str(uuid.UUID(int=4)), "voice-copy-of-loader-fix"),
            registry(str(uuid.UUID(int=5)), "orchestrator-event", cwd="/o"),
            registry(str(uuid.UUID(int=6)), "events", cwd="/o")], codex=sources.CodexReader(Path(tempfile.mkdtemp())),
            orch_dir="/o")
        self.assertEqual([(a["cli"], a["name"], a["folder"], a["status"]) for a in rows],
                         [("claude", "loader-fix", "loader", "idle")])

    @unittest.skipUnless(Path("/proc/locks").exists(), "a live Codex thread is one whose lock is held (/proc/locks)")
    def test_live_codex_agents_are_listed_but_not_their_sub_agents(self):
        home = Path(tempfile.mkdtemp()) / "codex"
        sub, done, orch = (str(uuid.UUID(int=n)) for n in (7, 8, 9))
        locks = fixtures.codex_home(home, [
            {"id": T, "name": "report", "cwd": "/w/report", "model": "gpt-6.1-sol"},
            {"id": sub, "source": '{"subagent": "review"}', "thread_source": "subagent"},
            {"id": done, "name": "done", "updated_at": int(time.time())},
            {"id": orch, "name": "orchestrator", "cwd": "/o"}], held=(T, sub, orch))
        self.addCleanup(lambda: [fh.close() for fh in locks])
        rows = voicedesk.agents(lambda: [registry(A, "loader-fix", cwd="/w/loader")], codex=sources.CodexReader(home),
                                orch_dir="/o")
        self.assertEqual([(a["cli"], a["name"], a["account"], a["model"], a["config_dir"]) for a in rows],
                         [("claude", "loader-fix", "default", "", "/h/.claude"),
                          ("codex", "report", "codex", "gpt-6.1-sol", str(home))])    # the home its reader read

    def test_codex_threads_that_cannot_be_read_are_said_not_left_out(self):
        home = Path(tempfile.mkdtemp())
        (home / "state_5.sqlite").write_bytes(b"not a database" * 100)
        with self.assertRaisesRegex(voicedesk.VoiceError, "could not read Codex's threads in %s \\(.*\\), so no agent "
                                                          "is listed" % home):
            voicedesk.agents(lambda: [registry(A, "loader-fix")], codex=sources.CodexReader(home), orch_dir="/o")

    def test_an_agent_is_found_as_said(self):
        rows = [row(A, "recipe-site"), row(B, "survey-model"), row(C, "survey-update-2026-11-16")]
        for said, want in (("recipe-site", A), ("Recipe site", A), (A[:8], A), (B, B),
                           ("recipe", A), ("site", A), ("survey model", B)):
            self.assertEqual(voicedesk.resolve(said, rows)["id"], want, said)
        with self.assertRaisesRegex(voicedesk.VoiceError, "could be survey-model, survey-update-2026-11-16"):
            voicedesk.resolve("survey", rows)
        with self.assertRaisesRegex(voicedesk.VoiceError, "no live agent called 'nobody'"):
            voicedesk.resolve("nobody", rows)


class Transcripts(unittest.TestCase):
    def test_the_newest_copy_across_logins_is_the_conversation(self):
        d = Path(tempfile.mkdtemp())
        old = write_jsonl(d / "default/projects/-w-repo" / (A + ".jsonl"), [reply("claude-opus-5-5")])
        new = write_jsonl(d / "work/projects/-w-repo" / (A + ".jsonl"), [reply("claude-fable-5-2")])
        os.utime(old, (time.time() - 600, time.time() - 600))
        homes = [("default", d / "default"), ("work", d / "work")]
        self.assertEqual(sources.newest_transcript(A, homes), ("work", d / "work", Path(new)))
        self.assertEqual(sources.newest_transcript(B, homes), (None, None, None))

    def test_its_model_size_and_folder_come_from_its_last_reply(self):
        d = Path(tempfile.mkdtemp())
        p = write_jsonl(d / "t.jsonl", [reply("claude-sonnet-5-5", read=10), {"type": "user", "cwd": "/w/repo"},
                                        reply("claude-opus-5-5", inp=5, read=100000, wrote=2000),
                                        {"type": "assistant", "message": {"model": "<synthetic>"}},
                                        {"type": "custom-title", "customTitle": "x"}])
        self.assertEqual(voicedesk.transcript_facts(p), {"model": "claude-opus-5-5", "context_tokens": 102005,
                                                         "cwd": "/w/repo"})


class Ask(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cwd = self.d / "repo"
        self.cwd.mkdir()
        write_jsonl(self.d / "work/projects/-repo" / (A + ".jsonl"), [reply("claude-fable-5-2", cwd=str(self.cwd))])
        self.homes = [("default", self.d / "default"), ("work", self.d / "work")]
        self.rows = [row(A, "loader-fix", cwd=str(self.cwd)), row(B, "other")]
        self.log = self.d / "asks.jsonl"

    def ask(self, fake, who="loader fix", question="What are you working on?", **kw):
        kw.setdefault("copies_path", self.d / "copies.json")
        return voicedesk.ask(who, question, popen=fake, claude_bin="/bin/claude", rows=self.rows, homes=self.homes,
                             log=self.log, **kw)

    def test_a_fork_answers_in_the_agents_folder_under_its_login_on_its_model(self):
        fake = FakeClaude()
        res = self.ask(fake)
        primer, call = fake.calls                    # a copy is made, asked only the primer; the question forks it
        for c in (primer, call):
            self.assertEqual((c["cwd"], c["env"]["CLAUDE_CONFIG_DIR"]), (str(self.cwd), str(self.d / "work")))
            self.assertEqual((c["stdout"], c["stderr"], c["text"]), (subprocess.PIPE, subprocess.STDOUT, True))
        self.assertEqual(primer["argv"], voicedesk.fork_argv("/bin/claude", A, "loader-fix", "claude-fable-5-2",
                                                             keep=True))
        self.assertEqual(primer["input"], voicedesk.PREAMBLE + voicedesk.PRIMER)
        self.assertEqual(call["argv"], voicedesk.fork_argv("/bin/claude", C, "loader-fix", "claude-fable-5-2"))
        self.assertEqual(call["input"], voicedesk.PREAMBLE + "What are you working on?")
        self.assertEqual((res["ok"], res["agent"], res["answer"], res["model"], res["context_tokens"]),
                         (True, "loader-fix", "I am fixing the loader.", "claude-fable-5-2", 90503))
        self.assertEqual(res["first_request"], {"cache_creation_input_tokens": 119000, "cache_read_input_tokens": 0})
        self.assertEqual((res["fork_id"], res["denials"], res["copy"]), (C, 0, "made " + C[:8]))
        logged = sources.read_jsonl(self.log)
        self.assertEqual([(r["target"], r["question"], r["answer"], r["model"], r["effort"]) for r in logged],
                         [("loader fix", "What are you working on?", "I am fixing the loader.", "claude-fable-5-2",
                           "low")])

    def test_a_later_question_forks_the_first_ones_copy_while_the_original_is_unchanged(self):
        original = self.d / "work/projects/-repo" / (A + ".jsonl")
        clock = [1000.0]
        fake = FakeClaude()

        def ask(**kw):
            """The copy said, and each fork's (resumed session, whether it keeps its transcript, its question)."""
            before = len(fake.calls)
            res = self.ask(fake, now=lambda: clock[0], **kw)
            return res["copy"], [(c["argv"][c["argv"].index("--resume") + 1], "--no-session-persistence" not in c["argv"],
                                  c["input"][len(voicedesk.PREAMBLE):]) for c in fake.calls[before:]]
        q = "What are you working on?"
        made = ("made " + C[:8], [(A, True, voicedesk.PRIMER), (C, False, q)])
        reused = ("reused " + C[:8], [(C, False, q)])
        self.assertEqual(ask(), made)                 # the copy kept holds the primer, never a question
        self.assertEqual(json.loads((self.d / "copies.json").read_text())[A]["copy"], C)
        write_jsonl(original.parent / (C + ".jsonl"), [reply("claude-fable-5-2")])   # the copy's transcript
        clock[0] += 600
        self.assertEqual(ask(), reused)               # forks the copy, keeps nothing
        self.assertEqual(ask(model="claude-opus-5-5"), made)       # another model: another cache
        self.assertEqual(ask(model="claude-opus-5-5"), reused)
        clock[0] += voicedesk.COPY_SECONDS + 1
        self.assertEqual(ask(model="claude-opus-5-5"), made)       # its cache is about to lapse
        with open(original, "a") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "more work"}}) + "\n")
        self.assertEqual(ask(model="claude-opus-5-5"), made)       # the agent has moved on
        self.assertEqual(ask(model="claude-opus-5-5", copies_path=None), ("none", [(A, False, q)]))

    GEMINI = {"apiKeyHelper": "/home/alex/bin/openrouter-key", "model": "google/gemini-model",
              "env": {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "ANTHROPIC_MODEL": "google/gemini-model"},
              "permissions": {"allow": ["Bash", "Write"], "defaultMode": "bypassPermissions"},
              "disableAllHooks": False, "crossSessionInbound": "accept", "includeCoAuthoredBy": False}

    def test_a_copys_settings_are_its_logins_with_its_read_only_settings_winning_on_every_key_they_set(self):
        merged = voicedesk.fork_settings(self.GEMINI)
        for key, value in voicedesk.FORK_SETTINGS.items():
            self.assertEqual(merged[key], value, key)
        self.assertEqual(merged["permissions"], {"deny": list(voicedesk.SECRET_READS)})     # the login's allow is gone
        for key in ("apiKeyHelper", "env", "model", "includeCoAuthoredBy"):
            self.assertEqual(merged[key], self.GEMINI[key], key)
        merged["permissions"]["deny"].append("x")
        self.assertNotIn("x", voicedesk.FORK_SETTINGS["permissions"]["deny"])              # a copy of them

    def test_a_gemini_agent_is_asked_on_its_logins_model_with_its_settings_under_the_read_only_ones(self):
        """An agent on a login with a settings file of its own (Gemini through OpenRouter): its copies run on the
        login's one model, whatever the caller or the transcript names, with one settings file, the login's with the
        copy's read-only settings laid over it, private while the copy runs and gone once it has exited."""
        gemini, cwd = self.d / "gemini", self.d / "gem"
        cwd.mkdir()
        # history from before it moved to Gemini: its last reply names a Claude model, which fails on OpenRouter
        write_jsonl(gemini / "projects/-gem" / (B + ".jsonl"), [reply("claude-opus-5-5", cwd=str(cwd))])
        (self.d / "gemini.json").write_text(json.dumps(self.GEMINI))
        self.homes.append(("gemini", gemini))
        self.rows[1] = row(B, "gem-agent", cwd=str(cwd), account="gemini", config_dir=str(gemini))
        cfg = {"usage": {"claude_accounts": {"default": {"config_dir": None}, "gemini": {
            "config_dir": str(gemini), "settings": str(self.d / "gemini.json"), "model": "google/gemini-model",
            "manual": True}}}}
        seen = []

        class Reading(FakeClaude):          # what the copy's --settings file holds, and its mode, as the copy starts
            def __call__(self, argv, **kw):
                f = Path(argv[argv.index("--settings") + 1])
                seen.append((json.loads(f.read_text()), stat.S_IMODE(f.stat().st_mode),
                             stat.S_IMODE(f.parent.stat().st_mode)))
                return super().__call__(argv, **kw)
        fake, folder = Reading(), self.d / "settings"
        modules = stand_ins.mailroom(accounts=stand_ins.MailroomAccounts, usage=stand_ins.MailroomUsage,
                                     config=stand_ins.MailroomConfig(cfg))
        with mock.patch.object(sources, "mailroom", modules), mock.patch.object(voicedesk, "SETTINGS_DIR", folder), \
                mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://elsewhere"}):
            res = self.ask(fake, who="gem agent", model="claude-sonnet-5-5")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["model"], "google/gemini-model")
        self.assertEqual(len(fake.calls), 2)                     # its copy, made with the primer, then the question
        for call in fake.calls:
            argv = call["argv"]
            self.assertEqual(argv[argv.index("--model") + 1], "google/gemini-model")
            self.assertEqual(argv.count("--settings"), 1)
            self.assertTrue(argv[argv.index("--settings") + 1].startswith(str(folder) + "/copy-"))
            self.assertEqual(call["env"]["CLAUDE_CONFIG_DIR"], str(gemini))
            self.assertNotIn("ANTHROPIC_BASE_URL", call["env"])      # from its settings file alone
        self.assertEqual(seen, [(voicedesk.fork_settings(self.GEMINI), 0o600, 0o700)] * 2)
        self.assertTrue(until(lambda: not list(folder.glob("copy-*.json"))))   # removed once each copy has exited

    def test_a_copys_settings_file_is_removed_with_any_left_by_a_board_that_died(self):
        folder = self.d / "settings"
        folder.mkdir()
        (folder / "copy-old.json").write_text("{}")
        os.utime(folder / "copy-old.json", (time.time() - voicedesk.STALE_SETTINGS - 5,) * 2)
        (folder / "copy-young.json").write_text("{}")             # another copy's, still running
        path, remove = voicedesk.settings_file({"model": "m"}, folder)
        self.assertEqual(sorted(f.name for f in folder.iterdir()), sorted(["copy-young.json", Path(path).name]))
        self.assertEqual(json.loads(Path(path).read_text()), {"model": "m"})
        remove()
        remove()                                                  # again: nothing to do
        self.assertFalse(Path(path).exists())
        ended = []

        def cannot(argv, **kw):
            raise OSError("no claude here")
        with self.assertRaises(OSError):                          # a copy that cannot start removes its file too
            sources.run_json_lines(["claude"], "", lambda e: False, timeout=5, popen=cannot,
                                   ended=lambda: ended.append(1))
        self.assertEqual(ended, [1])
        (self.d / "bad.json").write_text("[1]")
        cfg = {"usage": {"claude_accounts": {"g": {"config_dir": "/home/alex/.claude-g", "model": "google/gemini-model",
                                                   "settings": str(self.d / "bad.json")}}}}
        modules = stand_ins.mailroom(accounts=stand_ins.MailroomAccounts, usage=stand_ins.MailroomUsage,
                                     config=stand_ins.MailroomConfig(cfg))
        with mock.patch.object(sources, "mailroom", modules):
            with self.assertRaisesRegex(voicedesk.VoiceError, "holds no JSON object"):
                voicedesk.login_settings("/home/alex/.claude-g")
            self.assertEqual(voicedesk.login_settings(str(sources.DEFAULT_CLAUDE_HOME)), (None, None))

    def test_a_model_given_wins_over_the_sessions(self):
        fake = FakeClaude()
        self.ask(fake, model="claude-opus-5-5")
        self.assertEqual(fake.calls[0]["argv"][fake.calls[0]["argv"].index("--model") + 1], "claude-opus-5-5")

    def test_what_goes_wrong_is_said(self):
        init = json.dumps({"type": "system", "subtype": "init", "tools": ["Read"], "mcp_servers": [],
                           "permissionMode": "dontAsk"}) + "\n"
        cases = [(FakeClaude(code=1, stdout=""), {}, "loader-fix's copy failed \\(exit 1\\): boom"),
                 (FakeClaude(stdout=init + json.dumps({"type": "result", "is_error": True,
                                                       "result": "Prompt is too long"})), {}, "Prompt is too long"),
                 (FakeClaude(stdout=json.dumps({"type": "result", "result": "an answer"})), {},
                  "loader-fix's copy failed \\(exit 0\\): an answer"),        # no init: never taken as an answer
                 (FakeClaude(code=1, stdout=json.dumps({"type": "result", "is_error": True, "result": "You've hit your "
                                                        "weekly limit"}) + "\n"), {},
                  "loader-fix's copy failed \\(exit 1\\): You've hit your weekly limit"),
                 (FakeClaude(hang=True), {"timeout": 0.3}, "loader-fix's copy took over 0.3 seconds to answer"),
                 (FakeClaude(), {"who": "other"}, "other has no transcript to copy yet"),
                 (FakeClaude(), {"question": "  "}, "no question to ask"),
                 (FakeClaude(), {"question": "x" * 5000}, "over 4000 characters"),
                 (FakeClaude(), {"who": "nobody"}, "no live agent called 'nobody'")]
        for fake, kw, said in cases:
            res = self.ask(fake, **kw)
            self.assertFalse(res["ok"])
            self.assertRegex(res["error"], said)
        self.assertEqual(len(sources.read_jsonl(self.log)), len(cases))

    def test_a_fork_that_is_not_read_only_is_stopped_before_it_can_act(self):
        for fake, said in ((FakeClaude(tools=("Glob", "Grep", "Read", "Bash", "SendMessage")),
                            "it has tools beyond Read, Grep, Glob: Bash, SendMessage"),
                           (FakeClaude(tools=None), "it has tools beyond Read, Grep, Glob: \\?"),
                           (FakeClaude(servers=[{"name": "gmail", "status": "connected"}]), "it has MCP servers: gmail"),
                           (FakeClaude(mode="bypassPermissions"), "its permission mode is 'bypassPermissions'"),
                           (FakeClaude(stdout=json.dumps({"type": "assistant", "message": {}}) + "\n"),
                            "its first event was 'assistant', not its init"),
                           (FakeClaude(before=[{"type": "system", "subtype": "hook_started"}]),
                            "its first event was 'system' \\('hook_started'\\), not its init")):
            res = self.ask(fake)
            self.assertRegex(res["error"], "loader-fix's copy was stopped before it could use a tool: " + said)
            self.assertEqual(len(fake.calls), 1)           # the primer's fork: nothing went further
        self.assertFalse((self.d / "copies.json").exists())

    def test_a_fork_that_names_its_session_before_its_init_still_answers(self):
        # Claude Code 2.1.330 sends system/session_title_changed (from --name) ahead of the init
        fake = FakeClaude(before=[{"type": "system", "subtype": "session_title_changed", "title": "voice-copy-of-x",
                                   "session_id": C}])
        res = self.ask(fake)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["answer"], "I am fixing the loader.")

    def test_a_fork_of_an_agent_running_sub_agents_still_answers(self):
        # Claude Code tells a fork of an agent running background agents that each "didn't finish before the previous
        # session ended", ahead of its init: a notice, which runs nothing; anything else still
        # stops it
        notice = {"type": "system", "subtype": "task_notification", "task_id": "a1aa4fbb", "status": "failed",
                  "summary": "Background agent \"Build it\" didn't finish before the previous session ended"}
        res = self.ask(FakeClaude(before=[notice, notice]))
        self.assertEqual((res["ok"], res["answer"]), (True, "I am fixing the loader."), res)
        res = self.ask(FakeClaude(before=[notice, {"type": "system", "subtype": "hook_started"}]))
        self.assertRegex(res["error"], r"stopped before it could use a tool: its first event was 'system' "
                                       r"\('hook_started'\), not its init")

    def test_the_answer_streams_as_it_is_written_and_only_once_the_fork_is_checked(self):
        pieces = []
        fake = FakeClaude(deltas=["I am fixing", " the loader. ", "Nearly done."])
        res = self.ask(fake, on_text=pieces.append)
        self.assertEqual(pieces, ["I am fixing", " the loader. ", "Nearly done."])     # text, not its thinking
        primer, call = fake.calls
        self.assertNotIn("--include-partial-messages", primer["argv"])        # the copy kept is asked nothing to hear
        self.assertIn("--include-partial-messages", call["argv"])
        self.assertEqual(res["answer"], "I am fixing the loader.")            # the answer is still its result's
        pieces.clear()
        bad = FakeClaude(tools=("Glob", "Grep", "Read", "Bash"), deltas=["Done."])
        self.assertFalse(self.ask(bad, on_text=pieces.append, copies_path=None)["ok"])
        self.assertEqual(pieces, [])                                          # nothing from a fork that is stopped
        early = FakeClaude(before=[{"type": "stream_event", "event": {"type": "content_block_delta", "delta": {
            "type": "text_delta", "text": "Before my init."}}}])
        self.assertRegex(self.ask(early, on_text=pieces.append, copies_path=None)["error"],
                         "its first event was 'stream_event', not its init")
        self.assertEqual(pieces, [])
        plain = FakeClaude()                              # nobody listening (the voice page): no partial messages
        self.assertTrue(self.ask(plain, copies_path=None)["ok"])
        self.assertNotIn("--include-partial-messages", plain.calls[0]["argv"])

    def test_the_answer_comes_at_the_result_not_when_the_fork_has_ended(self):
        fake = FakeClaude(linger=True)                    # claude takes ~0.9 s to exit after its result
        res = self.ask(fake, copies_path=None)
        self.assertTrue(res["ok"], res)
        [call] = fake.calls
        self.assertIsNone(call["fork"].returncode)        # still running: left to end by itself
        fake.ended.set()
        self.assertTrue(fixtures.until(lambda: call["fork"].returncode == 0, 5))      # and waited for once it ended

    def test_a_fork_left_to_end_is_killed_at_its_timeout(self):
        fake = FakeClaude(linger=True)
        self.assertTrue(self.ask(fake, copies_path=None, timeout=0.5)["ok"])
        [call] = fake.calls
        self.assertTrue(fixtures.until(lambda: call["fork"].returncode == -9, 5))

    def test_a_fork_may_not_read_secrets_nor_run_in_the_home_folder(self):
        settings = json.loads(voicedesk.fork_argv("/bin/claude", A, "x")[voicedesk.fork_argv("/bin/claude", A, "x")
                                                                          .index("--settings") + 1])
        for rule in ("Read(**/.env)", "Read(**/*.pem)", "Read(**/*secret*)", "Read(~/.ssh/**)", "Read(~/.config/**)"):
            self.assertIn(rule, settings["permissions"]["deny"])
        self.assertIn("Never quote or reveal a key, token, password", voicedesk.PREAMBLE)
        fake = FakeClaude()
        for folder in (sources.HOME, sources.HOME.parent, Path("/")):
            self.rows[0]["cwd"] = str(folder)
            self.assertRegex(self.ask(fake)["error"], "the home folder or above it: a copy there could read all of it")
        (self.cwd / "inner").mkdir()
        self.rows[0]["cwd"] = str(self.cwd)
        self.rows.append(row(C, "inner-agent", cwd=str(self.cwd / "inner")))
        self.assertEqual(self.ask(fake)["error"], "loader-fix runs in %s, which holds other agents' folders "
                                                  "(inner-agent): a copy there could read their work" % self.cwd)
        self.assertEqual(fake.calls, [])
        self.rows[-1]["cwd"] = str(self.cwd)                      # two agents in one folder: neither holds the other
        self.assertTrue(self.ask(fake)["ok"])

    def test_a_folder_that_is_gone_is_said_before_any_fork(self):
        shutil.rmtree(self.cwd)
        fake = FakeClaude()
        self.assertRegex(self.ask(fake)["error"], "folder .* is gone")
        self.assertEqual(fake.calls, [])


class Calls:
    """Stands in for subprocess.Popen as FakeClaude does, each call answered by the next of `fakes` (the last one again
    once the others are used): `calls` keeps every call, in order."""

    def __init__(self, *fakes):
        self.fakes, self.calls = list(fakes), []

    def __call__(self, argv, **kw):
        fake = self.fakes.pop(0) if len(self.fakes) > 1 else self.fakes[0]
        fork = fake(argv, **kw)
        self.calls.append(fake.calls[-1])
        return fork


def replying(events, session=C):
    """FakeClaude printing its init and then `events` (fixtures.api_error_reply), then exiting 1, as claude does."""
    init = {"type": "system", "subtype": "init", "tools": ["Glob", "Grep", "Read"], "mcp_servers": [],
            "permissionMode": "dontAsk", "session_id": session}
    return FakeClaude(code=1, stdout="".join(json.dumps(e) + "\n" for e in [init] + events))


class AskOnAnotherLogin(unittest.TestCase):
    """A copy's login at its usage limit: the question goes to the mailroom's next login
    (stand_ins.mailroom_with_logins: personal, work, and Gemini's, a manual login), the agent's transcript copied
    there first."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cwd = self.d / "repo"
        self.cwd.mkdir()
        modules, self.logins = stand_ins.mailroom_with_logins(self.d)
        self.original = self.logins["personal"] / "projects/-repo" / (A + ".jsonl")
        write_jsonl(self.original, [reply("claude-fable-5-2", cwd=str(self.cwd))])
        write_jsonl(self.original.parent / A / "subagents" / "agent-1.jsonl", [reply("claude-fable-5-2")])
        self.homes = list(self.logins.items())
        self.rows = [row(A, "loader-fix", cwd=str(self.cwd))]
        patch = mock.patch.object(sources, "mailroom", modules)
        patch.start()
        self.addCleanup(patch.stop)
        self.limited = lambda: replying(fixtures.api_error_reply())

    def ask(self, fake, who="loader-fix", **kw):
        return voicedesk.ask(who, "What are you working on?", popen=fake, claude_bin="/bin/claude", rows=self.rows,
                             homes=self.homes, log=self.d / "asks.jsonl", copies_path=self.d / "copies.json", **kw)

    def logins_of(self, calls):
        return [Path(c["env"]["CLAUDE_CONFIG_DIR"]).name for c in calls]

    def test_the_mailrooms_logins_in_its_order_never_a_manual_one_nor_the_api_key(self):
        names = [(n, d) for n, d, _, _ in voicedesk.fallback_logins(["/bin/claude", "-p"])]
        self.assertEqual(names, [("personal", str(self.logins["personal"])), ("work", str(self.logins["work"]))])

    def test_a_login_at_its_limit_has_the_copy_made_on_the_next_with_the_conversation_copied_there(self):
        pieces, clock = [], [1000.0]
        fake = Calls(self.limited(), FakeClaude(deltas=["I am fixing the loader."]))
        res = self.ask(fake, on_text=pieces.append, now=lambda: clock[0])
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["login"], res["limited"]), ("work", ["personal: " + fixtures.LIMIT_TEXT]))
        self.assertEqual((res["answer"], res["copy"]), ("I am fixing the loader.", "made " + C[:8]))
        self.assertEqual(pieces, ["I am fixing the loader."])          # nothing of the limit's reply
        self.assertEqual(self.logins_of(fake.calls), ["personal", "work", "work"])     # its primer, then work's
        copied = self.logins["work"] / "projects/-repo" / (A + ".jsonl")
        self.assertEqual(copied.read_bytes(), self.original.read_bytes())
        self.assertTrue((copied.parent / A / "subagents" / "agent-1.jsonl").exists())    # its session folder too
        kept = json.loads((self.d / "copies.json").read_text())[A]
        self.assertEqual((kept["copy"], kept["login"]), (C, str(self.logins["work"])))
        write_jsonl(copied.parent / (C + ".jsonl"), [reply("claude-fable-5-2")])        # the copy's own transcript
        clock[0] += 600
        again = Calls(self.limited(), FakeClaude())
        res = self.ask(again, now=lambda: clock[0])
        self.assertEqual((res["login"], res["copy"]), ("work", "reused " + C[:8]))    # forked on the login it is on
        self.assertEqual(self.logins_of(again.calls), ["personal", "work"])
        self.assertEqual(again.calls[1]["argv"][again.calls[1]["argv"].index("--resume") + 1], C)
        logged = sources.read_jsonl(self.d / "asks.jsonl")
        self.assertEqual([r["login"] for r in logged], ["work", "work"])

    def test_with_every_login_at_its_limit_it_says_so_and_never_takes_a_manual_login(self):
        fake = Calls(self.limited())
        res = self.ask(fake)
        self.assertEqual(res["error"], "loader-fix's copy got no answer: each login is at its usage limit (personal: "
                                       "%s; work: %s)" % (fixtures.LIMIT_TEXT, fixtures.LIMIT_TEXT))
        self.assertEqual(self.logins_of(fake.calls), ["personal", "work"])
        self.assertFalse((self.d / "copies.json").exists())

    def test_an_agent_on_a_manual_login_is_asked_there_alone(self):
        cwd = self.d / "gem"
        cwd.mkdir()
        write_jsonl(self.logins["gemini"] / "projects/-gem" / (B + ".jsonl"), [reply("google/gemini-4-neon",
                                                                                     cwd=str(cwd))])
        self.rows.append(row(B, "gem-agent", cwd=str(cwd), account="gemini", config_dir=str(self.logins["gemini"])))
        fake = Calls(self.limited())
        res = self.ask(fake, who="gem-agent")
        self.assertEqual(res["error"], "gem-agent's copy got no answer: its login is at its usage limit (gemini: %s)"
                                       % fixtures.LIMIT_TEXT)
        self.assertEqual(self.logins_of(fake.calls), ["gemini"])
        self.assertEqual(list(self.logins["work"].iterdir()), [])     # nothing copied anywhere

    def test_a_mailroom_that_cannot_read_limits_is_said_not_taken_for_no_limit(self):
        reply = fixtures.api_error_reply()[1]                   # Claude Code's reply in place of an answer
        answer = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixing the loader."}]}}
        self.assertEqual(voicedesk.usage_limit(reply)["text"], fixtures.LIMIT_TEXT)
        self.assertIsNone(voicedesk.usage_limit(answer))

        def broken(module):
            raise RuntimeError("the mailroom's agentmail is broken")
        with mock.patch.object(sources, "mailroom", broken):
            self.assertIsNone(voicedesk.usage_limit(answer))          # no API error: the mailroom is not asked
            with self.assertRaisesRegex(voicedesk.VoiceError, "can't tell whether the reply is a usage limit's, as the "
                                                              "mailroom failed to load: RuntimeError"):
                voicedesk.usage_limit(reply)
        without = stand_ins.mailroom(accounts=stand_ins.MailroomAccounts)       # a mailroom older than limit reading
        with mock.patch.object(sources, "mailroom", without):
            self.assertIsNone(voicedesk.usage_limit(reply))

    def test_an_error_that_is_no_usage_limit_moves_nothing(self):
        fake = Calls(replying(fixtures.api_error_reply("API Error: 529 Overloaded", error="server_error", status=529,
                                                       api_error="overloaded_error", info=None)))
        res = self.ask(fake)
        self.assertEqual(res["error"], "loader-fix's copy failed (exit 1): API Error: 529 Overloaded")
        self.assertEqual(self.logins_of(fake.calls), ["personal"])
        self.assertEqual(list(self.logins["work"].iterdir()), [])


class CodexCommand(unittest.TestCase):
    def setUp(self):
        self.argv = voicedesk.codex_argv("/bin/codex", T, "gpt-6.1-sol", "/s/gpt-6.1-sol.json", "http://127.0.0.1:9")

    def settings(self):
        return [self.argv[i + 1] for i, a in enumerate(self.argv) if a == "-c"]

    def test_it_forks_the_thread_keeping_nothing_and_loading_none_of_the_users_config(self):
        self.assertEqual(self.argv[:8], ["/bin/codex", "exec", "fork", "--json", "--ephemeral", "--ignore-user-config",
                                         "--ignore-rules", "--skip-git-repo-check"])
        self.assertEqual(self.argv[-2:], [T, "-"])                 # the question on its stdin
        self.assertEqual(self.argv[self.argv.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="low"', self.settings())

    def test_every_feature_that_gives_a_tool_or_runs_anything_is_off(self):
        off = [self.argv[i + 1] for i, a in enumerate(self.argv) if a == "--disable"]
        for feature in ("hooks", "shell_tool", "unified_exec", "apps", "plugins", "image_generation", "view_image",
                        "goals", "multi_agent", "shell_snapshot", "enable_request_compression"):
            self.assertIn(feature, off)
        for setting in ("agents.enabled=false", 'web_search="disabled"', 'sandbox_mode="read-only"',
                        'approval_policy="never"', "analytics.enabled=false",
                        'model_catalog_json="/s/gpt-6.1-sol.json"',
                        'model_provider="voice-desk"', 'model_providers.voice-desk={name="voice desk", base_url='
                        '"http://127.0.0.1:9/v1", wire_api="responses", requires_openai_auth=true, '
                        'supports_websockets=false}', 'chatgpt_base_url="http://127.0.0.1:9/backend-api/"'):
            self.assertIn(setting, self.settings())

    def test_never_trust_nor_a_bypass(self):
        text = " ".join(self.argv)
        for bad in ("--trust", "dangerously", "bypass", "danger-full-access", "--enable"):
            self.assertNotIn(bad, text)

    def test_its_catalog_is_the_models_own_entry_without_code_mode_shell_or_patching(self):
        d = Path(tempfile.mkdtemp())
        (d / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-6.1-sol", "tool_mode": "code_mode_only", "shell_type": "unified_exec",
             "apply_patch_tool_type": "freeform", "experimental_supported_tools": ["clock"],
             "supports_search_tool": True, "context_window": 500000},
            {"slug": "gpt-6-astra", "tool_mode": "code_mode_only"}]}))
        path = voicedesk.codex_catalog(d, "gpt-6.1-sol", d / "models")
        self.assertEqual(json.loads(path.read_text()), {"models": [{
            "slug": "gpt-6.1-sol", "tool_mode": "default", "shell_type": "disabled", "apply_patch_tool_type": None,
            "experimental_supported_tools": [], "supports_search_tool": False, "context_window": 500000}]})
        with self.assertRaisesRegex(voicedesk.VoiceError, "Codex's model list in .* has no gpt-9"):
            voicedesk.codex_catalog(d, "gpt-9", d / "models")
        with self.assertRaisesRegex(voicedesk.VoiceError, "could not read Codex's model list"):
            voicedesk.codex_catalog(d / "nowhere", "gpt-6.1-sol", d / "models")

    def test_its_environment_is_no_other_sessions_and_its_agents_codex_home(self):
        leaked = {"CODEX_THREAD_ID": T, "CODEX_SESSION_ID": T, "CLAUDE_CODE_SESSION_ID": B, "TMUX": "/tmp/t,1,0",
                  "SSH_CONNECTION": "1 2 3 4", "CODEX_HOME": "/elsewhere", "LANG": "en_US.UTF-8"}
        with mock.patch.dict(os.environ, leaked):
            env = voicedesk.codex_env("/h/.codex")
        for k in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "TMUX", "SSH_CONNECTION"):
            self.assertNotIn(k, env)
        self.assertEqual((env["CODEX_HOME"], env["GIT_OPTIONAL_LOCKS"], env["LANG"]),
                         ("/h/.codex", "0", "en_US.UTF-8"))


class CodexTools(unittest.TestCase):
    def body(self, *tools, extra=()):
        return {"model": "m", "input": [{"type": "additional_tools", "role": "developer", "tools": list(tools)},
                                        {"type": "message", "role": "user", "content": []}, *extra]}

    def test_a_request_offering_request_user_input_alone_may_reach_the_model(self):
        self.assertIsNone(voicedesk.codex_unsafe(self.body(*TOOLS)))
        self.assertIsNone(voicedesk.codex_unsafe({"model": "m", "input": []}))

    def test_any_other_tool_anywhere_in_the_request_is_named(self):
        exec_ = {"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec"},
                                                                     {"type": "function", "name": "wait"}]}
        mcp = {"type": "namespace", "name": "mcp__slack", "tools": [{"type": "function", "name": "send_message"}]}
        for body, named in ((self.body(exec_), "exec, wait"), (self.body(*TOOLS, mcp), "mcp__slack.send_message"),
                            (dict(self.body(*TOOLS), tools=[{"type": "web_search"}]), "web_search"),
                            (self.body(extra=[{"type": "tool_search_output", "tools": [{"type": "function",
                                                                                         "name": "shell"}]}]),
                             "shell"),
                            (self.body("exec_command"), "'exec_command'")):
            self.assertEqual(voicedesk.codex_unsafe(body), "it was offered tools beyond request_user_input: " + named)
        self.assertEqual(voicedesk.codex_unsafe([]), "its request is not one the voice desk knows")

    def test_the_tool_lists_of_the_agents_own_conversation_are_not_the_copys(self):
        # after a compaction Codex re-sends the list it recorded, the agent's, and declares none of its own
        exec_ = [{"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec"}]}]
        old = {"type": "additional_tools", "id": "at_old", "tools": exec_}
        body = {"model": "m", "input": [old, {"type": "message", "role": "user", "content": []}]}
        self.assertIsNone(voicedesk.codex_unsafe(body, history={"at_old"}))
        self.assertEqual(voicedesk.codex_unsafe(body), "it was offered tools beyond request_user_input: exec")
        body["input"].append(dict(old, id="at_new"))
        self.assertEqual(voicedesk.codex_unsafe(body, history={"at_old"}),
                         "it was offered tools beyond request_user_input: exec")
        d = Path(tempfile.mkdtemp())
        rollout = write_jsonl(d / "r.jsonl", [
            {"type": "response_item", "payload": {"type": "additional_tools", "id": "at_a", "tools": []}},
            {"type": "compacted", "payload": {"replacement_history": [old, {"type": "message"}]}}])
        self.assertEqual(voicedesk.rollout_tools(rollout), {"at_a", "at_old"})

    def test_a_tool_result_reaches_the_model_only_as_codexs_refusal(self):
        def body(*results):
            return {"model": "m", "input": [{"type": "function_call_output", "call_id": c, "output": o}
                                            for c, o in results]}
        first = body(("h1", "total 48\ndrwx------ keys"))          # the conversation's own: its results stand
        self.assertIsNone(voicedesk.codex_unsafe(first, known=None))
        refused = body(("h1", "total 48"), ("c1", "unsupported call: exec_command"),
                       ("c2", [{"type": "input_text", "text": "unsupported custom tool call: apply_patch"}]),
                       ("c3", {"content": "request_user_input is unavailable in Default mode", "success": False}))
        self.assertIsNone(voicedesk.codex_unsafe(refused, known={"h1"}))
        ran = body(("h1", "x"), ("c4", "total 48\ndrwx------ keys"), ("c5", "unsupported call: x; then this"))
        self.assertEqual(voicedesk.codex_unsafe(ran, known={"h1"}),
                         "a tool ran in it (call c4, c5), and its result was kept from the model")

    def test_the_gate_turns_away_other_addresses_and_unreadable_requests(self):
        with voicedesk.Gate("http://127.0.0.1:9/backend-api/codex", timeout=5) as gate:
            for path in ("/v1/models", "/backend-api/wham/accounts/check"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(gate.url + path, timeout=5)
                self.assertEqual(e.exception.code, 404)
            for path, data, headers in (("/backend-api/codex/analytics-events/events", b"{}", {}),
                                        ("/v1/responses", b"x", {}),
                                        ("/v1/responses", b"{}", {"Content-Encoding": "zstd"})):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(urllib.request.Request(gate.url + path, data=data, headers=headers),
                                           timeout=5)
                self.assertEqual(e.exception.code, 403 if path == "/v1/responses" else 404)
            self.assertEqual(gate.passed, 0)
            self.assertRegex(gate.refused, "its request could not be read")


class CodexAsk(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cwd, self.home = self.d / "report", self.d / "codex"
        self.cwd.mkdir()
        rollout = write_jsonl(self.d / "rollout.jsonl", [
            {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "cwd": str(self.cwd)}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 215670, "cached_input_tokens": 215424}}}}])
        fixtures.codex_home(self.home, [{"id": T, "name": "report", "cwd": str(self.cwd), "model": "gpt-6.1-sol",
                                         "rollout_path": str(rollout)}])
        (self.home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-6.1-sol",
                                                                              "tool_mode": "code_mode_only"}]}))
        self.rows = [row(T, "report", cwd=str(self.cwd), cli="codex", config_dir=str(self.home), model="gpt-6.1-sol"),
                     row(A, "loader-fix")]
        self.log = self.d / "asks.jsonl"
        self.up = Upstream()
        self.addCleanup(self.up.close)
        patch = mock.patch.object(voicedesk, "CODEX_MODELS", self.d / "models")
        patch.start()
        self.addCleanup(patch.stop)

    def ask(self, fake, who="report", question="Which report did you pick?", **kw):
        kw.setdefault("upstream", self.up.url)
        return voicedesk.ask(who, question, popen=fake, codex_bin="/bin/codex", rows=self.rows, log=self.log,
                             copies_path=self.d / "copies.json", **kw)

    def test_a_copy_answers_in_the_agents_folder_under_its_codex_home_through_the_gate(self):
        fake = FakeCodex()
        res = self.ask(fake)
        self.assertTrue(res["ok"], res)
        [call] = fake.calls                                   # no kept copy: one fork of the live conversation
        argv = call["argv"]
        self.assertEqual(argv[:3] + argv[-2:], ["/bin/codex", "exec", "fork", T, "-"])
        self.assertIn('model_catalog_json="%s"' % (self.d / "models/gpt-6.1-sol.json"), argv)
        self.assertIn('chatgpt_base_url="%s/backend-api/"' % fake.base[:-3], argv)    # ChatGPT's, the gate's too
        self.assertEqual((call["cwd"], call["env"]["CODEX_HOME"], call["input"]),
                         (str(self.cwd), str(self.home), voicedesk.CODEX_PREAMBLE + "Which report did you pick?"))
        self.assertNotEqual(call["stderr"], subprocess.STDOUT)       # Codex's logs never split an event
        self.assertEqual((res["cli"], res["agent"], res["answer"], res["model"], res["context_tokens"], res["copy"]),
                         ("codex", "report", "I am checking the report.", "gpt-6.1-sol", 215670, "none"))
        self.assertEqual(res["usage"], {"input_tokens": 216236, "cache_creation_input_tokens": 0,
                                        "cache_read_input_tokens": 1000, "output_tokens": 5})
        self.assertEqual((res["first_request"], res["turns"], res["denials"], res["fork_id"]),
                         ({"cache_creation_input_tokens": 0, "cache_read_input_tokens": 1000}, 1, None, F))
        [(path, headers, body)] = self.up.got                 # sent on as it came, the login's headers and all
        self.assertEqual((path, headers["authorization"], body["input"][0]["tools"]),
                         ("/backend-api/codex/responses", "Bearer its-login", TOOLS))
        self.assertEqual(headers["accept-encoding"], "identity")      # not its gzip: the gate reads the usage
        self.assertEqual([g[:2] for g in fake.got], [("GET", 404), ("POST", 200)])
        self.assertIn("response.completed", fake.got[1][2])
        logged = sources.read_jsonl(self.log)
        self.assertEqual([(r["cli"], r["answer"], r["effort"]) for r in logged],
                         [("codex", "I am checking the report.", "low")])
        self.assertFalse((self.d / "copies.json").exists())

    def test_a_compacted_conversations_own_tool_list_goes_to_the_model_as_it_is(self):
        exec_ = [{"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec"}]}]
        with open(self.d / "rollout.jsonl", "a") as fh:
            fh.write(json.dumps({"type": "compacted", "payload": {"replacement_history": [
                {"type": "additional_tools", "id": "at_01a208ce", "tools": exec_}]}}) + "\n")
        res = self.ask(FakeCodex(tools=exec_, tools_id="at_01a208ce"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.up.got[0][2]["input"][0]["tools"], exec_)    # as recorded: the agent's prompt cache
        res = self.ask(FakeCodex(tools=exec_, tools_id="at_01a209ff"))     # a list of the copy's own: checked
        self.assertRegex(res["error"], "stopped before it could use a tool: it was offered tools beyond "
                                       "request_user_input: exec")

    def test_a_usage_that_cannot_be_read_is_unknown_not_low(self):
        self.up.close()
        self.up = Upstream(completed='{"type": "response.completed", "response": {"usage": ')
        self.addCleanup(self.up.close)
        res = self.ask(FakeCodex())
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["usage"], res["first_request"]), (dict.fromkeys(voicedesk.USAGE),
                                                                dict.fromkeys(voicedesk.USAGE[1:3])))

    def test_a_copy_offered_a_tool_is_stopped_before_the_model_sees_the_request(self):
        tools = [{"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec"},
                                                                      {"type": "custom", "name": "apply_patch"}]}]
        res = self.ask(FakeCodex(tools=tools))
        self.assertEqual(res["error"], "report's copy was stopped before it could use a tool: it was offered tools "
                                       "beyond request_user_input: apply_patch, exec")
        self.assertEqual(self.up.got, [])

    def test_a_copy_that_starts_anything_but_its_answer_is_stopped(self):
        for kind in ("command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call"):
            res = self.ask(FakeCodex(items=[{"id": "item_9", "type": kind}]))
            self.assertEqual(res["error"], "report's copy was stopped: it began %r, and may only answer" % kind)

    def test_an_answer_whose_requests_never_passed_the_gate_is_not_used(self):
        res = self.ask(FakeCodex(request=False))
        self.assertEqual(res["error"], "report's copy answered without its model requests passing the voice desk's "
                                       "check, so its answer is not used")

    def test_what_goes_wrong_is_said(self):
        cases = [(FakeCodex(code=1, stderr="Error: Unknown feature flag: goals\n"), {},
                  "report's copy failed \\(exit 1\\): Error: Unknown feature flag: goals"),
                 (FakeCodex(hang=True), {"timeout": 0.3}, "report's copy took over 0.3 seconds to answer"),
                 (FakeCodex(), {"model": "gpt-9"}, "Codex's model list in .* has no gpt-9"),
                 (FakeCodex(), {"question": " "}, "no question to ask")]
        for fake, kw, said in cases:
            self.assertRegex(self.ask(fake, **kw)["error"], said)
        self.up.status = 429
        self.assertRegex(self.ask(FakeCodex())["error"], "report's copy failed \\(exit 1\\): usage limit reached")
        gone = socket.socket()                          # a port nothing listens on: the model cannot be reached
        gone.bind(("127.0.0.1", 0))
        port = gone.getsockname()[1]
        gone.close()
        self.assertRegex(self.ask(FakeCodex(), upstream="http://127.0.0.1:%d/backend-api/codex" % port)["error"],
                         "report's copy failed \\(exit 1\\): voice desk: the model could not be reached: ")
        self.rows[0]["cwd"] = str(sources.HOME)
        self.assertRegex(self.ask(FakeCodex())["error"], "the home folder or above it")
        con = sqlite3.connect(str(self.home / "state_5.sqlite"))
        con.execute("delete from threads")
        con.commit()
        con.close()
        self.assertEqual(self.ask(FakeCodex())["error"], "report has no conversation to copy yet")
        (self.home / "state_5.sqlite").write_bytes(b"not a database" * 100)
        self.assertRegex(self.ask(FakeCodex())["error"], "could not read Codex's threads in %s: " % self.home)
        self.assertEqual(len(sources.read_jsonl(self.log)), len(cases) + 5)


class Looking(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.db = self.d / "mailroom.sqlite"
        con = mailroom_db(self.db)
        con.executemany("insert into sessions values (?, ?, ?, ?, ?, ?, ?)", [
            ("ab12cd34", "claude", A, "loader-fix", "/w", 0, "desktop"),
            ("mac00001", "claude", B, "mac-agent", "/Users/alex", 0, "mac")])
        con.executemany("insert into sent(tag, subject, sent_at) values (?, ?, ?)", [
            ("ab12cd34", "Loader cache: two choices before I merge [#ab12cd34]", int(time.time()) - 60),
            ("mac00001", "Re: Something on the Mac [#mac00001]", int(time.time()) - 30)])
        con.commit()
        self.rows = [row(A, "loader-fix", cwd="/w"), row(C, "survey-model", cwd="/p")]

    def behind(self, app, title, url="", rows=None, run=sources.run):
        return voicedesk.who_is_behind({"app": app, "title": title, "url": url}, rows or self.rows, db=self.db,
                                       run=run, host="desktop")

    def test_a_mail_tag_in_gmail_is_its_session(self):
        got, _ = self.behind("Google Chrome", "Re: Loader cache: two choices before I merge [#ab12cd34] - "
                             "owner@example.com - Gmail", "https://mail.google.com/mail/u/0/#inbox/FMfcg")
        self.assertEqual((got["name"], got["id"], got["live"], got["how"]), ("loader-fix", A, True, "mail tag [#ab12cd34]"))

    def test_a_mail_tab_window_has_the_subject_alone(self):
        got, _ = self.behind("AgentBoard", "Loader cache: two choices before I merge")
        self.assertEqual((got["name"], got["how"]), ("loader-fix", "mail subject"))
        self.assertIsNone(self.behind("AgentBoard", "Agent Board")[0])

    def test_a_mac_sessions_mail_is_not_askable_here(self):
        got, _ = self.behind("Mail", "Something on the Mac [#mac00001]")
        self.assertEqual((got["name"], got["live"], got["note"]), ("mac-agent", False, "that agent runs on mac, not the desktop"))

    def test_a_pull_request_is_the_agent_with_its_branch_checked_out(self):
        repo_with_worktree(self.d, "board", "voice")
        git("-C", str(self.d / "board"), "remote", "add", "origin", "git@github.com:example-user/agent-board.git")
        rows = [row(A, "on-main", cwd=str(self.d / "board")), row(B, "voice-agent", cwd=str(self.d / "board-voice"))]
        gh = []

        def run(cmd, timeout=60, **kw):        # gh stands in; git runs for real
            if cmd[0].endswith("gh"):
                gh.append(cmd)
                return 0, json.dumps({"headRefName": "voice", "title": "Voice desk"}), ""
            return sources.run(cmd, timeout=timeout, **kw)
        with mock.patch.object(sources, "find_bin", lambda name: "/bin/" + name):
            got, _ = self.behind("Google Chrome", "Voice desk by example-user · Pull Request #130 · example-user/agent-board",
                                 "https://github.com/example-user/agent-board/pull/130", rows=rows, run=run)
            self.assertEqual((got["name"], got["how"]), ("voice-agent", "pull request example-user/agent-board#130 on "
                                                                        "branch voice"))
            got, note = self.behind("Google Chrome", "Pull Request #7 · example-user/agent-board", "",
                                    rows=rows[:1], run=run)
            self.assertIsNone(got)
            self.assertEqual(note, "pull request example-user/agent-board#7 is on branch voice, and no live agent has it "
                                   "checked out")
        self.assertEqual(gh[0][1:], ["pr", "view", "130", "--repo", "example-user/agent-board", "--json", "headRefName,title"])

    def test_an_agents_name_in_the_title_and_else_nothing(self):
        got, _ = self.behind("kitty", "alex@desktop: tmux a -t survey-model")
        self.assertEqual((got["cli"], got["name"], got["how"]), ("claude", "survey-model", "name in the title"))
        got, _ = self.behind("kitty", "alex@desktop: tmux a -t report-fix",
                             rows=self.rows + [row(T, "report-fix", cwd="/w/report", cli="codex")])
        self.assertEqual((got["cli"], got["id"]), ("codex", T))       # a Codex agent is said as one
        self.assertEqual(self.behind("Terminal", "survey-model-update notes"), (None, ""))
        self.assertEqual(self.behind("Finder", "Downloads"), (None, ""))

    def test_the_boards_window_names_its_selection(self):
        # mac App.swift BoardTitle: the selected tile's agent, or the Mail tab's thread's newest agent
        got, _ = self.behind("AgentBoard", "Agent Board — loader-fix [session %s]" % A)
        self.assertEqual((got["name"], got["id"], got["how"]), ("loader-fix", A, "selection on the board"))
        got, _ = self.behind("AgentBoard", "Agent Board — Mail: Results are in [#ab12cd34] [session %s]" % C)
        self.assertEqual(got["name"], "survey-model")
        got, _ = self.behind("AgentBoard", "Agent Board — gone [session %s]" % B)    # its id: voiceweb may refuse it
        self.assertEqual((got["id"], got["live"], got["note"]), (B, False, "it is not a live agent here"))
        got, _ = self.behind("AgentBoard", "Agent Board — Mail: Loader cache: two choices before I merge")
        self.assertEqual((got["name"], got["how"]), ("loader-fix", "mail subject"))

    def test_a_kitty_window_is_the_tmux_session_last_typed_in(self):
        rows = [row(A, "loader-fix", tmux="work:@1.%1"), row(B, "other", tmux="work:@2.%5"),
                row(C, "survey-model", tmux="survey:@3.%7")]
        clients = []

        def run(cmd, timeout=60, **kw):         # tmux stands in: no test touches the desktop's tmux
            self.assertEqual(cmd[1:3], ["list-clients", "-F"])
            return 0, "".join(clients), ""
        with mock.patch.object(sources, "find_bin", lambda name: "/bin/" + name):
            clients[:] = ["100\tsurvey\t@3\t%7\n", "200\twork\t@2\t%5\n"]
            got, _ = self.behind("kitty", "notes.md", rows=rows, run=run)
            self.assertEqual((got["name"], got["how"]), ("other", "tmux session work, the one last typed in"))
            clients[:] = ["300\tsurvey\t@9\t%9\n"]               # another window of the session: its one agent
            self.assertEqual(self.behind("kitty", "x", rows=rows, run=run)[0]["name"], "survey-model")
            clients[:] = ["300\twork\t@9\t%9\n"]
            self.assertEqual(self.behind("kitty", "x", rows=rows, run=run),
                             (None, "tmux session work holds 2 live agents"))
            clients[:] = []
            self.assertEqual(self.behind("kitty", "x", rows=rows, run=run), (None, "no tmux client on the desktop"))
            self.assertEqual(self.behind("kitty", "x", rows=rows, run=lambda cmd, timeout=60, **kw: (
                1, "", "no server running on /tmp/tmux-1000/default")), (None, "no tmux client on the desktop"))
            with self.assertRaisesRegex(voicedesk.VoiceError, r"could not ask the desktop's tmux .*\(exit 124\): "
                                                              "timed out after 3s"):
                self.behind("kitty", "x", rows=rows, run=lambda cmd, timeout=60, **kw: (124, "", "timed out after 3s"))

    def test_a_mailroom_store_that_cannot_be_read_is_said_not_taken_for_no_agent(self):
        self.db.write_bytes(b"not a database at all, " * 100)
        res = voicedesk.looking({"ok": True, "windows": [{"app": "AgentBoard", "title": "Loader cache: two choices "
                                                                                          "before I merge"}]},
                                rows=self.rows, db=self.db)
        self.assertFalse(res["ok"])
        self.assertIn("could not read the mailroom's store %s" % self.db, res["error"])

    def test_the_front_window_takes_the_browsers_address(self):
        seen = {"ok": True, "windows": [{"app": "Google Chrome", "title": "PR", "timestamp": "t"}],
                "web": [{"url": "https://github.com/a/b/pull/1"}], "afk": "not-afk"}
        self.assertEqual(voicedesk.front_window(seen), {"app": "Google Chrome", "title": "PR", "url":
                                                        "https://github.com/a/b/pull/1", "at": "t", "afk": "not-afk"})
        seen["windows"][0]["app"] = "Finder"
        self.assertEqual(voicedesk.front_window(seen)["url"], "")

    def test_the_voice_page_s_own_window_is_passed_over(self):
        # Alex talks to the voice page about what was in front before it: the newest window and tab that are not it
        page = (Path(voicedesk.__file__).parent.parent / "web" / "voice" / "index.html").read_text()
        self.assertIn("<title>%s</title>" % voicedesk.VOICE_TITLE, page)
        seen = {"ok": True, "windows": [{"app": "Safari", "title": voicedesk.VOICE_TITLE, "timestamp": "t2"},
                                        {"app": "Safari", "title": "Voice desk · Pull Request #130 · example-user/agent-"
                                                                   "board", "timestamp": "t1"}],
                "web": [{"url": "https://desk.ts.net:8466/voice", "title": voicedesk.VOICE_TITLE},
                        {"url": "https://github.com/example-user/agent-board/pull/130", "title": "Voice desk"}]}
        self.assertEqual(voicedesk.front_window(seen)["title"],
                         "Voice desk · Pull Request #130 · example-user/agent-board")
        self.assertEqual(voicedesk.front_window(seen)["url"], "https://github.com/example-user/agent-board/pull/130")

    def test_what_is_said(self):
        seen = {"ok": True, "windows": [{"app": "AgentBoard", "title": "Loader cache: two choices before I merge"}]}
        res = voicedesk.looking(seen, rows=self.rows, db=self.db)
        self.assertEqual(res["said"], "You're looking at AgentBoard: Loader cache: two choices before I merge. Behind "
                                      "it: loader-fix (matched by its mail subject).")
        res = voicedesk.looking({"ok": True, "windows": [{"app": "Google Chrome", "title": "Re: Loader cache: two "
                                 "choices before I merge [#ab12cd34] - owner@example.com - Gmail"}]}, rows=self.rows,
                                db=self.db)
        self.assertEqual(res["said"], "You're looking at Google Chrome: Loader cache: two choices before I merge. Behind "
                                      "it: loader-fix (matched by its mail tag [#ab12cd34]).")
        res = voicedesk.looking({"ok": True, "windows": [{"app": "Finder", "title": "Downloads"}]}, rows=self.rows,
                                db=self.db)
        self.assertEqual((res["agent"], res["said"]), (None, "You're looking at Finder: Downloads. No agent found "
                                                             "behind it."))
        res = voicedesk.looking(None, rows=self.rows, db=self.db, mac=lambda run: {"ok": False, "error": "asleep"})
        self.assertEqual((res["ok"], res["error"]), (False, "asleep"))


class FakeActivityWatch(http.server.BaseHTTPRequestHandler):
    """ActivityWatch's REST API as aw-server answers it: the buckets, and each bucket's latest events, newest first, as
    many as the request's limit."""
    BUCKETS = {"aw-watcher-window_mac": {"id": "aw-watcher-window_mac", "type": "currentwindow", "last_updated": "2"},
               "aw-watcher-afk_mac": {"id": "aw-watcher-afk_mac", "type": "afkstatus", "last_updated": "2"},
               "aw-watcher-web-chrome_mac": {"id": "aw-watcher-web-chrome_mac", "type": "web.tab.current",
                                             "last_updated": "1"}}
    EVENTS = {"aw-watcher-window_mac": [{"app": "Google Chrome", "title": voicedesk.VOICE_TITLE},
                                        {"app": "Google Chrome", "title": "Re: Loader cache [#ab12cd34] - Gmail"},
                                        {"app": "kitty", "title": "tmux"}],
              "aw-watcher-afk_mac": [{"status": "not-afk"}, {"status": "afk"}],
              "aw-watcher-web-chrome_mac": [{"url": "https://mail.google.com/mail/u/0/", "title": "Gmail"}]}
    asked, failing = [], set()           # the buckets whose events it answers with an error

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        self.asked.append(self.path)
        if url.path == "/api/0/buckets/":
            body = self.BUCKETS
        elif url.path.split("/")[4] in self.failing:
            self.send_error(500, "bucket store unreadable")
            return
        else:
            limit = int(urllib.parse.parse_qs(url.query)["limit"][0])
            body = [{"id": i, "timestamp": "2026-11-22T10:00:0%dZ" % (9 - i), "duration": 5.0, "data": d}
                    for i, d in enumerate(self.EVENTS[url.path.split("/")[4]][:limit])]
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class MacScript(unittest.TestCase):
    """The Mac's ActivityWatch reader, run as the desktop runs it there (`python3 - --aw`, the script on stdin), under
    Python 3.9 when it is here."""

    def setUp(self):
        patch = mock.patch.dict(os.environ, {"AGENT_BOARD_MAC": "mac"})     # the Mac's alias, as the kit sets it up
        patch.start()
        self.addCleanup(patch.stop)

    @classmethod
    def setUpClass(cls):
        cls.aw = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeActivityWatch)
        threading.Thread(target=cls.aw.serve_forever, daemon=True).start()
        cls.aw_url = "http://127.0.0.1:%d" % cls.aw.server_port

    @classmethod
    def tearDownClass(cls):
        cls.aw.shutdown()

    def mac(self, aw=None):
        """sources.run for mac_front: `ssh mac python3 - --aw`, run here instead, against ActivityWatch at `aw`."""
        def run(cmd, input=None, timeout=60):
            self.assertEqual(cmd[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "mac"])
            self.assertEqual(cmd[-3:], ["python3", "-", "--aw"])
            r = subprocess.run([PY39] + cmd[-2:], input=input, capture_output=True, text=True, timeout=timeout,
                               env=dict(os.environ, VOICE_DESK_AW=aw or self.aw_url))
            return r.returncode, r.stdout, r.stderr
        return run

    def test_the_desktop_reads_the_mac_s_latest_windows_through_the_script(self):
        seen = voicedesk.mac_front(run=self.mac())
        self.assertEqual((seen["ok"], [w["title"] for w in seen["windows"]], seen["web"][0]["url"], seen["afk"]),
                         (True, [voicedesk.VOICE_TITLE, "Re: Loader cache [#ab12cd34] - Gmail", "tmux"],
                          "https://mail.google.com/mail/u/0/", "not-afk"))
        self.assertIn("/api/0/buckets/aw-watcher-window_mac/events?limit=5", FakeActivityWatch.asked)
        self.assertIn("/api/0/buckets/aw-watcher-afk_mac/events?limit=1", FakeActivityWatch.asked)
        self.assertEqual(voicedesk.front_window(seen)["title"], "Re: Loader cache [#ab12cd34] - Gmail")

    def test_a_bucket_that_cannot_be_read_is_said(self):
        self.addCleanup(FakeActivityWatch.failing.clear)
        FakeActivityWatch.failing.add("aw-watcher-web-chrome_mac")
        seen = voicedesk.mac_front(run=self.mac())
        self.assertEqual((seen["ok"], seen["web"], len(seen["errors"])), (True, [], 1))
        self.assertIn("aw-watcher-web-chrome_mac: HTTP Error 500", seen["errors"][0])
        FakeActivityWatch.failing.add("aw-watcher-window_mac")
        seen = voicedesk.mac_front(run=self.mac())
        self.assertFalse(seen["ok"])
        self.assertIn("ActivityWatch has no window watcher events (buckets: aw-watcher-afk_mac, "
                      "aw-watcher-web-chrome_mac, aw-watcher-window_mac; aw-watcher-window_mac: HTTP Error 500",
                      seen["error"])

    def test_a_mac_it_cannot_read_is_said(self):
        seen = voicedesk.mac_front(run=self.mac(aw="http://127.0.0.1:9"))
        self.assertFalse(seen["ok"])
        self.assertIn("ActivityWatch is not answering at http://127.0.0.1:9", seen["error"])
        gone = voicedesk.mac_front(run=lambda cmd, input=None, timeout=60: (255, "", "ssh: connect to host mac: timed out"))
        self.assertEqual(gone, {"ok": False, "error": "could not read the Mac's front window (ssh mac, exit 255): "
                                                      "ssh: connect to host mac: timed out"})

    def test_the_mac_is_the_configured_one_and_without_one_nothing_is_reached(self):
        ran = []

        def run(cmd, input=None, timeout=60):
            ran.append(cmd)
            return 255, "", "unreachable"
        with mock.patch.dict(os.environ, {"AGENT_BOARD_MAC": "studio", "VOICE_DESK_MAC": "air"}):
            voicedesk.mac_front(run=run)                               # VOICE_DESK_MAC first
        with mock.patch.dict(os.environ, {"AGENT_BOARD_MAC": "studio"}):
            os.environ.pop("VOICE_DESK_MAC", None)
            voicedesk.mac_front(run=run)                               # then the board's (config.mac_host)
        self.assertEqual([c[5] for c in ran], ["air", "studio"])
        for env in ({"AGENT_BOARD_MAC": ""}, {"AGENT_BOARD_MAC": "studio", "VOICE_DESK_MAC": ""}):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(voicedesk.mac_front(run=run), {"ok": False, "error": voicedesk.NO_MAC})
                said = voicedesk.looking(rows=[], run=run)
                self.assertEqual((said["ok"], said["error"]), (False, voicedesk.NO_MAC))
        self.assertEqual(len(ran), 2)                                   # no Mac: no ssh at all


class Commands(unittest.TestCase):
    """`agent-board agents`, `ask` and `looking`, run as a shell runs them, in a home folder of their own with one live
    agent (this test's own process, so its registry entry is live) and its transcript. claude and ssh are stand-in
    scripts on PATH (a real fork costs allowance, and ssh would reach the Mac): claude keeps its arguments and stdin and
    answers as `claude -p --output-format json` does, or fails; ssh answers with a window as the Mac's script would."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.cwd = self.home / "work" / "loader"
        self.cwd.mkdir(parents=True)
        (self.home / "socket").write_text("")
        # the mailroom's config as the kit's installer writes it: the owner's name, and the Mac's ssh alias
        (self.home / ".config/agent-mail").mkdir(parents=True)
        (self.home / ".config/agent-mail/config.json").write_text(json.dumps({
            "owner_name": "Alex", "owner_email": "owner@example.com", "remote_hosts": {"mac": "mac"}}))
        sessions = self.home / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "1.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": A, "name": "loader-fix", "cwd": str(self.cwd), "status": "idle",
            "kind": "interactive", "entrypoint": "cli", "messagingSocketPath": str(self.home / "socket")}))
        write_jsonl(self.home / ".claude/projects/-loader" / (A + ".jsonl"),
                    [reply("claude-opus-5-5", cwd=str(self.cwd))])
        self.bin = self.home / "stand-ins"
        self.bin.mkdir()
        init = {"type": "system", "subtype": "init", "tools": ["Glob", "Grep", "Read"], "mcp_servers": [],
                "permissionMode": "dontAsk", "session_id": C}
        answer = {"type": "result", "is_error": False, "result": "I am fixing the loader.", "session_id": C,
                  "num_turns": 1, "permission_denials": [], "usage": {}}
        self.script("claude", 'printf "%%s\\n" "$@" > "$0.args"; cat > "$0.stdin"\n'
                              '[ -n "$CLAUDE_FAILS" ] && { echo "$CLAUDE_FAILS" >&2; exit 1; }\n'
                              'echo \'%s\'\necho \'%s\'' % (json.dumps(init), json.dumps(answer)))
        window = {"ok": True, "windows": [{"app": "kitty", "title": "alex@desktop: tmux a -t loader-fix"}],
                  "errors": ["aw-watcher-web-chrome_mac: HTTP Error 500"]}
        self.script("ssh", 'cat > /dev/null\n[ -n "$SSH_FAILS" ] && { echo "$SSH_FAILS" >&2; exit 255; }\n'
                           'echo \'%s\'' % json.dumps(window))

    def script(self, name, body):
        exe = self.bin / name
        exe.write_text("#!/bin/sh\n" + body + "\n")
        exe.chmod(0o755)

    def board(self, *args, stdin="", **env):
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("AGENT_", "CLAUDE", "CODEX", "TMUX", "VOICE"))}
        clean.update(HOME=str(self.home), PATH="%s:/usr/bin:/bin" % self.bin, **env)
        return subprocess.run([sys.executable, str(Path(voicedesk.__file__).parent.parent / "bin/agent-board")]
                              + list(args), input=stdin, capture_output=True, text=True, env=clean, timeout=120)

    def test_agents_lists_the_live_agent(self):
        r = self.board("agents")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["loader-fix", "claude", "idle", A[:8], str(self.cwd)])
        self.assertEqual([a["name"] for a in json.loads(self.board("agents", "--json").stdout)], ["loader-fix"])

    def test_agents_says_when_codexs_threads_cannot_be_read(self):
        (self.home / ".codex").mkdir()
        (self.home / ".codex/state_5.sqlite").write_bytes(b"not a database" * 100)
        said = "could not read Codex's threads in %s" % (self.home / ".codex")
        r = self.board("agents")
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("could not list the agents: " + said, r.stderr)
        for args in (("ask", "loader-fix", "what", "now?"), ("looking",)):       # nor asked, nor looked for
            r = self.board(*args, "--json")
            self.assertEqual(r.returncode, 1, args)
            self.assertIn(said, json.loads(r.stdout)["error"])

    def test_ask_puts_the_question_to_a_read_only_copy_and_says_what_went_wrong(self):
        r = self.board("ask", "loader fix", "--stdin", "--effort", "", "--timeout", "60",
                       stdin="What is this mail about?")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("I am fixing the loader.\n\n-- loader-fix's read-only copy, "
                                            "claude-opus-5-5,"), r.stdout)
        argv = (self.bin / "claude.args").read_text().splitlines()    # the question's fork, of the copy just made
        self.assertEqual(argv[:5], ["-p", "--resume", C, "--fork-session", "--no-session-persistence"])
        self.assertNotIn("--effort", argv)                       # '' leaves the login's own
        copies = json.loads((self.home / ".local/state/agent-board/voice-desk/copies.json").read_text())
        self.assertEqual(copies[A]["copy"], C)
        self.assertEqual((self.bin / "claude.stdin").read_text(), voicedesk.PREAMBLE + "What is this mail about?")
        logged = sources.read_jsonl(self.home / ".local/state/agent-board/voice-desk/asks.jsonl")
        self.assertEqual([(a["ok"], a["effort"]) for a in logged], [(True, None)])
        self.assertEqual(self.board("ask", "loader-fix", "what", "now?").returncode, 0)
        self.assertEqual((self.bin / "claude.stdin").read_text(), voicedesk.PREAMBLE + "what now?")
        r = self.board("ask", "loader-fix", "what", "now?", "--json", CLAUDE_FAILS="rate limited")
        self.assertEqual(r.returncode, 1)
        self.assertIn("loader-fix's copy failed (exit 1): rate limited", json.loads(r.stdout)["error"])
        r = self.board("ask", "nobody", "hello?")
        self.assertEqual((r.returncode, r.stdout), (1, "could not ask: no live agent called 'nobody'\n"))

    def test_looking_names_the_agent_behind_the_window_and_what_activitywatch_missed(self):
        r = self.board("looking")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "You're looking at kitty: alex@desktop: tmux a -t loader-fix. Behind it: loader-fix "
                                   "(matched by its name in the title).\n(ActivityWatch: aw-watcher-web-chrome_mac: "
                                   "HTTP Error 500)\n")
        r = self.board("looking", "--json", SSH_FAILS="ssh: connect to host mac: timed out")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ssh: connect to host mac: timed out", json.loads(r.stdout)["error"])


if __name__ == "__main__":
    unittest.main()
