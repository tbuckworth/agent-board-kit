"""Tests never write to the live mailroom store, config or status log, whichever test module runs first.

agentmail.config and agentmail.statuslog read their directories from the environment once, at first import,
and otherwise use the live ones under HOME, so one test module that imports agentmail before isolate_state
binds every later module in the run to the live store. Every test module imports isolate_state first.
"""
import ast
import os
import site
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import isolate_state  # temporary state dirs; before agentmail

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
DIR_VARS = tuple(var for var, _ in isolate_state.DIRS)


def first_local_import(path):
    """The first repo module a test file imports at top level: agentmail, isolate_state or a test module."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top in ("agentmail", "isolate_state") or top.startswith("test_"):
                return top
    return None


class LiveDirectories(unittest.TestCase):
    def test_a_run_that_starts_with_a_module_naming_no_directories_leaves_home_alone(self):
        home = Path(tempfile.mkdtemp())
        env = {k: v for k, v in os.environ.items() if k not in DIR_VARS}
        # The run's HOME is the temp folder; PYTHONUSERBASE keeps the real HOME's site-packages (google-auth).
        env.update(HOME=str(home), PYTHONUSERBASE=site.getuserbase())
        r = subprocess.run([sys.executable, "-m", "unittest", "test_memcap.Sizes",
                            "test_review.AgentMail.test_withdraw_then_flush_sends_nothing"],
                           cwd=TESTS, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertEqual(sorted(str(p.relative_to(home)) for p in home.rglob("*")), [])

    def test_every_test_module_imports_isolate_state_first(self):
        for path in sorted(TESTS.glob("test_*.py")):
            with self.subTest(path.name):
                self.assertEqual(first_local_import(path), "isolate_state")

    def test_isolate_state_stops_the_run_when_config_is_already_imported(self):
        # The child inherits this process's temporary directories, so its agentmail.config binds to them.
        r = subprocess.run([sys.executable, "-c", "import agentmail.config, isolate_state; print('carried on')"],
                           cwd=TESTS, env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertIn("agentmail.config was imported before isolate_state", r.stderr)


if __name__ == "__main__":
    unittest.main()
