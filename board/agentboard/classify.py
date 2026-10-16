"""Pure rules: what state an agent is in, what to call it, and whether it is waiting on the owner.

Everything here takes plain dicts plus `now`, so tests can feed fixtures. No I/O.

States, in the order the legend shows them:
  busy     a turn is running
  background  Claude "shell": its turn ended but a background command it started is still running, so it is
           only waiting on that (it wakes when it finishes), told apart from real work.
           Not when every such command is a `pgrep -f` loop that finds itself and its target has gone: idle
  waiting  idle, and its latest mail to the owner has no reply yet (the owner must act)
  blocked  stuck at a terminal prompt, hit a usage limit, or its last note says it is blocked
  idle     live and idle, active within STALE_AFTER
  stale    live and idle for longer than STALE_AFTER
  offline  the process is not running. The only state for a dead session; every other state is live.
           Shown while it matters: recently exited after real work, or mail in flight with it
           ("stranded": the owner replied after it stopped, so nothing is reading the reply)
"""
import functools
import os
import re

STATES = ["busy", "background", "waiting", "blocked", "idle", "stale", "offline"]

STALE_AFTER = 6 * 3600          # idle longer than this is "stale"
SETTLE = 15 * 60                # a mail/note counts as the agent's last word if no turn ended this long after it
MAIL_WINDOW = 7 * 86400         # only mail this recent can make an agent "waiting"
OFFLINE_WINDOW = 12 * 3600      # an exited session that did real work (notes or mail) stays this long
MAIL_OFFLINE_WINDOW = 48 * 3600 # ...or this long while mail is in flight either way
CLOSED_WINDOW = 3600            # a deliberately closed session (agent-close) lingers this long

BLOCKED_WORDS = r"you|approval|input"


@functools.lru_cache(maxsize=8)
def blocked_note(owner=None):
    """A note that says the agent is blocked: blocked/stuck, or waiting on / needs you, approval, input, or the
    owner by name (mailroom config owner_name)."""
    who = BLOCKED_WORDS + ("|" + re.escape(owner.strip()) if owner and owner.strip() else "")
    return re.compile(r"^\W*(blocked|blocker|stuck|waiting (on|for) (%s)|needs? (%s))\b" % (who, who), re.I)


BLOCKED_NOTE = blocked_note()
GENERIC_TITLE = re.compile(r"^\s*(you were started by agent-spawn|#\s*brief\b)", re.I)
TRUNK = ("main", "master", "HEAD", "")


def fmt_age(seconds):
    """Compact age: 45s, 12m, 3.4h, 2d."""
    if seconds is None:
        return "-"
    d = max(0, int(seconds))
    if d < 90:
        return "%ds" % d
    if d < 5400:
        return "%dm" % (d // 60)
    if d < 172800:
        return "%.1fh" % (d / 3600)
    return "%dd" % (d // 86400)


def display_name(a):
    """Session name, else repo@branch, else a useful Codex title, else the directory."""
    name = (a.get("name") or "").strip()
    if name:
        return name
    repo, branch = a.get("repo") or "", a.get("branch") or ""
    if repo:
        return repo if branch in TRUNK else "%s@%s" % (repo, branch)
    title = (a.get("title") or "").strip().splitlines()[0] if (a.get("title") or "").strip() else ""
    if title and not GENERIC_TITLE.match(title):
        return title[:40]
    base = os.path.basename((a.get("cwd") or "").rstrip("/"))
    return base or (a.get("id") or "?")[:8]


def disambiguate(agents):
    """Give duplicate display names a short id suffix (Codex UUIDv7 prefixes collide, so use the tail)."""
    seen = {}
    for a in agents:
        seen.setdefault(a["display"], []).append(a)
    for name, group in seen.items():
        if len(group) > 1:
            for a in group:
                a["display"] = "%s · %s" % (name, (a.get("id") or "")[-4:])
    return agents


def mail_state(sent, inbound, now, window=MAIL_WINDOW):
    """One session's mail picture from the mailroom store.

    sent     rows this session sent to the owner: {sent_at, subject, thread_id}
    inbound  rows from the owner routed to this session: {received_at, subject, action, id}
    Returns {last_out, last_out_subject, last_in, unanswered, held, threads}; unanswered means the last
    word is the agent's: its newest mail to the owner is newer than anything the owner sent it.
    """
    since = now - window
    sent = sorted((s for s in sent if (s.get("sent_at") or 0) >= since), key=lambda s: s["sent_at"])
    inbound = sorted((i for i in inbound if (i.get("received_at") or 0) >= since), key=lambda i: i["received_at"])
    last_out = sent[-1] if sent else None
    last_in = inbound[-1] if inbound else None
    unanswered = bool(last_out) and (not last_in or last_out["sent_at"] > last_in["received_at"])
    threads = {}
    for s in sent:
        t = threads.setdefault(s.get("thread_id") or s.get("subject"), {"subject": s.get("subject") or "", "last_out": 0, "last_in": 0})
        t["last_out"] = max(t["last_out"], s["sent_at"])
        t["subject"] = s.get("subject") or t["subject"]
    for i in inbound:
        t = threads.setdefault(i.get("thread_id") or i.get("subject"), {"subject": i.get("subject") or "", "last_out": 0, "last_in": 0})
        t["last_in"] = max(t["last_in"], i["received_at"])
    ordered = sorted(threads.values(), key=lambda t: -max(t["last_out"], t["last_in"]))[:6]
    for t in ordered:
        t["subject"] = strip_tag(t["subject"])
        t["unanswered"] = t["last_out"] > t["last_in"]
    return {
        "last_out": last_out["sent_at"] if last_out else None,
        "last_out_subject": strip_tag(last_out.get("subject") or "") if last_out else "",
        "last_out_id": (last_out.get("message_id") or "") if last_out else "",
        "read": None,             # filled in from the owner's Gmail when that check is on: True / False / None
        "last_in": last_in["received_at"] if last_in else None,
        "unanswered": unanswered,
        "held": sum(1 for i in inbound if i.get("action") == "held"),
        "threads": ordered,
    }


def keep_offline(ev, mail, last_activity, now):
    """Should an exited session stay on the board? ev: its aggregated events; mail: mail_state()."""
    age = now - (last_activity or 0)
    if mail.get("last_in") and mail["last_in"] > (last_activity or 0) and now - mail["last_in"] < MAIL_OFFLINE_WINDOW:
        return True                       # the owner answered after it stopped: the reply needs a running agent
    if ev.get("closed"):
        return age < CLOSED_WINDOW
    if mail.get("unanswered") and now - (mail.get("last_out") or 0) < MAIL_OFFLINE_WINDOW:
        return True
    return age < OFFLINE_WINDOW and bool(ev.get("notes") or mail.get("last_out"))


def strip_tag(subject):
    return re.sub(r"\s*\[#[a-z0-9]{6,12}\]\s*$", "", subject or "").strip()


def classify(a, now, owner=None):
    """(state, reason) for one agent dict. Fields used:
    live, raw_status, waiting_for, last_activity, last_turn_end, last_note {ts,text}, quota_ts, mail {...}.
    owner: the owner's first name, so a note saying "waiting on <name>" also reads as blocked."""
    mail = a.get("mail") or {}
    turn_end = a.get("last_turn_end") or 0
    if not a.get("live"):
        last_in = mail.get("last_in") or 0
        if last_in > (a.get("last_activity") or 0):
            return "offline", "your reply %s ago is stranded: it is not running" % fmt_age(now - last_in)
        if a.get("closed"):
            return "offline", "closed"
        if mail.get("unanswered"):
            return "offline", "exited; its last mail to you is unanswered, a reply would need a resume"
        return "offline", "not running"
    raw = a.get("raw_status") or ""
    if raw == "waiting":
        wf = (a.get("waiting_for") or "").strip()
        return "blocked", "waiting at its terminal" + (": " + wf if wf else "")
    if raw == "busy":
        return "busy", "turn running"
    dead = ""
    if raw == "shell":
        w = a.get("bg_waits") or {}
        stuck = w.get("stuck") or []
        if not stuck or len(stuck) < w.get("shells", 0) or any(s["target"] for s in stuck):
            if stuck:
                return "background", ("turn ended; waiting on a background command it started, but its wait for `%s` "
                                      "will not end even when that stops: `pgrep -f` finds the waiting loop itself"
                                      % stuck[0]["pattern"])
            return "background", "turn ended; only waiting on a background command it started (it wakes when that finishes)"
        # every background command it has is a `pgrep -f` loop that only finds itself, and what it was meant to wait
        # for has gone: nothing will wake it, so it is idle
        dead = ("its background wait for `%s` can never end (`pgrep -f` finds the loop itself), and nothing it waits "
                "for is running · " % "`, `".join(s["pattern"] for s in stuck)) if len(stuck) == 1 else (
                "its %d background waits (for `%s`) can never end (`pgrep -f` finds each loop itself), and nothing "
                "they wait for is running · " % (len(stuck), "`, `".join(s["pattern"] for s in stuck)))
    # whatever stopped the last turn: the newest of a plan limit and an API error, unless a failover to
    # another login (or a finished turn) came after it
    q = a.get("quota_ts") or 0
    err = a.get("error") or {}
    fo = (a.get("failover") or {}).get("ts", 0)
    if q and q >= turn_end and q > fo and q >= err.get("ts", 0):
        lim = a.get("limit") or {}
        if lim.get("kind") == "limit-wait":
            return "blocked", "hit its plan limit and no other login has headroom; waiting for the reset" + (
                " (%s)" % lim["text"] if lim.get("text") else "")
        return "blocked", "hit a plan limit %s ago" % fmt_age(now - q) + (": " + lim["text"] if lim.get("text") else "")
    if err.get("ts") and err["ts"] >= turn_end and err["ts"] > fo:
        return "blocked", "its last turn ended on an API error" + (": " + err["text"] if err.get("text") else "")
    note = a.get("last_note") or {}
    if note.get("text") and blocked_note(owner).match(note["text"]) and turn_end <= note.get("ts", 0) + SETTLE:
        return "blocked", "last note: " + note["text"][:120]
    out = mail.get("last_out")
    owes = mail.get("unanswered") and out and turn_end <= out + SETTLE
    if owes and mail.get("read") is not True:          # read in Gmail: the owner has seen it, so it stops shouting
        return "waiting", "mailed you %s ago, no reply yet: %s" % (fmt_age(now - out), mail.get("last_out_subject") or "")
    age = now - (a.get("last_activity") or 0)
    seen = dead + ("you've read its mail, no reply yet · " if owes else "")
    f = a.get("failover") or {}
    if f.get("ts") and f["ts"] >= turn_end:
        seen += "moved to %s after a limit · " % (f.get("account") or "another login")
    if age > STALE_AFTER:
        return "stale", seen + "idle %s" % fmt_age(age)
    return "idle", seen + "idle %s" % fmt_age(age)


def login_note(a, accounts, order, thresholds, now):
    """Why a live Claude agent's tile gets a red star, or None: it runs on a later login in the mailroom's
    usage.order while the first login has room for its model by the thresholds routing uses (usage.assess:
    every window it counts at or above min_remaining). With its model unknown, every model window must have room.
    accounts: the snapshot's usage accounts; order: Claude login names, first preferred."""
    acct = a.get("account") or ""
    if not a.get("live") or a.get("cli") != "claude" or not order or acct == order[0] or acct not in order:
        return None
    home = order[0]
    snap = next((u for u in accounts or [] if u.get("target") == "claude:" + home), None)
    if not snap or not snap.get("windows") or snap.get("blocked"):
        return None
    model = (a.get("model") or "").lower()
    parts = []
    for w in snap["windows"]:
        kind = w.get("kind") or ""
        if kind not in ("session", "weekly") and model and not (w.get("model") and w["model"].lower() in model):
            continue
        left = w["remaining_pct"] if w.get("remaining_pct") is not None else 100 - (w.get("used_pct") or 0)
        if left <= 0 or left < thresholds.get("session" if kind.endswith("session") else "weekly", 0):
            return None
        parts.append("%s %d%% left" % (w.get("name") or kind, left))
    text = "Running on %s while %s has room for %s (%s)." % (acct, home, a.get("model") or "it", ", ".join(parts))
    f = a.get("failover") or {}
    if f.get("account") == acct:
        text += " agent-failover moved it here %s ago and moves it back after 5 minutes idle" % fmt_age(
            now - (f.get("ts") or now))
        text += ("; its background commands are running, so it isn't idle yet." if a.get("raw_status") == "shell"
                 else ".")
    else:
        text += " agent-failover didn't move it there, so it won't move it back."
    return text + " Switch… on the tile's menu moves it now."


def stranded(a):
    """The owner answered after the agent stopped: nothing is reading the reply until it is resumed."""
    return not a.get("live") and ((a.get("mail") or {}).get("last_in") or 0) > (a.get("last_activity") or 0)


def git_stage(g, pr=None):
    """Where the agent's work is on the way to main: uncommitted -> unpushed -> pushed -> PR -> merged.

    g: git facts (dirty, untracked, ahead, upstream, not_in_default, oid, branch); pr: the branch's newest PR
    ({number, state, isDraft, headRefOid, url}) or None. Returns {stage, chips, summary, pr} where stage is
    the least-landed step: uncommitted, unpushed, pushed (no PR), pr, pr_closed, merged or clean."""
    if not g:
        return None
    detached = (g.get("branch") or "HEAD") == "HEAD"
    trunk = (g.get("branch") or "") in ("main", "master")     # a detached HEAD is not trunk: its commits may be nowhere else
    if g.get("dirty") is None:
        return {"stage": "unknown", "chips": ["git ?"], "summary": "git status did not answer in time",
                "pr": None}
    dirty, untracked = g.get("dirty") or 0, g.get("untracked") or 0
    nid = g.get("not_in_default")
    if not trunk and not g.get("upstream") and nid is None:
        chips = ["±%d" % (dirty + untracked)] if dirty or untracked else []
        return {"stage": "uncommitted" if chips else "unknown", "chips": chips + ["no origin"],
                "summary": ("%d uncommitted · " % (dirty + untracked) if chips else "")
                + "no upstream and no origin/main to compare against", "pr": None}
    if g.get("upstream") and g.get("ahead") is not None and not detached:
        unpushed = g["ahead"]
    elif not trunk:
        unpushed = nid or 0           # never pushed (or detached): everything off main is local
    else:
        unpushed = 0
    chips, parts = [], []
    if dirty or untracked:
        chips.append("±%d" % (dirty + untracked))
        parts.append(" + ".join(x for x in ("%d uncommitted" % dirty if dirty else "",
                                             "%d untracked" % untracked if untracked else "") if x))
    if unpushed:
        chips.append("↑%d" % unpushed)
        parts.append("%d unpushed commit%s" % (unpushed, "" if unpushed == 1 else "s"))
    merged = False
    if pr:
        n, state = pr.get("number"), (pr.get("state") or "").upper()
        if state == "OPEN":
            chips.append(("draft #%d" if pr.get("isDraft") else "PR #%d") % n)
            parts.append(("draft PR #%d" if pr.get("isDraft") else "PR #%d open") % n)
        elif state == "MERGED":
            default = (g.get("default") or "").split("/", 1)[-1]
            into_main = not pr.get("baseRefName") or not default or pr.get("baseRefName") == default
            same_head = not pr.get("headRefOid") or pr.get("headRefOid") == g.get("oid")
            merged = into_main and same_head
            if not into_main:
                chips.append("merged #%d→%s" % (n, pr.get("baseRefName")))
                parts.append("PR #%d merged into %s, not %s" % (n, pr.get("baseRefName"), default))
            else:
                chips.append("merged #%d" % n if merged else "merged #%d +new" % n)
                parts.append("PR #%d merged" % n + ("" if merged else ", with newer commits since"))
        else:
            chips.append("closed #%d" % n)
            parts.append("PR #%d closed unmerged" % n)
    elif not trunk and nid and not unpushed:
        chips.append("no PR")
        parts.append("pushed, %d commit%s not in main, no PR" % (nid, "" if nid == 1 else "s"))
    if dirty or untracked:
        stage = "uncommitted"
    elif unpushed:
        stage = "unpushed"
    elif pr and (pr.get("state") or "").upper() == "OPEN":
        stage = "pr"
    elif merged and not nid:
        stage = "merged"
    elif merged:
        stage = "merged"              # squash/rebase merge: commits differ from main's but the PR landed them
    elif pr and (pr.get("state") or "").upper() == "CLOSED":
        stage = "pr_closed"
    elif not trunk and nid:
        stage = "pushed"
    else:
        stage = "clean"
        parts.append("in sync with %s" % (g.get("upstream") or "origin") if trunk else "nothing beyond main")
    if g.get("behind"):
        parts.append("%d behind upstream" % g["behind"])
    return {"stage": stage, "chips": chips, "summary": " · ".join(parts),
            "pr": {k: pr.get(k) for k in ("number", "state", "url", "isDraft", "title")} if pr else None}


def freshness(state, age):
    """0..1 brightness for the tile: idle tiles fade over STALE_AFTER, others stay solid."""
    if state != "idle":
        return 1.0
    return max(0.45, 1.0 - 0.55 * min(1.0, max(0.0, age) / STALE_AFTER))


def summarise(agents):
    counts = {s: 0 for s in STATES}
    for a in agents:
        counts[a["state"]] = counts.get(a["state"], 0) + 1
    counts["live"] = sum(1 for a in agents if a.get("live"))
    return counts
