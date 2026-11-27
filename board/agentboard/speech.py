"""The desktop's voice: text in, speech out, sentence by sentence, from Kokoro (hexgrad/Kokoro-82M) run here, so the
text never leaves the owner's machines. Kokoro is PyTorch and this package is the standard library only, so Kokoro runs
in a daemon of its own (bin/agent-speech, in a venv of its own: requirements-speech.txt; the unit
systemd/agent-speech.service.in), and this module is both sides of the Unix socket between them, with no TCP port: the
daemon's server (server), its client (synthesize), and the Narrator, which speaks an answer while it is still being
written.

The socket takes one request per connection: a JSON line {"text"} (at most MAX_TEXT characters). The daemon splits the
text into sentences (Sentences) and answers each as soon as it is synthesized: a JSON line {"seq", "text",
"bytes", "audio_seconds", "synth_seconds"} followed by that many bytes of WAV (16-bit mono), then {"done": true,
"clips"} once all are sent; or {"error"} and the end. Sentences are synthesized one at a time: those of requests made
at once take turns.

The ask box's question (actions.voice_ask) feeds the copy's answer to a Narrator as its text arrives; each sentence,
once complete, goes to the daemon in order, and each clip goes to the Mac as a part of the answer (bin/agent-board,
act_part). When the daemon cannot be reached, each part carries its text and why instead, and the Mac reads it with its
own voice, saying so. speech.log in the board's state folder has a line for each answer spoken and each sentence
synthesized (INFO), and for each time the daemon could not be reached (WARNING).

Settings: speech.json beside this file, overridden key by key by ~/.config/agent-board/speech.json (CONFIG; settings()):
- voice: Kokoro's voice, an English one ("af_heart" by default; its first letter is its accent, a American, b British,
  and its second its voice, f female, m male: "am_michael", "bf_emma", "bm_lewis" and so on);
- speed: how fast it speaks (0.5 to 2.0; 1.0 as recorded);
- device: where Kokoro runs: "auto" (the default: the GPU when PyTorch sees one, else the CPU), "cuda" ("cuda:N" for
  one GPU) or "cpu" (README, "The ask box on the Mac", says what each costs);
- socket: the daemon's Unix socket ("~" is the home folder).
The daemon reads voice, speed and device as it starts: restart it after changing them.
"""
import base64
import io
import json
import os
import queue
import re
import socket
import socketserver
import threading
import time
import wave
from pathlib import Path

from . import sources

DEFAULTS = Path(__file__).with_name("speech.json")
CONFIG = Path(os.environ.get("AGENT_BOARD_SPEECH_CONFIG") or sources.HOME / ".config/agent-board/speech.json")
LOG = sources.BOARD_STATE / "voice-desk" / "speech.log"
MAX_TEXT = 8000             # characters of one request
MAX_LINE = 64 * 1024        # bytes of one request or header line
TIMEOUT = 60                # seconds a client waits on the daemon for its next clip (CPU: ~2.5 s per 100 characters)
VOICE = re.compile(r"[ab][fm]_[a-z]{2,20}")      # Kokoro's English voices: misaki[en] is the G2P installed
DEVICE = re.compile(r"auto|cpu|cuda(:\d{1,2})?")


class SpeechError(Exception):
    """Why the desktop's voice did not speak."""


class Unreachable(SpeechError):
    """No daemon answers on the socket."""


def settings(path=None, defaults=DEFAULTS):
    """speech.json (`defaults`) with the overrides in the file at `path` (CONFIG by default), checked; `socket` with "~"
    expanded. Raises ValueError for a key that is not speech.json's or a value out of its range (the module's
    docstring)."""
    path = path or CONFIG
    cfg = sources.load_settings(defaults, path)
    if not (isinstance(cfg["voice"], str) and VOICE.fullmatch(cfg["voice"])):
        raise ValueError("%s: voice must be one of Kokoro's English voices, such as af_heart" % path)
    if isinstance(cfg["speed"], bool) or not isinstance(cfg["speed"], (int, float)) or not 0.5 <= cfg["speed"] <= 2:
        raise ValueError("%s: speed must be a number from 0.5 to 2" % path)
    if not (isinstance(cfg["device"], str) and DEVICE.fullmatch(cfg["device"])):
        raise ValueError("%s: device must be auto, cpu, cuda or cuda:N" % path)
    if not (isinstance(cfg["socket"], str) and cfg["socket"].startswith(("/", "~/"))):
        raise ValueError("%s: socket must be a path, absolute or from ~/" % path)
    cfg["socket"] = os.path.expanduser(cfg["socket"])
    if len(cfg["socket"].encode()) > 100:
        raise ValueError("%s: socket's path is too long for a Unix socket" % path)
    return cfg


def log(line, path=None):
    sources.append_log(path or LOG, line)


# ------------------------------------------------------------------ sentences

class Sentences:
    """Text split into sentences as it is written: feed() takes the next piece and gives the sentences it completed,
    flush() gives what is left. A sentence ends at . ! ? or … (and any closing quote or bracket) before a space, or at a
    line's end; not after an abbreviation such as "e.g." (so not inside "3.5" or "voicedesk.py" either). One shorter
    than SHORT characters is joined to the next, as Kokoro speaks short utterances badly (its VOICES.md); a run of over
    LONG characters with no end is cut at its last comma, or else at its last space."""
    END = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s)|\n+")
    ABBREV = {"e.g", "i.e", "etc", "vs", "mr", "mrs", "ms", "dr", "approx", "cf", "fig", "incl"}
    SHORT = 20
    LONG = 300

    def __init__(self):
        self.buf, self.pending = "", ""

    def feed(self, text):
        self.buf += text
        out, start, pos = [], 0, 0
        while True:
            m = self.END.search(self.buf, pos)
            if not m:
                break
            pos = m.end()
            word = re.search(r"(\S+?)\.+$", self.buf[start:m.end()].rstrip("\"'”’)]\n"))
            if m.group().startswith(".") and word and word.group(1).lower().lstrip("(\"'“‘") in self.ABBREV:
                continue
            out += self._done(self.buf[start:m.end()])
            start = pos
        self.buf = self.buf[start:]
        while len(self.buf) > self.LONG:
            cut = max(self.buf.rfind(", ", 1, self.LONG), self.buf.rfind("; ", 1, self.LONG)) + 1
            if cut <= 1:
                cut = self.buf.rfind(" ", 1, self.LONG)
            if cut <= 0:
                cut = self.LONG
            out += self._done(self.buf[:cut])
            self.buf = self.buf[cut:]
        return out

    def _done(self, sentence):
        text = " ".join((self.pending + " " + sentence).split())
        if len(text) < self.SHORT:
            self.pending = text
            return []
        self.pending = ""
        return [text] if text else []

    def flush(self):
        text = " ".join((self.pending + " " + self.buf).split())
        self.buf, self.pending = "", ""
        return [text] if text else []

    @classmethod
    def split(cls, text):
        s = cls()
        return s.feed(text) + s.flush()


def spoken(text):
    """A sentence as Kokoro is given it: no markdown marks (backticks, asterisks, heading hashes)."""
    return " ".join(re.sub(r"[`*]+|^#+\s*", "", text).split())


def wav(pcm, rate):
    """16-bit mono PCM samples (bytes) as a WAV file's bytes."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


# ------------------------------------------------------------------ the daemon's side

def _line(f):
    line = f.readline(MAX_LINE + 1)
    if len(line) > MAX_LINE or not line.endswith(b"\n"):
        raise ValueError("a line over %d bytes, or none" % MAX_LINE)
    return json.loads(line)


class _Handler(socketserver.StreamRequestHandler):
    timeout = 10                                    # to read the request

    def handle(self):
        srv = self.server
        try:
            req = _line(self.rfile)
            text = req.get("text") if isinstance(req, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                raise ValueError("the request must be {\"text\"}, text of 1 to %d characters" % MAX_TEXT)
        except (ValueError, OSError) as e:
            return self._send({"error": "not a request: %s" % e})
        n = 0
        for seq, sentence in enumerate(Sentences.split(text)):
            t = time.time()
            try:
                with srv.lock:
                    pcm, rate = srv.synth(spoken(sentence))
            except Exception as e:              # Kokoro's own failure: said, and the daemon carries on
                log("ERROR could not synthesize %d characters: %s: %s" % (len(sentence), type(e).__name__, e), srv.log)
                return self._send({"error": "Kokoro failed: %s: %s" % (type(e).__name__, e)})
            took, seconds = time.time() - t, len(pcm) / 2 / rate
            data = wav(pcm, rate)
            log("INFO synthesized %d characters: %.1f s of audio in %.2f s" % (len(sentence), seconds, took), srv.log)
            if not self._send({"seq": seq, "text": sentence, "bytes": len(data), "audio_seconds": round(seconds, 2),
                               "synth_seconds": round(took, 3)}, data):
                return                          # the client went away: the rest is not wanted
            n += 1
        self._send({"done": True, "clips": n})

    def _send(self, header, data=b""):
        try:
            self.wfile.write(json.dumps(header).encode() + b"\n" + data)
            self.wfile.flush()
            return True
        except OSError:
            return False


class Server(sources.UnixServer):
    """The daemon's server on the Unix socket at `path` (sources.UnixServer: 0600, for the owner's uid alone; a stale
    socket left by a daemon that died is replaced, one another daemon answers on is not), answering with `synth`
    (text -> (16-bit mono PCM bytes, sample rate); Kokoro in bin/agent-speech, a stand-in in a test), one at a time.
    Each sentence synthesized, or that Kokoro failed, is logged to `log_path` (LOG by default), as is a connection
    refused. Raises SpeechError."""

    def __init__(self, synth, path, log_path=None):
        self.synth, self.lock, self.log = synth, threading.Lock(), log_path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            super().__init__(path, _Handler, lambda: {os.getuid()}, "the desktop's voice",
                             refused=lambda uid: log("WARNING refused a connection from uid %d" % uid, log_path))
        except RuntimeError as e:
            raise SpeechError(str(e)) from None


# ------------------------------------------------------------------ the client's side

def synthesize(text, path, timeout=TIMEOUT):
    """The clips of `text`, one per sentence, from the daemon on the socket at `path`, each as soon as it is
    synthesized: {"seq", "text", "wav", "audio_seconds", "synth_seconds"}. Raises Unreachable when no daemon answers
    there, SpeechError when it fails or takes over `timeout` seconds for a clip."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        try:
            s.connect(path)
        except OSError as e:
            raise Unreachable("the desktop's voice (agent-speech) is not answering on %s: %s" % (
                path, e.strerror or e)) from None
        try:
            s.sendall(json.dumps({"text": text}).encode() + b"\n")
            f = s.makefile("rb")
            while True:
                head = _line(f)
                if head.get("done"):
                    return
                if "error" in head:
                    raise SpeechError("the desktop's voice failed: %s" % head["error"])
                data = f.read(int(head["bytes"]))
                if len(data) != head["bytes"]:
                    raise SpeechError("the desktop's voice stopped in the middle of a clip")
                yield {"seq": head["seq"], "text": head["text"], "wav": data, "audio_seconds": head["audio_seconds"],
                       "synth_seconds": head["synth_seconds"]}
        except socket.timeout:
            raise SpeechError("the desktop's voice took over %d s for a sentence" % timeout) from None
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise SpeechError("the desktop's voice did not answer properly: %s" % e) from None
    finally:
        s.close()


class Narrator:
    """Speaks an answer while it is being written: feed() its text as it comes, finish() at its end. Each sentence,
    once complete (Sentences), is synthesized in order on a thread of the Narrator's own, and handed to on_part as
    {"seq", "text", "audio": the WAV in base64} or, when the desktop's voice cannot speak it, {"seq", "text", "error"}.
    Once the daemon cannot be reached, it is not tried again for this answer. Each answer spoken, and a daemon that
    cannot be reached or fails, or bad settings, is logged to `log_path` (LOG by default). `cfg`: settings(), or None
    when they could not be read, `bad` saying why; `speak` is synthesize (the daemon's socket) or a test's stand-in."""

    def __init__(self, on_part, cfg, bad=None, speak=synthesize, log_path=None, clock=time.time):
        self.on_part, self.cfg, self.speak, self.log, self.clock = on_part, cfg, speak, log_path, clock
        self.down = bad and "the desktop's voice has a bad setting: %s" % bad
        if self.down:
            log("WARNING %s; the Mac reads the answer with its own voice" % self.down, self.log)
        self.started, self.sentences, self.queue, self.seq = clock(), Sentences(), queue.Queue(), 0
        self.fed, self.first, self.synth, self.chars = False, None, 0.0, 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def feed(self, text):
        self.fed = self.fed or bool(text.strip())
        for sentence in self.sentences.feed(text):
            self.queue.put(sentence)

    def pause(self):
        """A pause in the writing (the desk running a tool): what is written so far is spoken now, however short,
        rather than held for the sentence after it ("Asking loader-fix." before the answer that takes seconds)."""
        for sentence in self.sentences.flush():
            self.queue.put(sentence)

    def finish(self, whole=None):
        """The answer's end: what is left of it is spoken, or all of `whole` when nothing was fed; returns once every
        part has gone to on_part: {"parts", "chars", "first_audio", "synth_seconds", "error"}."""
        if not self.fed and whole:
            self.feed(whole)
        self.pause()
        self.queue.put(None)
        self.thread.join()
        stats = {"parts": self.seq, "chars": self.chars, "error": self.down,
                 "first_audio": None if self.first is None else round(self.first - self.started, 2),
                 "synth_seconds": round(self.synth, 2)}
        if self.first is not None:                 # some of it was heard in the desktop's voice
            log("INFO spoke an answer of %d characters in %d parts: the first after %.1f s, %.1f s synthesizing" % (
                self.chars, self.seq, stats["first_audio"], self.synth), self.log)
        return stats

    def _part(self, text, **kw):
        self.on_part(dict({"seq": self.seq, "text": text}, **kw))
        self.seq += 1
        self.chars += len(text)

    def _run(self):
        while True:
            sentence = self.queue.get()
            if sentence is None:
                return
            if self.down:
                self._part(sentence, error=self.down)
                continue
            said = False
            try:
                for clip in self.speak(sentence, self.cfg["socket"]):
                    if self.first is None:
                        self.first = self.clock()
                    self.synth += clip["synth_seconds"]
                    self._part(clip["text"], audio=base64.b64encode(clip["wav"]).decode())
                    said = True
            except Unreachable as e:
                self.down = str(e)
                log("WARNING %s; the Mac reads the answer with its own voice" % e, self.log)
                self._part(sentence, error=self.down)
            except SpeechError as e:
                log("WARNING %s" % e, self.log)
                if not said:                    # a sentence is one clip: one that came is not said again
                    self._part(sentence, error=str(e))
            except Exception as e:              # the Narrator's own failure: said, and the rest still goes out
                why = "the desktop's voice failed here: %s: %s" % (type(e).__name__, e)
                log("ERROR %s" % why, self.log)
                if not said:
                    self._part(sentence, error=why)
