"""Fake tmux, codex and claude executables for tests of starting an agent. tmux and the two CLIs are the boundary: a
test must not open a pane in the owner's tmux server or start an agent. install() puts them first on PATH, so the code
under test finds and runs them as it would the real ones; each records its arguments.

The fake tmux is called as the code calls it, with -S SOCKET first (recorded in sockets; Bins.calls leaves them out)
and notes each TMUX it inherits (tmux-env). It keeps a list of the sessions new-session made (has-session answers
from it) and shows a screen with Codex's prompt on capture-pane. display-message prints its format with
#{pane_in_mode} (FAKE_TMUX_IN_MODE, "0" by default), #{pane_current_command} (FAKE_TMUX_COMMAND) and #{pane_pid}
(FAKE_TMUX_PANE_PID) filled in and every other field empty, so no memory cap is applied to a made-up pane;
list-panes -a prints FAKE_TMUX_PANES (every pane), list-panes of one session FAKE_TMUX_SESSION_PANES, and
list-clients FAKE_TMUX_CLIENTS; load-buffer keeps its stdin in the file "buffer". With FAKE_TMUX_DIALOG set,
capture-pane shows that screen instead until send-keys types FAKE_TMUX_DIALOG_KEY after it was shown.

With FAKE_TMUX_EFFORT set (a level), the pane holds an agent of FAKE_TMUX_EFFORT_CLI, drawn as Claude Code and Codex
draw their prompt, whose effort its keys step as theirs do (S-Down and S-Up, between low and xhigh; not at all with
FAKE_TMUX_EFFORT_STUCK). The screen shows the level: Codex's status line always, Claude Code's line over its prompt
once a key was pressed, each only from the (FAKE_TMUX_EFFORT_LATE + 1)th read after a key (a slow redraw).
FAKE_TMUX_OUTPUT is agent output above the prompt, and from the FAKE_TMUX_DIALOG_AFTERth key on a permission dialog
takes the prompt's place; once FAKE_TMUX_SEND_FAIL_AFTER keys have gone, send-keys fails.
The fake codex answers `codex queue` with FAKE_CODEX_QUEUE (by default a success), exit 1 unless that starts with
"Queued message".

fake_gmail() stands in for the agentmail.gmail module, whose Gmail API calls are the network boundary: a test sets
and checks those, while the module's parsers run for real on messages built in Gmail's own shape (gmail_message)."""
import base64
import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path
from unittest import mock

import isolate_state  # noqa: F401  (temporary state dirs; before agentmail)
import homes
from agentmail import gmail

RECORD = '''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_BIN_DIR"]
with open(os.path.join(d, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\\n")
args = sys.argv[1:]
'''

TMUX = RECORD + '''import re
if args[:1] == ["-S"]:
    with open(os.path.join(d, "sockets"), "a") as fh:
        fh.write(args[1] + "\\n")
    args = args[2:]
if os.environ.get("TMUX"):
    with open(os.path.join(d, "tmux-env"), "a") as fh:
        fh.write(os.environ["TMUX"] + "\\n")
sessions = os.path.join(d, "sessions")
names = open(sessions).read().split() if os.path.exists(sessions) else []
fields = {"pane_in_mode": os.environ.get("FAKE_TMUX_IN_MODE", "0"),
          "pane_current_command": os.environ.get("FAKE_TMUX_COMMAND", ""),
          "pane_pid": os.environ.get("FAKE_TMUX_PANE_PID", "")}
levels = ["low", "medium", "high", "xhigh"]


def state(name, default):
    path = os.path.join(d, name)
    return open(path).read() if os.path.exists(path) else default


def keep(name, value):
    with open(os.path.join(d, name), "w") as fh:
        fh.write(str(value))


if os.environ.get("FAKE_TMUX_EFFORT") and not os.path.exists(os.path.join(d, "effort")):
    keep("effort", os.environ["FAKE_TMUX_EFFORT"])
effort = os.path.exists(os.path.join(d, "effort"))
if args[0] == "has-session":
    sys.exit(0 if args[args.index("-t") + 1].lstrip("=") in names else 1)
if args[0] == "new-session":
    with open(sessions, "a") as fh:
        fh.write(args[args.index("-s") + 1] + "\\n")
elif args[0] == "display-message":
    print(re.sub(r"#\\{(\\w+)\\}", lambda m: fields.get(m.group(1), ""), args[-1]))
elif args[0] == "list-panes":
    print(os.environ.get("FAKE_TMUX_PANES" if "-a" in args else "FAKE_TMUX_SESSION_PANES", ""))
elif args[0] == "list-clients":
    print(os.environ.get("FAKE_TMUX_CLIENTS", ""))
elif args[0] == "load-buffer":
    keep("buffer", sys.stdin.read())
elif args[0] == "send-keys" and args[-1] in ("S-Down", "S-Up") and effort:
    if os.environ.get("FAKE_TMUX_SEND_FAIL_AFTER") and \
            int(state("presses", "0")) >= int(os.environ["FAKE_TMUX_SEND_FAIL_AFTER"]):
        sys.stderr.write("lost server\\n")
        sys.exit(1)
    level = state("effort", "")
    if not os.environ.get("FAKE_TMUX_EFFORT_STUCK"):
        i = levels.index(level) + (1 if args[-1] == "S-Up" else -1)
        level = levels[min(max(i, 0), len(levels) - 1)]
    keep("effort", level)
    keep("presses", int(state("presses", "0")) + 1)
    keep("reads", 0)
elif (args[0] == "send-keys" and os.environ.get("FAKE_TMUX_DIALOG_KEY") == args[-1]
      and os.path.exists(os.path.join(d, "shown"))):
    open(os.path.join(d, "answered"), "a").close()
elif args[0] == "capture-pane" and effort:
    codex = os.environ.get("FAKE_TMUX_EFFORT_CLI") == "codex"
    presses, reads = int(state("presses", "0")), int(state("reads", "0")) + 1
    keep("reads", reads)
    if reads > int(os.environ.get("FAKE_TMUX_EFFORT_LATE", "0")) and (codex or presses):
        keep("drawn", state("effort", ""))
    drawn = state("drawn", os.environ["FAKE_TMUX_EFFORT"] if codex else "")
    out = [os.environ.get("FAKE_TMUX_OUTPUT", "")]
    after = os.environ.get("FAKE_TMUX_DIALOG_AFTER")
    if after and presses >= int(after):
        out += [" Do you want to proceed?", " \u276f 1. Yes", "   2. Yes, and don't ask again", "   3. No"] if not codex \\
            else ["  Allow command?", "\u203a 1. Yes, proceed", "  2. No"]
    elif codex:
        out += ["\u203a Ask Codex to do anything", "", "  GPT-6.1-Sol %s \u00b7 ~/repo" % drawn if drawn else "",
                "  ? for shortcuts"]   # as Codex draws it: a blank line between the composer and the status line
    else:
        out += [("%s\u25d0 %s \u00b7 /effort" % (" " * 60, drawn)) if drawn else "", "\u2500" * 60, "\u276f ",
                "\u2500" * 60, "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"]
    print("\\n".join(out))
elif args[0] == "capture-pane":
    dialog = os.environ.get("FAKE_TMUX_DIALOG")
    if dialog and not os.path.exists(os.path.join(d, "answered")):
        open(os.path.join(d, "shown"), "a").close()
        print(dialog)
    else:
        print(os.environ["FAKE_TMUX_SCREEN"])
'''

CODEX = RECORD + '''if args[:1] == ["queue"]:
    said = os.environ["FAKE_CODEX_QUEUE"]
    print(said)
    sys.exit(0 if said.startswith("Queued message") else 1)
'''


class Bins:
    def __init__(self, folder):
        self.dir = folder

    def calls(self, prog, raw=False):
        """Each call to prog ("tmux", "codex" or "claude"), as its argument list; a tmux call's leading -S SOCKET is
        left out unless raw."""
        log = self.dir / "calls.jsonl"
        lines = log.read_text().splitlines() if log.exists() else []
        calls = [c[1:] for c in map(json.loads, lines) if c[0] == prog]
        return calls if raw or prog != "tmux" else [c[2:] if c[:1] == ["-S"] else c for c in calls]

    def tmux_env(self):
        """The TMUX values the fake tmux inherited, one per call that had one."""
        path = self.dir / "tmux-env"
        return path.read_text().split() if path.exists() else []

    def add_session(self, name):
        with open(self.dir / "sessions", "a") as fh:
            fh.write(name + "\n")


SETTINGS = {"FAKE_TMUX_SCREEN": "\u203a Ask Codex", "FAKE_CODEX_QUEUE": "Queued message 1 for thread"}
# Codex's start-up screen when a hook it runs is new or changed since it was trusted (Codex 0.167)
CODEX_HOOK_REVIEW = ("  Hooks need review\n  1 hook is new or changed.\n"
                     "  Hooks can run outside the sandbox after you trust them.\n"
                     "\u203a 1. Review hooks\n  2. Trust all and continue\n"
                     "  3. Continue without trusting (hooks won't run)\n  enter confirm \u00b7 esc skip\n")


@contextlib.contextmanager
def keys_held_elsewhere():
    """commands.keys_lock held by another thread (another program typing in a pane) until the block ends."""
    from agentmail import commands
    held, done = threading.Event(), threading.Event()

    def hold():
        with commands.keys_lock(True):
            held.set()
            done.wait(30)
    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        yield
    finally:
        done.set()
        t.join()


def keys_taken():
    """Whether another thread finds commands.keys_lock taken (in this thread a holder takes it again at once)."""
    from agentmail import commands
    found = []

    def probe():
        try:
            with commands.keys_lock(False):
                found.append(False)
        except commands.LockBusy:
            found.append(True)
    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return found[0]


def install(test, **env):
    """Fake tmux, codex and claude first on PATH until the test ends; env overrides SETTINGS. Returns their Bins."""
    folder = Path(tempfile.mkdtemp())
    for name, text in (("tmux", TMUX), ("codex", CODEX), ("claude", RECORD)):
        (folder / name).write_text(text)
        (folder / name).chmod(0o755)
    patch = mock.patch.dict(os.environ, dict(SETTINGS, **env, FAKE_BIN_DIR=str(folder),
                                             PATH="%s%s%s" % (folder, os.pathsep, os.environ["PATH"])))
    patch.start()
    test.addCleanup(patch.stop)
    return Bins(folder)


# The agentmail.gmail functions that call the Gmail API. fake_gmail makes each a Mock; everything else is real.
GMAIL_API = ("service", "profile", "send_raw", "get_metadata", "get_full", "get_raw", "get_thread", "history_since",
             "search", "ensure_label", "modify", "thread_messages")
GMAIL_PARSERS = ("headers", "all_headers", "recipients", "payload_parts", "bodies", "pdf_parts", "decode_bytes",
                 "body_text", "auth_results", "HistoryGone", "web_url", "message_search", "OFF", "GmailOff")


def fake_gmail():
    """A stand-in for the agentmail.gmail module: its Gmail API calls (GMAIL_API) are Mocks for the test to set and
    check, its parsers, link builders, HistoryGone and the service held with Gmail off (OFF, which raises GmailOff)
    the real ones. Any other name raises AttributeError."""
    fake = mock.Mock(spec=GMAIL_API + GMAIL_PARSERS)
    for name in GMAIL_PARSERS:
        setattr(fake, name, getattr(gmail, name))
    return fake


def sending_gmail(test, *modules):
    """fake_gmail() as the gmail of each of modules until the test ends, keeping what is sent. Returns the list of
    (raw bytes, thread id) sent, each message answered with ids g1, g2... and the thread it went to (else t1,
    t2...); get_metadata answers <gN@mail.gmail.com> as message gN's Message-ID."""
    fake, sent = fake_gmail(), []

    def send_raw(svc, raw, thread_id=None):
        sent.append((raw, thread_id))
        return {"id": "g%d" % len(sent), "threadId": thread_id or "t%d" % len(sent)}
    fake.service.return_value = "svc"
    fake.send_raw.side_effect = send_raw
    fake.get_metadata.side_effect = lambda svc, gid, names: {
        "payload": {"headers": [{"name": "Message-ID", "value": "<%s@mail.gmail.com>" % gid}]}}
    for module in modules:
        homes.patch(test, module, "gmail", fake)
    return sent


def auth_header(domain, dkim="pass"):
    """An Authentication-Results header as Gmail writes it, with DKIM `dkim` for domain."""
    return ("mx.google.com; dkim=%s header.i=@%s header.s=20230601 header.b=abc; spf=pass smtp.mailfrom=%s; "
            "dmarc=pass header.from=%s" % (dkim, domain, domain, domain))


def text_part(text, mime="text/plain"):
    """A body part as Gmail's API returns it: the text base64url-encoded."""
    data = base64.urlsafe_b64encode(text.encode()).decode()
    return {"mimeType": mime, "filename": "", "body": {"size": len(text), "data": data}}


def gmail_message(headers, parts, gid="in", thread_id="thread"):
    """A message as Gmail's API returns it (format=full): headers, a list of (name, value) pairs in order, and
    parts (text_part and the like) under a multipart/mixed payload."""
    return {"id": gid, "threadId": thread_id, "internalDate": "1791192808000",
            "payload": {"mimeType": "multipart/mixed", "headers": [{"name": n, "value": v} for n, v in headers],
                        "parts": list(parts)}}
