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
    """The runner for a CLI, standing in for `claude` or `codex`: a real `update` would reinstall this machine's CLI
    over the network. `--version` answers from a list (before, after; None: it fails); `update` exits with `code`."""
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


class FakeTrial:
    """The runner for `agent-mail fast-trial`, standing in for the mailroom's trial, which starts a real CLI in tmux: it
    answers every call with `code`, `out` on stdout and `err` on stderr, or raises `raises`."""
    def __init__(self, code=0, out="", err="", raises=None):
        self.code, self.out, self.err, self.raises, self.calls = code, out, err, raises, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.code, self.out, self.err)


PASSED = "fast-trial claude 2.1.331: passed (effort xhigh -> low -> xhigh); added to fast mode's tested list\n"
FAILED = "fast-trial claude 2.1.331: FAILED: no new effort level showed; screens in /s/failed/claude-2.1.331\n"


class UpdateTest(unittest.TestCase):
    def one(self, fake, find=lambda n: "/bin/" + n):        # find: where this machine has the CLI
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
        log = Path(tempfile.mkdtemp()) / "state" / "cli-update.log"
        fakes = {"/bin/codex": FakeCli(["0.163.0", "0.164.0"]), "/bin/claude": FakeCli(["2.1.304", "2.1.304"], code=2),
                 "timeout": FakeTrial(out=PASSED)}
        ok = cliupdate.run_all(lambda cmd, **kw: fakes[cmd[0]](cmd, **kw), lambda n: "/bin/" + n, log)
        self.assertFalse(ok)
        lines = log.read_text().splitlines()
        self.assertTrue(lines[0].endswith(" codex 0.163.0 -> 0.164.0"))
        self.assertIn(" claude 2.1.304: update failed (exit 2)", lines[1])
        self.assertEqual(len(lines), 4)   # each CLI's trial runs even after a failed update: what runs may be untested


    def test_a_cli_that_is_not_installed_is_skipped_and_gets_no_trial(self):
        log = Path(tempfile.mkdtemp()) / "cli-update.log"
        fakes = {"/bin/claude": FakeCli(["2.1.304", "2.1.304"]), "timeout": FakeTrial(out=PASSED)}
        find = lambda n: None if n == "codex" else "/bin/" + n      # no Codex: optional in the kit
        self.assertTrue(cliupdate.run_all(lambda cmd, **kw: fakes[cmd[0]](cmd, **kw), find, log))
        lines = log.read_text().splitlines()
        self.assertTrue(lines[0].endswith(" codex: not installed, skipped"))
        self.assertTrue(lines[1].endswith(" claude 2.1.304: up to date"))
        self.assertEqual(len(lines), 3)                               # one trial: claude's
        self.assertEqual(fakes["timeout"].calls[0][0][3:], ["/bin/agent-mail", "fast-trial", "claude", "--if-untested"])
        self.assertFalse(cliupdate.run_all(FakeCli([]), lambda n: None, log))      # neither installed: a failure

class TrialTest(unittest.TestCase):
    """After the updates, `agent-mail fast-trial CLI --if-untested` for each CLI: fast mode's effort keys tried on a
    version not tried yet, which a pass adds to fast mode's tested list (the mailroom's agentmail/fasttrial.py)."""

    def one(self, fake, find=lambda n: "/bin/" + n):
        return cliupdate.trial_one("claude", fake, find)

    def test_runs_the_trial_if_untested_under_a_time_limit_and_logs_its_line(self):
        fake = FakeTrial(out=PASSED)
        self.assertEqual(self.one(fake), (PASSED.strip(), True))
        (cmd, kw), = fake.calls
        limit = str(cliupdate.TRIAL_SECONDS)
        self.assertEqual(cmd, ["timeout", "--kill-after=%d" % cliupdate.TRIAL_KILL_SECONDS, limit,
                               "/bin/agent-mail", "fast-trial", "claude", "--if-untested"])
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertGreater(kw["timeout"], cliupdate.TRIAL_SECONDS + cliupdate.TRIAL_KILL_SECONDS)

    def test_a_version_tested_already_is_logged_as_agent_mail_says(self):
        said = "fast-trial claude 2.1.330: on fast mode's tested list already; nothing to do"
        self.assertEqual(self.one(FakeTrial(out=said + "\n")), (said, True))

    def test_a_failed_trial_is_logged_and_fails_the_run(self):
        self.assertEqual(self.one(FakeTrial(code=1, out=FAILED)), (FAILED.strip(), False))
        self.assertEqual(self.one(FakeTrial(code=2, err="agent-mail: error: invalid choice: 'fast-trial'\n")),
                         ("fast-trial claude: failed (exit 2): agent-mail: error: invalid choice: 'fast-trial'", False))
        self.assertEqual(self.one(FakeTrial(code=1)), ("fast-trial claude: failed (exit 1): no output", False))

    def test_a_trial_past_its_time_or_that_cannot_run_is_a_failure(self):
        self.assertEqual(self.one(FakeTrial(code=124)),
                         ("fast-trial claude: stopped after %d s" % cliupdate.TRIAL_SECONDS, False))
        self.assertEqual(self.one(FakeTrial(code=137)),
                         ("fast-trial claude: stopped after %d s" % cliupdate.TRIAL_SECONDS, False))
        line, ok = self.one(FakeTrial(raises=subprocess.TimeoutExpired(["timeout"], 690)))
        self.assertFalse(ok)
        self.assertTrue(line.startswith("fast-trial claude: failed (Command "), line)
        self.assertEqual(self.one(FakeTrial(raises=FileNotFoundError("timeout"))),
                         ("fast-trial claude: failed (timeout)", False))

    def test_no_agent_mail_is_a_failure(self):
        self.assertEqual(self.one(FakeTrial(), find=lambda n: None),
                         ("fast-trial claude: agent-mail is not installed", False))

    def test_run_all_tries_each_cli_after_the_updates_and_a_failed_trial_undoes_nothing(self):
        log = Path(tempfile.mkdtemp()) / "cli-update.log"
        codex, claude = FakeCli(["0.171.0", "0.171.0"]), FakeCli(["2.1.330", "2.1.331"])
        trial = FakeTrial(code=1, out=FAILED)
        fakes = {"/bin/codex": codex, "/bin/claude": claude, "timeout": trial}
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return fakes[cmd[0]](cmd, **kw)
        self.assertFalse(cliupdate.run_all(run, lambda n: "/bin/" + n, log))
        self.assertEqual([c[1:] if c[0] != "timeout" else c[3:] for c in calls], [
            ["--version"], ["update"], ["--version"], ["--version"], ["update"], ["--version"],
            ["/bin/agent-mail", "fast-trial", "codex", "--if-untested"],
            ["/bin/agent-mail", "fast-trial", "claude", "--if-untested"]])   # nothing after: no update undone
        lines = log.read_text().splitlines()
        self.assertTrue(lines[1].endswith(" claude 2.1.330 -> 2.1.331"))
        self.assertTrue(lines[3].endswith(" " + FAILED.strip()))
        self.assertEqual(len(lines), 4)

    def test_run_all_passes_when_updates_and_trials_do(self):
        log = Path(tempfile.mkdtemp()) / "cli-update.log"
        fakes = {"/bin/codex": FakeCli(["0.171.0", "0.171.0"]), "/bin/claude": FakeCli(["2.1.330", "2.1.330"]),
                 "timeout": FakeTrial(out=PASSED)}
        self.assertTrue(cliupdate.run_all(lambda cmd, **kw: fakes[cmd[0]](cmd, **kw), lambda n: "/bin/" + n, log))


if __name__ == "__main__":
    unittest.main()
