"""agent-close: refuses unlanded work, removes only linked worktrees, ends the session. No tmux."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import homes  # noqa: E402
from agentmail import close, commands, statuslog  # noqa: E402


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t"] + list(args), cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


def setup_repo():
    """main clone of a bare origin, plus a linked worktree on branch inbox/1-x off origin/main."""
    d = Path(tempfile.mkdtemp())
    git("init", "-q", "--bare", "-b", "main", str(d / "origin.git"))
    git("clone", "-q", str(d / "origin.git"), str(d / "repo"))
    (d / "repo" / ".gitignore").write_text("*.pdf\n")
    git("add", "-A", cwd=d / "repo")
    git("commit", "-q", "-m", "first", cwd=d / "repo")
    git("push", "-q", "origin", "HEAD:main", cwd=d / "repo")
    git("worktree", "add", "-q", "-b", "inbox/1-x", str(d / "wt"), "origin/main", cwd=d / "repo")
    return d / "repo", d / "wt"


def branches(repo):
    return git("for-each-ref", "--format=%(refname:short)", "refs/heads/", cwd=repo).split()


class Close(unittest.TestCase):
    def setUp(self):
        homes.status_log(self)
        self.repo, self.wt = setup_repo()
        self.ended = []
        p = patch.object(close, "end_session", side_effect=lambda info, delay=0: self.ended.append(delay) or "ended")
        p.start()
        self.addCleanup(p.stop)

    def info(self, cwd=None):
        return {"name": "rm-1-x", "tmux": "rm-1-x", "cli": "claude", "session_id": "s1", "cwd": str(cwd or self.wt)}

    def test_kinds(self):
        self.assertEqual(close.worktree(str(self.wt))["kind"], "linked")
        self.assertEqual(close.worktree(str(self.wt))["branch"], "inbox/1-x")
        self.assertEqual(close.worktree(str(self.wt))["common"], str((self.repo / ".git").resolve()))
        self.assertEqual(close.worktree(str(self.repo))["kind"], "main")
        self.assertEqual(close.worktree(tempfile.mkdtemp())["kind"], "plain")
        self.assertEqual(close.worktree("/nonexistent/x")["kind"], "missing")

    def test_clean_merged_worktree_is_removed_with_branch(self):
        (self.wt / "doc.pdf").write_text("ignored files go with it")
        code, lines = close.close(self.info())
        self.assertEqual(code, 0, lines)
        self.assertFalse(self.wt.exists())
        self.assertNotIn("inbox/1-x", branches(self.repo))
        self.assertEqual(self.ended, [0])            # session ended first, synchronously
        ev = statuslog.read(event="closed")[-1]
        self.assertEqual((ev["name"], ev["branch"]), ("rm-1-x", "inbox/1-x"))
        self.assertIn("removed worktree", ev["text"])

    def test_untracked_or_unmerged_work_blocks_unless_forced(self):
        (self.wt / "notes.md").write_text("not committed")
        code, lines = close.close(self.info())
        self.assertEqual(code, 2)
        self.assertIn("notes.md", lines[0])
        git("add", "-A", cwd=self.wt)
        git("commit", "-q", "-m", "work", cwd=self.wt)
        code, lines = close.close(self.info())
        self.assertEqual(code, 2)
        self.assertIn("1 commit(s) on inbox/1-x not in origin/main", lines[0])
        self.assertTrue(self.wt.exists())
        self.assertEqual(self.ended, [])             # nothing touched on refusal
        code, lines = close.close(self.info(), force=True)
        self.assertEqual(code, 0, lines)
        self.assertFalse(self.wt.exists())
        self.assertNotIn("inbox/1-x", branches(self.repo))

    def test_work_merged_by_pr_counts_as_landed(self):
        (self.wt / "notes.md").write_text("landed")
        git("add", "-A", cwd=self.wt)
        git("commit", "-q", "-m", "work", cwd=self.wt)
        git("push", "-q", "origin", "HEAD:refs/heads/inbox/1-x", cwd=self.wt)
        git("fetch", "-q", "origin", cwd=self.repo)   # a merge commit on main, as `gh pr merge --merge` makes
        git("merge", "-q", "--no-ff", "-m", "Merge PR", "origin/inbox/1-x", cwd=self.repo)
        git("push", "-q", "origin", "HEAD:main", cwd=self.repo)
        code, lines = close.close(self.info())
        self.assertEqual(code, 0, lines)
        self.assertFalse(self.wt.exists())

    def commit_in_wt(self, name="notes.md"):
        (self.wt / name).write_text("work")
        git("add", "-A", cwd=self.wt)
        git("commit", "-q", "-m", "work", cwd=self.wt)

    def test_squash_merged_work_counts_as_landed(self):
        self.commit_in_wt()
        git("push", "-q", "origin", "HEAD:refs/heads/inbox/1-x", cwd=self.wt)
        git("fetch", "-q", "origin", cwd=self.repo)
        git("merge", "-q", "--squash", "origin/inbox/1-x", cwd=self.repo)
        git("commit", "-q", "-m", "Squash PR", cwd=self.repo)
        (self.repo / "later.md").write_text("main moved on after the squash")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "later", cwd=self.repo)
        git("push", "-q", "origin", "HEAD:main", cwd=self.repo)
        code, lines = close.close(self.info())
        self.assertEqual(code, 0, lines)
        self.assertFalse(self.wt.exists())

    def test_rebase_merged_work_counts_as_landed(self):
        self.commit_in_wt()
        sha = git("rev-parse", "HEAD", cwd=self.wt).strip()
        git("cherry-pick", sha, cwd=self.repo)
        git("push", "-q", "origin", "HEAD:main", cwd=self.repo)
        self.assertEqual(close.close(self.info())[0], 0)

    def test_bare_main_repo_worktree(self):
        origin = self.repo.parent / "origin.git"
        wt2 = self.repo.parent / "wt2"
        subprocess.run(["git", "--git-dir", str(origin), "worktree", "add", "-q", "-b", "b2", str(wt2), "main"],
                       check=True, capture_output=True)
        self.assertEqual(close.worktree(str(wt2))["common"], str(origin.resolve()))
        code, lines = close.close(self.info(wt2), force=True)   # a bare repo has no origin remote to check against
        self.assertEqual(code, 0, lines)
        self.assertFalse(wt2.exists())
        self.assertNotIn("b2", git("--git-dir", str(origin), "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())
        self.assertTrue(self.repo.exists())                       # the enclosing directory's repos are untouched

    def test_self_outside_tmux_refuses_unless_forced(self):
        info = dict(self.info(), tmux=None)
        code, lines = close.close(info, self_=True)
        self.assertEqual(code, 2)
        self.assertIn("not running in tmux", lines[0])
        self.assertTrue(self.wt.exists())
        code, lines = close.close(info, self_=True, force=True)
        self.assertEqual(code, 0, lines)
        self.assertFalse(self.wt.exists())
        self.assertIn("NOT ended", lines[-1])

    def test_worktree_deleted_by_hand_is_pruned_and_landed_branch_dropped(self):
        shutil.rmtree(self.wt)
        code, lines = close.close(self.info(self.wt / "inbox" / "folder"))
        self.assertEqual(code, 0, lines)
        self.assertIn("pruned stale worktree", lines[-1])
        self.assertNotIn("inbox/1-x", branches(self.repo))
        self.assertNotIn(str(self.wt), git("worktree", "list", cwd=self.repo))

    def test_deleted_worktree_with_unlanded_branch_keeps_branch(self):
        self.commit_in_wt()
        shutil.rmtree(self.wt)
        code, lines = close.close(self.info())
        self.assertEqual(code, 0, lines)
        self.assertIn("kept branch inbox/1-x: 1 commit(s) not landed", lines[-1])
        self.assertIn("inbox/1-x", branches(self.repo))

    def dangle_origin_main(self):
        """Point origin/main at an object that does not exist: rev-parse --verify still accepts the
        ref, fetch fails on it, and git rev-list cannot count against it."""
        (self.repo / ".git" / "refs" / "remotes" / "origin" / "main").write_text("1" * 40 + "\n")

    def test_failed_commit_count_is_unknown_not_zero(self):
        g = lambda *a: close.git(self.wt, *a)  # noqa: E731
        self.assertEqual(close.unmerged(g, "no-such-ref", "HEAD")[0], -1)
        self.dangle_origin_main()
        ahead, why = close.unmerged(g, "origin/main", "HEAD")
        self.assertEqual(ahead, -1)
        self.assertIn("git rev-list failed", why)      # git's own reason reaches the message

    def test_deleted_worktree_keeps_branch_when_git_cannot_count(self):
        self.commit_in_wt()
        shutil.rmtree(self.wt)
        self.dangle_origin_main()
        code, lines = close.close(self.info())
        self.assertEqual(code, 0, lines)
        self.assertIn("kept branch inbox/1-x: unknown (git rev-list failed: ", lines[-1])
        self.assertIn("inbox/1-x", branches(self.repo))

    def test_a_fetch_that_fails_is_said_with_the_result(self):
        """Without it, "N commit(s) not in origin/main" could not be told apart from an origin never fetched."""
        shutil.rmtree(self.repo.parent / "origin.git")
        self.commit_in_wt()
        code, lines = close.close(self.info())
        self.assertEqual(code, 2, lines)
        self.assertIn("1 commit(s) on inbox/1-x not in origin/main", lines[0])
        self.assertRegex(lines[1], r"^could not fetch origin \(.*origin\.git.*\); checked against the last fetched refs")
        code, lines = close.close(self.info(), force=True)
        self.assertEqual(code, 0, lines)
        self.assertIn("could not fetch origin", statuslog.read(event="closed")[-1]["text"])
        repo2, wt2 = setup_repo()   # a worktree deleted by hand: the same for the stale branch's check
        shutil.rmtree(repo2.parent / "origin.git")
        shutil.rmtree(wt2)
        code, lines = close.close(self.info(wt2))
        self.assertEqual(code, 0, lines)
        self.assertTrue(any(l.startswith("could not fetch origin") for l in lines), lines)

    @unittest.skipIf(os.geteuid() == 0, "root deletes in a folder whatever its mode")
    def test_a_prune_that_fails_is_an_error_and_keeps_the_branch(self):
        shutil.rmtree(self.wt)
        entry = self.repo / ".git" / "worktrees" / "wt"
        entry.chmod(0o500)                           # git worktree prune cannot delete the stale entry's files
        self.addCleanup(entry.chmod, 0o700)
        code, lines = close.close(self.info())
        self.assertEqual(code, 1, lines)
        self.assertRegex(lines[-1], r"^stale worktree .*wt not pruned: .*failed to delete")
        self.assertIn("inbox/1-x", branches(self.repo))
        self.assertIn("not pruned", statuslog.read(event="closed")[-1]["text"])

    def test_failed_git_status_refuses(self):
        index = git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=self.wt).strip()
        Path(index).write_bytes(b"not an index")      # git status fails: "index file smaller than expected"
        code, lines = close.close(self.info())
        self.assertEqual(code, 2, lines)
        self.assertIn("git status failed", lines[0])
        self.assertIn("index file smaller than expected", lines[0])
        self.assertTrue(self.wt.exists())
        self.assertEqual(self.ended, [])             # the session is left running

    def test_main_checkout_and_plain_dirs_are_never_removed(self):
        code, lines = close.close(self.info(self.repo))
        self.assertEqual(code, 0)
        self.assertTrue(self.repo.exists())
        self.assertIn("main checkout", lines[0])
        plain = tempfile.mkdtemp()
        code, lines = close.close(self.info(plain))
        self.assertTrue(os.path.isdir(plain))
        self.assertEqual(self.ended, [0, 0])

    def test_unknown_directory_blocks_unless_forced(self):
        info = dict(self.info(), cwd=None)
        code, lines = close.close(info)
        self.assertEqual(code, 2)
        self.assertIn("unknown", lines[0])
        self.assertEqual(self.ended, [])
        self.assertEqual(close.close(info, force=True)[0], 0)
        self.assertEqual(self.ended, [0])
        self.assertEqual(close.close(dict(info, cwd="/nonexistent/x"))[0], 0)   # deleted: nothing to lose

    def test_self_removes_first_then_ends_session_later(self):
        code, lines = close.close(self.info(), self_=True)
        self.assertEqual(code, 0, lines)
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.ended, [2])            # one delayed end, after the removal

    def test_dry_run_changes_nothing(self):
        code, lines = close.close(self.info(), dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("would end rm-1-x and remove worktree", lines[-1])
        self.assertTrue(self.wt.exists())
        self.assertEqual(self.ended, [])
        self.assertEqual(statuslog.read(event="closed"), [])

    def test_find_by_name_or_tmux(self):
        homes.Homes(self).claude("abc", "rm-1-x", cwd=str(self.wt))
        # tmux is the boundary: which tmux sessions exist is made up, Alex's tmux server is not asked
        with patch.object(close.commands, "tmux_has", side_effect=lambda n: n == "rm-1-x"):
            info, err = close.find("rm-1-x")
        self.assertIsNone(err)
        self.assertEqual((info["tmux"], info["cwd"], info["session_id"]), ("rm-1-x", str(self.wt), "abc"))
        with patch.object(close.commands, "tmux_has", return_value=False):
            info, err = close.find("nobody")
        self.assertIn("no session", err)

    def test_partial_name_is_refused(self):
        homes.Homes(self).claude("abcdef123456", "rm-1-x", cwd=str(self.wt))
        with patch.object(close.commands, "tmux_has", return_value=False):   # tmux, the boundary: no such session
            self.assertIn("only partly matches", close.find("rm-1")[1])
            self.assertIsNone(close.find("abcdef123456")[1])     # exact id is fine
        with patch.object(close.commands, "tmux_has", side_effect=lambda n: n == "rm-1"):
            info, err = close.find("rm-1")                       # an exact tmux name wins over the fuzzy hit
        self.assertIsNone(err)
        self.assertEqual((info["tmux"], info["session_id"]), ("rm-1", ""))


class Commands(unittest.TestCase):
    def test_a_command_that_must_succeed_raises_the_callers_error(self):
        not_a_repo = commands.run(["git", "-C", tempfile.mkdtemp(), "rev-parse", "HEAD"], timeout=30)
        with self.assertRaisesRegex(ValueError, r"^git rev-parse failed \(exit 128\): .*not a git repository"):
            commands.checked(not_a_repo, "git rev-parse", ValueError)
        ok = commands.run([sys.executable, "-c", "print('fine')"], timeout=30)
        self.assertIs(commands.checked(ok, "python", ValueError), ok)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
    def test_a_tmux_command_that_must_succeed_raises(self):
        # a tmux socket folder of its own, so this tmux reaches no server (Alex's least of all)
        env, sockets = {k: v for k, v in os.environ.items() if k != "TMUX"}, tempfile.mkdtemp()
        with patch.dict(os.environ, env, clear=True), patch.dict(os.environ, {"TMUX_TMPDIR": sockets}):
            self.assertEqual(commands.tmux("has-session", "-t", "=x", check=False).returncode, 1)
            # tmux's own words differ by version; both name the socket it could not reach
            with self.assertRaisesRegex(commands.CommandError,
                                        r"^tmux has-session -t =x failed \(exit 1\): .*" + re.escape(sockets)):
                commands.tmux("has-session", "-t", "=x", check=True)

    def test_a_command_that_cannot_start_or_times_out_is_a_failed_one(self):
        """agent-close counts a git it could not run as unlanded work; that needs a result, not an exception."""
        r = commands.run(["/nonexistent/no-such-command"], timeout=10)
        self.assertEqual(r.returncode, 1)
        self.assertIn("No such file", r.stderr)
        r = commands.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
        self.assertEqual(r.returncode, 1)
        self.assertIn("timed out", r.stderr)

    def test_a_worktrees_main_checkout_is_its_repos(self):
        repo, wt = setup_repo()
        self.assertEqual(commands.repo_root(wt), str(repo.resolve()))
        self.assertEqual(commands.repo_root(repo / ".git"), str(repo.resolve()))
        plain = tempfile.mkdtemp()
        self.assertIsNone(commands.common_dir(plain))
        self.assertEqual(commands.repo_root(plain), str(Path(plain).resolve()))


if __name__ == "__main__":
    unittest.main()
