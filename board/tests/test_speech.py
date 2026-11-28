"""The desktop's voice (agentboard/speech.py): its settings; text split into sentences as it is written; the daemon's
server and its client over a real Unix socket, and `agent-speech say` through it; the Narrator speaking an answer while
it is written, and saying why when the daemon cannot; bin/agent-speech's move to the speech venv and its device. Kokoro
is the only stand-in (fixtures.Voice), as it is PyTorch in a venv of its own; bin/agent-speech serve runs it for
real outside the suite. Every file goes to a temporary folder."""
import base64
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import stat
import sys
import subprocess
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fixtures import Voice, until  # noqa: E402
from agentboard import speech  # noqa: E402

AGENT_SPEECH = Path(__file__).resolve().parent.parent / "bin" / "agent-speech"


def agent_speech():
    """bin/agent-speech as a module, for its functions."""
    loader = importlib.machinery.SourceFileLoader("agent_speech", str(AGENT_SPEECH))
    spec = importlib.util.spec_from_loader("agent_speech", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def frames(data):
    """(rate, channels, sample width, frame count) of WAV bytes."""
    with wave.open(io.BytesIO(data)) as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()


class Settings(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def cfg(self, **over):
        path = self.d / "speech.json"
        path.write_text(json.dumps(dict({"socket": str(self.d / "speech.sock")}, **over)))
        return speech.settings(path)

    def test_the_defaults_name_every_setting_and_an_override_changes_only_its_own(self):
        with mock.patch.dict(os.environ, HOME="/home/alex"):         # a home folder short enough for a socket's path
            cfg = speech.settings(self.d / "none.json")
        self.assertEqual((cfg["voice"], cfg["speed"], cfg["device"]), ("af_heart", 1.0, "auto"))
        self.assertEqual(cfg["socket"], "/home/alex/.local/state/agent-board/voice-desk/speech.sock")
        cfg = self.cfg(device="cpu", speed=1.1, voice="bf_emma")
        self.assertEqual((cfg["voice"], cfg["speed"], cfg["device"]), ("bf_emma", 1.1, "cpu"))
        self.assertEqual(self.cfg(device="cuda:1")["device"], "cuda:1")

    def test_what_will_not_do_is_refused(self):
        for over, said in (({"voise": "bm_lewis"}, "no such setting voise"),
                           ({"voice": "jf_alpha"}, "voice must be one of Kokoro's English voices"),
                           ({"voice": "../af_heart"}, "voice must be"),
                           ({"speed": 3}, "speed must be a number from 0.5 to 2"), ({"speed": True}, "speed must be"),
                           ({"device": "gpu"}, "device must be auto, cpu, cuda or cuda:N"),
                           ({"socket": "speech.sock"}, "socket must be a path"),
                           ({"socket": "/" + "x" * 120}, "too long for a Unix socket")):
            with self.assertRaisesRegex(ValueError, said):
                self.cfg(**over)
        (self.d / "speech.json").write_text("[]")
        with self.assertRaisesRegex(ValueError, "must hold a JSON object"):
            speech.settings(self.d / "speech.json")


class SentencesTest(unittest.TestCase):
    def test_a_sentence_ends_at_its_mark_before_a_space_not_inside_a_number_a_file_or_an_abbreviation(self):
        self.assertEqual(speech.Sentences.split(
            "Two things: the cache and the tests. The cache needs its test, e.g. one at 3.5 GB in voicedesk.py. "
            "Then I merge it! What then? I wait for your answer… \"It's done.\" Next, the wiki"),
            ["Two things: the cache and the tests.", "The cache needs its test, e.g. one at 3.5 GB in voicedesk.py.",
             "Then I merge it! What then?", "I wait for your answer…", "\"It's done.\" Next, the wiki"])

    def test_a_short_one_joins_the_next_and_a_line_ends_one(self):
        self.assertEqual(speech.Sentences.split("Yes. The cache is in memory now.\nThe tests pass on both."),
                         ["Yes. The cache is in memory now.", "The tests pass on both."])
        self.assertEqual(speech.Sentences.split("OK."), ["OK."])

    def test_a_long_run_with_no_end_is_cut_at_a_comma(self):
        text = "word " * 50 + "and then, " + "more " * 30
        first, rest = speech.Sentences.split(text)
        self.assertTrue(first.endswith("and then,") and len(first) <= speech.Sentences.LONG)
        self.assertEqual(rest, ("more " * 30).strip())
        self.assertTrue(all(len(s) <= speech.Sentences.LONG for s in speech.Sentences.split("x" * 700)))

    def test_pieces_as_they_are_written_give_each_sentence_once_it_is_complete(self):
        s = speech.Sentences()
        got = [s.feed(p) for p in ("I am car", "rying out your answer.", " The team", " lunch is booked. It",
                                   " works.")]
        self.assertEqual(got, [[], [], ["I am carrying out your answer."], ["The team lunch is booked."], []])
        self.assertEqual(s.flush(), ["It works."])
        self.assertEqual(s.flush(), [])

    def test_markdown_marks_are_not_spoken(self):
        self.assertEqual(speech.spoken("## Done: `voicedesk.py` is **merged**."), "Done: voicedesk.py is merged.")


class Daemon(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def voice(self, **kw):
        v = Voice(self.d, **kw)
        self.addCleanup(v.close)
        return v

    def test_each_sentence_comes_back_as_its_own_clip_as_soon_as_it_is_made(self):
        v = self.voice()
        clips = list(speech.synthesize("I am fixing the **loader**. It is nearly done, then I merge it.", v.socket))
        self.assertEqual([(c["seq"], c["text"]) for c in clips],
                         [(0, "I am fixing the **loader**."), (1, "It is nearly done, then I merge it.")])
        self.assertEqual(v.said, ["I am fixing the loader.", "It is nearly done, then I merge it."])
        for c in clips:
            self.assertEqual(frames(c["wav"]), (24000, 1, 2, 240 * len(speech.spoken(c["text"]))))
            self.assertEqual(c["audio_seconds"], round(len(speech.spoken(c["text"])) / 100, 2))
        self.assertIn("INFO synthesized 27 characters: 0.2 s of audio", v.log.read_text())
        self.assertEqual(stat.S_IMODE(os.stat(v.socket).st_mode), 0o600)     # only the owner may connect

    def test_a_failure_is_said_and_the_daemon_carries_on(self):
        v = self.voice(fail="cannot")
        with self.assertRaisesRegex(speech.SpeechError, "the desktop's voice failed: Kokoro failed: RuntimeError"):
            list(speech.synthesize("This one it cannot say.", v.socket))
        self.assertEqual(len(list(speech.synthesize("This one it can say.", v.socket))), 1)
        for bad in (b"not json\n", json.dumps({"text": "x" * (speech.MAX_TEXT + 1)}).encode() + b"\n",
                    json.dumps({"text": " "}).encode() + b"\n"):
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(v.socket)
                s.sendall(bad)
                self.assertIn(b"not a request", s.makefile("rb").readline())

    def test_one_text_is_synthesized_at_a_time(self):
        v = self.voice(delay=0.05)
        threads = [threading.Thread(target=lambda: list(speech.synthesize("Sentence number one here. And two.",
                                                                          v.socket))) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((len(v.said), v.busy), (6, 1))

    def test_agent_speech_say_times_each_sentence_and_writes_their_audio(self):
        v = self.voice()
        out = self.d / "said.wav"
        env = dict(os.environ, AGENT_BOARD_SPEECH_CONFIG=str(v.config))
        r = subprocess.run([sys.executable, str(AGENT_SPEECH), "say", "First sentence here.", "Second one now.", "-o",
                            str(out)], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"clip 0: 0\.2 s of audio, synthesized in [\d.]+ s: First sentence here\.\n"
                                   r".*clip 1: 0\.1 s of audio, synthesized in [\d.]+ s: Second one now\.\nwrote ")
        self.assertEqual(frames(out.read_bytes()), (24000, 1, 2, 240 * len("First sentence here.Second one now.")))
        v.close()
        r = subprocess.run([sys.executable, str(AGENT_SPEECH), "say", "Nobody hears this."], capture_output=True,
                           text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not speak: the desktop's voice (agent-speech) is not answering on", r.stderr)

    def test_no_daemon_is_unreachable(self):
        with self.assertRaisesRegex(speech.Unreachable, "the desktop's voice \\(agent-speech\\) is not answering on "):
            list(speech.synthesize("Hello there.", str(self.d / "none.sock")))
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(self.d / "stale.sock"))           # a daemon that died: its socket is left, nobody listening
        stale.close()
        with self.assertRaises(speech.Unreachable):
            list(speech.synthesize("Hello there.", str(self.d / "stale.sock")))

    def test_a_stale_socket_is_replaced_but_a_live_one_or_another_file_is_not(self):
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(self.d / "speech.sock"))
        stale.close()
        v = self.voice()
        self.assertEqual(len(list(speech.synthesize("It answers on the old path.", v.socket))), 1)
        with self.assertRaisesRegex(speech.SpeechError, "another daemon answers on"):
            speech.Server(lambda t: (b"", 24000), v.socket)
        (self.d / "file").write_text("mine")
        with self.assertRaisesRegex(speech.SpeechError, "is there and is not a socket"):
            speech.Server(lambda t: (b"", 24000), self.d / "file")
        self.assertEqual((self.d / "file").read_text(), "mine")


class NarratorTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.log = self.d / "narrator.log"
        self.parts = []

    def narrator(self, voice=None, socket_path=None, bad=None):
        cfg = None if bad else {"socket": voice.socket if voice else str(socket_path or self.d / "none.sock")}
        return speech.Narrator(self.parts.append, cfg, bad, log_path=self.log)

    def test_the_first_sentence_is_spoken_while_the_rest_is_still_being_written(self):
        v = Voice(self.d)
        self.addCleanup(v.close)
        n = self.narrator(v)
        n.feed("I'm carrying out your answer on the calendar")
        n.feed(" commands. The team lunch")
        self.assertTrue(until(lambda: len(self.parts) == 1))       # before the rest of the answer is written
        self.assertEqual(v.said, ["I'm carrying out your answer on the calendar commands."])
        n.feed(" is in your calendar. That's all.")
        said = n.finish("this whole answer is not spoken again")
        self.assertEqual([(p["seq"], p["text"]) for p in self.parts],
                         [(0, "I'm carrying out your answer on the calendar commands."),
                          (1, "The team lunch is in your calendar."), (2, "That's all.")])
        for p in self.parts:
            self.assertNotIn("error", p)
            self.assertEqual(frames(base64.b64decode(p["audio"]))[3], 240 * len(p["text"]))
        self.assertEqual((said["parts"], said["error"], said["chars"]), (3, None, sum(len(p["text"])
                                                                                       for p in self.parts)))
        self.assertIsNotNone(said["first_audio"])
        self.assertRegex(self.log.read_text(), r"INFO spoke an answer of %d characters in 3 parts: the first after"
                         % said["chars"])

    def test_an_answer_that_came_whole_is_spoken_whole(self):
        v = Voice(self.d)
        self.addCleanup(v.close)
        said = self.narrator(v).finish("I am checking the report. Two more to read.")
        self.assertEqual([p["text"] for p in self.parts], ["I am checking the report.", "Two more to read."])
        self.assertEqual(said["parts"], 2)

    def test_with_no_daemon_each_sentence_comes_with_why_and_it_is_not_tried_again(self):
        n = self.narrator(socket_path=self.d / "speech.sock")
        n.feed("The first sentence of it. ")
        self.assertTrue(until(lambda: len(self.parts) == 1))
        v = Voice(self.d)                       # the daemon comes up now: this answer still does without it
        self.addCleanup(v.close)
        n.feed("The second sentence of it.")
        said = n.finish()
        self.assertEqual([p["text"] for p in self.parts], ["The first sentence of it.", "The second sentence of it."])
        for p in self.parts:
            self.assertNotIn("audio", p)
            self.assertRegex(p["error"], "the desktop's voice \\(agent-speech\\) is not answering on .*speech.sock")
        self.assertEqual(v.said, [])
        self.assertEqual(said["error"], self.parts[0]["error"])
        log = self.log.read_text()
        self.assertEqual(log.count("WARNING"), 1)
        self.assertIn("the Mac reads the answer with its own voice", log)
        self.assertNotIn("INFO", log)

    def test_a_sentence_the_daemon_fails_is_said_and_the_next_still_spoken(self):
        v = Voice(self.d, fail="cannot")
        self.addCleanup(v.close)
        n = self.narrator(v)
        n.feed("This sentence it cannot say. This one it can say.")
        n.finish()
        self.assertEqual([("audio" in p, p.get("error", "")[:26]) for p in self.parts],
                         [(False, "the desktop's voice failed"), (True, "")])

    def test_an_answer_no_sentence_of_which_could_be_spoken_still_ends(self):
        v = Voice(self.d, fail=".")
        self.addCleanup(v.close)
        said = self.narrator(v).finish("Nothing here can be said. Nor this, at all.")
        self.assertEqual([("audio" in p, p["error"][:26]) for p in self.parts],
                         [(False, "the desktop's voice failed"), (False, "the desktop's voice failed")])
        self.assertEqual((said["parts"], said["first_audio"]), (2, None))
        self.assertNotIn("INFO", self.log.read_text())

    def test_a_failure_of_its_own_is_said_and_the_next_sentence_still_spoken(self):
        v = Voice(self.d)
        self.addCleanup(v.close)

        def speak(text, path):                  # the real client, but for a sentence it chokes on
            if "chokes" in text:
                raise RuntimeError("it chokes")
            return speech.synthesize(text, path)
        n = speech.Narrator(self.parts.append, {"socket": v.socket}, speak=speak, log_path=self.log)
        said = n.finish("This sentence it chokes on. This one is spoken.")
        self.assertEqual([("audio" in p, p.get("error")) for p in self.parts],
                         [(False, "the desktop's voice failed here: RuntimeError: it chokes"), (True, None)])
        self.assertEqual(said["parts"], 2)
        self.assertIn("ERROR the desktop's voice failed here: RuntimeError: it chokes", self.log.read_text())

    def test_bad_settings_are_said_with_each_sentence(self):
        self.narrator(bad="speech.json: device must be cpu, cuda or cuda:N").finish("It is done. Nothing is left.")
        self.assertEqual([p["error"] for p in self.parts],
                         ["the desktop's voice has a bad setting: speech.json: device must be cpu, cuda or cuda:N"])
        self.assertEqual(self.parts[0]["text"], "It is done. Nothing is left.")
        self.assertIn("WARNING the desktop's voice has a bad setting: speech.json: device must be",
                      self.log.read_text())

    def test_nothing_to_say_says_nothing(self):
        self.assertEqual(self.narrator().finish()["parts"], 0)
        self.assertEqual(self.parts, [])


if __name__ == "__main__":
    unittest.main()


class SpeechVenv(unittest.TestCase):
    """bin/agent-speech serve and fetch need Kokoro and PyTorch, which only the speech venv holds: on another python
    (the kit's wrapper runs the kit's) they start again on the venv's, once, or say how to make it."""

    def setUp(self):
        self.mod = agent_speech()
        self.execs = []

    def execv(self, path, argv):
        self.execs.append((path, argv))

    def test_with_its_modules_here_it_carries_on_here(self):
        self.assertIsNone(self.mod.speech_python(("json",), execv=self.execv, environ={}))
        self.assertEqual(self.execs, [])

    def test_without_them_it_starts_again_on_the_speech_venvs_python_once(self):
        d = Path(tempfile.mkdtemp())
        python = d / "python"
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
        env = {}
        self.mod.speech_python(("no_such_module_here",), argv=["serve"], python=python, execv=self.execv, environ=env)
        self.assertEqual(self.execs, [(str(python), [str(python), str(AGENT_SPEECH), "serve"])])
        self.assertEqual(env, {self.mod.REEXEC: "1"})
        why = self.mod.speech_python(("no_such_module_here",), argv=["serve"], python=python, execv=self.execv,
                                     environ=env)                       # started so already: never again
        self.assertEqual(len(self.execs), 1)
        self.assertIn("%s cannot import them either" % python, why)

    def test_with_no_speech_venv_it_says_how_to_make_one(self):
        why = self.mod.speech_python(("no_such_module_here",), python="/nonexistent/python", execv=self.execv,
                                     environ={})
        self.assertEqual(self.execs, [])
        self.assertIn("cannot import no_such_module_here, and /nonexistent/python, the speech venv's python, does not "
                      "exist", why)
        for step in ("python3 -m venv", "pip install torch", "download.pytorch.org/whl/cpu",
                     "pip install --no-deps -r " + str(AGENT_SPEECH.parent.parent / "requirements-speech.txt"),
                     "agent-speech fetch", "AGENT_SPEECH_PYTHON"):
            self.assertIn(step, why)

    def test_fetch_on_a_python_without_its_modules_and_no_venv_exits_2_saying_so(self):
        env = dict(os.environ, AGENT_SPEECH_PYTHON=str(Path(tempfile.mkdtemp()) / "none"),
                   PYTHONPATH=str(Path(tempfile.mkdtemp())))
        probe = subprocess.run([sys.executable, "-s", "-c", "import huggingface_hub"], capture_output=True, env=env)
        if probe.returncode == 0:                   # -s: no user site, where a Hugging Face client often is
            self.skipTest("huggingface_hub is installed for this python")
        r = subprocess.run([sys.executable, "-s", str(AGENT_SPEECH), "fetch"], capture_output=True, text=True, env=env,
                           timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("Set up the speech venv once", r.stderr)

    def test_auto_is_the_gpu_when_pytorch_sees_one_else_the_cpu(self):
        self.assertEqual(self.mod.device_of({"device": "auto"}, lambda: True), "cuda")
        self.assertEqual(self.mod.device_of({"device": "auto"}, lambda: False), "cpu")
        self.assertEqual(self.mod.device_of({"device": "cuda:1"}, lambda: False), "cuda:1")
