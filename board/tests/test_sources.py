"""The shared helpers in agentboard/sources.py that every tool uses."""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
from agentboard import sources  # noqa: E402


class RunTest(unittest.TestCase):
    def test_output_and_exit_code(self):
        self.assertEqual(sources.run(["sh", "-c", "echo out; echo err >&2; exit 3"]), (3, "out\n", "err\n"))

    def test_input_goes_to_stdin(self):
        self.assertEqual(sources.run(["cat"], input="hello"), (0, "hello", ""))

    def test_a_command_runs_in_the_folder_given(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sources.run(["pwd"], cwd=d), (0, os.path.realpath(d) + "\n", ""))

    def test_a_command_that_cannot_start_gives_127(self):
        code, out, err = sources.run(["/nonexistent/agent-board-test"])
        self.assertEqual((code, out), (127, ""))
        self.assertIn("No such file", err)

    def test_a_command_over_its_timeout_is_stopped_with_124(self):
        self.assertEqual(sources.run(["sleep", "5"], timeout=0.2), (124, "", "timed out after 0.2s"))

    def test_run_merged_joins_and_strips_both_streams(self):
        self.assertEqual(sources.run_merged(["sh", "-c", "echo out; echo err >&2; exit 2"]), (2, "out\nerr"))
        self.assertEqual(sources.run_merged(["cat"], stdin="x\n"), (0, "x"))
        self.assertEqual(sources.run_merged(["/nonexistent/agent-board-test"])[0], 127)


class SmallHelpersTest(unittest.TestCase):
    def test_left_pct_prefers_remaining_then_used(self):
        self.assertEqual(sources.left_pct({"remaining_pct": 30, "used_pct": 90}), 30.0)
        self.assertEqual(sources.left_pct({"used_pct": 90}), 10.0)
        self.assertEqual(sources.left_pct({}), 100.0)

    def test_usage_target(self):
        self.assertEqual(sources.usage_target({"cli": "codex", "account": "work"}), "codex")
        for acct in ("", "default", None):              # the kit mailroom's default login is named "default"
            self.assertEqual(sources.usage_target({"cli": "claude", "account": acct}), "claude:default")
        self.assertEqual(sources.usage_target({"cli": "claude", "account": "work"}), "claude:work")

    def test_agent_send_argv(self):
        saved = sources.find_bin
        try:
            sources.find_bin = lambda name: "/bin/" + name          # where agent-send is depends on the machine
            self.assertEqual(sources.agent_send_argv("codex", "abc"), ["/bin/agent-send", "--raw", "--cli", "codex", "abc"])
            sources.find_bin = lambda name: None
            with self.assertRaisesRegex(RuntimeError, "agent-send is not installed"):
                sources.agent_send_argv("codex", "abc")
        finally:
            sources.find_bin = saved

    def test_agent_usage(self):
        calls = []

        def run(cmd, timeout):          # agent-usage asks the providers over the network: it answers here
            calls.append(cmd)
            return 0, json.dumps({"accounts": [{"target": "codex"}]}), ""
        saved = sources.find_bin
        try:
            sources.find_bin = lambda name: "/bin/" + name          # where agent-usage is depends on the machine
            self.assertEqual(sources.read_usage(run), [{"target": "codex"}])
            sources.agent_usage(True, run)
            self.assertEqual(calls, [["/bin/agent-usage", "--json"], ["/bin/agent-usage", "--json", "--refresh"]])
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                sources.agent_usage(False, lambda cmd, timeout: (1, "", "probe failed"))
            sources.find_bin = lambda name: None
            self.assertIsNone(sources.agent_usage(False, run))
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                sources.read_usage(run)
        finally:
            sources.find_bin = saved

    def test_detached_argv(self):
        argv = sources.detached_argv("u1", ["python3", "x", "worker"], {"A": "1"}, ["TimeoutStopSec=120"])
        self.assertEqual(argv, ["systemd-run", "--user", "--collect", "--unit", "u1", "--setenv=PATH=" + sources.LOGIN_PATH,
                                "--setenv=A=1", "--property=TimeoutStopSec=120", "python3", "x", "worker"])

    def test_transcript_tail_reads_only_the_end(self):
        path = Path(tempfile.mkdtemp()) / "t.jsonl"
        path.write_text("first line\n" + "x" * 50 + "\nlast line\n")
        self.assertEqual(sources.transcript_tail(path, 10_000), ["first line", "x" * 50, "last line"])
        self.assertEqual(sources.transcript_tail(path, 12), ["x", "last line"])

    @unittest.skipUnless(os.path.exists("/proc/stat"), "needs /proc")
    def test_boot_time_is_in_the_past(self):
        import time
        self.assertLess(sources.boot_time(), time.time())


class TmuxTest(unittest.TestCase):
    """tmux_list_panes runs tmux, whose real server holds the machine's agents; a stand-in script answers instead."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.saved = sources.find_bin
        sources.find_bin = lambda name: str(self.d / name) if (self.d / name).exists() else None

    def tearDown(self):
        sources.find_bin = self.saved

    def stand_in(self, out, err, code):
        exe = self.d / "tmux"
        exe.write_text("#!/bin/sh\nprintf '%s'\nprintf '%s' >&2\nexit %d\n" % (out, err, code))
        exe.chmod(0o755)

    def test_rows_are_parsed_and_odd_lines_skipped(self):
        self.stand_in("123\\tnova\\t/home/x\\nnot a row\\n456\\tbeta\\t/tmp\\n", "", 0)
        self.assertEqual(sources.tmux_list_panes(), [(123, "nova", "/home/x"), (456, "beta", "/tmp")])

    def test_no_tmux_or_no_server_is_no_panes(self):
        self.assertEqual(sources.tmux_list_panes(), [])                       # tmux not installed
        self.stand_in("", "no server running on /tmp/tmux-1000/default\\n", 1)
        self.assertEqual(sources.tmux_list_panes(), [])

    def test_any_other_failure_raises(self):
        self.stand_in("", "lost server\\n", 1)
        with self.assertRaisesRegex(RuntimeError, "tmux list-panes exit 1: lost server"):
            sources.tmux_list_panes()


class FileHelpersTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_append_log_stamps_each_line_and_makes_its_folder(self):
        log = self.d / "new" / "run.log"
        line = sources.append_log(log, "checked: nothing to do")
        self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d checked: nothing to do$")
        sources.append_log(log, "second")
        self.assertEqual(log.read_text().splitlines()[0], line)
        self.assertTrue(log.read_text().endswith(" second\n"))

    def test_append_jsonl_writes_private_whole_lines_that_read_back(self):
        path = self.d / "sub" / "log.jsonl"
        sources.append_jsonl(path, {"text": "café ✓", "n": 1})
        sources.append_jsonl(path, {"n": 2, "at": Path("/x")})          # anything else is written as its str()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("café ✓", path.read_text(encoding="utf-8"))
        self.assertEqual(sources.read_jsonl(path), [{"text": "café ✓", "n": 1}, {"n": 2, "at": "/x"}])

    def test_read_jsonl_is_strict_except_for_a_line_still_being_written(self):
        path = self.d / "x.jsonl"
        self.assertEqual(sources.read_jsonl(path), [])                    # no file yet
        path.write_text('{"a": 1}\n\n[2]\n{"a": ')                       # blank lines skip; the last is half written
        self.assertEqual(sources.read_jsonl(path), [{"a": 1}, [2]])
        path.write_text('{"a": 1}\nnot json\n{"a": 3}\n')
        with self.assertRaises(ValueError) as e:
            sources.read_jsonl(path)
        self.assertIn("x.jsonl line 2 is not JSON", str(e.exception))
        path.unlink()
        path.mkdir()
        with self.assertRaises(OSError):                                    # unreadable is not "empty"
            sources.read_jsonl(path)

    def test_read_json_gives_the_default_only_for_a_missing_file(self):
        path = self.d / "state.json"
        self.assertEqual(sources.read_json(path, {"phase": "idle"}), {"phase": "idle"})     # no file yet
        path.write_text('{"a": 1}')
        self.assertEqual(sources.read_json(path, {}), {"a": 1})
        path.write_text("{half a file")
        with self.assertRaisesRegex(ValueError, "state.json is not valid JSON"):
            sources.read_json(path, {})
        path.unlink()
        path.mkdir()
        with self.assertRaises(OSError):                                    # unreadable is not "missing"
            sources.read_json(path, {})

    def test_read_cache_reports_a_damaged_cache_and_starts_it_afresh(self):
        path, said = self.d / "cache.json", []
        path.write_text("{half a file")
        self.assertEqual(sources.read_cache(path, {}, said.append), {})
        self.assertEqual(len(said), 1)
        self.assertIn("cache.json is not valid JSON", said[0])
        path.write_text('{"a": 1}')
        self.assertEqual(sources.read_cache(path, {}, said.append), {"a": 1})
        self.assertEqual(len(said), 1)                                      # a good cache says nothing

    def test_append_jsonl_unless_checks_under_the_lock(self):
        path = self.d / "marks.jsonl"
        once = lambda rows: any(r.get("ref") == "a" for r in rows)
        self.assertTrue(sources.append_jsonl(path, {"ref": "a", "n": 1}, unless=once))
        self.assertFalse(sources.append_jsonl(path, {"ref": "a", "n": 2}, unless=once))
        self.assertEqual(sources.read_jsonl(path), [{"ref": "a", "n": 1}])

    def test_each_window_gets_its_key_and_resets_name_one_window_through_jitter(self):
        accounts = [{"target": "claude:default", "windows": [{"kind": "weekly"}, {"kind": "model_weekly", "model": "Fable"}]},
                    {"target": "codex"}]
        sources.mark_window_keys(accounts)
        self.assertEqual([w["key"] for w in accounts[0]["windows"]], ["weekly", "model:fable"])
        self.assertTrue(sources.same_reset(1000.2, 1000.9) and sources.same_reset(None, None))
        self.assertFalse(sources.same_reset(1000, 1000 + 7200) or sources.same_reset(1000, None))

    def test_read_appended_reads_on_and_leaves_a_line_still_being_written(self):
        path, cache, said = self.d / "log.jsonl", self.d / "cache.json", []

        def parse(lines, state, p):                       # keeps every line it is given
            return (state or []) + [json.loads(line)["n"] for line in lines]

        path.write_text('{"n": 1}\n{"n": 2}\n{"n": ')
        self.assertEqual(sources.read_appended([(str(path), parse)], 0, cache, said.append), {str(path): [1, 2]})
        with open(path, "a") as fh:
            fh.write('3}\n')
        self.assertEqual(sources.read_appended([(str(path), parse)], 0, cache, said.append), {str(path): [1, 2, 3]})
        self.assertEqual(sources.read_appended([(str(path), parse)], 9e12, cache, said.append), {})  # not modified since
        self.assertEqual((json.loads(cache.read_text()), said), ({}, []))
        empty = self.d / "empty.jsonl"
        empty.write_text("")
        self.assertEqual(sources.read_appended([(str(empty), lambda lines, s, p: None)], 0, cache, said.append), {})

    def test_file_lock_makes_a_second_holder_wait_for_the_first(self):
        lock, order = self.d / "new" / "x.lock", []
        held, go = threading.Event(), threading.Event()

        def first():
            with sources.file_lock(lock):
                held.set()
                go.wait(5)
                order.append("first")

        def second():
            with sources.file_lock(lock):
                order.append("second")

        t = threading.Thread(target=first)
        t.start()
        held.wait(5)
        waiter = threading.Thread(target=second)
        waiter.start()
        waiter.join(0.3)
        self.assertTrue(waiter.is_alive())                   # blocked while the first holds it
        go.set()
        t.join(5)
        waiter.join(5)
        self.assertEqual(order, ["first", "second"])

    def test_write_text_atomic_keeps_a_files_mode_and_makes_new_ones_private(self):
        new, old = self.d / "new.txt", self.d / "old.txt"
        sources.write_text_atomic(new, "a")
        self.assertEqual((new.read_text(), stat.S_IMODE(new.stat().st_mode)), ("a", 0o600))
        old.write_text("x")
        os.chmod(old, 0o644)
        sources.write_text_atomic(old, "b")
        self.assertEqual((old.read_text(), stat.S_IMODE(old.stat().st_mode)), ("b", 0o644))
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), ["new.txt", "old.txt"])     # no temporary left


class MailConfigTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.saved = sources.MAIL_CONFIG
        sources.MAIL_CONFIG = self.d / "config.json"

    def tearDown(self):
        sources.MAIL_CONFIG = self.saved

    def test_values_come_from_the_mailroom_config(self):
        sources.MAIL_CONFIG.write_text(json.dumps({"allowlist": ["Owner@Example.com", "other@example.com"],
                                                   "account": "Agents@Example.com", "host_id": "laptop"}))
        self.assertEqual(sources.owner_addresses(), ["owner@example.com", "other@example.com"])
        self.assertEqual(sources.owner(), "owner@example.com")      # no owner_email: the first allowlisted sender
        sources.MAIL_CONFIG.write_text(json.dumps({"owner_email": "Alex@Example.org", "allowlist": ["owner@example.com"]}))
        self.assertEqual(sources.owner_addresses(), ["alex@example.org", "owner@example.com"])
        self.assertEqual(sources.owner(), "alex@example.org")
        sources.MAIL_CONFIG.write_text(json.dumps({"allowlist": ["Owner@Example.com", "other@example.com"],
                                                   "account": "Agents@Example.com", "host_id": "laptop"}))
        self.assertEqual(sources.mail_account(), "agents@example.com")
        self.assertEqual(sources.mail_host(), "laptop")
        self.assertEqual(sources.MailStore(self.d / "none.sqlite").host_id, "laptop")
        self.assertEqual(sources.MailStore(self.d / "none.sqlite", host_id="desktop").host_id, "desktop")

    def test_without_a_config_the_defaults_hold(self):
        self.assertEqual(sources.mail_config(), {})
        self.assertIsNone(sources.owner())                  # no address is assumed
        self.assertEqual(sources.owner_addresses(), [])
        self.assertEqual((sources.mail_account(), sources.mail_host()), ("", "desktop"))

    def test_a_damaged_config_raises_rather_than_becoming_the_defaults(self):
        for text in ("[1, 2]", "{not json"):
            sources.MAIL_CONFIG.write_text(text)
            with self.assertRaises(ValueError):
                sources.owner()


    def test_login_labels_drop_the_part_in_brackets(self):
        sources.MAIL_CONFIG.write_text(json.dumps({"usage": {"claude_accounts": {
            "default": {"config_dir": None, "label": "Claude (Max plan; the default login, ~/.claude)"},
            "work": {"config_dir": "~/.claude-work", "label": "Work seat (~/.claude-work)"},
            "bare": {"config_dir": "~/.claude-bare"}}}}))
        self.assertEqual(sources.login_labels(), {"default": "Claude", "work": "Work seat"})


class SharedLoginTest(unittest.TestCase):
    """Two Claude logins signed in to one account spend one allowance (mark_shared_logins)."""

    def login(self, name, account, org="org-1"):
        home = self.d / name
        home.mkdir()
        if account is not None:
            (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": account,
                                                                           "organizationUuid": org}}))
        return home

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_a_later_login_on_an_earlier_ones_account_is_marked(self):
        logins = [("default", self.login("one", "acct-1")), ("work", self.login("two", "acct-1")),
                  ("team", self.login("three", "acct-1", org="org-2")), ("out", self.login("four", None))]
        accounts = [{"target": t} for t in ("claude:default", "claude:work", "claude:team", "claude:out", "codex")]
        sources.mark_shared_logins(accounts, logins)
        self.assertEqual(accounts[1], {"target": "claude:work", "same_account_as": ["claude:default"],
                                       "login_dir": str(self.d / "two")})
        self.assertEqual([a for a in accounts if "same_account_as" in a], [accounts[1]])  # another org is another pool

    def test_the_default_login_is_never_the_one_marked_and_a_home_folder_is_written_with_a_tilde(self):
        saved = sources.HOME, sources.DEFAULT_CLAUDE_HOME
        sources.HOME, sources.DEFAULT_CLAUDE_HOME = self.d, self.d / ".claude"
        try:
            (self.d / ".claude").mkdir()
            (self.d / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-1",
                                                                              "organizationUuid": "org-1"}}))
            work = self.login(".claude-work", "acct-1")
            accounts = [{"target": "claude:work"}, {"target": "claude:default"}]
            sources.mark_shared_logins(accounts, [("work", work), ("default", self.d / ".claude")])  # default later
            self.assertEqual(accounts, [{"target": "claude:work", "same_account_as": ["claude:default"],
                                         "login_dir": "~/.claude-work"}, {"target": "claude:default"}])
        finally:
            sources.HOME, sources.DEFAULT_CLAUDE_HOME = saved

    def test_a_damaged_login_config_raises(self):
        home = self.login("one", None)
        (home / ".claude.json").write_text("{half")
        with self.assertRaises(ValueError):
            sources.mark_shared_logins([{"target": "claude:default"}], [("default", home)])
        self.assertEqual(sources.login_config(sources.DEFAULT_CLAUDE_HOME), sources.HOME / ".claude.json")


class MailOwnerTest(unittest.TestCase):
    """mail_owner runs agent-mail, which would send real email; a stand-in script records what it was given."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.saved = (sources.find_bin, sources.MAIL_CONFIG)
        sources.MAIL_CONFIG = self.d / "config.json"
        sources.MAIL_CONFIG.write_text(json.dumps({"allowlist": ["owner@example.com"]}))

    def tearDown(self):
        sources.find_bin, sources.MAIL_CONFIG = self.saved

    def stand_in(self, code):
        exe = self.d / "agent-mail"
        exe.write_text('#!/bin/sh\nprintf "%%s\\n" "$@" > "%s/argv"; cat > "%s/stdin"; echo refused >&2; exit %d\n'
                       % (self.d, self.d, code))
        exe.chmod(0o755)
        sources.find_bin = lambda name: str(exe) if name == "agent-mail" else None

    def test_it_sends_unrouted_to_the_owner_with_the_body_on_stdin(self):
        self.stand_in(0)
        self.assertEqual(sources.mail_owner("Desktop is back", "All 3 agents restored."), (True, ""))
        self.assertEqual((self.d / "argv").read_text().split(),
                         ["send", "--unrouted", "--to", "owner@example.com", "--subject", "Desktop", "is", "back"])
        self.assertEqual((self.d / "stdin").read_text(), "All 3 agents restored.\n")

    def test_a_failure_says_why(self):
        self.stand_in(3)
        self.assertEqual(sources.mail_owner("s", "b"), (False, "agent-mail exit 3: refused"))
        sources.find_bin = lambda name: None
        self.assertEqual(sources.mail_owner("s", "b"), (False, "agent-mail is not installed"))

    def test_without_an_owner_address_nothing_is_sent(self):
        self.stand_in(0)
        sources.MAIL_CONFIG.write_text("{}")
        self.assertEqual(sources.mail_owner("s", "b"), (False, "no owner address in the mailroom config (owner_email)"))
        self.assertFalse((self.d / "argv").exists())


if __name__ == "__main__":
    unittest.main()
