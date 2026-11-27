"""Close an agent: end its session and remove its git worktree once its work is landed.

Only a *linked* worktree is ever removed, never a main checkout or a plain directory; for those
only the session ends. Nothing is closed while the worktree has uncommitted or untracked files,
or commits whose changes are not on origin's default branch (a squash or rebase merge counts as
landed), unless forced; a git error in either check counts as unlanded work. A failed `git fetch`
is said in the output (the check then uses the last fetched refs), and a stale worktree git would
not prune is an error. Git-ignored files (downloads, PDFs) go with the worktree.
"""
import os
import re
import shlex
import subprocess
import time

from . import commands, procs, registry, statuslog


def git(repo, *args, timeout=60):
    return commands.run(["git", "-C", repo] + list(args), timeout=timeout)


def gitd(common, *args, timeout=60):
    """git against a repository's common dir: right for normal, bare and gitdir-file layouts."""
    return commands.run(["git", "--git-dir", common] + list(args), timeout=timeout)


PANE_ID = re.compile(r"^%\d+$")   # a tmux pane id, as $TMUX_PANE holds it


def tmux_self():
    """(the tmux session this process runs in, "") or (None, why it cannot be named): outside tmux; with a config that
    does not say which server agent-close ends sessions on (commands.tmux_socket); in a pane of another server ($TMUX's
    socket, its first field, is not that one), where pane ids restart at %0 so that $TMUX_PANE there would name another
    agent's pane here; with no pane id in $TMUX_PANE (tmux would answer for the session used last, perhaps another
    agent's); in a pane whose shell is not among this process's ancestors; or when tmux names none."""
    env = os.environ.get("TMUX")
    if not env:
        return None, "this session is not running in tmux"
    try:
        ours = commands.tmux_socket()
    except ValueError as e:
        return None, "the tmux server agent-close ends sessions on is not known: %s" % e
    sock = env.split(",")[0]
    if os.path.realpath(sock) != os.path.realpath(ours):
        return None, "this session runs in a pane of the tmux server at %s, not the one agent-close ends sessions on " \
                     "(%s)" % (sock, ours)
    pane = os.environ.get("TMUX_PANE", "")
    if not PANE_ID.match(pane):
        return None, "$TMUX_PANE is %s, not a pane id such as %%3, so this session's pane is not known" % (
            repr(pane) if pane else "not set")
    r = commands.tmux("display-message", "-p", "-t", pane, "#{pane_pid}\t#S", check=False)
    shell, _, name = r.stdout.strip("\n").partition("\t")
    if r.returncode != 0 or not name:
        return None, "tmux could not name this pane's session: %s" % ((r.stderr or "").strip() or "no answer")
    if not shell.isdigit() or int(shell) not in procs.ancestors(os.getpid()):
        return None, "the shell of pane %s (pid %s) did not start this process, so it is not this session's pane" % (
            pane, shell or "unknown")
    return name, ""


def tmux_cwd(name):
    # "=name:" (session, current window); a bare "=name" is read as a pane and matches nothing
    r = commands.tmux("display-message", "-p", "-t", "=%s:" % name, "#{pane_current_path}", check=False)
    return r.stdout.strip() or None


def find(target=None, self_=False):
    """(info, error). info: name, tmux (session name or None), cli, session_id, cwd; for self_, also tmux_why (why
    tmux is None: tmux_self).
    A NAME must match exactly (session name, session id or tmux name): this command deletes."""
    if self_:
        sess = registry.current_session() or {}
        tm, why = tmux_self()
        return {"name": sess.get("name") or tm or "", "tmux": tm, "tmux_why": why, "cli": sess.get("cli", "?"),
                "session_id": sess.get("id", ""), "cwd": os.getcwd()}, None
    sess, code, cands = registry.resolve(target)
    exact_tmux = commands.tmux_has(target)
    if code == 3 and not exact_tmux:
        return None, "%r is ambiguous: %s" % (target, ", ".join(sorted({c.get("name") or c["id"][:8] for c in cands})))
    if sess and target not in (sess.get("name"), sess.get("id")):
        if not exact_tmux:
            return None, "%r only partly matches %s; agent-close needs the exact session name or id" % (
                target, sess.get("name") or sess["id"][:8])
        sess = None   # an exact tmux name beats a fuzzy registry hit
    tm = target if exact_tmux else (sess["name"] if sess and commands.tmux_has(sess.get("name")) else None)
    if not sess and not tm:
        return None, "no session or tmux session named %r" % target
    cwd = (sess or {}).get("cwd") or (tmux_cwd(tm) if tm else None)
    return {"name": (sess or {}).get("name") or tm, "tmux": tm, "cli": (sess or {}).get("cli", "?"),
            "session_id": (sess or {}).get("id", ""), "cwd": cwd}, None


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
    common = commands.common_dir(top)
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
    """(count, why): commits on ref not landed in base, 0 if ancestors or squash/rebase-merged. An unknown
    count is -1, and why says why: no base branch, or git's error."""
    if not base:
        return -1, "no base branch"
    r = g("rev-list", "--count", "%s..%s" % (base, ref))
    if r.returncode != 0:
        return -1, "git rev-list failed: %s" % (r.stderr or r.stdout).strip()[:200]
    n = int(r.stdout.strip())
    return (0 if n and contained(g, base, ref) else n), None


def commits_text(ahead, why):
    """A commit count from unmerged() for messages: the number, or "unknown (why)"."""
    return str(ahead) if ahead >= 0 else "unknown (%s)" % why


def fetch_origin(run):
    """`git fetch --quiet origin` through run(*args, timeout=...) (git or gitd for one repo). None if it worked,
    else the line that says so: the landed check then uses the refs of the last fetch."""
    r = run("fetch", "--quiet", "origin", timeout=120)
    if r.returncode == 0:
        return None
    said = (r.stderr or r.stdout).strip().splitlines() or ["exit %d" % r.returncode]
    return "could not fetch origin (%s); checked against the last fetched refs" % said[0][:200]


def unlanded(wt, fetch=True):
    """What would be lost by removing a linked worktree, after `git fetch origin` unless fetch is False:
    (dirty lines, unmerged commit count, why the count is unknown or None, base, git's error if git status
    failed, else None, fetch_origin's line if the fetch failed, else None)."""
    g = lambda *a, **kw: git(wt["path"], *a, **kw)  # noqa: E731
    fetch_note = fetch_origin(g) if fetch else None
    base = default_base(g)
    st = g("status", "--porcelain")
    dirty = [l for l in st.stdout.splitlines() if l.strip()]
    status_error = (st.stderr or st.stdout).strip()[:200] if st.returncode != 0 else None
    ahead, why = unmerged(g, base, "HEAD")
    return dirty, ahead, why, base, status_error, fetch_note


def _dirs(d):
    try:
        return sorted(e.path for e in os.scandir(d) if e.is_dir() and not e.name.startswith("."))
    except OSError:
        return []


def registered(common):
    """[(root, branch)] for each worktree the repository at common dir lists (git worktree list)."""
    out = []
    for block in gitd(common, "worktree", "list", "--porcelain").stdout.split("\n\n"):
        fields = dict(l.split(" ", 1) for l in block.splitlines() if " " in l)
        if fields.get("worktree"):
            out.append((fields["worktree"], fields.get("branch", "").replace("refs/heads/", "")))
    return out


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
        common = commands.common_dir(c)
        if not common or common in seen:   # not a git repo, or one already searched
            continue
        seen.add(common)
        for root, branch in registered(common):
            if p == root or p.startswith(root.rstrip("/") + "/"):
                return common, root, branch
    return None


def end_session(info, delay=0):
    """End the agent's tmux session. With delay, detach and do it later (used by --self, so the
    calling agent still gets this command's output before its own session goes)."""
    if not info.get("tmux"):
        return "no tmux session to end"
    if delay:
        subprocess.Popen(["sh", "-c", "sleep %d; tmux -S %s kill-session -t '=%s'"
                          % (delay, shlex.quote(commands.tmux_socket()), info["tmux"].replace("'", ""))],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env={k: v for k, v in os.environ.items() if k != "TMUX"})
        return "tmux session %s ends in %ds" % (info["tmux"], delay)
    commands.tmux("kill-session", "-t", "=" + info["tmux"], check=False)
    for _ in range(20):
        if not commands.tmux_has(info["tmux"]):
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
        return 2, ["not closing: %s, so agent-close cannot end it, and removing the worktree would leave you running "
                   "in a deleted directory. Exit the session yourself, or pass --force to remove the worktree anyway."
                   % (info.get("tmux_why") or "this session is not running in tmux")]
    wt = worktree(info.get("cwd"))
    lines, stale = [], None
    if wt["kind"] == "linked":
        dirty, ahead, why, base, status_error, fetch_note = unlanded(wt, fetch=fetch)
        if fetch_note:
            lines.append(fetch_note)
        problems = []
        if status_error:
            problems.append("git status failed, so uncommitted work can't be ruled out (%s)" % status_error)
        if dirty:
            problems.append("%d uncommitted/untracked path(s): %s" % (len(dirty), "; ".join(l[3:] for l in dirty[:5])))
        if ahead != 0:
            problems.append("%s commit(s) on %s not in %s" % (commits_text(ahead, why), wt["branch"], base or "origin"))
        if problems and not force:
            return 2, ["not closing %s: %s. Land the work first, or pass --force to discard it." % (
                who, " and ".join(problems))] + lines
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
            g = lambda *a, **kw: gitd(common, *a, **kw)  # noqa: E731
            fetch_note = fetch_origin(g) if fetch else None
            if fetch_note:
                lines.append(fetch_note)
            ahead, why = unmerged(g, default_base(g), branch) if branch else (0, None)
            stale = (common, root, branch, ahead == 0 or force, commits_text(ahead, why))
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
        common, root, branch, drop, ahead_text = stale
        r = gitd(common, "worktree", "prune")
        if r.returncode != 0 or root in [w for w, _ in registered(common)]:   # prune can warn and still exit 0
            ok = False
            lines.append("stale worktree %s not pruned: %s" % (root, (r.stderr or r.stdout).strip()[:200]
                                                               or "git still lists it"))
        else:
            msg = "pruned stale worktree %s" % root
            if branch:
                msg += "; " + (delete_branch(common, branch) if drop else
                               "kept branch %s: %s commit(s) not landed (--force to delete)" % (branch, ahead_text))
            lines.append(msg)
    statuslog.append({"cli": info.get("cli", "?"), "session_id": info.get("session_id", ""), "name": info.get("name") or "",
                      "cwd": info.get("cwd") or "", "branch": wt.get("branch", ""), "event": "closed",
                      "text": "; ".join(lines)})
    if self_:
        lines.append(end_session(info, delay=2) if info.get("tmux") else
                     "session NOT ended (not in tmux): exit it yourself")
    return (0 if ok else 1), lines
