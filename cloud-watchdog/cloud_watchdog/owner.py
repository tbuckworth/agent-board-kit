"""Owner resolution, deterministic first: registration > provider provenance > transcript search.

The investigator model (investigator.py) is only used by the ladder when this returns nothing
confident.
"""
import json
from pathlib import Path

from . import config, transcripts, util


def sessions():
    """Address book from agent-send: {session_id: {name, cli, live, cwd}}."""
    code, out, _ = util.run(["agent-send", "--list", "--json"], timeout=60)
    try:
        rows = json.loads(out) if code == 0 else []
    except ValueError:
        rows = []
    return {r.get("id"): r for r in rows if r.get("id")}


def repos_with_commit(commit, repos_root="~/code"):
    """Top-level git repos under repos_root that contain this commit (worktrees share objects)."""
    if not commit:
        return []
    hits = []
    base = config.expand(repos_root)
    for d in sorted(base.iterdir()) if base.exists() else []:
        if (d / ".git").is_dir():
            code, _, _ = util.run(["git", "-C", str(d), "cat-file", "-e", commit + "^{commit}"], timeout=15)
            if code == 0:
                hits.append(str(d.resolve()))
    return hits


def worktrees(repo):
    code, out, _ = util.run(["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=15)
    return [l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("worktree ")]


def search_terms(res):
    extra = json.loads(res["extra"] or "{}")
    terms = [res["name"], res["native_id"]]
    if res["provider"] == "lambda":
        terms.append(extra.get("ip"))
    if res["provider"] == "slurm":
        terms = ["Submitted batch job %s" % res["native_id"], res["name"]]
    return [t for t in terms if t and not t.endswith("(unnamed)")]


def rank(cands, launched_at, repo_dirs, disowned, book=None):
    book = book or {}
    for c in cands:
        s = 0.0
        if c["launch_hit"]:
            s += 10
        t = c["launch_ts"] or c["first_ts"]
        if launched_at and t and -2 * 3600 <= launched_at - t <= 2 * 3600:
            s += 5
        if c["cwd"] and any(c["cwd"] == w or c["cwd"].startswith(w.rstrip("/") + "/") for w in repo_dirs):
            s += 5
        s += min(c["hits"], 5) * 0.2
        if (book.get(c["session_id"]) or {}).get("live"):
            s += 1  # tie-break toward a session that can still answer
        if c["session_id"] in disowned:
            s = -1
        c["score"] = s
    cands = [c for c in cands if c["score"] >= 0]
    cands.sort(key=lambda c: (-c["score"], c["launch_ts"] or c["first_ts"] or 0))
    return cands


def resolve(db, cfg, res, now, book=None):
    """Returns a dict of owner_* fields (possibly with owner_session None)."""
    disowned = set(json.loads(res["disowned"] or "[]"))
    if res["owner_source"] in ("registered", "human", "claimed") and res["owner_session"] not in disowned:
        return {}
    if res["ownership"] == "foreign":
        return {"owner_session": None, "owner_source": "foreign", "owner_confidence": "high",
                "owner_evidence": "deployed or created by %s" % (util.modal_user(res) or "someone else"), "owner_checked_at": now}
    since = (res["launched_at"] or res["first_seen"] or now) - cfg["transcripts"]["lookback_days"] * 86400
    cands = transcripts.search(search_terms(res), since, cfg, res["launched_at"])
    repo_dirs = []
    for repo in repos_with_commit(res["git_commit"], cfg.get("repos_root") or "~/code"):
        repo_dirs += worktrees(repo)
    book = book if book is not None else sessions()
    cands = rank(cands, res["launched_at"], repo_dirs, disowned, book)
    if not cands:
        return {"owner_session": None, "owner_source": None, "owner_confidence": None,
                "owner_evidence": "no transcript mentions it", "owner_checked_at": now}
    best = cands[0]
    margin = best["score"] - (cands[1]["score"] if len(cands) > 1 else 0)
    conf = "high" if best["launch_hit"] and margin >= 5 else "medium" if best["launch_hit"] else "low"
    entry = book.get(best["session_id"]) or {}
    ev = "%s %s in %s: %s" % ("launched it" if best["launch_hit"] else "mentioned it",
                              util.iso(best["launch_ts"] or best["first_ts"]), best["cwd"] or "?", best["snippet"])
    if repo_dirs:
        ev += " | commit %s is in %s" % (res["git_commit"], ", ".join(sorted({str(Path(r).name) for r in repo_dirs[:1]})))
    others = ", ".join("%s(%.0f)" % (c["session_id"][:8], c["score"]) for c in cands[1:4])
    if others:
        ev += " | other candidates: " + others
    return {"owner_session": best["session_id"] if conf != "low" else None,
            "owner_cli": entry.get("cli") or best["cli"], "owner_name": entry.get("name"),
            "owner_source": "transcript", "owner_confidence": conf, "owner_evidence": ev[:900],
            "owner_checked_at": now, "_candidates": cands[:5]}


def is_live(book, session_id):
    e = book.get(session_id) or {}
    return bool(e.get("live"))
