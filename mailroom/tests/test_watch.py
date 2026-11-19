"""The loop agent-failover and agent-memwatch run: one pass or forever, state saved only when it changed and
never on a dry run, and a failing pass logged without stopping the watch."""
import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from agentmail import jsonfile, watch  # noqa: E402


class Stop(Exception):
    pass


def options(*argv):
    ap = argparse.ArgumentParser()
    watch.add_options(ap, "act on nothing")
    return ap.parse_args(list(argv))


class Loop(unittest.TestCase):
    def setUp(self):
        self.state = jsonfile.JsonFile(Path(tempfile.mkdtemp()) / "state.json", jsonfile.PLAIN, 1)
        self.seen = []

    def run_loop(self, a, step, passes=None):
        """watch.run, stopped after `passes` passes when it would loop; returns (exit code, log lines)."""
        def interval():
            if passes is not None and len(self.seen) >= passes:
                raise Stop()
            return 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = watch.run(a, step, self.state, interval)
            except Stop:
                code = None
        return code, out.getvalue()

    def counting(self, state):
        self.seen.append(dict(state))
        return dict(state, n=state.get("n", 0) + 1)

    def test_once_saves_a_changed_state(self):
        self.assertTrue(watch.once(options("--once")))
        self.assertEqual(self.run_loop(options("--once"), self.counting), (0, ""))
        self.assertEqual((self.seen, self.state.read()), ([{}], {"n": 1}))

    def test_dry_run_is_one_pass_that_saves_nothing(self):
        a = options("--dry-run")
        self.assertTrue(watch.once(a))
        self.assertEqual(self.run_loop(a, self.counting)[0], 0)
        self.assertEqual(len(self.seen), 1)
        self.assertFalse(self.state.path.exists())

    def test_an_unchanged_state_is_not_written(self):
        self.assertEqual(self.run_loop(options("--once"), lambda st: self.seen.append(st) or st)[0], 0)
        self.assertFalse(self.state.path.exists())

    def test_a_step_that_changes_the_state_in_place_is_saved(self):
        """agent-failover's pass adds its "already noted" markers to the dict it is given and returns it: those
        are saved, so the next pass does not log the same note again."""
        def marks(state):
            state.setdefault("sessions", {}).setdefault("s1", {})["stuck_until"] = 1800
            return state
        self.state.write({"sessions": {"s1": {"home": "personal"}}})
        self.assertEqual(self.run_loop(options("--once"), marks)[0], 0)
        self.assertEqual(self.state.read(), {"sessions": {"s1": {"home": "personal", "stuck_until": 1800}}})

    def test_a_failing_pass_is_logged_and_exits_1_when_once(self):
        def boom(state):
            raise ValueError("no usage")
        code, log = self.run_loop(options("--once"), boom)
        self.assertEqual(code, 1)
        self.assertRegex(log, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d error: ValueError: no usage\n$")

    def test_a_corrupt_state_file_fails_the_pass_and_names_the_file(self):
        self.state.path.write_text('{"sessions": ')
        code, log = self.run_loop(options("--once"), self.counting)
        self.assertEqual((code, self.seen), (1, []))
        self.assertIn("error: ValueError: %s is not JSON" % self.state.path, log)
        self.assertEqual(self.state.path.read_text(), '{"sessions": ')   # left for the operator to look at

    def test_forever_outlives_a_failing_pass(self):
        def flaky(state):
            if len(self.seen) == 1:
                self.seen.append("failed")
                raise OSError("disk full")
            return self.counting(state)
        self.assertFalse(watch.once(options()))
        code, log = self.run_loop(options(), flaky, passes=3)
        self.assertIsNone(code)   # still looping when stopped
        self.assertEqual(self.seen, [{}, "failed", {"n": 1}])
        self.assertIn("error: OSError: disk full", log)
        self.assertEqual(self.state.read(), {"n": 2})


if __name__ == "__main__":
    unittest.main()
