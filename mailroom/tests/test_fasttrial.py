"""Fast mode's one list of tested versions (paneeffort.TESTED and the file agent-mail fast-trial adds to, read at each
use), and agent-mail fast-trial around the trial itself: its arguments, --if-untested, the installed version and its
binary in each CLI's own install layout, the seeded config, the launch line, and what a pass or a failure records and
prints. The trial proper needs a real tmux and a real CLI, so it is run by hand on the desktop (README.md, "Fast
mode"); here it is stubbed (fasttrial.trial)."""
import contextlib
import fcntl
import io
import json
import os
import shlex
import signal
import stat
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import commands, fasttrial, jsonfile, paneeffort  # noqa: E402

NEW = "2.1.999"   # a Claude Code version no list names


class Lists(unittest.TestCase):
    """paneeffort.tested: the versions tried by hand (TESTED) and those the trial passed on this machine (the file)."""

    def setUp(self):
        self.path = homes.temp_dir(self) / "fast-tested.json"
        homes.patch(self, paneeffort, "TESTED_PATH", self.path)

    def test_the_code_list_alone_while_there_is_no_file(self):
        self.assertEqual(paneeffort.tested("claude"), set(paneeffort.TESTED["claude"]))
        self.assertEqual(paneeffort.tested("codex"), set(paneeffort.TESTED["codex"]))

    def test_the_file_adds_its_versions_to_the_code_list_for_its_own_cli(self):
        self.path.write_text(json.dumps({"claude": {NEW: "2026-11-23T10:00:00Z"}}))
        self.assertEqual(paneeffort.tested("claude"), set(paneeffort.TESTED["claude"]) | {NEW})
        self.assertEqual(paneeffort.tested("codex"), set(paneeffort.TESTED["codex"]))

    def test_read_at_each_call_so_a_running_daemon_sees_a_version_added_since(self):
        self.assertNotIn(NEW, paneeffort.tested("claude"))
        paneeffort.add_tested("claude", NEW, "2026-11-23T10:00:00Z")
        self.assertIn(NEW, paneeffort.tested("claude"))

    def test_a_damaged_file_fails_loudly_naming_it_and_is_never_read_as_empty(self):
        for text in ("{not json", "[]", json.dumps({"claude": ["2.1.999"]}), json.dumps({"claude": {"x": 1}}),
                     json.dumps({"claude": {"2.1.999": 5}})):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(ValueError) as caught:
                    paneeffort.tested("claude")
                self.assertIn(str(self.path), str(caught.exception))

    def test_add_tested_keeps_what_the_file_holds_and_never_writes_over_a_damaged_one(self):
        paneeffort.add_tested("codex", "0.172.0", "2026-11-23T10:00:00Z")
        paneeffort.add_tested("claude", NEW, "2026-11-23T11:00:00Z")
        self.assertEqual(json.loads(self.path.read_text()), {"codex": {"0.172.0": "2026-11-23T10:00:00Z"},
                                                             "claude": {NEW: "2026-11-23T11:00:00Z"}})
        self.path.write_text("{not json")
        with self.assertRaises(ValueError):
            paneeffort.add_tested("claude", "2.1.998", "2026-11-23T12:00:00Z")
        self.assertEqual(self.path.read_text(), "{not json")

    def test_untested_counts_the_file_and_points_to_the_trial(self):
        sess, pane = {"cli": "claude", "id": "x", "version": NEW}, paneeffort.Pane("%1", os.getpid())
        self.assertEqual(paneeffort.untested(sess, pane),
                         "Claude Code %s isn't on fast mode's tested list yet: `agent-mail fast-trial claude "
                         "--version %s` tries its keys and adds it" % (NEW, NEW))
        paneeffort.add_tested("claude", NEW, "2026-11-23T10:00:00Z")
        self.assertEqual(paneeffort.untested(sess, pane), "")

    def test_untested_says_when_the_list_cannot_be_read(self):
        self.path.write_text("{not json")
        sess = {"cli": "claude", "id": "x", "version": paneeffort.TESTED["claude"][-1]}
        why = paneeffort.untested(sess, paneeffort.Pane("%1", os.getpid()))
        self.assertTrue(why.startswith("fast mode's tested list cannot be read: %s is not JSON" % self.path), why)


class Command(unittest.TestCase):
    """agent-mail fast-trial: arguments, --if-untested, and what a pass or a failure records and prints. The trial
    itself is stubbed: it records the versions it was asked to try, and passes unless told why it fails."""

    def setUp(self):
        self.dir = homes.temp_dir(self)
        homes.patch(self, paneeffort, "TESTED_PATH", self.dir / "fast-tested.json")
        homes.patch(self, fasttrial, "ROOT", self.dir / "fast-trial")
        homes.patch(self, fasttrial, "trial", self.fake_trial)
        homes.patch(self, fasttrial, "installed_version", lambda cli: {"claude": NEW, "codex": "0.172.0"}[cli])
        self.tried, self.fails = [], None
        self.mail = load_script("agent-mail")

    def fake_trial(self, cli, version):
        self.tried.append((cli, version))
        if self.fails:
            raise fasttrial.Failed(self.fails)
        return "effort xhigh -> low -> xhigh"

    def run_cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.mail.main(["fast-trial"] + list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_a_pass_records_the_version_and_prints_one_line(self):
        code, out, _ = self.run_cmd("claude", "--version", NEW)
        self.assertEqual((code, self.tried), (0, [("claude", NEW)]))
        self.assertEqual(out.count("\n"), 1)
        self.assertTrue(out.startswith("fast-trial claude %s: passed (effort xhigh -> low -> xhigh)" % NEW), out)
        self.assertIn(NEW, json.loads(paneeffort.TESTED_PATH.read_text())["claude"])
        self.assertIn(NEW, paneeffort.tested("claude"))

    def test_a_failure_records_nothing_and_exits_1(self):
        self.fails = "no new effort level showed"
        code, out, _ = self.run_cmd("codex", "--version", "0.172.0")
        self.assertEqual(code, 1)
        self.assertEqual(out, "fast-trial codex 0.172.0: FAILED: no new effort level showed\n")
        self.assertFalse(paneeffort.TESTED_PATH.exists())

    def test_without_a_version_the_installed_one_is_tried(self):
        self.assertEqual(self.run_cmd("codex")[0], 0)
        self.assertEqual(self.tried, [("codex", "0.172.0")])

    def test_if_untested_does_nothing_for_a_version_tested_already(self):
        old = paneeffort.TESTED["claude"][0]
        code, out, _ = self.run_cmd("claude", "--version", old, "--if-untested")
        self.assertEqual((code, self.tried), (0, []))
        self.assertEqual(out, "fast-trial claude %s: on fast mode's tested list already; nothing to do\n" % old)
        paneeffort.add_tested("claude", NEW, "2026-11-23T10:00:00Z")
        self.assertEqual(self.run_cmd("claude", "--if-untested")[0], 0)   # the installed one, tested by the trial
        self.assertEqual(self.tried, [])

    def test_if_untested_tries_a_version_not_tested_yet(self):
        self.assertEqual(self.run_cmd("claude", "--if-untested")[0], 0)
        self.assertEqual(self.tried, [("claude", NEW)])

    def test_without_if_untested_a_tested_version_is_tried_again(self):
        old = paneeffort.TESTED["codex"][0]
        self.assertEqual(self.run_cmd("codex", "--version", old)[0], 0)
        self.assertEqual(self.tried, [("codex", old)])

    def test_if_untested_with_a_damaged_list_fails_instead_of_trying(self):
        paneeffort.TESTED_PATH.write_text("{not json")
        code, out, _ = self.run_cmd("claude", "--if-untested")
        self.assertEqual((code, self.tried), (1, []))
        self.assertIn("FAILED: %s is not JSON" % paneeffort.TESTED_PATH, out)

    def test_an_installed_version_that_cannot_be_told_fails(self):
        def unknown(cli):
            raise fasttrial.Failed("no claude is installed")
        homes.patch(self, fasttrial, "installed_version", unknown)
        self.assertEqual(self.run_cmd("claude"), (1, "fast-trial claude: FAILED: no claude is installed\n", ""))

    def test_usage_errors_exit_2_and_try_nothing(self):
        for argv in ([], ["gemini"], ["claude", "--version", "../../etc"], ["claude", "--version", "2.1"],
                     ["claude", "--version", "2.1.330 x"], ["claude", "extra"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cmd(*argv)[0], 2)
        self.assertEqual(self.tried, [])

    def test_another_trial_running_is_a_failure_that_tries_nothing(self):
        lock = fasttrial.lock_path()
        lock.parent.mkdir(parents=True)
        with open(lock, "a+") as fh:   # held as another process's trial holds it: on a file description of its own
            fcntl.flock(fh, fcntl.LOCK_EX)
            code, out, _ = self.run_cmd("claude", "--version", NEW)
        self.assertEqual((code, self.tried), (1, []))
        self.assertIn("another fast-trial is running", out)

    def test_help_documents_the_command(self):
        code, out, _ = self.run_cmd("--help")
        self.assertEqual(code, 0)
        self.assertIn("--if-untested", out)
        self.assertIn("agent-mail fast-trial", self.mail.__doc__)


class Session(unittest.TestCase):
    """trial() around its steps, on the repo's fake tmux (tests/fakes.py), with steps stubbed where they would need a
    real CLI: a session of its own, ended whatever happens; one left by a killed trial ended first; the screens kept on
    a failure; and a SIGTERM to agent-mail fast-trial ending the session too."""

    def setUp(self):
        self.dir = homes.temp_dir(self)
        homes.patch(self, fasttrial, "ROOT", self.dir / "fast-trial")
        homes.patch(self, fasttrial, "CLAUDE_VERSIONS", self.dir / "versions")
        homes.patch(self, paneeffort, "TESTED_PATH", self.dir / "fast-tested.json")
        exe = fasttrial.CLAUDE_VERSIONS / NEW
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        self.bins = fakes.install(self, FAKE_TMUX_SCREEN="$ ", FAKE_TMUX_PANE_PID="1")
        self.name = "fasttrial-claude-%s" % NEW.replace(".", "-")

    def sessions(self):
        """The tmux calls that make or end a session, in order."""
        return [c[:3] for c in self.bins.calls("tmux") if c[0] in ("new-session", "kill-session")]

    def stub(self, steps):
        homes.patch(self, fasttrial, "steps", steps)

    def test_a_pass_ends_its_session(self):
        self.stub(lambda pane, cli, version, home, link: "seen")
        self.assertEqual(fasttrial.trial("claude", NEW), "seen")
        self.assertEqual(self.sessions(), [["new-session", "-d", "-P"], ["kill-session", "-t", "=" + self.name]])
        self.assertFalse((fasttrial.ROOT / "failed").exists())

    def test_a_session_left_by_a_killed_trial_is_ended_first(self):
        self.bins.add_session(self.name)
        self.stub(lambda pane, cli, version, home, link: "seen")
        fasttrial.trial("claude", NEW)
        self.assertEqual(self.sessions(), [["kill-session", "-t", "=" + self.name], ["new-session", "-d", "-P"],
                                           ["kill-session", "-t", "=" + self.name]])

    def test_a_failure_keeps_the_screens_read_names_them_and_ends_the_session(self):
        def fails(pane, cli, version, home, link):
            pane.screen("started")
            raise fasttrial.Failed("no new effort level showed")
        self.stub(fails)
        with self.assertRaises(fasttrial.Failed) as caught:
            fasttrial.trial("claude", NEW)
        kept, = (fasttrial.ROOT / "failed").iterdir()
        self.assertTrue(kept.name.startswith("claude-%s-" % NEW))
        self.assertEqual(str(caught.exception), "no new effort level showed; screens in %s" % kept)
        self.assertEqual(sorted(p.name for p in kept.iterdir()), ["01-started.txt", "02-at-the-failure.txt"])
        self.assertEqual((kept / "01-started.txt").read_text(), "$ \n")
        self.assertEqual(self.sessions()[-1], ["kill-session", "-t", "=" + self.name])

    def test_tmux_failing_mid_trial_is_a_failure_with_its_screens(self):
        def fails(pane, cli, version, home, link):
            pane.screen("started")
            raise commands.CommandError("tmux send-keys failed (exit 1): lost server")
        self.stub(fails)
        with self.assertRaisesRegex(fasttrial.Failed, "^tmux send-keys failed \\(exit 1\\): lost server; screens in "):
            fasttrial.trial("claude", NEW)
        self.assertEqual(self.sessions()[-1], ["kill-session", "-t", "=" + self.name])

    def test_a_shell_that_shows_no_prompt_fails_at_once_saying_so(self):
        fakes.install(self, FAKE_TMUX_SCREEN="", FAKE_TMUX_PANE_PID="1")
        homes.patch(self, fasttrial, "SHOW_SECONDS", 0.2)
        with self.assertRaisesRegex(fasttrial.Failed, "^the pane's shell showed no prompt within 0.2 s; screens in "):
            fasttrial.trial("claude", NEW)

    def test_a_sigterm_to_the_command_ends_the_session_and_the_handler_goes_with_it(self):
        def terminated(pane, cli, version, home, link):
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("SIGTERM did not stop the trial")
        self.stub(terminated)
        before = signal.getsignal(signal.SIGTERM)
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as caught:
            load_script("agent-mail").main(["fast-trial", "claude", "--version", NEW])
        self.assertEqual((caught.exception.code, out.getvalue()), (128 + signal.SIGTERM, ""))
        self.assertEqual(self.sessions()[-1], ["kill-session", "-t", "=" + self.name])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        self.assertFalse(paneeffort.TESTED_PATH.exists())


class Pieces(unittest.TestCase):
    """The parts of the trial that need no tmux: the versions' binaries, the installed version, the seeded config,
    the session's name, and the line that starts the CLI."""

    def setUp(self):
        self.dir = homes.temp_dir(self)
        homes.patch(self, fasttrial, "CLAUDE_VERSIONS", self.dir / "claude" / "versions")
        homes.patch(self, fasttrial, "CODEX_RELEASES", self.dir / "codex" / "releases")

    def install(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def test_each_version_s_binary_in_its_cli_s_own_install_layout(self):
        claude = self.install(fasttrial.CLAUDE_VERSIONS / "2.1.330")
        codex = self.install(fasttrial.CODEX_RELEASES / "0.171.0-x86_64-unknown-linux-musl" / "bin" / "codex")
        self.assertEqual(fasttrial.binary("claude", "2.1.330"), claude)
        self.assertEqual(fasttrial.binary("codex", "0.171.0"), codex)
        for cli, version in (("claude", "2.1.331"), ("codex", "0.172.0")):
            with self.subTest(cli=cli), self.assertRaisesRegex(fasttrial.Failed, "is not installed here"):
                fasttrial.binary(cli, version)

    def test_the_installed_version_is_read_from_the_path_the_cli_s_link_leads_to(self):
        claude = self.install(fasttrial.CLAUDE_VERSIONS / "2.1.330")
        codex = self.install(fasttrial.CODEX_RELEASES / "0.171.0-x86_64-unknown-linux-musl" / "bin" / "codex")
        current = self.dir / "codex" / "current"
        current.symlink_to(codex.parent.parent)
        links = self.dir / "bin"
        links.mkdir()
        (links / "claude").symlink_to(claude)
        (links / "codex").symlink_to(current / "bin" / "codex")
        homes.patch(self, fasttrial.registry, "find_bin", lambda name, extra: str(links / name))
        self.assertEqual(fasttrial.installed_version("claude"), "2.1.330")
        self.assertEqual(fasttrial.installed_version("codex"), "0.171.0")
        elsewhere = self.install(self.dir / "npm" / "claude")
        homes.patch(self, fasttrial.registry, "find_bin", lambda name, extra: str(elsewhere))
        with self.assertRaisesRegex(fasttrial.Failed, "pass --version"):
            fasttrial.installed_version("claude")
        homes.patch(self, fasttrial.registry, "find_bin", lambda name, extra: None)
        with self.assertRaisesRegex(fasttrial.Failed, "no claude is installed"):
            fasttrial.installed_version("claude")

    def test_the_seed_for_claude_code_answers_onboarding_the_key_question_and_trust_keeping_the_rest(self):
        home, work = self.dir / "home", self.dir / "work"
        home.mkdir()
        (home / ".claude.json").write_text(json.dumps({"numStartups": 4, "projects": {"/elsewhere": {"x": 1}}}))
        fasttrial.seed("claude", home, work)
        fasttrial.seed("claude", home, work)   # each run: the same, never a second entry
        d = json.loads((home / ".claude.json").read_text())
        self.assertEqual(d["numStartups"], 4)
        self.assertIs(d["hasCompletedOnboarding"], True)
        self.assertEqual(d["customApiKeyResponses"]["approved"], [fasttrial.PLACEHOLDER_KEY[-20:]])
        self.assertIs(d["projects"][str(work)]["hasTrustDialogAccepted"], True)
        # a CLAUDE.md above the folder (such as ~/CLAUDE.md, over the state folder) importing files from outside it:
        # answered no, as one who was asked, so that no dialog asks and no such file is read
        self.assertIs(d["projects"][str(work)]["hasClaudeMdExternalIncludesApproved"], False)
        self.assertIs(d["projects"][str(work)]["hasClaudeMdExternalIncludesWarningShown"], True)
        self.assertEqual(d["projects"]["/elsewhere"], {"x": 1})
        self.assertEqual(stat.S_IMODE((home / ".claude.json").stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in home.iterdir()), [".claude.json"])

    def test_the_seed_for_codex_signs_in_with_the_placeholder_key_alone(self):
        home = self.dir / "home"
        fasttrial.seed("codex", home, self.dir / "work")
        self.assertEqual(json.loads((home / "auth.json").read_text()),
                         {"auth_mode": "apikey", "OPENAI_API_KEY": fasttrial.PLACEHOLDER_KEY})
        self.assertEqual(stat.S_IMODE((home / "auth.json").stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in home.iterdir()), ["auth.json"])

    def test_a_damaged_claude_json_stops_the_seed(self):
        home = self.dir / "home"
        home.mkdir()
        (home / ".claude.json").write_text("{not json")
        with self.assertRaises(ValueError):
            fasttrial.seed("claude", home, self.dir / "work")

    def test_the_session_name_has_no_dot_which_tmux_takes_for_a_window(self):
        self.assertEqual(fasttrial.session_name("claude", "2.1.330"), "fasttrial-claude-2-1-330")
        self.assertEqual(fasttrial.session_name("codex", "0.171.0"), "fasttrial-codex-0-171-0")

    def test_the_launch_line_gives_the_cli_nothing_but_the_placeholder_key_and_its_isolated_config(self):
        home, link = self.dir / "my home", self.dir / "bin" / "claude"
        homes.config_file(self, {"spawn_claude_model": "claude-opus-5-5", "spawn_codex_model": None})
        for cli, var, home_var, args in (
                ("claude", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR",
                 ["--model", "claude-opus-5-5", "--effort", "xhigh"]),
                ("codex", "OPENAI_API_KEY", "CODEX_HOME", ["-c", "check_for_update_on_startup=false", "-c",
                                                           "model_reasoning_effort=xhigh"])):
            with self.subTest(cli=cli):
                words = shlex.split(fasttrial.launch_line(cli, link, home))
                self.assertEqual(words[:2], ["env", "-i"])   # nothing of this process's environment
                env = dict(w.split("=", 1) for w in words[2:] if "=" in w and not w.startswith("-"))
                self.assertEqual(env[var], fasttrial.PLACEHOLDER_KEY)
                self.assertEqual(env[home_var], str(home))
                self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
                self.assertEqual((env["TMUX"], env["TMUX_PANE"]), ("$TMUX", "$TMUX_PANE"))   # the pane's own
                self.assertFalse({k for k in env if k.endswith("_KEY") or k.endswith("TOKEN")} - {var})
                start = words.index(str(link))
                self.assertEqual(words[start + 1:], args)


if __name__ == "__main__":
    unittest.main()
