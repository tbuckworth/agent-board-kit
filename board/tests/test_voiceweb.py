"""The voice page (agentboard/voiceweb.py, agentboard/gemini.py, web/voice/): python3 -m unittest discover -s tests

The server tests run the real mail-web server with the voice page (voiceweb.handler) on a socket of their own, through
test_mailweb's harness (MailWebTest: the headers tailscale serve adds, a real passkey enrolment and sign-in), so the
device check and the session are the Mail page's own code. What is stood in for, each at its boundary, and why:
- Gemini: no test reaches Google, which a real mint would (with a real key, which must never be in a test). The
  mint's subprocess, gemini.py (under a key command when one is set), is a stand-in run function that keeps what it is
  given and answers as gemini.py prints; gemini.py itself runs in-process against a stand-in for urllib's urlopen that
  keeps the request and answers as Google's auth_tokens does, with made-up keys in a test's own files and variables.
- a key command (gemini_key_command, such as a secrets manager's runner): none is run; the stand-in run function
  answers for it.
- the fork: voicedesk.ask runs for real, with test_voicedesk's FakeClaude for the claude CLI (a real fork costs
  allowance); the live agents are given.
- the Mac: what_am_i_looking_at reads a window given here, as ssh to the Mac's ActivityWatch would bring one.

The page itself (VoiceInChrome) runs in headless Chrome against the real server, with Chrome's fake microphone, and
talks to a stand-in for Gemini Live's socket (FakeGemini): Chrome is told that Google's host is on this machine and to
take its made-up certificate, so the page's own policy, which allows Google's socket alone, is the one in force.
"""
import base64
import datetime
import functools
import hashlib
import http.client
import http.server
import io
import json
import os
import queue
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (first: a scratch HOME and config, an example owner)
import chrome  # noqa: E402
import stand_ins  # noqa: E402
import test_mailweb  # noqa: E402
from fixtures import write_jsonl  # noqa: E402
from test_mail import MailTest  # noqa: E402
from test_voicedesk import A, B, T, FakeClaude, reply, row  # noqa: E402
from agentboard import gemini, groups, mailweb, mailwebstate, sources, voicedesk, voiceweb  # noqa: E402


def setUpModule():
    """A copy's environment is the mailroom's scrub (relaunch.claude_env): its modules stand in here, at
    stand_ins.MAILROOM, for the live install, another repo's, with its own tests."""
    modules = stand_ins.mailroom(accounts=stand_ins.MailroomAccounts, usage=stand_ins.MailroomUsage,
                                 config=stand_ins.MailroomConfig({}))
    for name, value in (("mailroom_root", lambda: str(stand_ins.MAILROOM)), ("mailroom", modules)):
        patch = mock.patch.object(sources, name, value)
        patch.start()
        unittest.addModuleCleanup(patch.stop)


# The Mail page's policy as it was before the voice page, word for word: the voice page must not
# loosen it at all, so it is pinned here rather than read from mailweb.
MAIL_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SOCKET = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService." \
         "BidiGenerateContentConstrained"
VOICE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self' %s; "
             "base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % SOCKET)
TOKEN = "auth_tokens/" + "t" * 24        # a made-up token and key: neither is ever a real one
FAKE_KEY = "fake" * 8


class Settings(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)
        self.path = self.d / "voice.json"

    def given(self, **over):
        self.path.write_text(json.dumps(over))
        return voiceweb.settings(self.path)

    def test_the_defaults_are_the_repo_s_file_and_a_setting_overrides_one(self):
        cfg = voiceweb.settings(self.d / "none.json")
        self.assertEqual((cfg["model"], cfg["api_version"], cfg["voice"]), ("gemini-3.8-live", "v1beta", "Puck"))
        self.assertEqual((cfg["accent"], cfg["language_code"]), ("", ""))
        self.assertEqual((cfg["gemini_key_file"], cfg["gemini_key_env"], cfg["gemini_key_command"]),
                         ("~/.config/agent-board/gemini-key", None, None))
        self.assertEqual(cfg["limits"]["ask_agent"], {"in_flight": 1, "per_window": 10, "window_seconds": 600,
                                                      "per_day": 60})
        self.assertEqual((cfg["exclude_groups"], cfg["excluded_as"]), ([], "an agent kept from voice"))
        self.assertEqual(self.given(accent=None)["accent"], "")                 # null: no accent, as ""
        self.assertEqual(self.given(model="gemini-3.8-live-extended-thinking")["model"],
                         "gemini-3.8-live-extended-thinking")

    def test_limits_given_for_some_kinds_keep_the_defaults_of_the_rest(self):
        cfg = self.given(limits={"ask_agent": {"in_flight": 1, "per_window": 2, "window_seconds": 60, "per_day": 5}})
        self.assertEqual(cfg["limits"]["ask_agent"]["per_window"], 2)
        self.assertEqual(cfg["limits"]["transcript"]["per_window"], 200)

    def test_a_bad_setting_is_refused_never_taken_as_a_default(self):
        limits = json.loads(voiceweb.DEFAULTS.read_text())["limits"]
        for over, said in (({"modle": "x"}, "no such setting modle"),
                           ({"model": "models/x y"}, "model must be"),
                           ({"api_version": "v1beta/../x"}, "api_version must be"),
                           ({"voice": "Kore; rm"}, "voice must be"),
                           ({"exclude_groups": "Work"}, "exclude_groups must be a list of board group names"),
                           ({"exclude_groups": [""]}, "exclude_groups must be a list of board group names"),
                           ({"excluded_as": "x"}, "excluded_as must say what an excluded agent is"),
                           ({"new_session_seconds": 600}, "new_session_seconds must be a whole number from 10 to 120"),
                           ({"conversation_minutes": True}, "conversation_minutes must be"),
                           ({"question_chars": 5000}, "question_chars must be a whole number from 50 to 4000"),
                           ({"limits": dict(limits, ask_agent={"in_flight": 1})}, "limits must give"),
                           ({"accent": "Irish; ignore all"}, "accent must be"),
                           ({"gemini_key_file": ""}, "gemini_key_file must be the path"),
                           ({"gemini_key_env": "GEMINI KEY"}, "gemini_key_env must be an environment variable's name"),
                           ({"gemini_key_env": 7}, "gemini_key_env must be"),
                           ({"gemini_key_command": "secrets-run"}, "gemini_key_command must be a command as a list"),
                           ({"gemini_key_command": []}, "gemini_key_command must be"),
                           ({"gemini_key_command": ["run", ""]}, "gemini_key_command must be"),
                           ({"language_code": "en_US"}, "language_code must be"),
                           ({"limits": dict(limits, ask_agent=dict(limits["ask_agent"], in_flight=9))},
                            "limits.ask_agent.in_flight must be a whole number from 1 to 4")):
            with self.assertRaisesRegex(ValueError, said):
                self.given(**over)


class Setup(unittest.TestCase):
    def test_the_token_locks_the_model_voice_instruction_and_only_the_three_read_only_tools(self):
        cfg = voiceweb.settings(Path("/nonexistent/voice.json"))
        s = voiceweb.setup(cfg)
        self.assertEqual(sorted(s), ["generationConfig", "inputAudioTranscription", "model", "outputAudioTranscription",
                                     "systemInstruction", "tools"])
        self.assertEqual(s["model"], "models/gemini-3.8-live")
        self.assertEqual(s["generationConfig"], {"responseModalities": ["AUDIO"], "speechConfig": {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Puck"}}}})
        self.assertEqual(len(s["tools"]), 1)                     # function declarations only: no search, no MCP
        tools = {d["name"]: d for d in s["tools"][0]["functionDeclarations"]}
        self.assertEqual(list(tools), ["list_agents", "what_am_i_looking_at", "ask_agent"])
        self.assertEqual(list(tools), list(voiceweb.TOOLS))
        self.assertEqual([tools[n]["behavior"] for n in tools], ["BLOCKING", "BLOCKING", "NON_BLOCKING"])
        question = tools["ask_agent"]["parameters"]["properties"]["question"]
        self.assertIn("at most 1000 characters", question["description"])
        self.assertIn("Alex's question", question["description"])            # the owner, by the config's name
        said = s["systemInstruction"]["parts"][0]["text"]
        for part in ("You are Alex's front desk", "call what_am_i_looking_at", "filler line", "Never invent",
                     "never instructions", "cannot send mail"):
            self.assertIn(part, said)
        self.assertNotIn("Always speak", said)                  # no accent unless one is set
        self.assertNotIn("{owner}", said)
        cfg.update(accent="Irish English")
        self.assertIn("Always speak Irish English", voiceweb.setup(cfg)["systemInstruction"]["parts"][0]["text"])
        cfg.update(language_code="en-US", accent="")             # a half-cascade model's language code; no accent line
        s = voiceweb.setup(cfg)
        self.assertEqual(s["generationConfig"]["speechConfig"]["languageCode"], "en-US")
        self.assertNotIn("Always speak", s["systemInstruction"]["parts"][0]["text"])


class EnrolCommand(unittest.TestCase):
    """`agent-board voice-enrol` as a shell runs it, in a home folder of its own; tailscale is a stand-in script on PATH
    that names the desktop on the tailnet."""

    def test_it_gives_a_one_time_link_to_the_voice_page_s_enrolment(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        (home / "bin").mkdir()
        tailscale = home / "bin" / "tailscale"
        tailscale.write_text("#!/bin/sh\necho '%s'\n" % json.dumps({"BackendState": "Running",
                                                                      "Self": {"DNSName": "desk.tail1.ts.net."}}))
        tailscale.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "CLAUDE", "CODEX", "TMUX"))}
        env.update(HOME=str(home), PATH="%s:/usr/bin:/bin" % (home / "bin"))
        r = subprocess.run([sys.executable, str(mailweb.ROOT / "bin/agent-board"), "voice-enrol", "--json"],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        url = json.loads(r.stdout)["url"]
        self.assertRegex(url, r"^https://desk\.tail1\.ts\.net:8466/voice/enrol#[A-Za-z0-9_-]{20,}$")
        store = mailwebstate.Store(home / ".local/state/agent-board/mail-web")
        self.assertTrue(store.take_code(url.split("#", 1)[1], time.time()))      # the page's own one-time code
        self.assertFalse(store.take_code(url.split("#", 1)[1], time.time()))
        recorded = [json.loads(ln) for ln in (home / ".local/state/agent-board/actions.jsonl").read_text().splitlines()]
        self.assertEqual([(r["action"], r["page"]) for r in recorded], [("mail_web_enrol", "/voice/enrol")])
        self.assertNotIn(url.split("#", 1)[1], json.dumps(recorded))             # on the record without its code


class Urlopen:
    """Stands in for urllib.request.urlopen against Google's auth_tokens: keeps each request, and answers with
    `answer` (a dict, as JSON), or raises `raises`."""

    def __init__(self, answer=None, raises=None):
        self.requests, self.answer, self.raises = [], answer, raises

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.raises:
            raise self.raises
        return io.BytesIO(json.dumps(self.answer).encode())


class Mint(unittest.TestCase):
    """gemini.py, as voiceweb runs it: the request Google gets, and what it prints."""

    def body(self):
        return gemini.token_request({"model": "models/gemini-3.8-live"}, 1_800_000_000, 30, 600)

    def test_the_request_is_one_use_short_lived_and_locks_the_whole_setup(self):
        self.assertEqual(self.body(), {"uses": 1, "newSessionExpireTime": "2027-01-15T08:00:30Z",
                                       "expireTime": "2027-01-15T08:10:00Z",
                                       "bidiGenerateContentSetup": {"model": "models/gemini-3.8-live"}})
        self.assertNotIn("fieldMask", self.body())                # no fieldMask: the browser's setup is ignored

    def test_the_key_goes_in_its_header_alone(self):
        google = Urlopen({"name": TOKEN, "expireTime": "2027-01-15T08:10:00Z", "uses": 1,
                          "newSessionExpireTime": "2027-01-15T08:00:30Z"})
        got = gemini.mint(self.body(), "v1beta", FAKE_KEY, google)
        self.assertEqual(got, {"token": TOKEN, "expire_time": "2027-01-15T08:10:00Z",
                               "new_session_expire_time": "2027-01-15T08:00:30Z"})
        req, timeout = google.requests[0]
        self.assertEqual((req.get_method(), req.full_url, timeout),
                         ("POST", "https://generativelanguage.googleapis.com/v1beta/auth_tokens", gemini.TIMEOUT))
        self.assertEqual(req.get_header("X-goog-api-key"), FAKE_KEY)
        self.assertEqual(json.loads(req.data), self.body())
        self.assertNotIn(FAKE_KEY, req.full_url + req.data.decode())
        with self.assertRaisesRegex(gemini.MintError, "not one like v1beta"):
            gemini.mint(self.body(), "v1beta/../../evil", FAKE_KEY, google)
        self.assertEqual(len(google.requests), 1)

    def asked(self, **where):
        return io.StringIO(json.dumps(dict({"api_version": "v1beta", "request": self.body()}, **where)))

    def test_main_takes_the_key_clears_the_rest_and_prints_the_token(self):
        env = {gemini.KEY: FAKE_KEY, "OTHER_SECRET": "other", "PATH": "/bin"}
        out, google = io.StringIO(), Urlopen({"name": TOKEN})
        code = gemini.main(self.asked(key_env=gemini.KEY), out, env, google)
        self.assertEqual((code, env), (0, {}))
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "token": TOKEN, "expire_time": None,
                                                      "new_session_expire_time": None})
        self.assertEqual(google.requests[0][0].get_header("X-goog-api-key"), FAKE_KEY)
        env = {"MY_GEMINI": FAKE_KEY, gemini.KEY: "not this one"}                 # a variable the owner named
        google = Urlopen({"name": TOKEN})
        self.assertEqual(gemini.main(self.asked(key_env="MY_GEMINI"), io.StringIO(), env, google), 0)
        self.assertEqual((google.requests[0][0].get_header("X-goog-api-key"), env), (FAKE_KEY, {}))

    def test_a_key_file_of_the_owner_s_alone_is_read_by_the_mint_itself(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        path = d / "gemini-key"
        path.write_text("  %s\n" % FAKE_KEY)
        path.chmod(0o600)
        env, out, google = {"OTHER_SECRET": "other"}, io.StringIO(), Urlopen({"name": TOKEN})
        self.assertEqual(gemini.main(self.asked(key_file=str(path)), out, env, google), 0, out.getvalue())
        self.assertEqual((google.requests[0][0].get_header("X-goog-api-key"), env), (FAKE_KEY, {}))
        link = d / "link"
        link.symlink_to(path)
        (d / "two-lines").write_text("a\nb\n")
        (d / "two-lines").chmod(0o600)
        (d / "big").write_text("k" * (gemini.KEY_FILE_BYTES + 1))
        (d / "big").chmod(0o600)
        for key_file, said in ((str(d / "none"), "does not exist; put it in ~/.config/agent-board/gemini-key"),
                               (str(link), "could not be opened"),                       # never through a link
                               (str(d), "is not a regular file"),
                               (str(d / "two-lines"), "must hold the key alone, on one line"),
                               (str(d / "big"), "is over 4096 bytes")):
            out, google = io.StringIO(), Urlopen({"name": TOKEN})
            self.assertEqual(gemini.main(self.asked(key_file=key_file), out, {}, google), 1, key_file)
            self.assertIn(said, json.loads(out.getvalue())["error"])
            self.assertEqual(google.requests, [])
        for mode in (0o640, 0o604, 0o660):                       # others may read it: refused, saying how to fix it
            path.chmod(mode)
            out, google = io.StringIO(), Urlopen({"name": TOKEN})
            self.assertEqual(gemini.main(self.asked(key_file=str(path)), out, {}, google), 1)
            self.assertIn("may be read by others (mode %o); make it private: chmod 600" % mode,
                          json.loads(out.getvalue())["error"])
            self.assertNotIn(FAKE_KEY, out.getvalue())
            self.assertEqual(google.requests, [])

    def test_what_goes_wrong_is_said_without_the_key(self):
        refused = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(json.dumps(
            {"error": {"code": 400, "message": "API key %s not valid" % FAKE_KEY}}).encode()))
        with_key = {gemini.KEY: FAKE_KEY}
        cases = [(with_key, Urlopen(raises=refused), "Google refused the token \\(HTTP 400\\): API key \\[the key\\] "
                                                     "not valid"),
                 (with_key, Urlopen(raises=urllib.error.URLError("no route")), "could not ask Google"),
                 (with_key, Urlopen(raises=http.client.RemoteDisconnected("closed")),
                  "could not ask Google for a " + "token: Remote.*: closed"),
                 (with_key, Urlopen({"error": "?"}), "Google's answer holds no token"),
                 (with_key, Urlopen({"name": "auth_tokens/a b"}), "Google's answer holds no token"),
                 ({}, Urlopen({"name": TOKEN}), "the environment variable GEMINI_API_KEY \\(voice.json's "
                                                "gemini_key_env\\) is not set")]
        for env, google, said in cases:
            out = io.StringIO()
            code = gemini.main(self.asked(key_env=gemini.KEY), out, dict(env), google)
            got = json.loads(out.getvalue())
            self.assertEqual((code, got["ok"]), (1, False))
            self.assertRegex(got["error"], said)
            self.assertNotIn(FAKE_KEY, out.getvalue())
        out = io.StringIO()
        self.assertEqual(gemini.main(io.StringIO("not json"), out, dict(with_key), Urlopen()), 1)
        self.assertIn("not a JSON object with a request", out.getvalue())
        out = io.StringIO()
        self.assertEqual(gemini.main(self.asked(), out, dict(with_key), Urlopen()), 1)   # neither a variable nor a file
        self.assertIn("names neither a variable nor a file", out.getvalue())
        self.assertNotIn(FAKE_KEY, out.getvalue())


    def test_a_redirect_is_refused_so_the_key_goes_nowhere_else(self):
        hits = []

        class Google(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append((self.path, self.headers.get("x-goog-api-key")))
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/elsewhere" % self.server.server_port)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Google)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        req = urllib.request.Request("http://127.0.0.1:%d/auth_tokens" % srv.server_port, data=b"{}", method="POST",
                                     headers={"x-goog-api-key": FAKE_KEY})
        with self.assertRaises(urllib.error.HTTPError) as got:
            gemini.URLOPEN(req, timeout=10)
        got.exception.close()
        self.assertEqual((got.exception.code, hits), (302, [("/auth_tokens", FAKE_KEY)]))


class Limits(unittest.TestCase):
    def test_calls_at_once_per_window_and_per_day(self):
        lim = voiceweb.Limiter("ask_agent")
        lim.take(0, 1, 2, 600, 3)
        with self.assertRaisesRegex(mailweb.Refusal, "ask_agent: 1 already going; wait for it"):
            lim.take(1, 1, 2, 600, 3)
        lim.give()
        lim.take(2, 1, 2, 600, 3)
        lim.give()
        with self.assertRaisesRegex(mailweb.Refusal, "2 in 10 minutes is the limit; try again in 599 seconds"):
            lim.take(2, 1, 2, 600, 3)
        lim.take(600.5, 1, 2, 600, 3)                            # the first has left the window
        lim.give()
        with self.assertRaisesRegex(mailweb.Refusal, "3 in a day is the limit; try again in 1419 minutes"):
            lim.take(1300, 1, 2, 600, 3)
        lim.take(voiceweb.DAY + 1, 1, 2, 600, 3)                 # the first has left the day


class MintRun:
    """Stands in for subprocess.run running `python3 -I gemini.py` (see the module's docstring): keeps each call
    and answers with what gemini.py would print, `printed`; `gate`, when set, holds each call until it is set."""

    def __init__(self, printed=None, code=0, stderr=""):
        self.calls, self.code, self.stderr, self.gate = [], code, stderr, None
        self.printed = printed if printed is not None else json.dumps({
            "ok": True, "token": TOKEN, "expire_time": "2027-01-15T08:10:00Z",
            "new_session_expire_time": "2027-01-15T08:00:30Z"}) + "\n"

    def __call__(self, argv, **kw):
        self.calls.append(dict(kw, argv=argv))
        if self.gate:
            self.gate.wait(10)
        return subprocess.CompletedProcess(argv, self.code, self.printed, self.stderr)


class VoicePage(test_mailweb.MailWebTest):
    """The voice page on the real server (see the module's docstring). Only the tests below run here (load_tests):
    MailWebTest's own run in test_mailweb."""

    def setUp(self):
        self.vd = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.vd)
        self.cwd = self.vd / "loader"
        self.cwd.mkdir()
        write_jsonl(self.vd / "home/projects/-loader" / (A + ".jsonl"), [reply("claude-opus-5-5", cwd=str(self.cwd))])
        self.rows = [row(A, "loader-fix", cwd=str(self.cwd)), row(B, "survey-model", cwd="/p", status="busy")]
        self.claude, self.mint_run = FakeClaude(), MintRun()
        self.config = self.vd / "voice.json"
        ask = functools.partial(voicedesk.ask, popen=lambda *a, **kw: self.claude(*a, **kw), claude_bin="/bin/claude",
                                homes=[("default", self.vd / "home")], log=self.vd / "asks.jsonl", copies_path=None)
        self.window = {"ok": True, "windows": [{"app": "kitty", "title": "alex@desktop: tmux a -t loader-fix"}]}
        self.desk = voiceweb.Desk(config=self.config, agents=lambda: self.rows, ask=ask,
                                  looking=lambda: voicedesk.looking(self.window, rows=self.rows,
                                                                    db=self.vd / "none.sqlite"),
                                  run=self.mint_run, clock=lambda: self.now, transcripts=self.vd / "transcripts")
        super().setUp()
        self.board([])                                     # MailTest's groups.json: a layout, its Work group empty

    def board(self, work, other=()):
        """The board's layout as the Mac sends it: `work` in Work (one in a sub-group), `other` in Personal."""
        groups.save_groups([{"name": "Work", "members": list(work[:1]), "subgroups": [
                                {"name": "Evals", "members": list(work[1:])}]},
                            {"name": "Personal", "members": list(other), "subgroups": []}], live_ids=set())

    def start(self, **cfg):
        """MailWebTest's server, answering with the voice page's handler (as `agent-board mail-web` does): the phone
        and the tablet are the Mail page's devices, and the phone and the Mac the voice page's."""
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        cfg = dict(mailwebstate.DEFAULTS, **dict({"devices": [test_mailweb.PHONE, test_mailweb.TABLET],
                                                  "voice_devices": [test_mailweb.PHONE, test_mailweb.MAC]}, **cfg))
        keyring = getattr(self, "keyring", None)          # MailWebTest's stand-in keyring, when it has one
        self.guard = mailweb.Guard(cfg, [test_mailweb.OWNER], test_mailweb.DESK, str(self.sock), self.store,
                                   self.commands, self.popen, clock=lambda: self.now,
                                   read_keyring=keyring.read if keyring else None,
                                   unlock_command=keyring.command if keyring else None)
        self.srv = mailweb.PageServer(self.sock, self.guard, voiceweb.handler(self.desk))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def voice(self, req, **headers):
        """(status, answer) of one call to the voice page's API; a session cookie it sets is kept for the next calls."""
        headers.setdefault("X-Agent-Board", "1")
        status, got, body = self.ask("POST", "/voice/api", json.dumps(req).encode(), **headers)
        cookie = got.get("Set-Cookie", "")
        if cookie.startswith(mailweb.COOKIE + "="):
            self.cookie = cookie.split(";")[0].split("=", 1)[1] or None
        return status, json.loads(body) if body.startswith(b"{") else body

    def tool(self, name, args=None, **headers):
        return self.voice({"action": "tool", "name": name, "args": {} if args is None else args}, **headers)

    def settle(self, **over):
        self.config.write_text(json.dumps(over))

    # the route and its headers

    def test_the_voice_page_has_its_own_headers_and_the_mail_page_keeps_its_own(self):
        for path, ctype in (("/voice", "text/html"), ("/voice/voice.js", "text/javascript"),
                            ("/voice/capture.js", "text/javascript"), ("/voice/voice.css", "text/css")):
            status, got, body = self.ask("GET", path)
            self.assertEqual((status, got["Content-Type"].split(";")[0]), (200, ctype), path)
            self.assertEqual((got["Content-Security-Policy"], got["Permissions-Policy"]),
                             (VOICE_CSP, "camera=(), microphone=(self), geolocation=()"), path)
            for h in ("Cache-Control", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
                self.assertEqual(got[h], dict(mailweb.SECURITY_HEADERS)[h])
        self.assertEqual(self.ask("GET", "/voice/../app.js")[0], 404)
        status, got, _ = self.ask("GET", "/voice/nothing")
        self.assertEqual((status, got["Content-Security-Policy"]), (404, VOICE_CSP))
        # the Mail page, on the same server: its own policy, unchanged, and no microphone
        for status, got, _ in (self.ask("GET", "/"), self.ask("GET", "/app.js"),
                               self.ask("POST", "/api", b'{"action": "status"}', **{"X-Agent-Board": "1"})):
            self.assertEqual((status, got["Content-Security-Policy"], got["Permissions-Policy"]),
                             (200, MAIL_CSP, "camera=(), microphone=(), geolocation=()"))
        self.assertEqual(dict(mailweb.SECURITY_HEADERS)["Content-Security-Policy"], MAIL_CSP)
        status, got, _ = self.ask("POST", "/voice/api", b'{"action": "status"}', **{"X-Agent-Board": "1"})
        self.assertEqual((status, got["Content-Security-Policy"]), (200, VOICE_CSP))

    def test_a_voice_call_is_small(self):
        self.signed_in()
        big = json.dumps({"action": "tool", "name": "list_agents", "args": {}, "pad": "x" * voiceweb.MAX_BODY})
        status, _, body = self.ask("POST", "/voice/api", big.encode(), **{"X-Agent-Board": "1"})
        self.assertEqual((status, json.loads(body)["error"]), (413, "a request is 1 to %d bytes" % voiceweb.MAX_BODY))

    def test_the_page_s_icon_is_its_own_not_the_mail_page_s(self):
        page = (voiceweb.WEB / "index.html").read_text()
        self.assertNotIn("/icons/", page)
        status, got, body = self.ask("GET", "/voice/icon.png", address=test_mailweb.MAC_IP)
        self.assertEqual((status, got["Content-Type"], body[:4]), (200, "image/png", b"\x89PNG"))

    def test_a_conversation_s_transcript_is_saved_on_the_desktop(self):
        self.signed_in()
        conv = "20261123-141502-a1b2c3"
        call = {"action": "transcript", "conversation": conv,
                "entries": [{"who": "you", "text": "What is\nloader-fix doing?"},
                            {"who": "desk", "text": "It says…"}]}
        self.assertEqual(self.voice(call)[0], 200)
        self.assertEqual(self.voice(dict(call, entries=[{"who": "note", "text": "Conversation ended"}]))[0], 200)
        path = self.vd / "transcripts" / (conv + ".txt")
        lines = path.read_text().splitlines()
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d Alex: What is loader-fix doing\?$")
        self.assertEqual([l[9:] for l in lines[1:]], ["Front desk: It says…", "Note: Conversation ended"])
        self.assertEqual((path.stat().st_mode & 0o777, path.parent.stat().st_mode & 0o777), (0o600, 0o700))
        for bad, said in (({"conversation": "../../x"}, "conversation must be"),
                          ({"entries": []}, "entries must be a list"),
                          ({"entries": [{"who": "alex", "text": "x"}]}, "an entry is"),
                          ({"entries": [{"who": "you", "text": 3}]}, "an entry is"),
                          ({"extra": 1}, "transcript takes")):
            status, got = self.voice(dict(call, **bad))
            self.assertEqual(status, 400, bad)
            self.assertIn(said, got["error"])
        self.cookie = None
        self.assertEqual(self.voice(call)[0], 401)                    # signed in, as every call but status
        self.assertEqual(sorted(p.name for p in (self.vd / "transcripts").iterdir()), [conv + ".txt"])

    def test_a_transcript_is_bounded(self):
        self.signed_in()
        call = {"action": "transcript", "conversation": "20261123-141502-a1b2c3",
                "entries": [{"who": "desk", "text": "x" * 6000}] * 2}
        with mock.patch.object(voiceweb, "TRANSCRIPT_MAX_BYTES", 20000):
            for _ in range(2):
                self.assertEqual(self.voice(call)[0], 200)
            status, got = self.voice(call)
        self.assertEqual((status, got["error"]), (413, "this conversation's transcript is full (20000 bytes)"))
        self.assertLessEqual((self.vd / "transcripts/20261123-141502-a1b2c3.txt").stat().st_size, 20000)
        small = dict(call, conversation="20261123-141502-ffffff", entries=[{"who": "you", "text": "y"}])
        for _ in range(197):                                         # 3 above: 200 in ten minutes
            self.voice(small)
        status, got = self.voice(small)
        self.assertEqual(status, 429)
        self.assertIn("transcript: 200 in 10 minutes is the limit", got["error"])

    def test_the_page_s_scripts_keep_nothing_on_the_device(self):
        scripts = [f for f, ctype in voiceweb.PAGES.values() if ctype.startswith("text/javascript")]
        self.assertEqual({f.name for f in scripts}, {"voice.js", "capture.js", "passkeys.js"})
        for f in scripts:
            for word in ("serviceWorker", "localStorage", "sessionStorage", "indexedDB", "caches.", "document.cookie",
                         "innerHTML", "insertAdjacentHTML", "eval("):
                self.assertNotIn(word, f.read_text(), f.name)

    def test_the_voice_api_and_the_mail_api_are_apart(self):
        self.signed_in()
        self.assertEqual(self.voice({"action": "mail_list", "box": "inbox"}),
                         (400, {"ok": False, "error": "action must be one of status, start, tool, "
                                                      "transcript, enrol_begin, enrol_finish, signin_begin, "
                                                      "signin_finish, signout"}))
        for action in ("mail_reply", "mail_researcher", "orchestrate", "stepup_begin", "mail_show", "mail_fast"):
            self.assertEqual(self.voice({"action": action})[0], 400, action)
        status, got = self.api({"action": "start"})
        self.assertEqual((status, got["error"]), (400, "action must be one of " + ", ".join(mailweb.API)))
        self.assertEqual(self.mint_run.calls, [])

    def test_only_the_owner_on_their_device_reaches_the_voice_page(self):
        for how in (dict(login=None), dict(login="someone@else.org"), dict(address=test_mailweb.TABLET_IP),
                    dict(**{"Tailscale-Funnel-Request": "?1"}), dict(host="evil.example:8466"), dict(proto="http")):
            self.assertEqual(self.ask("GET", "/voice", **how)[0], 403, how)
            self.assertEqual(self.voice({"action": "status"}, **how)[0], 403, how)
        self.serve_status = {"AllowFunnel": {test_mailweb.DESK + ":8466": True}}
        self.now += mailweb.FUNNEL_CHECK
        self.assertEqual(self.ask("GET", "/voice")[0], 503)
        self.assertEqual(self.voice({"action": "status"})[0], 503)

    def test_a_voice_only_device_reaches_the_voice_page_and_nothing_of_the_mail_page(self):
        mac = dict(address=test_mailweb.MAC_IP)
        self.assertEqual(self.ask("GET", "/voice", **mac)[0], 200)
        self.assertEqual(self.voice({"action": "status"}, **mac)[0], 200)
        for method, path, body in (("GET", "/", None), ("GET", "/app.js", None), ("GET", "/enrol", None),
                                   ("GET", "/part/s7/2", None),
                                   ("POST", "/api", {"action": "status"}), ("POST", "/api", {"action": "signin_begin"}),
                                   ("POST", "/api", {"action": "mail_list", "box": "inbox"}),
                                   ("POST", "/api", {"action": "stepup_begin", "approves": "mail_reply",
                                                     "digest": "0" * 64}),
                                   ("POST", "/api", {"action": "orchestrate", "request": "{}"})):
            status, _, _ = self.ask(method, path, json.dumps(body).encode() if body else None,
                                    **dict(mac, **{"X-Agent-Board": "1"}))
            self.assertEqual(status, 403, (method, path, body))
        self.assertIn("device laptop.tail1.ts.net (nMac3CNTRL) is not one the page answers", self.log())
        self.assertEqual(self.agent_mail.calls, [])

    def test_a_voice_only_device_enrols_and_signs_in_here_and_its_session_opens_no_mail(self):
        mac, passkey = dict(address=test_mailweb.MAC_IP), test_mailweb.Authenticator()
        self.assertEqual(self.voice({"action": "status"}, **mac)[1]["enrolled"], False)
        code, _ = self.store.mint_code(self.now, 10)
        status, got = self.voice({"action": "enrol_begin", "code": code}, **mac)
        self.assertEqual(status, 200, got)
        status, got = self.voice({"action": "enrol_finish", "credential": passkey.create(got["options"])}, **mac)
        self.assertEqual((status, got), (200, {"ok": True}))
        self.assertIn("on device laptop.tail1.ts.net", (self.d / "mail-stdin").read_text())   # Alex is told
        self.assertEqual(self.store.passkeys()[webauthn_id(passkey)]["device"], test_mailweb.MAC)
        self.assertEqual(self.voice({"action": "status"}, **mac)[1]["signed_in"], True)
        self.assertEqual(self.tool("list_agents", **mac)[0], 200)
        # the Mac's session is no key to the Mail page: its device is refused before any session is looked at
        for method, path, body in (("GET", "/", None), ("POST", "/api", {"action": "mail_list", "box": "inbox"}),
                                   ("POST", "/api", {"action": "stepup_begin", "approves": "orchestrate",
                                                     "digest": "0" * 64})):
            self.assertEqual(self.ask(method, path, json.dumps(body).encode() if body else None,
                                      **dict(mac, **{"X-Agent-Board": "1"}))[0], 403, path)
        # nor is it the phone's: a session stays on the device that signed in
        self.assertEqual(self.tool("list_agents")[0], 401)
        self.assertEqual(self.voice({"action": "signout"}, **mac)[1], {"ok": True})
        self.assertEqual(self.tool("list_agents", **mac)[0], 401)
        # signing in again, on the voice page, with the passkey
        status, got = self.voice({"action": "signin_begin"}, **mac)
        status, got = self.voice({"action": "signin_finish", "credential": passkey.get(got["options"])}, **mac)
        self.assertEqual((status, got), (200, {"ok": True}))
        self.assertEqual(self.tool("list_agents", **mac)[0], 200)
        self.assertEqual(self.agent_mail.calls, [])

    @unittest.skipUnless(hasattr(test_mailweb, "NONCE"), "test_mailweb has no keyring stand-in (the keyring unlock's "
                         "tests bring it)")
    def test_a_voice_only_device_cannot_unlock_the_desktop_nor_begin_its_step_up(self):
        """Signed in with its own passkey on the voice page, the Mac is still refused the Mail page's unlock and the
        step-up for it (its device, before anything else is looked at), and the voice API offers neither: the keyring is
        never read, keyring-unlock never runs, and the password is nowhere."""
        mac, passkey = dict(address=test_mailweb.MAC_IP), test_mailweb.Authenticator()
        code, _ = self.store.mint_code(self.now, 10)
        status, got = self.voice({"action": "enrol_begin", "code": code}, **mac)
        status, got = self.voice({"action": "enrol_finish", "credential": passkey.create(got["options"])}, **mac)
        self.assertEqual((status, got), (200, {"ok": True}))
        self.keyring.answer(0, {"result": "unlocked", "locked": False})
        request = json.dumps({"nonce": test_mailweb.NONCE})
        begin = json.dumps({"action": "stepup_begin", "approves": mailweb.UNLOCK,
                            "digest": hashlib.sha256(request.encode()).hexdigest()}).encode()
        unlock = (json.dumps({"action": mailweb.UNLOCK, "request": request, "credential": {}}).encode() + b"\n"
                  + test_mailweb.TYPED.encode())
        for path, body, refused in (("/api", begin, 403), ("/api", unlock, 403), ("/voice/api", begin, 400),
                                    ("/voice/api", unlock, 400)):
            status, _, said = self.ask("POST", path, body, **dict(mac, **{"X-Agent-Board": "1"}))
            self.answers.append(said)
            self.assertEqual(status, refused, (path, body[:40]))
        self.assertEqual((self.keyring.reads, self.keyring.runs(), self.audited()), (0, [], []))
        self.assert_nowhere()

    def test_a_mail_device_reaches_the_voice_page_only_when_listed_for_it(self):
        self.signed_in()                                                       # the phone, on the Mail page
        self.assertEqual(self.tool("list_agents")[0], 200)                    # its session holds here too
        self.start(proxy_uids=[os.getuid()], voice_devices=[test_mailweb.MAC])
        self.assertEqual(self.ask("GET", "/voice")[0], 403)
        self.assertEqual(self.tool("list_agents")[0], 403)
        self.assertEqual(self.api({"action": "status"})[1]["signed_in"], True)   # the Mail page is as it was

    def test_every_call_but_status_needs_the_mail_page_s_sign_in(self):
        self.assertEqual(self.voice({"action": "status"}), (200, {"ok": True, "signed_in": False, "enrolled": False,
                                                                  "ready": True, "error": None,
                                                                  "model": "gemini-3.8-live"}))
        for req in ({"action": "start"}, {"action": "tool", "name": "list_agents", "args": {}}):
            self.assertEqual(self.voice(req), (401, {"ok": False, "error": "not signed in"}))
        self.assertEqual(self.voice({"action": "start"}, **{"X-Agent-Board": "0"})[0], 404)   # not the page's script
        self.assertEqual((self.mint_run.calls, self.claude.calls), ([], []))
        self.signed_in()
        self.assertTrue(self.voice({"action": "status"})[1]["signed_in"])
        self.assertEqual(self.tool("list_agents")[0], 200)
        self.cookie = "a-made-up-session"
        self.assertEqual(self.tool("list_agents")[0], 401)

    # the token

    def test_a_token_is_minted_for_one_conversation_and_never_logged(self):
        self.signed_in()
        status, got = self.voice({"action": "start"})
        self.assertEqual((status, got), (200, {"ok": True, "token": TOKEN, "url": SOCKET, "model": "gemini-3.8-live"}))
        call = self.mint_run.calls[0]
        self.assertEqual(call["argv"], [sys.executable, "-I", str(Path(gemini.__file__).resolve())])
        self.assertEqual((call["cwd"], sorted(call["env"])), (str(mailweb.ROOT), ["HOME", "LANG", "PATH"]))
        asked = json.loads(call["input"])
        self.assertEqual((asked["api_version"], asked["key_env"], asked["key_file"]),
                         ("v1beta", None, str(Path.home() / ".config/agent-board/gemini-key")))   # read by gemini.py
        setup = voiceweb.setup(voiceweb.settings(self.config))
        self.assertEqual(asked["request"], gemini.token_request(setup, self.now, 30, 600))
        self.assertNotIn(gemini.KEY, json.dumps(call) + self.log())   # the key goes to gemini.py alone
        self.assertNotIn(TOKEN, self.log())                           # the token goes to the page and nowhere else
        self.assertIn("voice: a Gemini Live token for gemini-3.8-live", self.log())
        for folder in (self.d, self.vd):
            for f in folder.rglob("*"):
                if f.is_file():
                    self.assertNotIn(TOKEN.encode(), f.read_bytes(), f)

    def test_the_key_comes_from_a_named_variable_or_through_a_key_command_never_an_argument(self):
        self.signed_in()
        self.settle(gemini_key_env="MY_GEMINI")                 # in mail-web's own environment: passed to the mint alone
        with mock.patch.dict(os.environ, {"MY_GEMINI": FAKE_KEY, "OTHER_SECRET": "other"}):
            self.assertEqual(self.voice({"action": "start"})[0], 200)
        call = self.mint_run.calls[-1]
        self.assertEqual(call["argv"], [sys.executable, "-I", str(Path(gemini.__file__).resolve())])
        self.assertEqual((sorted(call["env"]), call["env"]["MY_GEMINI"]), (["HOME", "LANG", "MY_GEMINI", "PATH"],
                                                                           FAKE_KEY))
        self.assertEqual({k: json.loads(call["input"])[k] for k in ("key_env", "key_file")},
                         {"key_env": "MY_GEMINI", "key_file": None})
        self.settle(gemini_key_command=["secrets-run", "--project", "agents"])     # the command puts it in GEMINI_API_KEY
        with mock.patch.dict(os.environ, {gemini.KEY: FAKE_KEY}):
            self.assertEqual(self.voice({"action": "start"})[0], 200)
        call = self.mint_run.calls[-1]
        self.assertEqual(call["argv"], [str(self.root / "bin" / "secrets-run"), "--project", "agents", sys.executable,
                                        "-I", str(Path(gemini.__file__).resolve())])
        self.assertEqual(sorted(call["env"]), ["HOME", "LANG", "PATH"])         # this server's key is not passed on
        self.assertEqual(json.loads(call["input"])["key_env"], gemini.KEY)
        for c in self.mint_run.calls:
            self.assertNotIn(FAKE_KEY, json.dumps(c["argv"]) + c["input"])
        self.assertNotIn(FAKE_KEY, self.log())

    def test_no_token_is_said_without_the_token_or_the_key(self):
        self.signed_in()
        for run, said in ((MintRun(json.dumps({"ok": False, "error": "no Gemini API key: /h/gemini-key does not "
                                                                       "exist"})),
                           "no Gemini token: no Gemini API key: /h/gemini-key does not exist"),
                          (MintRun("", code=1, stderr="Traceback: boom"),
                           "no Gemini token: the mint exited 1: Traceback: boom")):
            self.desk.run = run
            self.assertEqual(self.voice({"action": "start"}), (502, {"ok": False, "error": said}))
        self.settle(gemini_key_command=["secrets-run"])
        self.desk.run = MintRun("", code=1, stderr="secrets-run: no project id")
        self.assertEqual(self.voice({"action": "start"}),
                         (502, {"ok": False, "error": "no Gemini token: secrets-run exited 1: secrets-run: no project "
                                                      "id"}))

    def test_the_mint_is_limited(self):
        self.signed_in()
        self.mint_run.gate = threading.Event()
        going = threading.Thread(target=self.voice, args=({"action": "start"},))
        going.start()
        self.addCleanup(going.join)
        self.addCleanup(self.mint_run.gate.set)
        while not self.mint_run.calls:
            time.sleep(0.01)
        self.assertEqual(self.voice({"action": "start"}), (429, {"ok": False, "error": "start: 1 already going; wait "
                                                                                       "for it"}))
        self.mint_run.gate.set()
        going.join()
        self.mint_run.gate = None
        for _ in range(5):
            self.assertEqual(self.voice({"action": "start"})[0], 200)
        status, got = self.voice({"action": "start"})
        self.assertEqual(status, 429)
        self.assertRegex(got["error"], "start: 6 in 10 minutes is the limit")
        self.now += 600
        self.assertEqual(self.voice({"action": "start"})[0], 200)
        self.assertEqual(len(self.mint_run.calls), 7)

    # the tools

    def test_list_agents_and_what_am_i_looking_at(self):
        self.signed_in()
        self.assertEqual(self.tool("list_agents"), (200, {"ok": True, "response": {
            "agents": [{"name": "loader-fix", "status": "idle", "folder": "loader"},
                       {"name": "survey-model", "status": "busy", "folder": "p"}], "more": 0,
            "note": voiceweb.DATA_NOTE}}))
        status, got = self.tool("what_am_i_looking_at")
        self.assertEqual((status, got["response"]["agent"], got["response"]["note"]),
                         (200, "loader-fix", voiceweb.DATA_NOTE))
        self.assertIn("You're looking at kitty: alex@desktop: tmux a -t loader-fix. Behind it: loader-fix",
                      got["response"]["summary"])
        self.desk.looking = lambda: {"ok": False, "error": "could not read the Mac's front window: asleep"}
        self.assertEqual(self.tool("what_am_i_looking_at")[1]["response"],
                         {"error": "could not read the Mac's front window: asleep"})
        self.assertIn("voice: what_am_i_looking_at failed: could not read the Mac", self.log())

    def test_an_agent_is_asked_through_its_read_only_fork_and_its_answer_is_data(self):
        self.signed_in()
        status, got = self.tool("ask_agent", {"agent": "Loader-Fix ", "question": "What is\x07 this\x00 mail about?"})
        self.assertEqual((status, got), (200, {"ok": True, "response": {
            "agent": "loader-fix", "answer": "I am fixing the loader.", "note": voiceweb.DATA_NOTE}}))
        call = self.claude.calls[0]
        self.assertEqual(call["argv"], voicedesk.fork_argv("/bin/claude", A, "loader-fix", "claude-opus-5-5"))
        self.assertEqual(call["input"], voicedesk.PREAMBLE + "What is  this  mail about?")
        self.assertEqual(call["cwd"], str(self.cwd))
        self.assertIn("voice: ask_agent answered", self.log())
        self.settle(answer_chars=200)
        self.claude = FakeClaude(answer="x" * 5000)
        self.assertEqual(len(self.tool("ask_agent", {"agent": "loader-fix", "question": "q"})[1]["response"]["answer"]),
                         200)
        self.claude = FakeClaude(code=1, stdout="")
        self.assertEqual(self.tool("ask_agent", {"agent": "loader-fix", "question": "q"})[1]["response"],
                         {"agent": "loader-fix", "error": "loader-fix's copy failed (exit 1): boom"})

    def test_tool_calls_are_checked_and_bounded_before_anything_runs(self):
        self.signed_in()
        for req, said in (({"name": "send_mail"}, "name must be one of list_agents, what_am_i_looking_at, ask_agent"),
                          ({"name": ["ask_agent"]}, "name must be one of"),
                          ({"name": "ask_agent", "args": "loader-fix"}, "args must be a JSON object"),
                          ({"name": "list_agents", "args": {"all": True}}, "this tool takes no arguments"),
                          ({"name": "what_am_i_looking_at", "args": {"x": 1}}, "this tool takes no arguments"),
                          ({"name": "ask_agent", "args": {"agent": "loader-fix"}}, "exactly an agent and a question"),
                          ({"name": "ask_agent", "args": {"agent": "loader-fix", "question": "q", "model": "opus"}},
                           "exactly an agent and a question"),
                          ({"name": "ask_agent", "args": {"agent": 7, "question": "q"}}, "agent must be an agent's"),
                          ({"name": "ask_agent", "args": {"agent": "x" * 101, "question": "q"}}, "1 to 100 characters"),
                          ({"name": "ask_agent", "args": {"agent": "loader-fix", "question": ["q"]}},
                           "question must be text"),
                          ({"name": "ask_agent", "args": {"agent": "loader-fix", "question": " \x00 "}},
                           "question must be 1 to 1000 characters"),
                          ({"name": "ask_agent", "args": {"agent": "loader-fix", "question": "q" * 1001}},
                           "question must be 1 to 1000 characters"),
                          ({"name": "ask_agent", "args": {"agent": "loader", "question": "q"}},
                           "no live agent is called 'loader': use a name list_agents gives"),
                          ({"name": "ask_agent", "args": {"agent": A, "question": "q"}}, "no live agent is called"),
                          ({"name": "ask_agent", "args": {"agent": "voice-copy-of-loader-fix", "question": "q"}},
                           "no live agent is called")):
            status, got = self.voice(dict(req, action="tool"))
            self.assertEqual(status, 400, req)
            self.assertIn(said, got["error"], req)
        self.assertEqual(self.claude.calls, [])
        self.rows.append(row("c" * 36, "loader-fix"))
        self.assertIn("2 live agents are called 'loader-fix'",
                      self.tool("ask_agent", {"agent": "loader-fix", "question": "q"})[1]["error"])
        self.assertEqual(self.claude.calls, [])

    def test_asks_are_one_at_a_time_and_so_many_in_ten_minutes(self):
        self.signed_in()
        gate, claude = threading.Event(), self.claude

        def held(*a, **kw):
            gate.wait(10)
            return claude(*a, **kw)
        self.claude = held
        going = threading.Thread(target=self.tool, args=("ask_agent", {"agent": "loader-fix", "question": "q"}))
        going.start()
        self.addCleanup(going.join)
        self.addCleanup(gate.set)
        while not self.desk.limiters["ask_agent"].going:
            time.sleep(0.01)
        self.assertEqual(self.tool("ask_agent", {"agent": "survey-model", "question": "q"}),
                         (429, {"ok": False, "error": "ask_agent: 1 already going; wait for it"}))
        self.assertEqual(self.tool("list_agents")[0], 200)                # the other tools have limits of their own
        gate.set()
        going.join()
        self.claude = claude
        for _ in range(9):
            self.assertEqual(self.tool("ask_agent", {"agent": "loader-fix", "question": "q"})[0], 200)
        status, got = self.tool("ask_agent", {"agent": "loader-fix", "question": "q"})
        self.assertEqual(status, 429)
        self.assertRegex(got["error"], "ask_agent: 10 in 10 minutes is the limit; try again in [0-9]+ seconds")
        self.assertEqual(len(claude.calls), 10)
        self.now += 600
        self.assertEqual(self.tool("ask_agent", {"agent": "loader-fix", "question": "q"})[0], 200)

    # agents in groups kept from Google (exclude_groups): never asked by voice, so their work never goes to Google

    def exclude_work(self):
        self.settle(exclude_groups=["Work"], excluded_as="a work agent")

    def test_by_default_no_group_is_kept_from_voice(self):
        self.signed_in()
        self.board([B], other=[A])
        self.assertEqual([a["name"] for a in self.tool("list_agents")[1]["response"]["agents"]],
                         ["loader-fix", "survey-model"])
        groups.GROUPS.unlink()                            # nor is the board's layout needed
        self.assertEqual(self.tool("ask_agent", {"agent": "survey-model", "question": "q"})[0], 200)

    def test_a_work_agent_is_not_listed_nor_asked(self):
        self.signed_in()
        self.exclude_work()
        self.board([B], other=[A])
        self.assertEqual([a["name"] for a in self.tool("list_agents")[1]["response"]["agents"]], ["loader-fix"])
        status, got = self.tool("ask_agent", {"agent": "survey-model", "question": "What are the results?"})
        self.assertEqual((status, got), (403, {"ok": False,
                                               "error": "that's a work agent; it isn't available by voice"}))
        self.assertEqual(self.claude.calls, [])
        self.assertEqual(self.tool("ask_agent", {"agent": "loader-fix", "question": "q"})[0], 200)

    def test_a_codex_work_agent_is_refused_as_a_claude_one_is(self):
        self.signed_in()
        self.exclude_work()
        self.rows.append(row(T, "eval-consolidation", cwd=str(self.cwd), cli="codex", model="gpt-6-astra"))
        self.board([T], other=[A, B])
        self.assertNotIn("eval-consolidation", [a["name"] for a in self.tool("list_agents")[1]["response"]["agents"]])
        status, got = self.tool("ask_agent", {"agent": "eval-consolidation", "question": "What are the results?"})
        self.assertEqual((status, got["error"]), (403, "that's a work agent; it isn't available by voice"))
        self.assertEqual(self.claude.calls, [])          # no copy was started: the popen stand-in had no call

    def test_agents_that_cannot_be_read_are_said(self):
        self.signed_in()

        def unread():
            raise voicedesk.VoiceError("could not read Codex's threads in /h/.codex (locked), so no agent is listed")
        self.desk.agents = unread
        for name, args in (("list_agents", {}), ("ask_agent", {"agent": "loader-fix", "question": "q"})):
            self.assertEqual(self.tool(name, args), (503, {"ok": False, "error": "could not read Codex's threads in "
                                                           "/h/.codex (locked), so no agent is listed"}))

    def test_a_work_agent_in_a_sub_group_or_closed_is_still_one(self):
        self.signed_in()
        self.exclude_work()
        self.board(["d" * 36, B])                         # survey-model in Work's sub-group
        self.assertEqual(self.tool("ask_agent", {"agent": "survey-model", "question": "q"})[0], 403)
        self.board([])                                    # its tile closed: the history keeps its group
        groups.save_groups([{"name": "Work", "members": [], "subgroups": []}], live_ids={A})
        self.assertEqual(groups.history()[B]["group"], "Work")
        self.assertEqual(self.tool("ask_agent", {"agent": "survey-model", "question": "q"})[0], 403)
        self.settle(exclude_groups=["Personal"], excluded_as="a personal agent")    # another group, called so
        self.board([A])
        self.assertEqual(self.tool("ask_agent", {"agent": "survey-model", "question": "q"})[0], 200)
        self.board([], other=[A])
        self.assertEqual(self.tool("ask_agent", {"agent": "loader-fix", "question": "q"})[1]["error"],
                         "that's a personal agent; it isn't available by voice")
        self.settle(exclude_groups=[])                    # with no group excluded, it may be asked
        self.assertEqual([a["name"] for a in self.tool("list_agents")[1]["response"]["agents"]],
                         ["loader-fix", "survey-model"])

    def test_the_screen_behind_a_work_agent_is_not_described(self):
        self.signed_in()
        self.exclude_work()
        self.board([B])
        self.window = {"ok": True, "windows": [{"app": "kitty",
                                                "title": "Q3 sales results: tmux a -t survey-model"}]}
        status, got = self.tool("what_am_i_looking_at")
        self.assertEqual((status, got["response"]), (200, {"summary": "That is a work agent's work; it isn't "
                                                                      "available by voice.", "agent": None,
                                                           "note": voiceweb.DATA_NOTE}))
        for secret in ("survey", "sales", "Q3"):
            self.assertNotIn(secret, json.dumps(got))
        self.window = {"ok": True, "windows": [{"app": "kitty", "title": "tmux a -t loader-fix"}]}
        self.assertEqual(self.tool("what_am_i_looking_at")[1]["response"]["agent"], "loader-fix")
        # the board's window showing a work agent, live or not (ended, or a Codex agent): its name never goes out
        for sid in (B, "e" * 36):
            self.board([sid])
            self.window = {"ok": True, "windows": [{"app": "AgentBoard",
                                                    "title": "Agent Board — survey-model [session %s]" % sid}]}
            got = self.tool("what_am_i_looking_at")[1]["response"]
            self.assertEqual(got["summary"], voiceweb.BEHIND % "a work agent", sid)
            self.assertNotIn("survey", json.dumps(got))

    def test_without_the_board_s_groups_no_agent_is_offered_while_a_group_is_kept_from_voice(self):
        self.signed_in()
        self.exclude_work()
        groups.GROUPS.unlink()                            # the Mac has sent no layout: work agents cannot be told
        for name, args in (("list_agents", {}), ("what_am_i_looking_at", {}),
                           ("ask_agent", {"agent": "loader-fix", "question": "q"})):
            status, got = self.tool(name, args)
            self.assertEqual(status, 503, name)
            self.assertIn("the board's groups are not known", got["error"])
        groups.GROUPS.write_text("not json")
        self.assertIn("the board's groups could not be read", self.tool("list_agents")[1]["error"])
        self.assertEqual(self.claude.calls, [])

    def test_bad_settings_are_said_and_the_page_may_reach_no_socket(self):
        self.signed_in()
        self.settle(model="Gemini Live!")
        status, got = self.voice({"action": "status"})
        self.assertEqual((got["ready"], got["model"]), (False, None))
        self.assertIn("model must be a Live model's name", got["error"])
        self.assertEqual(self.voice({"action": "start"})[0], 503)
        self.assertEqual(self.tool("list_agents")[0], 503)
        self.assertNotIn("wss:", self.ask("GET", "/voice")[1]["Content-Security-Policy"])
        self.assertEqual(self.mint_run.calls, [])


class FakeGemini:
    """Gemini Live's socket as the page meets it, standing in for Google (see the module's docstring): TLS on a free
    local port with a made-up certificate for Google's host, the WebSocket handshake and frames (RFC 6455), and one
    conversation as Gemini holds one: setupComplete once the setup comes (and a message that is not JSON, which the
    page must say it could not read); once the microphone has sent a few chunks,
    Alex's words transcribed and a call of ask_agent; once the page answers it, Gemini's voice and its words. What the
    page sends is kept in `got` (each message parsed), the address it opened in `opened`, and `closed` is set when it
    closes the socket."""
    HOST = "generativelanguage.googleapis.com"

    def __init__(self, test):
        d = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, d)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.HOST)])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(self.HOST)]), critical=False)
                .sign(key, hashes.SHA256()))
        (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (d / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption()))
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(d / "cert.pem", d / "key.pem")
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.got, self.opened, self.closed, self.failed = queue.Queue(), [], threading.Event(), []
        threading.Thread(target=self._serve, daemon=True).start()
        test.addCleanup(self.listener.close)

    def _serve(self):
        try:
            raw, _ = self.listener.accept()
            conn = self.tls.wrap_socket(raw, server_side=True)
            self._talk(conn, conn.makefile("rb"))
        except Exception as e:          # noqa: BLE001 -- said in the test, which then fails on what it waited for
            self.failed.append("%s: %s" % (type(e).__name__, e))
        finally:
            self.closed.set()

    def _handshake(self, conn, f):
        lines = []
        while (line := f.readline().decode()) not in ("\r\n", ""):
            lines.append(line.strip())
        self.opened.append(lines[0].split(" ")[1])
        key = next(v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:]) if k.lower() == "sec-websocket-key")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")

    def _receive(self, f):
        """The next message from the page, parsed, or None when it closes the socket."""
        data = b""
        while True:
            head = f.read(2)
            if len(head) < 2:
                return None
            fin, opcode, n = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", f.read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", f.read(8))[0]
            mask = f.read(4) if head[1] & 0x80 else b"\0\0\0\0"
            data += bytes(b ^ mask[i % 4] for i, b in enumerate(f.read(n)))
            if opcode == 8:
                return None
            if fin:
                msg = json.loads(data)
                self.got.put(msg)
                return msg

    def _send(self, conn, obj):
        self._frame(conn, json.dumps(obj).encode())

    def _frame(self, conn, data):
        n = len(data)
        head = bytes([0x81, n]) if n < 126 else bytes([0x81, 126]) + struct.pack(">H", n) if n < 65536 \
            else bytes([0x81, 127]) + struct.pack(">Q", n)
        conn.sendall(head + data)

    def _talk(self, conn, f):
        self._handshake(conn, f)
        if "setup" not in self._receive(f):
            raise AssertionError("the page's first message is not its setup")
        self._send(conn, {"setupComplete": {}})
        self._frame(conn, b"not JSON: a message the page cannot read")
        heard = 0
        while heard < 5:
            msg = self._receive(f)
            heard += "realtimeInput" in msg
        self._send(conn, {"serverContent": {"inputTranscription": {"text": "What is loader-fix doing?"}}})
        self._send(conn, {"toolCall": {"functionCalls": [{"id": "call-1", "name": "ask_agent", "args": {
            "agent": "loader-fix", "question": "What are you doing?"}}]}})
        while "toolResponse" not in (msg := self._receive(f)):
            pass
        voice = base64.b64encode(b"\0\0" * 4800).decode()               # 0.2 s of silence at 24 kHz
        self._send(conn, {"serverContent": {"modelTurn": {"parts": [{"inlineData": {
            "mimeType": "audio/pcm;rate=24000", "data": voice}}]},
            "outputTranscription": {"text": "Loader-fix says it is fixing the loader."}}})
        self._send(conn, {"serverContent": {"turnComplete": True}})
        while self._receive(f) is not None:
            pass

    def messages(self, until, seconds=20):
        """The page's messages so far, read until one for which `until(message)` is true; fails the wait (raises) when
        none is within `seconds`."""
        seen, ends = [], time.time() + seconds
        while True:
            msg = self.got.get(timeout=max(0.01, ends - time.time()))
            seen.append(msg)
            if until(msg):
                return seen


@unittest.skipUnless(chrome.EXE, "google-chrome is not installed")
class VoiceInChrome(MailTest):
    """The voice page in headless Chrome (see the module's docstring), signed in, as on the Mac: 1280 by 800."""

    def setUp(self):
        super().setUp()
        groups.save_groups([{"name": "Work", "members": [], "subgroups": []}], live_ids=set())   # the board's layout
        self.vd = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.vd)
        cwd = self.vd / "loader"
        cwd.mkdir()
        write_jsonl(self.vd / "home/projects/-loader" / (A + ".jsonl"), [reply("claude-opus-5-5", cwd=str(cwd))])
        rows = [row(A, "loader-fix", cwd=str(cwd))]
        self.fork_may_answer, claude = threading.Event(), FakeClaude()

        def fork(*a, **kw):                 # the read-only copy answers once the test has seen the page wait for it
            self.fork_may_answer.wait(20)
            return claude(*a, **kw)
        ask = functools.partial(voicedesk.ask, popen=fork, claude_bin="/bin/claude",
                                homes=[("default", self.vd / "home")], log=self.vd / "asks.jsonl", copies_path=None)
        self.mint = MintRun()
        desk = voiceweb.Desk(config=self.vd / "voice.json", agents=lambda: rows, ask=ask, looking=lambda: {},
                             run=self.mint, transcripts=self.vd / "transcripts")
        guard, store, sock = test_mailweb.serve_page(self, "localhost", lambda cmd, input: (1, "", "no mail here"),
                                                     handler=voiceweb.handler(desk))[:3]
        guard.cfg["voice_devices"] = [test_mailweb.PHONE]           # serve_page's device: the voice page's too
        self.store, self.token = store, store.new_session("cid", test_mailweb.PHONE, time.time(), 1)
        # the session given, or (self.token None) the browser's own
        proxy = test_mailweb.serve_like_tailscale(self, sock, guard.host_port, lambda: self.token)
        guard.origin = self.base = "http://localhost:%d" % proxy.server_address[1]
        saved = voiceweb.sources.mail_owner                 # the mail to Alex about an enrolment goes nowhere
        voiceweb.sources.mail_owner = lambda subject, body, *, alert: (True, "")
        self.addCleanup(setattr, voiceweb.sources, "mail_owner", saved)
        self.gemini = FakeGemini(self)
        self.chrome = chrome.Chrome([
            "--host-resolver-rules=MAP %s:443 127.0.0.1:%d" % (FakeGemini.HOST, self.gemini.port),
            "--ignore-certificate-errors", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            "--autoplay-policy=no-user-gesture-required"])
        self.addCleanup(self.chrome.close)
        self.page = self.chrome.page(width=1280, height=800)

    def text(self, selector):
        return self.page.js("Array.from(document.querySelectorAll(%s), e => e.textContent)" % json.dumps(selector))

    def test_a_conversation_asks_an_agent_and_both_sides_are_written_down(self):
        self.page.go(self.base + "/voice")
        self.page.wait("!document.getElementById('talk').disabled")
        self.assertEqual(self.page.js("document.getElementById('model').textContent"), "gemini-3.8-live")
        self.page.js("document.getElementById('talk').click()")
        # the token, then Google's socket opened with it, and the microphone streamed there once the setup is done
        first = self.gemini.messages(lambda m: True)[0]
        self.assertEqual(first, {"setup": {"model": "models/gemini-3.8-live"}})
        self.assertEqual(self.gemini.opened, ["/ws/google.ai.generativelanguage.v1beta.GenerativeService."
                                              "BidiGenerateContentConstrained?access_token=" +
                                              urllib.parse.quote(TOKEN, safe="")])
        audio = self.gemini.messages(lambda m: "realtimeInput" in m)[-1]["realtimeInput"]["audio"]
        self.assertRegex(audio["mimeType"], r"^audio/pcm;rate=(16000|44100|22050)$")
        self.assertEqual(len(base64.b64decode(audio["data"])) % 2, 0)
        self.assertEqual(self.page.wait("document.getElementById('state').textContent === 'Asking loader-fix…' "
                                        "&& document.getElementById('state').className"), "state asking")
        # Stop stays in view at the bottom of the window while the conversation goes
        self.assertEqual(self.page.js("(() => { const r = document.getElementById('stop').getBoundingClientRect(); "
                                      "return [document.getElementById('bar').hidden, r.bottom <= innerHeight, "
                                      "getComputedStyle(document.getElementById('bar')).position]; })()"),
                         [False, True, "fixed"])
        self.fork_may_answer.set()
        answer = self.gemini.messages(lambda m: "toolResponse" in m)[-1]["toolResponse"]
        self.assertEqual(answer, {"functionResponses": [{"id": "call-1", "name": "ask_agent", "scheduling": "WHEN_IDLE",
                                                         "response": {"agent": "loader-fix",
                                                                      "answer": "I am fixing the loader.",
                                                                      "note": voiceweb.DATA_NOTE}}]})
        self.page.wait("document.querySelector('#transcript .desk')")
        self.assertEqual(self.text("#transcript .you .text"), ["What is loader-fix doing?"])
        self.assertEqual(self.text("#transcript .desk .text"), ["Loader-fix says it is fixing the loader."])
        self.assertRegex(self.text("#transcript .tool .text")[0], r"^Asking loader-fix: done in \d+ s$")
        self.assertRegex(self.text("#transcript .note .text")[1], r"^A message from Gemini could not be read: ")
        self.page.js("document.getElementById('stop').click()")                  # Stop, from the bottom bar
        self.assertTrue(self.gemini.closed.wait(10))
        self.assertEqual(self.gemini.failed, [])
        self.assertEqual(self.page.js("document.getElementById('talklabel').textContent"), "Talk")
        self.assertEqual(self.text("#transcript .note")[-1], "Conversation ended")
        self.assertTrue(self.page.js("document.getElementById('bar').hidden"))
        # the transcript, saved on the desktop: one file for the conversation, both sides and the tool in it
        deadline = time.time() + 10
        while time.time() < deadline and not any("Conversation ended" in p.read_text()
                                                 for p in (self.vd / "transcripts").glob("*.txt")):
            time.sleep(0.1)
        files = list((self.vd / "transcripts").glob("*.txt"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^\d{8}-\d{6}-[0-9a-f]{6}\.txt$")
        saved = [line[9:] for line in files[0].read_text().splitlines()]
        notes = self.text("#transcript .note .text")             # as the page shows them
        self.assertRegex(saved[3], r"^Tool: Asking loader-fix: done in \d+ s$")      # once, as it ended
        self.assertEqual(saved[:3] + saved[4:], ["Note: " + notes[0], "Note: " + notes[1],
                                                 "Alex: What is loader-fix doing?",
                                                 "Front desk: Loader-fix says it is fixing the loader.",
                                                 "Note: Conversation ended"])

    def test_the_device_enrols_its_passkey_from_the_link_and_signs_in_with_it(self):
        self.token = None                                   # the browser's own session, from signing in here
        p = self.page
        p.send("WebAuthn.enable", {"enableUI": False})
        p.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
            "isUserVerified": True, "automaticPresenceSimulation": True}})
        p.go(self.base + "/voice")
        p.wait("document.getElementById('gatetext').textContent.startsWith('No passkey is enrolled')")
        self.assertIn("agent-board voice-enrol", p.js("document.getElementById('gatetext').textContent"))
        self.assertTrue(p.js("document.getElementById('gatebutton').hidden"))
        code, _ = self.store.mint_code(time.time(), 10)
        p.go(self.base + "/voice/enrol#" + code)
        p.wait("document.getElementById('gatebutton').textContent === 'Enrol this device'")
        self.assertEqual(p.js("[location.pathname, location.hash]"), ["/voice", ""])    # the code has left the address
        p.js("document.getElementById('gatebutton').click()")
        p.wait("!document.getElementById('talk').disabled && document.getElementById('gate').hidden")
        self.assertEqual(len(self.store.passkeys()), 1)
        self.store.sign_out(forget=False)
        p.reload()
        p.wait("document.getElementById('gatebutton').textContent === 'Sign in with your passkey'")
        p.js("document.getElementById('gatebutton').click()")
        p.wait("!document.getElementById('talk').disabled && document.getElementById('gate').hidden")
        self.assertEqual(self.mint.calls, [])

    def test_the_browser_holds_each_page_to_its_own_policy(self):
        self.page.go(self.base + "/voice")
        self.page.wait("!document.getElementById('talk').disabled")
        # the voice page may reach Google's Live socket alone, besides its own server: anything else is refused
        refused = """new Promise((resolve) => {
          document.addEventListener('securitypolicyviolation',
                                    (e) => resolve(e.effectiveDirective + ' ' + e.blockedURI), {once: true});
          %s; })"""
        self.assertEqual(self.page.js(refused % "new WebSocket('wss://example.org/x').onopen = () => resolve('no')"),
                         "connect-src wss://example.org/x")
        self.assertEqual(self.page.js(refused % "fetch('https://example.org/y').then(() => resolve('fetched'))"),
                         "connect-src https://example.org/y")
        self.assertEqual(self.page.js(refused % "new WebSocket('wss://generativelanguage.googleapis.com/ws/other')"),
                         "connect-src wss://generativelanguage.googleapis.com/ws/other")
        self.assertEqual(self.page.js("navigator.mediaDevices.getUserMedia({audio: true}).then(s => { "
                                      "s.getTracks().forEach(t => t.stop()); return 'microphone'; }, e => e.name)"),
                         "microphone")
        # the Mail page, on the same origin, may not have the microphone at all
        self.page.go(self.base + "/")
        self.assertEqual(self.page.js("navigator.mediaDevices.getUserMedia({audio: true}).then(() => 'microphone', "
                                      "e => e.name)"), "NotAllowedError")
        self.assertEqual(self.mint.calls, [])


def webauthn_id(passkey):
    """A test authenticator's credential id as the store keys it."""
    return test_mailweb.b64(passkey.id)


def load_tests(loader, tests, pattern):
    """This module's tests, and of VoicePage only its own: those it inherits from MailWebTest run in test_mailweb."""
    suite = unittest.TestSuite()
    for cls in (Settings, Setup, EnrolCommand, Mint, Limits, VoicePage, VoiceInChrome):
        suite.addTests(cls(name) for name in loader.getTestCaseNames(cls) if name in vars(cls))
    return suite


if __name__ == "__main__":
    unittest.main()
