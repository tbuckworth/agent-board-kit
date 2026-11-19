"""Fake tmux, codex and claude executables for tests of starting an agent. tmux and the two CLIs are the boundary: a
test must not open a pane in the owner's tmux server or start an agent. install() puts them first on PATH, so the code
under test finds and runs them as it would the real ones; each records its arguments.

The fake tmux keeps a list of the sessions new-session made (has-session answers from it) and shows a screen with
Codex's prompt on capture-pane; display-message prints nothing, so no memory cap is applied to a made-up pane. With
FAKE_TMUX_DIALOG set, capture-pane shows that screen instead until send-keys types FAKE_TMUX_DIALOG_KEY after it was
shown.
The fake codex answers `codex queue` with FAKE_CODEX_QUEUE (by default a success), exit 1 unless that starts with
"Queued message".

fake_gmail() stands in for the agentmail.gmail module, whose Gmail API calls are the network boundary: a test sets
and checks those, while the module's parsers run for real on messages built in Gmail's own shape (gmail_message)."""
import base64
import json
import os
import tempfile
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

TMUX = RECORD + '''sessions = os.path.join(d, "sessions")
names = open(sessions).read().split() if os.path.exists(sessions) else []
if args[0] == "has-session":
    sys.exit(0 if args[args.index("-t") + 1].lstrip("=") in names else 1)
if args[0] == "new-session":
    with open(sessions, "a") as fh:
        fh.write(args[args.index("-s") + 1] + "\\n")
elif (args[0] == "send-keys" and os.environ.get("FAKE_TMUX_DIALOG_KEY") == args[-1]
      and os.path.exists(os.path.join(d, "shown"))):
    open(os.path.join(d, "answered"), "a").close()
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

    def calls(self, prog):
        """Each call to prog ("tmux", "codex" or "claude"), as its argument list."""
        log = self.dir / "calls.jsonl"
        lines = log.read_text().splitlines() if log.exists() else []
        return [c[1:] for c in map(json.loads, lines) if c[0] == prog]

    def add_session(self, name):
        with open(self.dir / "sessions", "a") as fh:
            fh.write(name + "\n")


SETTINGS = {"FAKE_TMUX_SCREEN": "\u203a Ask Codex", "FAKE_CODEX_QUEUE": "Queued message 1 for thread"}
# Codex's start-up screen when a hook it runs is new or changed since it was trusted (Codex 0.167)
CODEX_HOOK_REVIEW = ("  Hooks need review\n  1 hook is new or changed.\n"
                     "  Hooks can run outside the sandbox after you trust them.\n"
                     "\u203a 1. Review hooks\n  2. Trust all and continue\n"
                     "  3. Continue without trusting (hooks won't run)\n  enter confirm \u00b7 esc skip\n")


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
                 "body_text", "auth_results", "HistoryGone", "web_url", "message_search")


def fake_gmail():
    """A stand-in for the agentmail.gmail module: its Gmail API calls (GMAIL_API) are Mocks for the test to set and
    check, its parsers, link builders and HistoryGone the real ones. Any other name raises AttributeError."""
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
