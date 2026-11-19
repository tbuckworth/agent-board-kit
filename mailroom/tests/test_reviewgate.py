"""agentmail/reviewgate.py: a Codex agent in a linked worktree runs the main checkout's .codex/hooks.json, and the kit
never bypasses Codex's hook approval."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import homes  # noqa: E402
from agentmail import reviewgate  # noqa: E402


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t"] + list(args), cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


def new_repo(test):
    """A git repo on main with one commit."""
    d = homes.temp_dir(test) / "proj"
    git("init", "-q", "-b", "main", str(d))
    (d / "a.py").write_text("x = 1\n")
    git("add", "-A", cwd=d)
    git("commit", "-qm", "start", cwd=d)
    return d


def worktree(test, repo, branch):
    wt = homes.temp_dir(test) / ("proj-" + branch)
    git("worktree", "add", "-q", "-b", branch, str(wt), cwd=repo)
    return wt


class CodexHooksGap(unittest.TestCase):
    """A Codex agent in a linked worktree runs the main checkout's .codex/hooks.json, not the worktree's."""

    def test_a_worktree_whose_main_checkout_lacks_its_hook_file(self):
        r = new_repo(self)
        wt = worktree(self, r, "hooks")
        self.assertIsNone(reviewgate.codex_hooks_gap(wt))                        # no hooks in the worktree
        (wt / ".codex").mkdir()
        (wt / ".codex" / "hooks.json").write_text('{"hooks": "new"}\n')
        self.assertEqual(reviewgate.codex_hooks_gap(wt), "Codex runs the main checkout's .codex/hooks.json even in a "
                         "linked worktree, and %s has none, so the agent would run with none of this branch's hooks"
                         % r)
        (r / ".codex").mkdir()
        (r / ".codex" / "hooks.json").write_text('{"hooks": "old"}\n')
        self.assertIn("%s has another one than this worktree's, so the agent would run another version of them" % r,
                      reviewgate.codex_hooks_gap(wt))
        (wt / "src").mkdir()
        self.assertEqual(reviewgate.codex_hooks_gap(wt / "src"), reviewgate.codex_hooks_gap(wt))   # its checkout's
        self.assertIsNone(reviewgate.codex_hooks_gap(homes.temp_dir(self)))   # no git repo
        (r / ".codex" / "hooks.json").write_text('{"hooks": "new"}\n')
        self.assertIsNone(reviewgate.codex_hooks_gap(wt))
        self.assertIsNone(reviewgate.codex_hooks_gap(r))                          # the main checkout itself


class NoBypass(unittest.TestCase):
    def test_codex_keeps_its_hook_approval_everywhere(self):
        r = new_repo(self)
        (r / ".codex").mkdir()
        (r / ".codex" / "hooks.json").write_text('{"hooks": {}}\n')
        for cwd in (r, homes.temp_dir(self), None):
            self.assertEqual(reviewgate.codex_hook_args(cwd), [])
        self.assertIsNone(reviewgate.hooks_hash(homes.temp_dir(self)))
        self.assertEqual(len(reviewgate.hooks_hash(r)), 64)


if __name__ == "__main__":
    unittest.main()
