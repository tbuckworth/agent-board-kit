"""Files given with a message: agent-send's and agent-orchestrate's --input-json read {"text", "files"} from stdin
(mailbox.piped_input), save the files for the agent (replyfiles.attach) and end the message with their paths. No real
sessions, no orchestrator runs: the inbox socket, the Codex queue and the run are stand-ins."""
import base64
import contextlib
import io
import json
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import mailbox, orchestrate, replyfiles  # noqa: E402

SID = "cccccccc-0000-4000-8000-000000000000"
RAN = {"ok": True, "cost": 0, "turns": 1, "duration_s": 1, "result": "done"}


def b64(data):
    return base64.b64encode(data).decode()


def piped(test, text):
    """sys.stdin is a file holding text until the test ends, as a redirect from a file gives it (piped.piped_text)."""
    f = tempfile.TemporaryFile("w+")
    test.addCleanup(f.close)
    f.write(text)
    f.seek(0)
    homes.patch(test, sys, "stdin", f)


def files_root(test):
    """replyfiles.FILES_DIR is a new empty folder until the test ends; returns it."""
    root = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, root)
    homes.patch(test, replyfiles, "FILES_DIR", root)
    return root


def saved(root):
    """{name: bytes} of the files saved under root, which holds at most one message's folder."""
    folders = list(root.iterdir())
    assert len(folders) <= 1, folders
    return {p.name: p.read_bytes() for f in folders for p in f.iterdir()}


class Attach(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_each_message_has_a_private_folder_and_says_where_its_files_are(self):
        said = replyfiles.attach(self.root, [("shot.png", b"\x89PNG"), ("notes.txt", b"hello")], "Alex")
        (folder,) = list(self.root.iterdir())
        self.assertRegex(folder.name, r"^m[0-9]+-[0-9a-f]{6}$")
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for name, data in (("shot.png", b"\x89PNG"), ("notes.txt", b"hello")):
            self.assertEqual(((folder / name).read_bytes(), stat.S_IMODE((folder / name).stat().st_mode)), (data, 0o600))
        self.assertEqual(said, "Alex attached 2 files, saved on this machine; open them with your file or image tools:\n"
                               "- %s (image/png, 4 B)\n- %s (text/plain, 5 B)" % (folder / "shot.png", folder / "notes.txt"))
        replyfiles.attach(self.root, [("shot.png", b"other")], "Alex")
        self.assertEqual(len(list(self.root.iterdir())), 2, "a second message has a folder of its own")

    def test_a_name_with_no_known_type_is_octet_stream(self):
        self.assertIn("(application/octet-stream, 1 B)", replyfiles.attach(self.root, [("data.zzq", b"x")], "Alex"))

    def test_only_a_replys_key_or_a_messages_is_written(self):
        replyfiles.write(self.root, "i12", [("a.txt", b"x")])
        replyfiles.write(self.root, "m1-abc123", [("a.txt", b"x")])
        for key in ("x1", "m1", "m1-ABC123", "m1-abc12", "../m1-abc123"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                replyfiles.write(self.root, key, [("a.txt", b"x")])


class PipedInput(unittest.TestCase):
    def test_text_and_files(self):
        piped(self, json.dumps({"text": "hi", "files": [{"name": "../a.png", "data": b64(b"x")}]}))
        self.assertEqual(mailbox.piped_input("the message"), ("hi", [("a.png", b"x")]))

    def test_what_is_not_text_with_files_is_refused_naming_it(self):
        for text, said, code in (("{not json", "the message is not JSON", 1),
                                 ("", 'the message is not {"text"', 1),
                                 (json.dumps({"text": "x", "files": [{"name": "a", "data": b64(b"x")}] * 21}),
                                  "the message has 21 files; the most is 20", mailbox.ANSWER_REFUSED)):
            with self.subTest(said=said):
                piped(self, text)
                with self.assertRaises(mailbox.MailError) as e:
                    mailbox.piped_input("the message")
                self.assertIn(said, str(e.exception))
                self.assertEqual(e.exception.exit_code, code)


class AgentSend(unittest.TestCase):
    def setUp(self):
        homes.Homes(self).claude(SID, "files-test", live=True, cwd="/x")
        self.root = files_root(self)

    def send(self, argv, payload):
        """agent-send's main with payload (JSON, or text as it is) on stdin: (exit code, stdout, stderr, the inbox
        socket's stand-in, the orchestrator run's)."""
        piped(self, payload if isinstance(payload, str) else json.dumps(payload))
        sender = load_script("agent-send")
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sender, "send_claude_socket", return_value="") as sock, \
                patch.object(sender, "queue_codex") as queue, \
                patch.object(orchestrate, "run_event", return_value=RAN) as run, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sender.main(argv)
        queue.assert_not_called()
        return code, out.getvalue(), err.getvalue(), sock, run

    def test_the_files_are_saved_and_the_message_ends_with_their_paths(self):
        code, out, _, sock, _ = self.send(["--raw", "--from", "Alex", "--input-json", SID],
                                          {"text": " look at this ", "files": [{"name": "shot.png", "data": b64(b"PNG")}]})
        self.assertEqual(code, 0, out)
        (folder,) = list(self.root.iterdir())
        self.assertEqual(saved(self.root), {"shot.png": b"PNG"})
        self.assertEqual(sock.call_args.args[1], "look at this\n\nAlex attached 1 file, saved on this machine; open it "
                                                 "with your file or image tools:\n- %s (image/png, 3 B)"
                         % (folder / "shot.png"))

    def test_files_alone_make_a_message(self):
        code, _, _, sock, _ = self.send(["--raw", "--from", "Alex", "--input-json", SID],
                                        {"text": "", "files": [{"name": "a.txt", "data": b64(b"x")}]})
        self.assertEqual(code, 0)
        self.assertTrue(sock.call_args.args[1].startswith("Alex attached 1 file"), sock.call_args.args[1])

    def test_a_message_without_files_is_sent_as_it_is(self):
        code, _, _, sock, _ = self.send(["--raw", "--input-json", SID], {"text": "just words"})
        self.assertEqual((code, sock.call_args.args[1], saved(self.root)), (0, "just words", {}))

    def test_a_dry_run_names_the_files_and_saves_none(self):
        code, out, _, sock, _ = self.send(["--dry-run", "--raw", "--input-json", SID],
                                          {"text": "hi", "files": [{"name": "a.png", "data": b64(b"x")},
                                                                   {"name": "b.txt", "data": b64(b"y")}]})
        self.assertEqual(code, 0)
        self.assertIn("hi\n\n[files, not saved in a dry run: a.png, b.txt]", out)
        self.assertEqual(saved(self.root), {})
        sock.assert_not_called()

    def test_to_the_orchestrator_the_event_ends_with_the_paths(self):
        code, _, _, sock, run = self.send(["--raw", "--from", "Alex", "--input-json", "orchestrator"],
                                          {"text": "start an agent on this", "files": [{"name": "a.png", "data": b64(b"x")}]})
        self.assertEqual(code, 0)
        sock.assert_not_called()
        (folder,) = list(self.root.iterdir())
        self.assertIn("start an agent on this\n\nAlex attached 1 file, saved on this machine; open it with your file "
                      "or image tools:\n- %s (image/png, 1 B)" % (folder / "a.png"), run.call_args.args[0])

    def test_input_that_will_not_do_sends_nothing(self):
        for argv, payload, said in (
                (["--input-json", SID, "words"], {"text": "x"}, "the message is in the JSON on stdin"),
                (["--input-json", SID], "{not json", "--input-json: the message is not JSON"),
                (["--input-json", SID], {"text": "x", "files": [{"name": "a", "data": "not base64!"}]},
                 "file 'a': its data is not base64"),
                (["--input-json", SID], {"text": " ", "files": []}, "empty message")):
            with self.subTest(said=said):
                code, _, err, sock, run = self.send(argv, payload)
                self.assertEqual(code, 1)
                self.assertIn(said, err)
                sock.assert_not_called()
                run.assert_not_called()
        self.assertEqual(saved(self.root), {})


class AgentOrchestrate(unittest.TestCase):
    def setUp(self):
        homes.Homes(self)
        self.root = files_root(self)

    def run_it(self, argv, payload):
        piped(self, payload if isinstance(payload, str) else json.dumps(payload))
        orch = load_script("agent-orchestrate")
        out, err = io.StringIO(), io.StringIO()
        with patch.object(orchestrate, "run_event", return_value=RAN) as run, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = orch.main(argv)
        return code, err.getvalue(), run

    def test_the_event_ends_with_the_paths_of_the_owners_files(self):
        code, _, run = self.run_it(["--source", "board", "--input-json"],
                                   {"text": "spin up an agent on this", "files": [{"name": "shot.png", "data": b64(b"PNG")}]})
        self.assertEqual(code, 0)
        (folder,) = list(self.root.iterdir())
        self.assertEqual(run.call_args.args[:2], (
            "spin up an agent on this\n\nAlex attached 1 file, saved on this machine; open it with your file or image "
            "tools:\n- %s (image/png, 3 B)" % (folder / "shot.png"), "board"))

    def test_input_that_will_not_do_runs_nothing(self):
        for argv, payload, said in ((["--input-json", "words"], {"text": "x"}, "the event is in the JSON on stdin"),
                                    (["--input-json"], "{not json", "--input-json: the message is not JSON"),
                                    (["--input-json"], {"text": ""}, "no event text")):
            with self.subTest(said=said):
                code, err, run = self.run_it(argv, payload)
                self.assertEqual(code, 1)
                self.assertIn(said, err)
                run.assert_not_called()
        self.assertEqual(saved(self.root), {})


if __name__ == "__main__":
    unittest.main()
