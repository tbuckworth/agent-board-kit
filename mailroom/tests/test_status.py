import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import config, deliver, jsonfile, launch, registry, report, statuslog, trust, worktree  # noqa: E402


class StatusLog(unittest.TestCase):
    def setUp(self):
        homes.status_log(self)

    def test_append_read_filter(self):
        statuslog.append({"cli": "claude", "session_id": "s1", "name": "a", "cwd": "/x", "event": "idle"})
        statuslog.append({"cli": "codex", "session_id": "s2", "name": "b", "cwd": "/y", "event": "note", "text": "hi"})
        self.assertEqual(len(statuslog.read()), 2)
        self.assertEqual(statuslog.read(event="note")[0]["text"], "hi")
        self.assertEqual(statuslog.read(session_id="s1")[0]["cli"], "claude")
        self.assertEqual(statuslog.read(since=time.time() + 10), [])
        self.assertEqual(oct(os.stat(statuslog.LOG).st_mode & 0o777), "0o600")

    def test_a_damaged_line_is_skipped_and_counted(self):
        statuslog.append({"cli": "claude", "session_id": "s1", "event": "idle"})
        with open(statuslog.LOG, "a") as fh:
            fh.write('{"cli": "claude", "event": \n')                  # a hook killed mid-write
        statuslog.append({"cli": "claude", "session_id": "s1", "event": "quota"})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual([e["event"] for e in statuslog.read()], ["idle", "quota"])
        self.assertEqual(err.getvalue(), "%s: skipped 1 line(s) that are not JSON objects\n" % statuslog.LOG)

    def test_a_report_whose_store_cannot_be_read_fails(self):
        db = Path(tempfile.mkdtemp()) / "mailroom.sqlite"
        db.write_bytes(b"not a database" * 100)
        homes.Homes(self)                                                   # no sessions
        with mock.patch("agentmail.config.DB_PATH", db), self.assertRaisesRegex(Exception, "file is not a database"):
            report.gather()

    def test_parse_since(self):
        now = 1000000.0
        self.assertAlmostEqual(statuslog.parse_since("2h", now), now - 7200)
        self.assertAlmostEqual(statuslog.parse_since("3d", now), now - 3 * 86400)
        self.assertIsNone(statuslog.parse_since(None))
        with self.assertRaises(ValueError):
            statuslog.parse_since("soon")

    def test_git_facts(self):
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"], check=True)
        f = statuslog.git_facts(d)
        self.assertEqual(f["branch"], "main")
        self.assertEqual(f["dirty"], 0)
        self.assertIn("first", f["last_commit"])
        self.assertEqual(statuslog.git_facts("/nonexistent")["branch"], "")
        # git status failing is not "no changes": the count is unknown, and the report says so
        Path(d, ".git", "index").write_bytes(b"not an index")
        f = statuslog.git_facts(d)
        self.assertEqual((f["branch"], f["dirty"]), ("main", None))
        row = {"cli": "claude", "name": "a", "id": "s1", "cwd": d, "live": True, "status": "idle", "repo": "r",
               "branch": "main", "dirty": None, "last_commit": "", "turns": 0, "last_idle": None,
               "last_activity": 0, "notes": [], "title": ""}
        rep = {"host": "h", "generated": time.time(), "since_seconds": 3600, "rows": [row], "held_mail": 0,
               "unattributed_notes": [], "live": 1, "waiting": 0, "quota_events": 0, "hook_errors": 0}
        self.assertIn("changes unknown", report.render_text(rep))
        self.assertIn("| r @ main | ? |", report.render_markdown(rep))
        self.assertIn("tabular-nums\">?</span>", report.render_html(rep))


class Report(unittest.TestCase):
    def setUp(self):
        homes.status_log(self)
        h = homes.Homes(self)
        h.claude("s1", "worker-a", cwd="/x", updated_at=time.time(), status="idle", kind="interactive")
        h.codex("s9", title="old", live=False, cwd="/z")
        statuslog.append({"cli": "claude", "session_id": "s1", "name": "worker-a", "cwd": "/x", "event": "idle"})
        statuslog.append({"cli": "claude", "session_id": "s1", "name": "worker-a", "cwd": "/x", "event": "note", "text": "tests pass"})
        statuslog.append({"cli": "codex", "session_id": "s3", "name": "gone", "cwd": "/g", "event": "idle"})

    def test_gather_and_render(self):
        rep = report.gather(since_seconds=3600)
        names = [r["name"] for r in rep["rows"]]
        self.assertIn("worker-a", names)          # live
        self.assertIn("gone", names)              # exited but had events
        self.assertNotIn("", names)               # stale codex thread with no events is left out
        a = next(r for r in rep["rows"] if r["name"] == "worker-a")
        self.assertEqual(a["turns"], 1)
        self.assertEqual(a["notes"][0]["text"], "tests pass")
        for fn in (report.render_text, report.render_markdown, report.render_html):
            out = fn(rep)
            self.assertIn("worker-a", out)
            self.assertIn("tests pass", out)
        self.assertIn("narr", report.render_html(rep, "narr"))

    def test_a_failing_hook_prints_nothing_and_its_error_is_counted_in_the_report(self):
        """The hook must not disturb the agent (a SessionStart hook's stdout goes into its context), yet its failure
        must show: the mailroom's quota alerts depend on the events it writes."""
        st = load_script("agent-status")
        errors = Path(tempfile.mkdtemp()) / "hook-errors.jsonl"
        payload = json.dumps({"hook_event_name": "Notification", "notification_type": "quota_auto_resume_fired",
                              "session_id": "s1", "cwd": "/x"})
        with mock.patch.object(statuslog, "HOOK_ERRORS", errors), \
                mock.patch.object(statuslog, "identity", side_effect=RuntimeError("registry broke")), \
                mock.patch.object(st.sys, "stdin", io.StringIO(payload)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(st.cmd_hook(mock.Mock(cli="claude")), 0)
            rep = report.gather(since_seconds=3600)
        self.assertEqual(out.getvalue(), "")
        [e] = jsonfile.read_lines(errors)
        self.assertEqual((e["cli"], e["hook"], e["error"]), ("claude", "Notification", "RuntimeError: registry broke"))
        self.assertEqual(rep["hook_errors"], 1)
        for fn in (report.render_text, report.render_markdown, report.render_html):
            self.assertIn("hook errors 1", fn(rep).replace("<b>", "").replace("</b>", ""))

    def test_the_hook_error_file_stays_small(self):
        errors = Path(tempfile.mkdtemp()) / "hook-errors.jsonl"
        with mock.patch.object(statuslog, "HOOK_ERRORS", errors), mock.patch.object(statuslog, "HOOK_ERRORS_BYTES", 300):
            for i in range(20):
                statuslog.record_hook_error("claude", "Stop", "OSError: disk full %d" % i)
        self.assertLess(errors.stat().st_size, 300 + 200)                 # rotated once past the cap
        self.assertEqual(jsonfile.read_lines(errors)[-1]["error"], "OSError: disk full 19")
        self.assertTrue(Path(str(errors) + ".1").exists())


class StaleEntry(unittest.TestCase):
    """A claude SIGKILLed by a login move leaves its registry entry. Read after the live one (personal, then work),
    it must not show the agent as exited in the report, nor lend it its old name."""
    SID = "c0c0c0c0-0000-4000-8000-000000000003"

    def setUp(self):
        homes.status_log(self)
        h = homes.Homes(self, logins=("personal", "work"))
        h.claude(self.SID, "renamed", status="busy", updated_at=1)
        h.claude(self.SID, "old-name", login="work", live=False, status="idle", updated_at=2,
                 messagingSocketPath=str(h.socket))                     # SIGKILLed: its socket file stays

    def test_report_and_identity_use_the_live_entry(self):
        self.assertEqual([(s["account"], s["live"]) for s in registry.claude_sessions()], [("personal", True)])
        statuslog.append({"cli": "claude", "session_id": self.SID, "name": "renamed", "cwd": "/w", "event": "idle"})
        row = next(r for r in report.gather(since_seconds=3600)["rows"] if r["id"] == self.SID)
        self.assertEqual((row["live"], row["status"], row["name"]), (True, "busy", "renamed"))
        self.assertEqual(statuslog.identity(cli="claude", session_id=self.SID)["name"], "renamed")


class Spawn(unittest.TestCase):
    def test_helpers(self):
        sp = load_script("agent-spawn")
        self.assertEqual(sp.slug("Fix: the thing!"), "Fix-the-thing")
        self.assertEqual(sp.tmux_name("feat/a.b:c"), "feat/a-b-c")
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"], check=True)
        base, warning = worktree.fetch_base(d, sp.BASES, 1, 30, 0)   # no origin: the fetch fails
        self.assertEqual(base, "main")
        self.assertIn("git fetch origin failed 1 time (last: ", warning)
        self.assertEqual(worktree.fetch_base(d, ("origin/dev",), 1, 30, 0)[0], "origin/dev")
        pre = launch.preamble(config.load(), "n", "/p", "b", "codex", "")
        self.assertIn("--session n", pre)
        self.assertIn("agent-close --self", pre)

    def test_dry_run_dir(self):
        d = tempfile.mkdtemp()
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "agent-spawn"), "--dir", d, "--brief-text", "x", "--dry-run"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"mode": "dir"', r.stdout)

    def spawn_plan(self, *args, conf=None):
        cdir = tempfile.mkdtemp()
        if conf is not None:
            Path(cdir, "config.json").write_text(json.dumps(conf))
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "agent-spawn"), "--dir", tempfile.mkdtemp(),
                            "--brief-text", "x", "--dry-run"] + list(args),
                           capture_output=True, text=True, env=dict(os.environ, AGENT_MAIL_CONFIG_DIR=cdir))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_default_models(self):
        p = self.spawn_plan()
        self.assertEqual((p["cli"], p["model"], p["effort"]), ("claude", None, "xhigh"))    # Claude Code's own model
        p = self.spawn_plan(conf={"spawn_claude_model": "claude-opus-5-5"})
        self.assertEqual((p["cli"], p["model"], p["effort"]), ("claude", "claude-opus-5-5", "xhigh"))
        self.assertEqual(self.spawn_plan("--model", "fable")["model"], "fable")   # another model when asked for
        p = self.spawn_plan("--cli", "codex")
        self.assertEqual((p["model"], p["effort"]), ("gpt-6.1-sol", "xhigh"))
        p = self.spawn_plan("--cli", "codex", "--model", "gpt-6-astra", "--effort", "medium")
        self.assertEqual((p["model"], p["effort"]), ("gpt-6-astra", "medium"))
        self.assertEqual(self.spawn_plan("--effort", "max")["effort"], "max")
        p = self.spawn_plan("--cli", "codex", conf={"spawn_codex_model": "gpt-6-astra", "spawn_codex_effort": None})
        self.assertEqual((p["model"], p["effort"]), ("gpt-6-astra", None))

    def test_launch_passes_model_and_effort(self):
        # tmux is the boundary: the line is typed into no pane
        with mock.patch.object(launch.commands, "tmux"), mock.patch.object(launch.time, "sleep"):
            line = launch.start_claude("=n:", "claude", "n", None, "claude-opus-5-5", "xhigh", "/b")
            self.assertIn("--model claude-opus-5-5 --effort xhigh", line)
            self.assertNotIn("--model", launch.start_claude("=n:", "claude", "n", None, None, None, "/b"))

    def run_spawn(self, *args, where=None, sessions=(), agent_send=(0, "sent to spawn-test via inbox socket"),
                  live_ids=(), env=None, claude_json=None):
        """Run agent-spawn's main here, as agent "spawn-test" in where (default: --dir, a new folder), with the fake
        tmux, codex and claude (tests/fakes.py) and these tmux sessions already there, a fake agent-send (it would
        deliver into a live session) answering agent_send (exit code, output), and Codex's live threads (read from
        its lock files) answering live_ids, one set per look, the last one from then on; env: settings for the
        fakes. Trust files (claude_json: the text the login's .claude.json starts with), briefs and the status log
        are temp files. Returns (exit code, stdout, stderr, the fakes)."""
        bins = fakes.install(self, **(env or {}))
        for name in sessions:
            bins.add_session(name)
        sp = load_script("agent-spawn")
        home = Path(tempfile.mkdtemp())
        if claude_json is not None:
            (home / ".claude.json").write_text(claude_json)
        sp.AGENT_SEND = home / "agent-send"
        sp.AGENT_SEND.write_text("#!/bin/sh\necho '%s'\nexit %d\n" % (agent_send[1], agent_send[0]))
        sp.AGENT_SEND.chmod(0o755)
        homes.status_log(self)
        ids = [set(x) for x in live_ids] or [set()]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(launch, "BRIEF_DIR", home / "briefs"), \
                mock.patch.object(registry, "CLAUDE_JSON", home / ".claude.json"), \
                mock.patch.object(trust, "CODEX_CONFIG", home / "config.toml"), \
                mock.patch.object(registry, "codex_live_ids", side_effect=lambda: ids.pop(0) if len(ids) > 1 else ids[0]), \
                mock.patch.object(launch, "CODEX_THREAD_SECONDS", 0.2), mock.patch.object(launch.time, "sleep"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = sp.main((where or ["--dir", tempfile.mkdtemp()]) + ["--name", "spawn-test", "--brief-text", "x"]
                               + list(args))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue(), bins

    def test_a_branch_off_stale_refs_says_so_in_the_plan_and_the_brief(self):
        """After a failed fetch, the plan's base_warning and the brief's preamble say the base may be behind."""
        repo = Path(tempfile.mkdtemp()) / "tool"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q",
                        "--allow-empty", "-m", "first"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(repo.parent / "gone.git")], check=True)
        code, out, err, _ = self.run_spawn("--json", where=["--repo", str(repo), "--branch", "try"])
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(plan["base"], "main")
        self.assertIn("git fetch origin failed", plan["base_warning"])
        brief = Path(plan["brief_path"]).read_text()
        self.assertIn(plan["base_warning"], brief.split("Brief:")[0])
        self.assertIn("git fetch origin` and rebase", brief)

    def test_a_folder_it_cannot_trust_stops_it_before_the_agent_starts(self):
        """An untrusted folder would hold the agent at Claude's trust dialog: agent-spawn exits 1 with the reason."""
        code, out, err, bins = self.run_spawn(claude_json="{not json")
        self.assertEqual(code, 1)
        self.assertRegex(err, r"agent-spawn: cannot trust .*: Expecting property name")
        self.assertNotIn("new-session", [c[0] for c in bins.calls("tmux")])

    def test_existing_session_does_not_claim_model(self):
        for extra in (["--json"], []):
            code, out, err, _ = self.run_spawn("--reuse", *extra, sessions=["spawn-test"])
            self.assertEqual(code, 0, err)
            if extra:
                plan = json.loads(out)
                self.assertEqual((plan["tmux"], plan["model"], plan["effort"]), ("existing", None, None))
            else:
                self.assertIn("model and effort unchanged", out)
                self.assertNotIn("effort %s" % config.DEFAULTS["spawn_claude_effort"], out)
                self.assertTrue(out.endswith("\nattach: tmux attach -t spawn-test   (from another machine: ssh <this "
                                             "host> -t tmux attach -t spawn-test)\n"), out)

    def test_a_brief_agent_send_could_not_deliver_is_a_failure(self):
        """When agent-send fails, agent-spawn exits 1 with its output, puts it in --json's "error" and records
        FAILED in the spawn event; it never says "brief sent"."""
        for extra in (["--json"], []):
            code, out, err, _ = self.run_spawn("--reuse", *extra, sessions=["spawn-test"],
                                               agent_send=(4, "agent-send: no session matches 'spawn-test'"))
            self.assertEqual(code, 1)
            self.assertIn("no session matches", err)
            self.assertNotIn("brief sent", out)
            if extra:
                self.assertIn("no session matches", json.loads(out)["error"])
            self.assertIn("FAILED", statuslog.read(event="spawn")[-1]["text"])

    def test_a_brief_a_codex_agent_holds_in_its_queue_is_no_failure_and_says_why(self):
        """agent-send exits HELD: the brief is in the running agent's queue and arrives at its next turn. It must not
        be reported as undelivered (which invites sending it again); a note says it waits there, and why."""
        # no ' in it: the fake agent-send echoes it in '...'
        held = (deliver.HELD, "held in the Codex queue for codex spawn-test\nits last turn was interrupted, so Codex "
                              "holds it until another turn starts")
        code, out, err, _ = self.run_spawn("--reuse", "--json", sessions=["spawn-test"], agent_send=held)
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertNotIn("error", plan)
        self.assertIn("the brief waits in Codex's queue: its last turn was interrupted, so Codex holds it until "
                      "another turn starts", plan["notes"])
        self.assertNotIn("FAILED", statuslog.read(event="spawn")[-1]["text"])

    def test_a_codex_agent_that_never_got_its_brief_is_a_failure(self):
        """When `codex queue` fails or no thread appears, agent-spawn exits 1 with the reason, in --json's "error"
        and the spawn event."""
        for live_ids, queue, why in (([(), ("thread-1",)], "error: no such thread", "codex queue failed"),
                                     ([()], None, "did not appear")):
            env = {"FAKE_CODEX_QUEUE": queue} if queue else {}
            with self.subTest(why=why):
                code, out, err, bins = self.run_spawn("--cli", "codex", "--json", live_ids=live_ids, env=env)
                self.assertEqual(code, 1)
                self.assertIn(why, err)
                self.assertIn(why, json.loads(out)["error"])
                event = statuslog.read(event="spawn")[-1]
                self.assertIn("FAILED", event["text"])
                self.assertIn(why, event["text"])

    def test_a_codex_agent_gets_its_brief_queued(self):
        code, out, err, bins = self.run_spawn("--cli", "codex", "--json", live_ids=[(), ("thread-1",)])
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual((plan["thread"], plan["queued"]), ("thread-1", True))
        queued = [c for c in bins.calls("codex") if c[:1] == ["queue"]]
        self.assertEqual(queued[0][:3], ["queue", "--thread", "thread-1"])
        self.assertTrue(queued[0][4].endswith("Brief:\n\nx\n"))
        tmux = bins.calls("tmux")
        self.assertIn(["set-environment", "-t", "=spawn-test:", "-r", "CLAUDE_CONFIG_DIR"], tmux)   # no Claude login
        self.assertIn(["send-keys", "-t", "=spawn-test:", "-l", "/rename spawn-test"], tmux)
        self.assertTrue(plan["launch"].startswith("stty sane; unset CLAUDE_CONFIG_DIR; "), plan["launch"])

    def test_codex_hook_review_is_answered_without_trusting_before_the_rename(self):
        """Codex's question about a new or changed hook shows "›" as its prompt does: it gets the number of its
        "Continue without trusting" choice, never "/rename", and the plan says the hook was left off."""
        code, out, err, bins = self.run_spawn("--cli", "codex", "--json", live_ids=[(), ("thread-1",)],
                                              env={"FAKE_TMUX_DIALOG": fakes.CODEX_HOOK_REVIEW, "FAKE_TMUX_DIALOG_KEY": "3"})
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(plan["notes"], ["Codex: 1 hook is new or changed, left untrusted (review in Codex's /hooks)"])
        self.assertIn("note: Codex: 1 hook is new or changed", self.run_spawn("--cli", "codex", live_ids=[(), ("thread-1",)],
                      env={"FAKE_TMUX_DIALOG": fakes.CODEX_HOOK_REVIEW, "FAKE_TMUX_DIALOG_KEY": "3"})[1])
        typed = [c[-1] for c in bins.calls("tmux") if c[:1] == ["send-keys"] and "-l" in c]
        self.assertEqual(typed[1:], ["3", "/rename spawn-test"])      # after the launch line: the answer, then rename
        self.assertTrue(plan["queued"])

    def test_a_hook_review_already_on_the_untrusted_choice_gets_enter(self):
        """Should Codex's number key only move the highlight, the next look finds it there and confirms."""
        on_it = fakes.CODEX_HOOK_REVIEW.replace("› 1. Review", "  1. Review").replace("  3. Continue", "› 3. Continue")
        code, out, err, bins = self.run_spawn("--cli", "codex", "--json", live_ids=[(), ("thread-1",)],
                                              env={"FAKE_TMUX_DIALOG": on_it, "FAKE_TMUX_DIALOG_KEY": "Enter"})
        self.assertEqual(code, 0, err)
        typed = [c[-1] for c in bins.calls("tmux") if c[:1] == ["send-keys"]]
        self.assertEqual(typed[0], "C-u")                              # the shell's line cleared, then the launch line
        self.assertEqual(typed[2:5], ["Enter", "Enter", "/rename spawn-test"])   # the line's Enter, the confirm, rename
        self.assertNotIn("3", typed)

    def test_a_codex_hook_review_without_the_untrusted_choice_is_left_alone(self):
        """No key is guessed and nothing is typed into the question; the brief is still queued to the thread."""
        screen = fakes.CODEX_HOOK_REVIEW.replace("Continue without trusting", "Skip")
        with mock.patch.object(launch, "CODEX_PROMPT_SECONDS", 0.3):
            code, out, err, bins = self.run_spawn("--cli", "codex", "--json", live_ids=[(), ("thread-1",)],
                                                  env={"FAKE_TMUX_DIALOG": screen, "FAKE_TMUX_DIALOG_KEY": "3"})
        plan = json.loads(out)
        typed = [c[-1] for c in bins.calls("tmux") if c[:1] == ["send-keys"]]
        self.assertEqual(typed[0], "C-u")                              # the shell's line cleared, then the launch line
        self.assertEqual(typed[2:], ["Enter"])                         # only the launch line and its Enter
        self.assertEqual(plan["notes"], ["Codex asks to review its hooks and offers no 'Continue without trusting': "
                                         "answer it in the agent's window",
                                         "Codex's prompt did not show within 0.3 s, so the agent was not renamed"])
        self.assertTrue(plan["queued"])

    def test_codex_is_refused_in_a_worktree_whose_main_checkout_lacks_its_hook_file(self):
        """Codex runs the main checkout's .codex/hooks.json even in a linked worktree, so it would run without the
        worktree's hooks there; Claude, which runs the worktree's own hooks, still starts."""
        repo, wt = homes.gated_worktree(self)
        code, out, err, bins = self.run_spawn("--cli", "codex", where=["--dir", str(wt)])
        self.assertEqual(code, 1)
        self.assertIn("agent-spawn: not starting Codex in %s: Codex runs the main checkout's .codex/hooks.json even "
                      "in a linked worktree, and %s has none, so the agent would run with none of this branch's "
                      "hooks. Bring the main checkout up to date" % (wt, repo), err)
        self.assertNotIn("new-session", [c[0] for c in bins.calls("tmux")])
        self.assertEqual(bins.calls("codex"), [])
        code, out, err, bins = self.run_spawn("--cli", "claude", where=["--dir", str(wt)])
        self.assertEqual(code, 0, err)

    def test_new_repo_plans_its_agent_a_worktree(self):
        root = tempfile.mkdtemp()
        env = dict(os.environ, AGENT_SPAWN_ROOT=root)   # the test identity's config: github_owner example-user
        base = [sys.executable, str(ROOT / "bin" / "agent-spawn"), "--new-repo", "Shiny Tool",
                "--cli", "claude", "--brief-text", "x", "--dry-run"]
        p = json.loads(subprocess.run(base, capture_output=True, text=True, env=env, check=True).stdout)
        self.assertEqual((p["repo"], p["path"], p["branch"], p["github"]),
                         (root + "/Shiny-Tool", root + "/Shiny-Tool-initial", "initial", "example-user/Shiny-Tool"))
        p = json.loads(subprocess.run(base + ["--branch", "build"], capture_output=True, text=True, env=env,
                                      check=True).stdout)
        self.assertEqual((p["path"], p["branch"]), (root + "/Shiny-Tool-build", "build"))

    def test_new_repo_goes_under_config_repos_root_and_github_owner(self):
        root, conf = tempfile.mkdtemp(), tempfile.mkdtemp()
        Path(conf, "config.json").write_text(json.dumps({"repos_root": root, "github_owner": "someone"}))
        env = {k: v for k, v in os.environ.items() if k != "AGENT_SPAWN_ROOT"}
        env["AGENT_MAIL_CONFIG_DIR"] = conf
        base = [sys.executable, str(ROOT / "bin" / "agent-spawn"), "--new-repo", "tool",
                "--cli", "claude", "--brief-text", "x", "--dry-run"]
        p = json.loads(subprocess.run(base, capture_output=True, text=True, env=env, check=True).stdout)
        self.assertEqual((p["repo"], p["github"]), (root + "/tool", "someone/tool"))
        p = json.loads(subprocess.run(base + ["--org", "other"], capture_output=True, text=True, env=env,
                                      check=True).stdout)
        self.assertEqual(p["github"], "other/tool")
        Path(conf, "config.json").write_text(json.dumps({"repos_root": root}))   # no github_owner: --org or nothing
        r = subprocess.run(base, capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("--new-repo needs a GitHub owner: pass --org OWNER or set github_owner", r.stderr)
        p = json.loads(subprocess.run(base + ["--org", "other"], capture_output=True, text=True, env=env,
                                      check=True).stdout)
        self.assertEqual(p["github"], "other/tool")

    def spawn_new_repo_over_existing(self, *flags, tmux_session_exists, leftover_branch=None):
        """Run agent-spawn --new-repo where <repos root>/tool already exists; return the module and that repo."""
        sp = load_script("agent-spawn")
        root = mock.patch.dict(os.environ, AGENT_SPAWN_ROOT=tempfile.mkdtemp())
        root.start()
        self.addCleanup(root.stop)
        repo = Path(os.environ["AGENT_SPAWN_ROOT"]) / "tool"   # already created: with --reuse gh repo create is skipped
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q",
                        "--allow-empty", "-m", "first"], check=True)
        if leftover_branch:
            subprocess.run(["git", "-C", str(repo), "branch", leftover_branch], check=True)
        briefs = mock.patch.object(launch, "BRIEF_DIR", Path(tempfile.mkdtemp()))
        briefs.start()
        self.addCleanup(briefs.stop)
        sp.tmux_has = lambda name: tmux_session_exists   # tmux is the boundary: the brief goes to a stub session
        # the real ensure_trust writes ~/.claude.json and ~/.codex/config.toml in the user's home
        sp.ensure_trust = lambda path, claude_json=None: {}
        real_run = sp.run   # git runs; gh and the rest would reach GitHub or the agent
        sp.run = lambda cmd, *a, **k: real_run(cmd, *a, **k) if cmd[0] == "git" else subprocess.CompletedProcess(cmd, 0, "", "")
        homes.status_log(self)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            sp.main(["--new-repo", "tool", "--cli", "claude", "--brief-text", "x", *flags])
        return sp, repo

    def test_new_repo_agent_starts_in_a_worktree_and_main_stays_put(self):
        branch = lambda d: subprocess.run(["git", "-C", str(d), "branch", "--show-current"], capture_output=True,
                                          text=True).stdout.strip()
        for leftover in (None, "initial"):      # a branch left behind by a worktree deleted by hand is reused
            with self.subTest(leftover_branch=leftover):
                sp, repo = self.spawn_new_repo_over_existing("--reuse", tmux_session_exists=True,
                                                             leftover_branch=leftover)
                self.assertEqual((branch(repo.parent / "tool-initial"), branch(repo)), ("initial", "main"))

    def test_new_repo_refuses_an_existing_repo_without_reuse(self):
        with self.assertRaises(SystemExit) as e:
            self.spawn_new_repo_over_existing(tmux_session_exists=False)
        self.assertEqual(e.exception.code, 3)

if __name__ == "__main__":
    unittest.main()
