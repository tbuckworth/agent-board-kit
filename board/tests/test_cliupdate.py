"""agent-cli-update with a fake runner: python3 -m unittest discover -s tests"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
from agentboard import cliupdate  # noqa: E402


class FakeCli:
    """`--version` answers from a list (before, after; None: it fails); `update` exits with `code`."""
    def __init__(self, versions, code=0, said=""):
        self.versions, self.code, self.said, self.calls = list(versions), code, said, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd[1] == "--version":
            v = self.versions.pop(0)
            if v is None:
                return subprocess.CompletedProcess(cmd, 127, "", "not found")
            return subprocess.CompletedProcess(cmd, 0, "%s (Claude Code)\n" % v, "")
        return subprocess.CompletedProcess(cmd, self.code, "", self.said)


class UpdateTest(unittest.TestCase):
    def one(self, fake, find=lambda n: "/bin/" + n):
        return cliupdate.update_one("claude", ["update"], fake, find)

    def test_up_to_date_changed_and_failed(self):
        self.assertEqual(self.one(FakeCli(["2.1.304", "2.1.304"])), ("claude 2.1.304: up to date", True))
        self.assertEqual(self.one(FakeCli(["2.1.304", "2.1.305"])), ("claude 2.1.304 -> 2.1.305", True))
        line, ok = self.one(FakeCli(["2.1.304", "2.1.304"], code=1, said="network down\nno route"))
        self.assertEqual((line, ok), ("claude 2.1.304: update failed (exit 1): no route", False))
        self.assertEqual(self.one(FakeCli([]), find=lambda n: None), ("claude: not installed, skipped", None))

    def test_no_version_after_an_update_is_a_failure(self):
        self.assertEqual(self.one(FakeCli(["2.1.304", None])),
                         ("claude 2.1.304: update failed: `claude --version` gives no version afterwards", False))

    def test_never_waits_for_input(self):
        fake = FakeCli(["0.163.0", "0.163.0"])
        cliupdate.update_one("codex", ["update"], fake, lambda n: "/bin/codex")
        update = next(kw for cmd, kw in fake.calls if cmd == ["/bin/codex", "update"])
        self.assertIs(update["stdin"], subprocess.DEVNULL)
        self.assertEqual(update["env"]["CODEX_NON_INTERACTIVE"], "1")

    def test_run_all_logs_a_line_each(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "state" / "cli-update.log"
            fakes = {"/bin/codex": FakeCli(["0.163.0", "0.164.0"]), "/bin/claude": FakeCli(["2.1.304", "2.1.304"], code=2)}
            ok = cliupdate.run_all(lambda cmd, **kw: fakes[cmd[0]](cmd, **kw), lambda n: "/bin/" + n, log)
            self.assertFalse(ok)
            lines = log.read_text().splitlines()
            self.assertTrue(lines[0].endswith(" codex 0.163.0 -> 0.164.0"))
            self.assertIn(" claude 2.1.304: update failed (exit 2)", lines[1])

    def test_a_cli_that_is_not_installed_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "cli-update.log"
            claude = FakeCli(["2.1.304", "2.1.304"])
            find = lambda n: "/bin/claude" if n == "claude" else None      # no Codex: optional in the kit
            self.assertTrue(cliupdate.run_all(claude, find, log))
            lines = log.read_text().splitlines()
            self.assertTrue(lines[0].endswith(" codex: not installed, skipped"))
            self.assertTrue(lines[1].endswith(" claude 2.1.304: up to date"))
            self.assertFalse(cliupdate.run_all(FakeCli([]), lambda n: None, log))      # neither installed: a failure


if __name__ == "__main__":
    unittest.main()
