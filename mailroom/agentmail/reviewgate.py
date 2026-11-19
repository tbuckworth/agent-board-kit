"""Codex and a repo's own .codex/hooks.json, for agent-spawn and agent-switch.

Codex runs a repo's hooks only after someone approves them on its /hooks screen, and skips them silently otherwise;
in a linked worktree it runs the main checkout's .codex/hooks.json, not the worktree's. So a Codex agent started in a
worktree whose branch changed that file runs the main checkout's version (or none): `codex_hooks_gap` says so, and
agent-spawn and agent-switch refuse to start Codex there.

The kit sets up no review gate of its own, so it never passes Codex's hook-trust bypass: `codex_hook_args` gives no
arguments, and every repo keeps Codex's approval step (agentmail/launch.py answers it with "Continue without
trusting" for an unattended start).
"""
import hashlib
from pathlib import Path

from . import commands


def hooks_hash(repo):
    """The sha256 of repo's .codex/hooks.json, or None when it has none."""
    p = Path(repo) / ".codex" / "hooks.json"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def codex_hooks_gap(cwd):
    """Why a Codex agent in `cwd` would not run the hooks of cwd's own branch, or None. Codex runs the main
    checkout's .codex/hooks.json even in a linked worktree, so a worktree whose .codex/hooks.json the main checkout
    lacks (has none, or another) gets none or other hooks. None where cwd's checkout has no .codex/hooks.json, where
    cwd is in no git repo, and in the main checkout itself. Runs git in cwd for its checkout's top level and main
    checkout, and reads both checkouts' .codex/hooks.json."""
    r = commands.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], timeout=30)
    top = r.stdout.strip() if r.returncode == 0 else ""
    own = hooks_hash(top) if top else None
    if not own:
        return None
    main = commands.repo_root(top)
    if Path(main).resolve() == Path(top).resolve():
        return None
    theirs = hooks_hash(main)
    if theirs == own:
        return None
    return ("Codex runs the main checkout's .codex/hooks.json even in a linked worktree, and %s %s, so the agent "
            "would run %s" % (main, "has none" if theirs is None else "has another one than this worktree's",
                              "with none of this branch's hooks" if theirs is None else "another version of them"))


def codex_hook_args(cwd):
    """Extra Codex arguments for a session in `cwd`: none. The kit never bypasses Codex's hook approval."""
    return []
