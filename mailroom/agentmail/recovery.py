"""Bounded, read-only ownership search. A text match is a lead, never a routing decision."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import alerts, registry, remote, routing, statuslog


def search(queries, cfg):
    if not isinstance(queries, list) or not 1 <= len(queries) <= 8:
        raise ValueError("provide one to eight literal queries")
    if any(not isinstance(q, str) or not 8 <= len(q) <= 500 for q in queries):
        raise ValueError("queries must be between 8 and 500 characters")
    deadline = time.monotonic() + min(20, cfg.get("recovery_seconds", 20))
    cutoff = time.time() - min(30, cfg.get("recovery_days", 14)) * 86400
    remaining = 64 * 1024 * 1024
    candidates, checked, limits = {}, 0, set()
    sessions = registry.all_sessions()
    known = {(s["cli"], s["id"]): s for s in sessions}
    needles = [q.casefold() for q in queries]

    def add(cli, sid, path, found, cwd=""):
        key = (cli, sid or str(path))
        s = known.get((cli, sid), {})
        c = candidates.setdefault(key, {"cli": cli, "session_id": sid or None,
                                       "name": s.get("name", ""), "cwd": s.get("cwd") or cwd,
                                       "live_hint": s.get("live", False), "files": [], "queries": []})
        if path and str(path) not in c["files"]:
            c["files"].append(str(path))
        c["queries"] = sorted(set(c["queries"] + found))

    for s in sessions:
        hay = "\n".join(s.get(k, "") or "" for k in ("id", "name", "cwd", "title")).casefold()
        found = [q for q, needle in zip(queries, needles) if needle in hay]
        if found:
            add(s["cli"], s["id"], None, found)

    # Status notes can retain the owner after its live registry entry disappears.
    try:
        with statuslog.LOG.open(errors="replace") as fh:
            for line in fh:
                if time.monotonic() >= deadline:
                    limits.add("status search time budget reached")
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("ts", 0) < cutoff:
                    continue
                found = [q for q, needle in zip(queries, needles) if needle in line.casefold()]
                if found:
                    cli, sid = event.get("cli", "status"), event.get("session_id", "")
                    add(cli, sid, statuslog.LOG, found, event.get("cwd", ""))
    except OSError:
        limits.add("status log unavailable")

    def scan(path, cli, sid="", cwd=""):
        nonlocal remaining, checked
        if time.monotonic() >= deadline or remaining <= 0 or len(candidates) >= 20 or checked >= 1000:
            limits.add("time/byte/file/result budget reached")
            return False
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_mtime < cutoff:
                return True
            size = path.stat().st_size
            cap = min(8 * 1024 * 1024, remaining)
            with path.open("rb") as fh:
                raw = fh.read(cap)
            remaining -= len(raw)
            checked += 1
            if size > cap:
                limits.add("one or more files exceeded scan byte limit")
            text = raw.decode("utf-8", errors="replace")
            found = [q for q, needle in zip(queries, needles) if needle in text.casefold()]
            if found:
                if cli == "codex":
                    try:
                        meta = json.loads(text.splitlines()[0]).get("payload", {})
                        sid, cwd = meta.get("id", sid), meta.get("cwd", cwd)
                    except (ValueError, IndexError):
                        pass
                add(cli, sid, path, found, cwd)
        except OSError:
            limits.add("one or more files could not be read")
        return True

    roots = [(registry.CODEX_HOME / "sessions", "codex"),
             (registry.CODEX_HOME / "archived_sessions", "codex")]
    roots += [(home / "projects", "claude") for _, home, _ in registry.claude_homes()]
    for root, cli in roots:
        if not root.exists():
            limits.add("missing " + str(root))
            continue
        for directory, dirs, files in os.walk(str(root), followlinks=False):
            dirs.sort(reverse=True)  # recent dated Codex directories first
            if time.monotonic() >= deadline:
                limits.add("time budget reached")
                break
            for name in sorted(files, reverse=True):
                if not name.endswith(".jsonl"):
                    continue
                if not scan(Path(directory) / name, cli, name[:-6] if cli == "claude" else ""):
                    break

    # Only text documentation in recent known worktrees; never credential files or caches.
    worktrees = sorted({s.get("cwd") for s in sessions if s.get("cwd") and s.get("updated_at", 0) >= cutoff})
    for cwd in worktrees:
        if time.monotonic() >= deadline or remaining <= 0 or checked >= 1000:
            limits.add("worktree search budget reached")
            break
        root = Path(cwd)
        if root == Path.home() or not (root / ".git").exists():
            continue
        for directory, dirs, files in os.walk(str(root), followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                       ("node_modules", "venv", "data", "logs", "checkpoints", "dist", "build")]
            if time.monotonic() >= deadline:
                limits.add("time budget reached")
                break
            for name in files:
                if Path(name).suffix not in (".md", ".txt") or re.search(r"secret|token|credential|auth", name, re.I):
                    continue
                if not scan(Path(directory) / name, "artifact", cwd=cwd):
                    break
    return {"host": cfg["host_id"], "candidates": list(candidates.values())[:20],
            "files_checked": checked, "limits": sorted(limits),
            "note": "Literal matches only; do not deliver to a candidate without verified ownership."}


def default_queries(st, inb):
    sent = st.sent_in_thread(inb["thread_id"])
    subject = (sent["subject"] if sent else inb["subject"]) or ""
    subject = re.sub(r"^(?:(?:re|fwd?):\s*)+", "", subject, flags=re.I)
    subject = re.sub(r"\s*\[#[a-z0-9]+\]", "", subject).strip()
    values = [subject, sent["message_id"] if sent else None]
    return [v for v in values if v and 8 <= len(v) <= 500]


def recover(st, cfg, inb, queries=None, refresh=False):
    if routing.sender_tier(inb["from_addr"], cfg) is None or (inb["detail"] or "").startswith("DKIM check failed"):
        raise ValueError("recovery is only available for verified owner/collaborator mail")
    if inb["recovery_json"] and not refresh:
        return json.loads(inb["recovery_json"])
    queries = list(dict.fromkeys(default_queries(st, inb) + (queries or [])))[:8]
    result = {"queries": queries, "checks": [], "time": int(time.time()), "status": "needs_handoff"}
    origin = st.session_by_tag(inb["matched_tag"]) if inb["matched_tag"] else None
    if origin:
        result["origin"] = {k: origin[k] for k in ("origin_host", "cli", "session_id", "session_name", "cwd")}
    if not queries:
        result["checks"].append({"host": cfg["host_id"], "error": "no sufficiently specific query; add --query with an exact title or artifact ID"})
    else:
        # A subprocess bounds even a slow filesystem or session registry read.
        try:
            cmd = [sys.executable, str(Path(__file__).resolve().parent.parent / "bin/agent-mail"), "search-local"]
            r = subprocess.run(cmd, input=json.dumps(queries), text=True, capture_output=True, timeout=25)
            if r.returncode:
                raise ValueError("local search failed: " + r.stderr[:300])
            local = json.loads(r.stdout)
            result["checks"].append(local)
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            result["checks"].append({"host": cfg["host_id"], "error": str(e)[:500]})
        # Local matches are not proof of ownership, so the orchestrator may still need the Mac.
        for host in cfg.get("recovery_hosts", []):
            try:
                result["checks"].append(remote.call(cfg, host, {"op": "search", "queries": queries}))
            except remote.RemoteError as e:
                result["checks"].append({"host": host, "error": str(e)})
    st.update_inbound(inb["id"], action="held", recovery_json=json.dumps(result),
                      detail="ownership unresolved; read-only recovery saved; explicit handoff needed")
    return result


def hold_delivery(st, inb, session, detail):
    result = {"status": "delivery_failed", "checks": [{"host": session["origin_host"], "error": detail}],
              "origin": {k: session[k] for k in ("origin_host", "cli", "session_id", "session_name", "cwd")}}
    st.update_inbound(inb["id"], action="held", detail=detail, recovery_json=json.dumps(result))


def notify(svc, st, cfg, inb):
    """One successful notification per inbound item; failed sends remain retryable."""
    if inb["notified_at"] or not inb["recovery_json"] or inb["action"] != "held":
        return False
    prior = st.get_meta("alert:reply-recovery:%s" % inb["id"])
    if prior:
        st.update_inbound(inb["id"], notified_at=int(float(prior)))
        return False
    result = json.loads(inb["recovery_json"])
    origin = result.get("origin")
    if origin:
        who = origin.get("session_name") or origin["session_id"][:8]
        happened = "I couldn't confirm that your reply reached %s on %s." % (who, origin["origin_host"])
        if "original working folder no longer exists" in (inb["detail"] or ""):
            happened += " Its working folder has been removed, so I did not restart it elsewhere."
            action = "Reply here to tell the orchestrator how you want to continue the work."
        else:
            happened += " The session may have received it before the error."
            action = ("If the session has already acted on your reply, no action is needed. Otherwise, reply here "
                      "and ask the orchestrator to check delivery and arrange a handoff. Check before retrying "
                      "so the same instruction isn't sent twice.")
    else:
        happened = "I couldn't identify which session should receive your reply."
        action = "Reply here with the intended session or document so the orchestrator can arrange a handoff."
    body = ("%s\n\nNothing has been lost: your email is saved in Gmail and in the mailroom. "
            "You don't need to resend it.\n\n%s\n\n"
            "Your reply: %s\nFrom: %s\n"
            "Open email: https://mail.google.com/mail/?authuser=%s#all/%s\n\n"
            "For diagnosis: %s\nSaved mail: agent-mail show %s\n") % (
                happened, action, inb["subject"], inb["from_addr"], cfg["account"], inb["thread_id"],
                inb["detail"] or "recipient unknown", inb["id"])
    if origin:
        body += "Original session: %s (%s)\n" % (origin["session_id"], origin["origin_host"])
    if alerts.send_alert(svc, st, cfg, "reply-recovery", str(inb["id"]),
                         "reply #%s needs attention: %s" % (inb["id"], inb["subject"]), body):
        st.update_inbound(inb["id"], notified_at=int(time.time()))
        return True
    return False
