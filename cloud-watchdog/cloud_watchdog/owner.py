"""Owner resolution, deterministic first: registration > provider provenance > transcript search.

The investigator model (investigator.py) is only used by the ladder when this finds no owner session.
"""
import json
from pathlib import Path

from . import config, providers, transcripts, util


# The owner sources a session declared itself (a registration, a claim by ack or close). The others (transcript, model)
# are inferred: they never replace a declared owner, and another session may claim such a resource.
AUTHORITATIVE_SOURCES = ("registered", "claimed")


def assignment(*, session, cli, name, source, confidence, evidence, now):
    """The owner_* fields of a resource whose owner is `session` (None: not found), as found by `source` at `now`."""
    return {"owner_session": session, "owner_cli": cli, "owner_name": name, "owner_source": source,
            "owner_confidence": confidence, "owner_evidence": evidence, "owner_checked_at": now}


class AddressBookError(RuntimeError):
    """`agent-send --list --json` failed, so nobody can tell which sessions exist or are live."""


def sessions():
    """Address book from `agent-send --list --json`: {session_id: {name, cli, live, cwd}}.
    Raises AddressBookError, naming the cause, when agent-send fails or prints no JSON."""
    code, out, err = util.run(["agent-send", "--list", "--json"], timeout=60)
    if code != 0:
        raise AddressBookError("agent-send --list exited %s: %s" % (code, (err or out).strip()[-200:]))
    try:
        rows = json.loads(out)
    except ValueError:
        raise AddressBookError("agent-send --list printed no JSON: %r" % out.strip()[:200]) from None
    return {r.get("id"): r for r in rows if r.get("id")}


def repos_with_commit(commit, roots):
    """The git repos directly under these root directories that contain this commit (worktrees share objects)."""
    if not commit:
        return []
    hits = []
    for base in map(config.expand, roots):
        for d in sorted(base.iterdir()) if base.exists() else []:
            if (d / ".git").is_dir():
                code, _, _ = util.run(["git", "-C", str(d), "cat-file", "-e", commit + "^{commit}"], timeout=15)
                if code == 0:
                    hits.append(str(d.resolve()))
    return hits


def worktrees(repo):
    """The repo's worktree directories, from `git worktree list`. Raises RuntimeError when git fails."""
    code, out, err = util.run(["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=15)
    if code != 0:
        raise RuntimeError("git worktree list in %s exited %s: %s" % (repo, code, (err or out).strip()[-200:]))
    return [l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("worktree ")]


def search_terms(res):
    """What a launch command's transcript would show: the name, the id as the provider prints it, and the IP."""
    terms = [res["name"], providers.get(res["provider"]).ID_TERM % res["native_id"],
             json.loads(res["extra"] or "{}").get("ip")]
    return [t for t in terms if t and not t.endswith(providers.UNNAMED)]


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


def resolve(cfg, res, now, book):
    """The owner_* fields to store for `res` (owner_session None: not found), or {} when a session declared itself
    its owner and has not disowned it. After a transcript search that found candidates, the dict also holds
    `_candidates`: the top five, ranked. Reads the transcripts under transcripts.roots (grep) and runs git in the
    repos under repo_roots. `book` is the address book from sessions(), or None when it could not be read:
    candidates then get no live tie-break and no session names. Searches that failed are named in owner_evidence."""
    disowned = set(json.loads(res["disowned"] or "[]"))
    if res["owner_source"] in AUTHORITATIVE_SOURCES and res["owner_session"] not in disowned:
        return {}
    if res["ownership"] == "foreign":
        return {"owner_session": None, "owner_source": "foreign", "owner_confidence": "high",
                "owner_evidence": "deployed or created by %s" % (util.launched_by(res) or "someone else"), "owner_checked_at": now}
    since = (res["launched_at"] or res["first_seen"] or now) - cfg["transcripts"]["lookback_days"] * 86400
    cands, failures = transcripts.search(search_terms(res), since, cfg)
    repo_dirs = []
    for repo in repos_with_commit(res["git_commit"], cfg["repo_roots"]):
        try:
            repo_dirs += worktrees(repo)
        except RuntimeError as e:
            failures.append(str(e))
    book = book or {}
    cands = rank(cands, res["launched_at"], repo_dirs, disowned, book)
    if not cands:
        ev = "transcript search failed: %s" % "; ".join(failures) if failures else "no transcript mentions it"
        return {"owner_session": None, "owner_source": None, "owner_confidence": None,
                "owner_evidence": ev[:900], "owner_checked_at": now}
    best = cands[0]
    margin = best["score"] - (cands[1]["score"] if len(cands) > 1 else 0)
    conf = "high" if best["launch_hit"] and margin >= 5 else "medium" if best["launch_hit"] else "low"
    entry = book.get(best["session_id"]) or {}
    ev = "%s %s in %s: %s" % ("launched it" if best["launch_hit"] else "mentioned it",
                              util.iso(best["launch_ts"] or best["first_ts"]), best["cwd"] or "?", best["snippet"])
    if failures:
        ev = "incomplete search (%d failed: %s) | %s" % (len(failures), failures[0][:200], ev)
    if repo_dirs:
        ev += " | commit %s is in %s" % (res["git_commit"], ", ".join(sorted({str(Path(r).name) for r in repo_dirs[:1]})))
    others = ", ".join("%s(%.0f)" % (c["session_id"][:8], c["score"]) for c in cands[1:4])
    if others:
        ev += " | other candidates: " + others
    return dict(assignment(session=best["session_id"] if conf != "low" else None, cli=entry.get("cli") or best["cli"],
                           name=entry.get("name"), source="transcript", confidence=conf, evidence=ev[:900], now=now),
                _candidates=cands[:5])


def refresh(db, cfg, res, now, book, max_age_s, resolve):
    """Search again for the owner of `res` (its resources row), unless it has an owner session, is foreign, or was
    searched less than `max_age_s` ago. `resolve` is the search (resolve's signature; `book` as there). Saves the
    owner_* fields it returns and logs their evidence as an `owner` action. Returns (the resource's row now, the
    search's ranked candidates or None)."""
    if res["owner_session"] or res["ownership"] == "foreign":
        return res, None
    if res["owner_checked_at"] and now - res["owner_checked_at"] < max_age_s:
        return res, None
    upd = resolve(cfg, res, now, book)
    cands = upd.pop("_candidates", None)
    if upd:
        db.update_resource(res["key"], **upd)
        db.log("owner", upd.get("owner_evidence") or "-", key=res["key"], shadow=False, now=now)
    return db.resource(res["key"]), cands


def is_live(book, session_id):
    e = book.get(session_id) or {}
    return bool(e.get("live"))
