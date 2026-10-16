"""Close an agent: end its session and remove its git worktree once its work is landed.

Only a *linked* worktree is ever removed, never a main checkout or a plain directory; for those
only the session ends. Nothing is closed while the worktree has uncommitted or untracked files,
or commits whose changes are not on origin's default branch (a squash or rebase merge counts as
landed), unless forced. Git-ignored files (downloads, PDFs) go with the worktree.
"""
import os
import subprocess
import time

from . import registry, statuslog


def run(*cmd, cwd=None, timeout=60):
    try:
        return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def git(repo, *args, timeout=60):
    return run("git", "-C", str(repo), *args, timeout=timeout)


def gitd(common, *args, timeout=60):
    """git against a repository's common dir: right for normal, bare and gitdir-file layouts."""
    return run("git", "--git-dir", str(common), *args, timeout=timeout)


def tmux_has(name):
    return bool(name) and run("tmux", "has-session", "-t", "=" + name, timeout=10).returncode == 0


def tmux_self():
    """The tmux session this process runs in, or None."""
    if not os.environ.get("TMUX"):
        return None
    r = run("tmux", "display-message", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#S", timeout=10)
    return r.stdout.strip() or None


def tmux_cwd(name):
    # "=name:" (session, current window); a bare "=name" is read as a pane and matches nothing
    r = run("tmux", "display-message", "-p", "-t", "=%s:" % name, "#{pane_current_path}", timeout=10)
    return r.stdout.strip() or None


def find(target=None, self_=False):
    """(info, error). info: name, tmux (session name or None), cli, session_id, cwd.
    A NAME must match exactly (session name, session id or tmux name): this command deletes."""
    if self_:
        sess = registry.current_session() or {}
        tm = tmux_self()
        return {"name": sess.get("name") or tm or "", "tmux": tm, "cli": sess.get("cli", "?"),
                "session_id": sess.get("id", ""), "cwd": os.getcwd()}, None
    sess, code, cands = registry.resolve(target)
    exact_tmux = tmux_has(target)
    if code == 3 and not exact_tmux:
        return None, "%r is ambiguous: %s" % (target, ", ".join(sorted({c.get("name") or c["id"][:8] for c in cands})))
    if sess and target not in (sess.get("name"), sess.get("id")):
        if not exact_tmux:
            return None, "%r only partly matches %s; agent-close needs the exact session name or id" % (
                target, sess.get("name") or sess["id"][:8])
        sess = None   # an exact tmux name beats a fuzzy registry hit
    tm = target if exact_tmux else (sess["name"] if sess and tmux_has(sess.get("name")) else None)
    if not sess and not tm:
        return None, "no session or tmux session named %r" % target
    cwd = (sess or {}).get("cwd") or (tmux_cwd(tm) if tm else None)
    return {"name": (sess or {}).get("name") or tm, "tmux": tm, "cli": (sess or {}).get("cli", "?"),
            "session_id": (sess or {}).get("id", ""), "cwd": cwd}, None


def _common(top):
    c = git(top, "rev-parse", "--git-common-dir").stdout.strip()
    return os.path.realpath(c if os.path.isabs(c) else os.path.join(top, c))


def worktree(cwd):
    """Facts about the git checkout at cwd. kind: linked, main, plain, missing or unknown."""
    if not cwd:
        return {"kind": "unknown", "path": None}
    if not os.path.isdir(cwd):
        return {"kind": "missing", "path": cwd}
    top = git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return {"kind": "plain", "path": cwd}
    top = top.stdout.strip()
    gitdir = os.path.realpath(git(top, "rev-parse", "--absolute-git-dir").stdout.strip())
    common = _common(top)
    return {"kind": "linked" if gitdir != common else "main", "path": top, "common": common,
            "branch": git(top, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()}


def default_base(g):
    """origin's default branch as a ref name, given g(*args) that runs git in the repo."""
    head = g("symbolic-ref", "--short", "refs/remotes/origin/HEAD").stdout.strip()
    return next((b for b in (head, "origin/main", "origin/master", "main")
                 if b and g("rev-parse", "--verify", "--quiet", b).returncode == 0), None)


def contained(g, base, ref):
    """True if merging ref into base would change nothing: its changes are already there, as after
    a squash or rebase merge whose commits are not ancestors of base."""
    r = g("merge-tree", "--write-tree", base, ref)
    return r.returncode == 0 and r.stdout.split()[:1] == [g("rev-parse", base + "^{tree}").stdout.strip()]


def unmerged(g, base, ref):
    """Commits on ref not landed in base: 0 if ancestors or squash/rebase-merged, -1 if unknown."""
    if not base:
        return -1
    n = int(g("rev-list", "--count", "%s..%s" % (base, ref)).stdout.strip() or 0)
    return 0 if n and contained(g, base, ref) else n


def unlanded(wt, fetch=True):
    """What would be lost by removing a linked worktree: (dirty lines, unmerged commit count, base)."""
    g = lambda *a: git(wt["path"], *a)  # noqa: E731
    if fetch:
        git(wt["path"], "fetch", "--quiet", "origin", timeout=120)
    base = default_base(g)
    dirty = [l for l in g("status", "--porcelain").stdout.splitlines() if l.strip()]
    return dirty, unmerged(g, base, "HEAD"), base


def _dirs(d):
    try:
        return sorted(e.path for e in os.scandir(d) if e.is_dir() and not e.name.startswith("."))
    except OSError:
        return []


def find_registration(path, limit=400):
    """For a worktree directory that no longer exists: (common dir, registered root, branch) of
    the repo that still lists it. Searches the nearest existing ancestor and its siblings, which
    covers both <parent>/<repo>-<branch> and a dedicated worktrees directory."""
    p = os.path.abspath(path)
    anc = p
    while anc != "/" and not os.path.isdir(anc):
        anc = os.path.dirname(anc)
    seen = set()
    for c in ([anc] + _dirs(anc) + _dirs(os.path.dirname(anc)))[:limit]:
        if git(c, "rev-parse", "--git-dir", timeout=10).returncode != 0:
            continue
        common = _common(c)
        if common in seen:
            continue
        seen.add(common)
        for block in gitd(common, "worktree", "list", "--porcelain").stdout.split("\n\n"):
            fields = dict(l.split(" ", 1) for l in block.splitlines() if " " in l)
            root = fields.get("worktree")
            if root and (p == root or p.startswith(root.rstrip("/") + "/")):
                return common, root, fields.get("branch", "").replace("refs/heads/", "")
    return None


def end_session(info, delay=0):
    """End the agent's tmux session. With delay, detach and do it later (used by --self, so the
    calling agent still gets this command's output before its own session goes)."""
    if not info.get("tmux"):
        return "no tmux session to end"
    if delay:
        subprocess.Popen(["sh", "-c", "sleep %d; tmux kill-session -t '=%s'" % (delay, info["tmux"].replace("'", ""))],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "tmux session %s ends in %ds" % (info["tmux"], delay)
    run("tmux", "kill-session", "-t", "=" + info["tmux"], timeout=10)
    for _ in range(20):
        if not tmux_has(info["tmux"]):
            return "ended tmux session %s" % info["tmux"]
        time.sleep(0.25)
    return "tmux session %s did not end" % info["tmux"]


def delete_branch(common, branch):
    b = gitd(common, "branch", "-D", branch)   # landed (checked) or forced
    return "deleted branch %s" % branch if b.returncode == 0 else "branch %s kept (%s)" % (
        branch, (b.stderr or b.stdout).strip()[:120])


def remove(wt, force=False):
    """Remove a linked worktree and its branch. Returns (ok, message)."""
    r = gitd(wt["common"], "worktree", "remove", *(["--force"] if force else []), wt["path"])
    if r.returncode != 0:
        return False, "worktree %s not removed: %s" % (wt["path"], (r.stderr or r.stdout).strip()[:200])
    msg = "removed worktree %s" % wt["path"]
    if wt.get("branch") and wt["branch"] != "HEAD":
        msg += "; " + delete_branch(wt["common"], wt["branch"])
    return True, msg


def close(info, force=False, dry_run=False, self_=False, fetch=True):
    """Returns (code, lines). code 0 closed, 2 refused, 1 error."""
    who = info.get("name") or info.get("tmux")
    if self_ and not info.get("tmux") and not force:
        return 2, ["not closing: this session is not running in tmux, so agent-close cannot end it, and removing "
                   "the worktree would leave you running in a deleted directory. Exit the session yourself, or "
                   "pass --force to remove the worktree anyway."]
    wt = worktree(info.get("cwd"))
    lines, stale = [], None
    if wt["kind"] == "linked":
        dirty, ahead, base = unlanded(wt, fetch=fetch)
        problems = []
        if dirty:
            problems.append("%d uncommitted/untracked path(s): %s" % (len(dirty), "; ".join(l[3:] for l in dirty[:5])))
        if ahead != 0:
            problems.append("%s commit(s) on %s not in %s" % (ahead if ahead > 0 else "unknown", wt["branch"], base or "origin"))
        if problems and not force:
            return 2, ["not closing %s: %s. Land the work first, or pass --force to discard it." % (who, " and ".join(problems))]
        if problems:
            lines.append("discarding (--force): " + " and ".join(problems))
    elif wt["kind"] == "unknown":
        if not force:
            return 2, ["not closing %s: its working directory is unknown, so unlanded work can't be checked. "
                       "Pass --force to end the session anyway." % who]
        lines.append("working directory unknown; ending the session only (--force)")
    elif wt["kind"] == "missing":
        reg = find_registration(wt["path"])
        if reg:
            common, root, branch = reg
            g = lambda *a: gitd(common, *a)  # noqa: E731
            if fetch:
                gitd(common, "fetch", "--quiet", "origin", timeout=120)
            ahead = unmerged(g, default_base(g), branch) if branch else 0
            stale = (common, root, branch, ahead == 0 or force, ahead)
        else:
            lines.append("%s is already gone and no repo still lists it" % wt["path"])
    else:
        lines.append("left %s in place (%s)" % (wt["path"], "main checkout" if wt["kind"] == "main" else "not a git worktree"))
    if dry_run:
        what = ("remove worktree %s" % wt["path"]) if wt["kind"] == "linked" else (
            "prune stale worktree %s" % stale[1]) if stale else "remove nothing"
        return 0, lines + ["would end %s and %s" % (info.get("tmux") or "no tmux session", what)]
    if not self_:
        lines.append(end_session(info))
    ok = True
    if wt["kind"] == "linked":
        ok, msg = remove(wt, force=force)
        lines.append(msg)
    elif stale:
        common, root, branch, drop, ahead = stale
        gitd(common, "worktree", "prune")
        msg = "pruned stale worktree %s" % root
        if branch:
            msg += "; " + (delete_branch(common, branch) if drop else
                           "kept branch %s: %s commit(s) not landed (--force to delete)" % (branch, ahead))
        lines.append(msg)
    statuslog.append({"cli": info.get("cli", "?"), "session_id": info.get("session_id", ""), "name": info.get("name") or "",
                      "cwd": info.get("cwd") or "", "branch": wt.get("branch", ""), "event": "closed",
                      "text": "; ".join(lines)})
    if self_:
        lines.append(end_session(info, delay=2) if info.get("tmux") else
                     "session NOT ended (not in tmux): exit it yourself")
    return (0 if ok else 1), lines
