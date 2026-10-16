"""agent-close: refuses unlanded work, removes only linked worktrees, ends the session. No tmux."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
os.environ["AGENT_MAIL_STATE_DIR"] = TMP
os.environ["AGENT_STATUS_DIR"] = TMP
from agentmail import close, statuslog  # noqa: E402


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
        statuslog.LOG = Path(tempfile.mkdtemp()) / "events.jsonl"
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
        sess = {"name": "rm-1-x", "id": "abc", "cli": "claude", "cwd": str(self.wt), "live": True}
        with patch.object(close.registry, "resolve", return_value=(sess, 0, [sess])), \
                patch.object(close, "tmux_has", side_effect=lambda n: n == "rm-1-x"):
            info, err = close.find("rm-1-x")
        self.assertIsNone(err)
        self.assertEqual((info["tmux"], info["cwd"], info["session_id"]), ("rm-1-x", str(self.wt), "abc"))
        with patch.object(close.registry, "resolve", return_value=(None, 4, [])), \
                patch.object(close, "tmux_has", return_value=False):
            info, err = close.find("nobody")
        self.assertIn("no session", err)

    def test_partial_name_is_refused(self):
        sess = {"name": "rm-1-x", "id": "abcdef123456", "cli": "claude", "cwd": str(self.wt), "live": True}
        with patch.object(close.registry, "resolve", return_value=(sess, 0, [sess])), \
                patch.object(close, "tmux_has", return_value=False):
            self.assertIn("only partly matches", close.find("rm-1")[1])
            self.assertIsNone(close.find("abcdef123456")[1])     # exact id is fine
        with patch.object(close.registry, "resolve", return_value=(sess, 0, [sess])), \
                patch.object(close, "tmux_has", side_effect=lambda n: n == "rm-1"):
            info, err = close.find("rm-1")                       # an exact tmux name wins over the fuzzy hit
        self.assertIsNone(err)
        self.assertEqual((info["tmux"], info["session_id"]), ("rm-1", ""))


if __name__ == "__main__":
    unittest.main()
