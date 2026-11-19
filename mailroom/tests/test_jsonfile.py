"""The JSON files the tools keep: written whole or not at all, with the mode each caller asks for."""
import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from agentmail import jsonfile  # noqa: E402


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class JsonFiles(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_write_replaces_the_file_whole(self):
        path = self.dir / "new" / "state.json"   # the folder is created
        jsonfile.write(path, {"a": 1}, jsonfile.PRIVATE, None)
        with open(path) as before:                # a reader that opened the old file
            jsonfile.write(path, {"a": 2, "b": [1]}, jsonfile.PRIVATE, 1)
            self.assertEqual(before.read(), '{"a": 1}')   # still sees all of the old one, never half of the new
        self.assertEqual(jsonfile.read(path), {"a": 2, "b": [1]})
        self.assertEqual(path.read_text(), '{\n "a": 2,\n "b": [\n  1\n ]\n}')
        self.assertEqual(os.listdir(path.parent), ["state.json"])   # no temp file left behind

    def test_a_write_that_cannot_be_put_in_place_raises_and_leaves_no_temp_file(self):
        (self.dir / "state.json").mkdir()          # a folder where the file should go: the rename fails
        for durable in (False, True):
            with self.assertRaises(OSError):
                jsonfile.write_bytes(self.dir / "state.json", b"{}", jsonfile.PRIVATE, durable)
            self.assertEqual(os.listdir(self.dir), ["state.json"])
            self.assertTrue((self.dir / "state.json").is_dir())

    def test_mode_is_the_callers(self):
        umask = os.umask(0o022)
        try:
            jsonfile.write(self.dir / "private.json", {}, jsonfile.PRIVATE, None)
            jsonfile.write(self.dir / "plain.json", {}, jsonfile.PLAIN, None)
        finally:
            os.umask(umask)
        self.assertEqual(mode(self.dir / "private.json"), 0o600)
        self.assertEqual(mode(self.dir / "plain.json"), 0o644)

    def test_missing_reads_as_empty_and_broken_raises_with_its_path(self):
        self.assertEqual(jsonfile.read(self.dir / "none.json"), {})
        (self.dir / "broken.json").write_text('{"a": ')
        with self.assertRaisesRegex(ValueError, "^%s is not JSON: Expecting value" % (self.dir / "broken.json")):
            jsonfile.read(self.dir / "broken.json")

    def test_a_log_skips_lines_that_are_not_json_and_says_how_many(self):
        log = self.dir / "events.jsonl"
        self.assertEqual(jsonfile.read_lines(log), [])                  # missing: nothing logged yet
        log.write_text('{"n": 1}\n{"n": 2, "cut sh\n\n[3]\n{"n": 4}\n{"n": \n')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(jsonfile.read_lines(log), [{"n": 1}, {"n": 4}])
        self.assertEqual(err.getvalue(), "%s: skipped 3 line(s) that are not JSON objects\n" % log)
        log.write_text('{"n": 1}\n')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(jsonfile.read_lines(log), [{"n": 1}])
        self.assertEqual(err.getvalue(), "")

    def test_a_log_line_is_appended_whole_with_the_callers_mode(self):
        log = self.dir / "events.jsonl"
        jsonfile.append_line(log, {"n": 1, "text": "é"}, jsonfile.PRIVATE)
        jsonfile.append_line(log, {"n": 2}, jsonfile.PLAIN)   # the mode applies only when the file is made
        self.assertEqual(log.read_text(), '{"n": 1, "text": "é"}\n{"n": 2}\n')
        self.assertEqual(mode(log), 0o600)

    def test_tail_of_a_log(self):
        log = self.dir / "t.jsonl"
        log.write_bytes(b'{"n": 1}\n{"n": 2, "s": "\xc3\xa9"}\n{"n": 3}\n')
        self.assertEqual(jsonfile.read_tail(log, 1 << 20), ['{"n": 1}', '{"n": 2, "s": "é"}', '{"n": 3}'])
        self.assertEqual(jsonfile.read_tail(log, 13), ['�"}', '{"n": 3}'])   # cut mid-line and mid-character

    def test_a_json_file_keeps_its_place_and_format(self):
        f = jsonfile.JsonFile(self.dir / "s.json", jsonfile.PRIVATE, 1)
        self.assertEqual(f.read(), {})
        f.write({"x": 1})
        self.assertEqual((f.read(), (self.dir / "s.json").read_text(), mode(self.dir / "s.json")),
                         ({"x": 1}, '{\n "x": 1\n}', 0o600))


if __name__ == "__main__":
    unittest.main()
