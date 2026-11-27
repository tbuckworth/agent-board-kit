"""Move a Claude agent to another login, or onto the next model, when its own runs out mid-task or, while it is idle,
before its next turn fails there, and up again once a better place has headroom.

Detection. When a limit stops a turn, Claude Code writes its own record into the transcript, in one of these shapes:
  - a plan limit (5-hour, weekly): an assistant entry with isApiErrorMessage, error "rate_limit" and quotaLimits
    {status: "rejected", rateLimitType, resetsAt}. That is the server's verdict, not a transient 429.
  - a model's own weekly bucket used up, with extra usage off (Fable): an assistant entry with
    isApiErrorMessage, apiError "model_requires_usage_credits", no quotaLimits, and the text "You're out of usage
    credits. Run /usage-credits to keep using Fable 5.1 or /model to switch models." (`claude -p` words it "You're
    out of usage credits. Switch to another model, or manage usage credits at ..."). It carries no reset time; that
    comes from the login's per-model window in agent-usage, which also tells a record from before the window's reset.
  - the end of Claude Code's grace allowance (Claude Code 2.1.314 on): no API error, only a system entry, subtype
    "informational", "Usage limit reached · continuing automatically at 4:30pm · esc to cancel" (or "Usage limit
    reached again · ..."). The reset is the next 4:30pm local time after the entry; when the line gives no time this
    reads, it is the reset of the login's used-up window in agent-usage, and with none used up, a line from before
    the latest reset of the login's windows is over.
A live agent whose latest such record is newer than its latest good reply, with the reset still ahead, is stuck,
whatever user lines came after the record: a background task's notice or a queued message can land after a refusal
without starting a turn. A record from before the agent's last move, or its last switch by hand (agent-switch), belongs
to the login it left.
The transcript tail of a live agent is the only signal of a limit read. Text that merely quotes such a message, such
as a hook's systemMessage, is not a limit: a hook's own `claude -p` running out says nothing about the agent's login.
An idle agent has no such record, however long its login has been used up, so its own login's usage is read for it
instead (decide, spent): a fresh reading in which a window counted for its model is used up (usage.assess "out")
moves it as a limit would, before a message to it fails there (or, with the login's extra usage on, bills it). A
reading that failed, or an older one standing in for it, never moves an agent.
Where it goes is the first place with headroom in usage.places order (the README's "login and model order"): each
Claude login in usage.order on the model it started on, then each on the next of config.claude_model_order, and
so on, never an earlier model (a move to the same login on another model restarts it there with --model); with
none, the place usage.last_resort allows, where it bills extra usage. An idle agent leaving a used-up login goes
only where there is headroom: with none it stays, noted once, rather than bill before it has work (and a limit,
if its next turn hits one, then moves it as above). Headroom is judged for the model the agent
would run there, so a Fable agent stays on Fable only on a login with Fable left, and comes back to its first
login and model only once that has reset. Until it replies on a model a move gave it, its model is the one that
move recorded (its transcript still shows the old one). An agent switched by hand, or running a model not on that
list, keeps its model: it goes only to a login with headroom for that model, and with none, where it bills extra
usage on it (spend_watch mails the owner, who may switch it).

The move (migrate) keeps the session id, so mail replies keep reaching the agent. Its claude is stopped with
SIGKILL, not SIGTERM: a graceful exit kills the agent's background jobs, while after SIGKILL they keep running
(reparented) and its MCP servers exit on their own. The agent is between turns (its turn ended on the limit, or it
is idle), so nothing is half written. A killed claude cannot remove its own registry entry, so the move does. The
transcript is copied to the other login, the folder trusted there and its auto memory linked, and the session
resumed in the same tmux pane with its flags. Every move but a limit's (up, back, or off a used-up login, whether or
not it bills extra usage: README, "Idle on a used-up login" and "Moving it up") waits until the agent is idle and
resumes it with no prompt, so a busy agent is never interrupted; at most idle_moves_per_pass of them are made in one
pass (pace), so that many agents leaving a login or coming back at its reset restart a few at a time, a pass apart.

A Claude Code background session (`claude --bg`, or one dispatched from `claude agents`; the claude daemon runs
it, with no terminal) moves with the daemon's own commands: `claude stop ID` under its login (this also ends its
background commands, and the note it resumes with says so), the copy, then `claude --resume ID --bg <its model,
effort and permission mode, read from the transcript> prompt` under the other login, in the session's folder.
memcap then moves the daemon out of the caller's cgroup, so a restart of agent-failover (or the end of the agent
that ran agent-switch) cannot take it down.

Two kinds of agent are moved: background sessions, and agents in a tmux pane whose shell started claude
(agent-spawn's layout); a session in someone's terminal is left alone. A manual login (accounts.manual) is a person's
choice: nothing moves an agent onto one, nor off one, and a resume keeps an agent there. This module decides (decide)
and moves (migrate, place_resume); bin/agent-failover runs the watch loop.

While live agents bill a login's extra usage, the owner is mailed when that starts, at each further step of spend
and when it ends (spend_watch decides; bin/agent-failover sends). That only reports: it moves nothing.
"""
import contextlib
import copy
import datetime
import fnmatch
import json
import os
import re
import shlex
import signal
import time

from . import accounts, claude_session, commands, config, jsonfile, launch, memcap, procs, registry, usage

DEFAULTS = {
    "enabled": True,
    # Background commands the agent started: "keep" leaves them running and lists them in the note it resumes
    # with (pid, command, output file); "kill" stops them first and says so.
    "background": "keep",
    "interval_seconds": 20,       # between watch passes
    # move idle agents (decide): up to a better place with headroom, and off a login that is used up for their model
    # (whether or not it bills extra usage); false leaves every idle agent where it is, so only a limit moves one
    "switch_back": True,
    "idle_seconds": 300,          # an agent must have been idle this long before such a move
    # of such moves, at most this many in one pass (pace); the rest are decided again at the next pass
    "idle_moves_per_pass": 3,
    "max_moves_per_hour": 4,      # per agent: a guard against ping-pong
    "launch_wait_seconds": 60,    # for a moved agent to show live on its new login
    # Session names (shell globs) that stay on their login: never moved when a limit stops them (they wait for its
    # reset), and resumed where their newest copy is. E.g. ["nightly-*"] for jobs that must only use the login
    # they were started on.
    "pinned": [],
    "tail_bytes": 256 << 10,      # of each transcript, read for limits, model and flags
    # Mail the owner while agents run on a Claude login that bills extra usage (a window counted for their model is
    # used up and extra usage is on): once when they start, again each time the stretch's spend grows by spend_step
    # (in the login's currency), and once with the stretch's total when none bills any more. Usage is read for it
    # at most every spend_check_seconds while a Claude agent is live or a stretch is open.
    "spend_mail": True,
    "spend_step": 100,
    "spend_check_seconds": 300,
}
LIMIT_PROMPT = ("[agent-failover] Your Claude login '{src}' hit its usage limit ({why}), so this session was moved "
                "to {dst} and resumed here. Carry on with your task from where you stopped.")
NO_PROMPT_JOBS = "[agent-failover] This session was moved to {dst} ({why}). Nothing else changed."
LIMIT_WAIT = "limit-wait"   # the status-log event of a note that a limit stopped an agent and it waits (decide)
SWITCH_HINT = ("To stop one billing, switch it to another model or login: right-click it on the agent board, or run "
               "agent-switch NAME --model MODEL or --to claude:LOGIN.")
CREDITS_ERROR = "model_requires_usage_credits"   # Claude Code's apiError when a model's own bucket is used up
CREDITS_TEXT = "You're out of usage credits"
LIMIT_NOTICE = "Usage limit reached"             # how Claude Code's system line starts when a limit ends a turn
AUTO_CONTINUE = re.compile(r"continuing automatically at (1[0-2]|[1-9])(?::([0-5]\d))?([ap]m)\s*(?:·|$)")
# What record() and switched_by_hand() keep, under "spend" each login's open extra-usage stretch (spend_watch), and
# under "spend_unreadable" why each login's spend could not be checked last time (so agent-failover logs it once).
# Deleting it when damaged forgets each agent's first login and model, its recent moves (the ping-pong guard) and
# when it was last moved or switched by hand (so a switch by hand no longer keeps an agent from moving up), and
# starts any open stretch again with a new first mail.
STATE = jsonfile.JsonFile(config.STATE_DIR / "failover.json", jsonfile.PLAIN, 1)


def settings(cfg):
    return dict(DEFAULTS, **(cfg.get("failover") or {}))


def pinned(sess, f):
    """Whether this session stays on its login (settings' "pinned", by session name)."""
    name = sess.get("name") or ""
    return bool(name) and any(fnmatch.fnmatchcase(name, p) for p in f.get("pinned") or [])


# ------------------------------------------------------------------ reading a session (pure where possible)
def entry_text(d):
    content = (d.get("message") or {}).get("content")
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return str(content or "")


def credits_model(text):
    """'Fable 5.1' from "... Run /usage-credits to keep using Fable 5.1 or /model to switch models.", or None."""
    m = re.search(r"keep using (.+?) or /model", text or "")
    return m.group(1).strip() if m else None


def auto_continue_at(text, at):
    """The epoch of the local time in "continuing automatically at 4:30pm": the first such time after `at`, the
    epoch of the line. None when the line gives no time of day this reads."""
    m = AUTO_CONTINUE.search(text)
    if not (m and at):
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    t = datetime.datetime.fromtimestamp(at).replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
    if t.timestamp() <= at:
        t += datetime.timedelta(days=1)
    return t.timestamp()


def limit_record(d):
    """Claude Code's own record of a limit stopping a turn, read from one transcript entry, its reset past or not:
    {"window": "five_hour" | "seven_day" | ... | "model_credits" | "plan" (a "Usage limit reached" line),
    "resets_at": epoch or None, "at": epoch of the record, "model": "Fable 5.1" (credits only), "text": ...}.
    None for any other entry."""
    at = usage.iso_epoch(d.get("timestamp"))
    if d.get("type") == "system" and d.get("subtype") == "informational":
        text = str(d.get("content") or "")
        if not text.startswith(LIMIT_NOTICE):
            return None
        return {"window": "plan", "resets_at": auto_continue_at(text, at), "at": at, "text": text[:160]}
    if not (d.get("type") == "assistant" and d.get("isApiErrorMessage")):
        return None
    text = entry_text(d)
    q = d.get("quotaLimits") or {}
    if d.get("error") == "rate_limit" and q.get("status") == "rejected":
        return {"window": q.get("rateLimitType") or "limit", "resets_at": q.get("resetsAt"), "at": at,
                "text": text[:160]}
    if d.get("apiError") == CREDITS_ERROR or text.startswith(CREDITS_TEXT):
        return {"window": "model_credits", "resets_at": None, "at": at, "model": credits_model(text),
                "text": text[:160]}
    return None


def limit_hit(lines, now=None):
    """The limit that stopped this session: its latest limit_record, if no good reply came after it and its reset
    is not known to be past. User lines after it do not matter. None otherwise."""
    now = time.time() if now is None else now
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        hit = limit_record(d)
        if hit:
            if hit["resets_at"] and hit["resets_at"] <= now:
                return None   # reset already: Claude Code's own auto-continue (or the next input) carries on
            return hit
        if d.get("type") == "assistant" and not d.get("isApiErrorMessage") and not d.get("isMeta"):
            return None       # a good reply since the latest limit record
    return None


VALUE_FLAGS = ("--model", "--effort", "--permission-mode", "--agent")
BARE_FLAGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--chrome", "--no-chrome")


def relaunch_flags(argv):
    """The flags of a running claude worth keeping across a move (model, effort, permissions); never its prompt
    or a --resume/--continue it was started with."""
    out, i = [], 1
    while i < len(argv):
        a = argv[i]
        if a in BARE_FLAGS:
            out.append(a)
        elif a in VALUE_FLAGS and i + 1 < len(argv):
            out += [a, argv[i + 1]]
            i += 1
        elif "=" in a and a.split("=", 1)[0] in VALUE_FLAGS:
            out.append(a)
        i += 1
    return out


def jobs_note(jobs, killed):
    if not jobs:
        return ""
    lines = ["pid %d: %s%s" % (j["pid"], j["command"], (" (output: %s)" % j["output"]) if j["output"] else "")
             for j in jobs]
    if killed:
        return (" These background commands from before the move were stopped; start again any you still "
                "need: " + "; ".join(lines))
    return (" These background commands from before the move are still running, but this session no longer "
            "tracks them (Claude Code's notice that they \"didn't finish\" only means that), so check them with "
            "ps or their output files: " + "; ".join(lines))


def stop_jobs(jobs):
    ppids = procs.parents()
    for j in jobs:
        for p in reversed(procs.tree(j["pid"], ppids)):
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass


def override_flags(flags, overrides):
    """flags with --model/--effort (etc.) replaced or added from overrides {flag: value}; None values ignored."""
    out, i, done = [], 0, set()
    while i < len(flags):
        f = flags[i]
        name = f.split("=", 1)[0]
        if name in (overrides or {}) and overrides[name] is not None:
            out += [name, overrides[name]]
            done.add(name)
            i += 1 if "=" in f else 2
            continue
        out.append(f)
        i += 1
    for name, value in (overrides or {}).items():
        if value is not None and name not in done:
            out += [name, value]
    return out


# ------------------------------------------------------------------ deciding (pure)
def recent_moves(entry, now, window=3600):
    return [m for m in (entry or {}).get("moves", []) if now - m[0] < window]


def last_move(entry):
    """When this agent was last moved (record) or switched by hand (switched_by_hand), as an epoch; 0 for never."""
    entry = entry or {}
    return max([m[0] for m in entry.get("moves", [])] + [entry.get("switched_at", 0)])


def stuck_key(hit):
    """What a 'waits for the reset' note is logged once per: the reset, or the record itself when no reset is known."""
    return hit.get("resets_at") or hit.get("at") or hit.get("text")


def where(login, model=None):
    """How a move's note names where the agent went: "login 'work'", and its new model when it changes."""
    return "login '%s'" % login + (" on model %s" % model if model else "")


def models_from(cfg, model, manual):
    """The models an agent that started on `model` may be moved onto, best first: that model and each after it in
    config.claude_model_order, or only its own model when it was switched by hand or `model` is not on that list.
    With no headroom left for any of them, it may go where it bills extra usage on the last (next_place)."""
    models = config.claude_model_order(cfg)
    if manual or model not in models:
        return [model]
    return models[models.index(model):]


def next_place(snaps, cfg, current, model, models, owner, now=None, bills=False):
    """(login, model, passed over): the first place with headroom, each Claude login in usage.order on models[0], then
    each on models[1] and so on, other than the agent's own (`current`, `model`), on a login whose owner rules allow
    the agent's repo (`owner`, its GitHub owner; accounts.owner_allowed); else, with `bills`, the first one
    usage.last_resort allows; else (None, None, passed). passed: "login: why" for each place skipped (not set up,
    closed to the repo's owner, used up, or its usage could not be read)."""
    s = usage.settings(cfg)
    passed, ready = [], {}
    rungs = usage.places(s, models, None)
    for t, m in rungs:
        acct = usage.account_of(t)
        if acct is None or (acct, m) == (current, model):
            continue
        name = acct if len(models) == 1 else "%s on %s" % (acct, m)
        if acct not in ready:
            ready[acct] = accounts.account_ready(accounts.account(s, acct))
            if ready[acct][0]:
                ready[acct] = accounts.owner_allowed(accounts.account(s, acct), owner)
        if not ready[acct][0]:
            passed.append("%s: %s" % (name, ready[acct][1]))
            continue
        a = usage.assess(snaps.get(t) or {"windows": []}, s, m, now)
        if a["status"] == "ok":
            return acct, m, passed
        passed.append("%s: %s" % (name, "; ".join(a["why"]) or a["status"]))
    if bills:
        for t, m in rungs:
            acct = usage.account_of(t)
            if acct and (acct, m) != (current, model) and ready.get(acct, (False,))[0] \
                    and usage.last_resort(snaps.get(t) or {}, m, models):
                return acct, m, passed
    return None, None, passed


def rank(cfg, acct, model, models):
    """Where (acct, model) comes among the places of `models` (models_from): lower is better."""
    order = usage.ranked(usage.settings(cfg))
    t = usage.claude_target(acct)
    return (models.index(model) if model in models else len(models), order.index(t) if t in order else len(order))


def model_window(snap, model):
    """The login's per-model window for this model (its "weekly Fable"), or None."""
    for w in (snap or {}).get("windows") or []:
        if w.get("kind") == "model_weekly" and usage.counted(w, model):
            return w
    return None


def credits_hit(hit, snap, model, now):
    """A model-credits record (no reset time of its own) read against the login's usage: the hit with the
    window's reset filled in, or None when that window has reset since the record was written."""
    w = model_window(snap, model)
    if not w:
        return hit   # no per-model window known (or usage unread): the transcript is the only verdict
    last = usage.claude_last_reset(w)
    if w["remaining_pct"] > 0 and last and hit.get("at") and hit["at"] < last:
        return None  # written before the window's last reset, and it has headroom now: the next input carries on
    return dict(hit, resets_at=w.get("resets_at") if w["remaining_pct"] <= 0 else None,
                window_name=w.get("name") or "model window")


def reset_from_usage(hit, snap, model):
    """A limit record that gives no reset time (a "Usage limit reached" line with no time this reads), read against
    the login's counted windows: the hit with the name and reset of the used-up window that resets last. With none
    used up, None when the record is older than the latest of their last resets (usage.claude_last_reset): the
    window that ran out has reset since, so the next input carries on. Unchanged otherwise, and when usage is
    unread (the transcript is then the only verdict)."""
    windows = [w for w in (snap or {}).get("windows") or [] if usage.counted(w, model)]
    out = [w for w in windows if w["remaining_pct"] <= 0]
    if not out:
        last = max(filter(None, map(usage.claude_last_reset, windows)), default=None)
        return None if last and hit.get("at") and hit["at"] < last else hit
    w = last_to_reset(out)
    return dict(hit, window=w["name"], resets_at=w["resets_at"]) if w else hit


def last_to_reset(windows):
    """Of these used-up windows, the one whose reset is the latest known (an agent can run again only once that one
    has reset), or None when none has a known reset."""
    timed = [w for w in windows if w.get("resets_at")]
    return max(timed, key=lambda w: w["resets_at"]) if timed else None


def limit_why(hit, now):
    if hit["window"] == "model_credits":
        return "out of %s usage credits%s" % (
            hit.get("model") or "model", (", %s resets %s" % (hit.get("window_name") or "its window",
                                                             usage.fmt_reset(hit["resets_at"], now)))
            if hit.get("resets_at") else "")
    return "%s limit, resets %s" % (hit["window"].replace("_", "-"), usage.fmt_reset(hit["resets_at"], now))


def bills_extra(snap, s, model, now):
    """Whether an agent running `model` on the login of this usage snapshot bills extra usage: the login's extra usage
    is on and a window counted for the model is used up (usage.assess)."""
    return bool((snap.get("extra_usage") or {}).get("enabled")) and usage.assess(snap, s, model, now)["status"] == "out"


def spent(snap, s, model, now):
    """Whether the login of this usage snapshot is used up for an agent running `model`, so that the agent's next turn
    there would stop on the limit, or with the login's extra usage on, bill it (bills_extra): usage.assess says "out"
    (a window counted for the model is used up, the rule by which next_place passes a login over), in a fresh
    reading. A login whose usage read failed is not spent, nor is
    one judged from the older reading that stands in for a failed read (stale): its windows may have reset since.
    {"windows": "weekly" (or "5-hour and weekly"), "resets_at": the latest of their resets, None when none is known}
    or None."""
    snap = snap or {}
    if snap.get("stale") or usage.assess(snap, s, model, now)["status"] != "out":
        return None
    gone = usage.used_up(snap, model)
    last = last_to_reset(gone)
    return {"windows": " and ".join(w["name"] for w in gone), "resets_at": last["resets_at"] if last else None}


def no_room(models, passed):
    """Where an agent could not go, for a note: no other Claude login has headroom for its models, with the logins
    next_place passed over (`passed`) and why."""
    return "no other Claude login has headroom for %s%s" % (" or ".join(map(str, models)),
                                                            (" (%s)" % "; ".join(passed)) if passed else "")


def decide(sessions, limits, snaps, state, cfg, now):
    """-> (moves, notes). moves: [{"session", "to", "model", "from_model", "reason", "prompt", "resets_at", "idle"}],
    "idle" true for every move but a limit's (pace). notes: [(session id, key, text, status-log event or None)], each
    a wait to log once: act keeps the key as the session's "stuck_until", and a note whose key is already there is
    not made again. Its event is "limit-wait" for an agent its limit stopped, and None for an idle one that stays on
    a used-up login: it has hit no limit, so the status log (and the board) must not show it stopped.
    sessions: live Claude sessions movable here, each with the "model" of its last reply if known and that reply's
    time ("model_at"), and its repo's GitHub "owner" (None: none known), for the logins' owner rules; limits:
    {session id: limit_hit(...)}.

    An agent a limit stopped goes to the next place with headroom, or where it bills extra usage. An idle one (status
    idle, no dialog, for idle_seconds), with switch_back on, goes up to a better place with headroom once what moved
    it off its first place (a limit, or a used-up window) has reset; failing that, off a login used up for its model
    (spent), whose extra usage it would bill ("rebalance") or not ("spent"), as a limit would move it but only where
    there is headroom, with the window's reset recorded so that it comes back after it (record) and not before."""
    f = settings(cfg)
    s = usage.settings(cfg)
    moves, notes = [], []
    for sess in sessions:
        sid, cur = sess["id"], sess.get("account")
        model = sess.get("model") or cfg.get("spawn_claude_model")
        entry = (state.get("sessions") or {}).get(sid) or {}
        if cur is None:
            continue
        if entry.get("model") and entry.get("current") == cur and (sess.get("model_at") or 0) < last_move(entry):
            model = entry["model"]   # moved onto it, with no reply since: the transcript still shows the old one
        # the place it started on (its first move's origin), and where it may go: from that model down
        home, home_model = entry.get("home"), entry.get("home_model") or model
        models = models_from(cfg, home_model, "switched_at" in entry)

        def move(to, to_model, reason, limit, resets_at):
            """A move to (to, to_model); `limit` is why its limit moves it (its prompt says so), None for none."""
            changed = to_model if to_model != model else None
            return {"session": sess, "to": to, "model": changed, "from_model": model, "resets_at": resets_at,
                    "reason": reason + (" (model %s)" % changed if changed else ""), "idle": limit is None,
                    "prompt": None if limit is None else LIMIT_PROMPT.format(src=cur, dst=where(to, changed),
                                                                             why=limit)}
        hit = limits.get(sid)
        if hit and hit.get("at") and hit["at"] < last_move(entry):
            hit = None   # the login it left: still the latest record until the resumed agent's first reply
        if hit and hit["window"] == "model_credits":
            hit = credits_hit(hit, snaps.get(usage.claude_target(cur)), model, now)
        elif hit and not hit["resets_at"]:
            hit = reset_from_usage(hit, snaps.get(usage.claude_target(cur)), model)
        moved = len(recent_moves(entry, now))
        if moved >= f["max_moves_per_hour"]:
            if hit and entry.get("stuck_until") != stuck_key(hit):
                notes.append((sid, stuck_key(hit), "%s (%s) hit its limit on %s (%s) but was moved %d times in the "
                              "last hour, the most failover.max_moves_per_hour allows; it waits" % (
                                  sess.get("name") or sid[:8], sid[:8], cur, limit_why(hit, now), moved), LIMIT_WAIT))
            continue
        on_manual = accounts.manual(accounts.account(s, cur))
        if pinned(sess, f) or on_manual:
            if hit and entry.get("stuck_until") != stuck_key(hit):
                notes.append((sid, stuck_key(hit), "%s (%s) hit its limit on %s (%s); %s, so it waits for the reset" % (
                    sess.get("name"), sid[:8], cur, limit_why(hit, now),
                    "that is a manual login (usage.claude_accounts), which nothing moves it off" if on_manual else
                    "it is pinned there (failover.pinned)"), LIMIT_WAIT))
            continue
        if hit:
            dst, dst_model, passed = next_place(snaps, cfg, cur, model, models, sess.get("owner"), now, bills=True)
            why = limit_why(hit, now)
            if dst:
                moves.append(move(dst, dst_model, "limit: " + why, why, hit["resets_at"]))
            elif entry.get("stuck_until") != stuck_key(hit):
                notes.append((sid, stuck_key(hit), "%s (%s) hit its limit on %s (%s) and %s, and none bills extra "
                              "usage for %s; it waits for the reset" % (
                                  sess.get("name") or sid[:8], sid[:8], cur, why, no_room(models, passed),
                                  models[-1]), LIMIT_WAIT))
            continue
        idle = sess.get("status") == "idle" and not sess.get("waiting_for") and \
            now - (sess.get("updated_at") or now) >= f["idle_seconds"]
        if not (f["switch_back"] and idle):
            continue
        if now >= (entry.get("resets_at") or 0):   # what moved it off its first place (limit, used-up window) has reset
            if "switched_at" in entry:   # switched by hand, the owner's choice: back only where failover took it from
                ok = home and not accounts.manual(accounts.account(s, home)) and usage.assess(
                    snaps.get(usage.claude_target(home)) or {"windows": []}, s, home_model, now)["status"] == "ok"
                dst, dst_model = (home, home_model) if ok else (None, None)
            else:                        # up to the best place with headroom, wherever it started
                dst, dst_model, _ = next_place(snaps, cfg, cur, model, models, sess.get("owner"), now)
            if dst and rank(cfg, dst, dst_model, models) < rank(cfg, cur, model, models):
                moves.append(move(dst, dst_model, "switch back: %s has reset" % dst
                                  if (dst, dst_model) == (home, home_model) else "move up: %s has headroom" % dst,
                                  None, None))
                continue
        snap = snaps.get(usage.claude_target(cur)) or {"windows": []}
        gone = spent(snap, s, model, now)
        if not gone:
            continue
        # its next turn would stop on the limit, or bill the login's extra usage: it goes now, as the limit would move
        # it, but only where there is headroom, and with the window's reset, so that it comes back after that (record)
        # and not before
        bills = bills_extra(snap, s, model, now)
        state_now = "%s (%s window%s used up, resets %s)" % (
            "bills extra usage" if bills else "has no headroom", gone["windows"],
            "s" if " and " in gone["windows"] else "", usage.fmt_reset(gone["resets_at"], now))
        dst, dst_model, passed = next_place(snaps, cfg, cur, model, models, sess.get("owner"), now)
        key = "spent %s %s" % (cur, gone["resets_at"])   # noted once per login and reset
        if dst:
            moves.append(move(dst, dst_model, "%s: %s %s" % ("rebalance" if bills else "spent", cur, state_now), None,
                              gone["resets_at"]))
        elif entry.get("stuck_until") != key:
            notes.append((sid, key, "%s (%s) is idle on %s, which %s, and %s; it stays there%s" % (
                              sess.get("name") or sid[:8], sid[:8], cur, state_now, no_room(models, passed),
                              ", billing extra usage when it works" if bills else ""), None))
    return moves, notes


def pace(moves, f):
    """(the moves to make in this pass, the moves left for a later one), from decide's moves in its order: every move
    a limit makes (its agent is stuck), and the first f["idle_moves_per_pass"] idle ones. Moves run one after another
    (bin/agent-failover's act), so this keeps a pass's restarts to a few: many idle agents leaving a used-up login, or
    coming back at its reset, go a few each pass. Those left are not kept: the next pass decides them again."""
    room, go, later = f["idle_moves_per_pass"], [], []
    for m in moves:
        if m.get("idle"):
            if room <= 0:
                later.append(m)
                continue
            room -= 1
        go.append(m)
    return go, later


def record(state, move, now):
    """The state after a successful move. `home` and `home_model` are the login and model the agent was first moved
    off; `resets_at` is when what moved it off them resets: the limit, or for an idle agent the used-up window of
    that login (decide, spent). Until then it is not moved up, so it cannot come straight back."""
    st = copy.deepcopy(state or {})
    sid = move["session"]["id"]
    src, src_model = move["session"].get("account"), move.get("from_model")
    entry = st.setdefault("sessions", {}).setdefault(sid, {})
    entry.setdefault("home", src)
    if not entry.get("home_model"):   # its first move; or a record from before moves changed models, whose model
        entry["home_model"] = src_model   # has never changed
    entry["current"] = move["to"]
    entry["model"] = move.get("model") or src_model
    entry["name"] = move["session"].get("name") or ""
    if move.get("resets_at") and (src, src_model) == (entry["home"], entry["home_model"]):
        entry["resets_at"] = move["resets_at"]
    entry["moves"] = recent_moves(entry, now, 86400) + [[now, src, move["to"], move["reason"]]]
    if (entry["home"], entry["home_model"]) == (move["to"], entry["model"]):
        entry.pop("resets_at", None)
    return st


def switched_by_hand(state, sid, now):
    """The state after agent-switch switched session sid by hand at `now`. That is the owner's choice, so the agent's
    record is forgotten: no move back to its first login, and its earlier moves no longer count toward
    max_moves_per_hour (nor does the switch). Only the switch's time is kept: for last_move, so a limit record from
    before the switch belongs to the login it left, and as the mark that keeps the agent on its model
    (models_from)."""
    st = copy.deepcopy(state or {})
    st.setdefault("sessions", {})[sid] = {"switched_at": now}
    return st


# ------------------------------------------------------------------ extra-usage spend (pure)
# The alerts.send_alert kinds of the mails spend_watch writes (spend_alert): agents began billing extra usage or spent
# another step of it, and they stopped, with the total.
SPEND_ALERT, SPEND_END_ALERT = "extra-usage-spend", "extra-usage-end"


def spend_unreadable(snap):
    """Why a login's usage reading cannot say who bills extra usage and how much, or None when it can: it must be a
    fresh reading (not the stale fallback of a failed read) with windows, the login's extra-usage state and, while
    extra usage is on, its spend this month."""
    if not snap:
        return "its usage was not read"
    if snap.get("stale"):
        return "its usage read failed (%s), leaving only an older reading" % snap.get("error")
    if not snap.get("windows"):
        return "its usage read failed (%s)" % (snap.get("error") or "no windows")
    if snap.get("extra_usage") is None:
        return "its reading has no extra-usage state"
    if snap["extra_usage"]["enabled"] and snap["extra_usage"].get("used") is None:
        return "its reading has no extra-usage spend"
    return None


def spend_alert(kind, login, st, eu, detail, cfg, now):
    """(alert kind, key, subject, body) of one spend_watch mail about stretch st on login, whose extra_usage reading is
    eu: kind "start" (detail: why its agents bill), "step", or "end" (detail: what ended it), the alert kind
    SPEND_END_ALERT for an end and SPEND_ALERT for the others. The key is unique to the stretch and its point, so
    alerts.send_alert sends each mail once."""
    def money(v):
        return usage.money(v, st["currency"])
    agents = ", ".join(st["agents"])
    month = "Extra usage on %s this month: %s." % (login, usage.extra_spend(eu))
    nxt = ("You will get another mail at each further %s they spend, and one with the total when they stop."
           % money(settings(cfg)["spend_step"]))
    since = usage.fmt_reset(st["since"], now)
    if kind == "start":
        n = len(st["agents"])
        who = ("%d agents" % n, "bill", "are") if n > 1 else ("1 agent", "bills", "is")
        subject = "%s now %s extra usage on Claude login %s" % (who[0], who[1], login)
        body = ("%s %s running on the Claude login %s, whose %s. Its extra usage is on, so their work is now paid for "
                "as extra usage.\n\n%s\n\nAgents on it: %s.\n\n%s\n\n%s"
                % (who[0], who[2], login, detail, month, agents, SWITCH_HINT, nxt))
    elif kind == "step":
        subject = "%s of extra usage spent on Claude login %s since %s" % (money(st["spent"]), login, since)
        body = ("Agents on the Claude login %s have spent %s of extra usage since %s, when they started billing it."
                "\n\n%s\n\nStill on it: %s.\n\n%s" % (login, money(st["spent"]), since, month, agents, nxt))
    else:
        subject = "Agents stopped billing extra usage on Claude login %s: %s spent" % (login, money(st["spent"]))
        body = ("No agent bills extra usage on the Claude login %s any more: %s. From %s to %s they spent %s of extra "
                "usage.\n\n%s" % (login, detail, since, usage.fmt_reset(now, now), money(st["spent"]), month))
    return (SPEND_END_ALERT if kind == "end" else SPEND_ALERT, "%s:%s:%d:%s" % (login, kind, st["since"], st["mailed"]),
            subject, body)


def used_up_text(snap, models, now):
    """Which of the login's windows counted for these models are used up (usage.used_up), and until when: "5-hour
    window is used up until 16:30" (the windows joined with "and")."""
    gone = {w["name"]: w for m in sorted(models) for w in usage.used_up(snap, m)}
    return " and ".join("%s window is used up until %s" % (name, usage.fmt_reset(w.get("resets_at"), now))
                        for name, w in gone.items())


def spend_watch(sessions, snaps, state, cfg, now):
    """What the owner hears about extra usage: ([(login, stretch, mail)] for each Claude login with live agents or an
    open stretch whose usage reading can tell, {login: why} for the others (spend_unreadable)). A stretch opens when
    a live agent on the login bills extra usage (bills_extra) and ends when none does: its agents have left, its
    window has reset or its extra usage was turned off. stretch is the login's new record for state "spend" (None
    once it has ended): when it began ("since"), the month's spend at the last reading ("last_used"), the stretch's
    spend ("spent", which counts on from zero when a new month resets the month's spend), the spend its last step
    mail marked ("mailed"), the currency, and the agents billing (their names, each with the model it runs, and
    their session ids in "ids"). mail is spend_alert's (kind, key, subject, body), or None when there is nothing to
    say. sessions: the live Claude
    sessions, each with the "model" it runs if known; those on a manual login are left out."""
    f, s = settings(cfg), usage.settings(cfg)
    on = {}
    for x in sessions:   # a manual login bills per token, never extra usage
        if x.get("account") and not accounts.manual(accounts.account(s, x["account"])):
            on.setdefault(x["account"], []).append(x)
    stretches = state.get("spend") or {}
    out, unreadable = [], {}
    for login in sorted(set(on) | set(stretches)):
        snap = snaps.get(usage.claude_target(login))
        why_not = spend_unreadable(snap)
        if why_not:
            unreadable[login] = why_not
            continue
        models = {x["id"]: x.get("model") or cfg.get("spawn_claude_model") for x in on.get(login, [])}
        billing = [x for x in on.get(login, []) if bills_extra(snap, s, models[x["id"]], now)]
        names = sorted("%s (%s)" % (x.get("name") or x["id"][:8], models[x["id"]]) if models[x["id"]]
                       else x.get("name") or x["id"][:8] for x in billing)
        ids = sorted(x["id"] for x in billing)
        eu = snap["extra_usage"]
        old = stretches.get(login)
        if old is None:
            if billing:   # extra usage is on, so the reading has its spend (spend_unreadable)
                st = {"since": now, "last_used": eu["used"], "spent": 0.0, "mailed": 0.0, "currency": eu["currency"],
                      "agents": names, "ids": ids}
                why = used_up_text(snap, {models[x["id"]] for x in billing}, now)
                out.append((login, st, spend_alert("start", login, st, eu, why, cfg, now)))
            continue
        if eu.get("used") is None:   # only with extra usage off (spend_unreadable): the last reading stands
            eu = dict(eu, used=old["last_used"])
        used = eu["used"]
        grew = used - old["last_used"] if used >= old["last_used"] else used
        st = dict(old, last_used=used, spent=round(old["spent"] + grew, 2), agents=names, ids=ids)
        if not billing:
            here = {x["id"] for x in on.get(login, [])}
            why = ("its extra usage was turned off" if not eu["enabled"] else
                   "its used-up window has reset" if here & set(old["ids"]) else
                   "no agent runs on it any more" if not here else "the agents that billed it have left")
            out.append((login, None, spend_alert("end", login, st, eu, why, cfg, now)))
            continue
        steps = int((st["spent"] - st["mailed"]) // f["spend_step"])
        st["mailed"] += steps * f["spend_step"]
        out.append((login, st, spend_alert("step", login, st, eu, None, cfg, now) if steps else None))
    return out, unreadable


def keep_stretch(state, login, stretch):
    """The state with login's stretch (spend_watch) kept under "spend", or dropped when stretch is None (it ended)."""
    if stretch:
        state.setdefault("spend", {})[login] = stretch
    else:
        state.setdefault("spend", {}).pop(login, None)
    return state


# ------------------------------------------------------------------ acting
def fill_flags(flags, defaults):
    """flags plus each of defaults [flag, value, ...] it does not set already (a skip-permissions flag counts as
    setting the permission mode)."""
    have = {f.split("=", 1)[0] for f in flags}
    if have & set(BARE_FLAGS[:2]):
        have.add("--permission-mode")
    out = list(flags)
    for i in range(0, len(defaults) - 1, 2):
        if defaults[i] not in have:
            out += defaults[i:i + 2]
    return out


def bg_launch(claude, sess, flags, config_dir, prompt, cfg):
    """Resume a background session under config_dir (bg_resume, with that login's options), then move the daemon out
    of this process's cgroup (and the session into its own capped scope), as agent-send does for a resume. (ok,
    output). A cap config does not allow (memcap.limits) leaves the scope uncapped, and the output says why."""
    ok, out = claude_session.bg_resume(claude, sess, flags, config_dir, prompt, usage.settings(cfg))
    if not ok:
        return False, out
    try:
        props, uncapped = memcap.limits(cfg), ""
    except ValueError as e:
        props, uncapped = {}, "uncapped: %s; " % e

    def live_pid():
        return next((x["pid"] for x in registry.live_on(config_dir) if x["id"] == sess["id"]), None)
    return True, out + "; memory: " + uncapped + memcap.cap_bg_session(sess["id"], sess.get("name") or sess["id"][:8],
                                                                       props, session_pid=live_pid)


def move_flags(s, cfg, src, dst, flags, overrides):
    """flags for a move from Claude login src to dst (s: usage.settings), on the model accounts.move_model gives
    (asked: overrides' --model; current: the one flags carry; fresh: config spawn_claude_model, null: no --model). The
    login's own options are added where the command line is made (launch.claude_line, claude_session.bg_resume).
    Raises ValueError for a model move_model refuses."""
    current = commands.flag_value(flags, "--model")
    model = accounts.move_model(s, src, dst, (overrides or {}).get("--model"), current, cfg.get("spawn_claude_model"))
    if model == current:
        return flags
    return override_flags(flags, {"--model": model}) if model else commands.without_flags(flags, "--model")


def migrate(sess, to, prompt, cfg, trust=None, claude=None, dry_run=False, why="", overrides=None):
    """Move one live agent to login `to`. Returns (ok, detail); once its process has been stopped, the detail ends
    with the background jobs the move stopped or left running. Never leaves it without a launch attempt once its
    process has been stopped. `why` names the reason in the note sent when only background jobs need one."""
    f = settings(cfg)
    s = usage.settings(cfg)
    acct = accounts.account(s, to)
    ready, not_ready = accounts.account_ready(acct)
    if not ready:
        return False, "login %s not ready: %s" % (to, not_ready)
    pane, not_movable = claude_session.movable(sess)
    if not pane:
        return False, "not moved: %s" % not_movable
    bg = pane == claude_session.BG
    src = claude_session.transcript(sess)
    if not src:
        return False, "not moved: no transcript under %s" % registry.claude_home(sess.get("config_dir"))
    pid = int(sess["pid"])
    claude = claude or registry.claude_bin() or "claude"
    dst_dir = accounts.expand(acct.get("config_dir"))
    flags = relaunch_flags(procs.cmdline(pid))   # now: its command line goes with it
    if bg:   # a daemon's spare names no flags: take what the transcript last recorded
        flags = fill_flags(flags, claude_session.transcript_flags(jsonfile.read_tail(src, f["tail_bytes"])))
    base, flags = flags, override_flags(flags, overrides)
    try:
        flags = move_flags(s, cfg, sess.get("account"), to, flags, overrides)
    except ValueError as e:
        return False, "not moved: %s" % e
    saved = claude_session.bg_saved_flags(dst_dir, sess["id"]) if bg else None
    if saved is not None:   # that daemon resumes it with these, whatever is asked
        asked = [(k, v) for k, v in (overrides or {}).items()
                 if v is not None and v != commands.flag_value(base, k) and v != commands.flag_value(saved, k)]
        if asked:
            return False, ("not moved: login %s's claude daemon has run this background session before and resumes "
                           "it there with its saved options (%s), so %s cannot be changed by moving it; use /model "
                           "inside the session" % (to, " ".join(saved) or "none",
                                                    " and ".join("%s %s" % (k[2:], v) for k, v in asked)))
        flags = []
    same_login = (accounts.expand(sess.get("config_dir")) or None) == (dst_dir or None)
    jobs = claude_session.background_jobs(pid)
    kill_jobs = bg or f["background"] == "kill"   # `claude stop` ends a background session's jobs
    if jobs:   # the agent must hear about them, even on a move that would otherwise send no prompt
        prompt = (prompt or NO_PROMPT_JOBS.format(dst=where(to, (overrides or {}).get("--model")),
                                                  why=why or "moved")) + jobs_note(jobs, kill_jobs)
    # what the move does to them, for its detail (the journal and the status log)
    jobs_done = ("; its background jobs %s: pid %s" % ("stopped" if kill_jobs else "left running",
                                                        ", ".join(str(j["pid"]) for j in jobs))) if jobs else ""
    if dry_run:
        if bg:
            how = "claude stop %s, copy %s to %s and run in %s: %sclaude %s" % (
                sess["id"][:8], src.name, registry.claude_home(dst_dir), sess.get("cwd") or ".",
                ("CLAUDE_CONFIG_DIR=%s " % dst_dir) if dst_dir else "",
                " ".join(shlex.quote(c) for c in claude_session.bg_command(
                    claude, sess["id"], None if saved is not None else sess.get("name"),
                    flags if saved is not None else accounts.login_flags(s, dst_dir, flags), prompt)[1:]))
            if saved is not None:
                how += " (no flags: that login's daemon resumes it with its saved options, %s)" % (" ".join(saved) or "none")
            return True, "would %s%s" % (how, (" (ending %d background job(s))" % len(jobs)) if jobs else "")
        return True, "would stop pid %d%s, copy %s to %s and type: %s" % (
            pid, (" and %d background job(s)" % len(jobs)) if kill_jobs and jobs else "", src.name,
            registry.claude_home(dst_dir),
            launch.claude_line(claude, sess["id"], sess.get("name"), flags, dst_dir, prompt, None, s))
    keys = contextlib.ExitStack()   # the keys lock, held from the kill to the resume line's Enter (below)
    try:
        if bg:
            stopped, out = claude_session.bg_job(claude, "stop", sess["id"], sess.get("config_dir"), s)
            if not stopped and not claude_session.wait_dead(pid, 1.0):
                return False, "not moved: claude stop failed: %s" % out
            if not claude_session.wait_dead(pid):
                return False, "pid %d still running after claude stop (%s)" % (pid, out)
            stop_jobs(jobs)   # any that outlived the stop
        else:
            try:   # so that no other keys land in the pane's shell between the kill and the resume line
                keys.enter_context(commands.keys_lock(True))
            except commands.LockBusy as e:
                return False, "not moved, nothing stopped: %s" % e
            if kill_jobs:
                stop_jobs(jobs)
            try:
                os.kill(pid, signal.SIGKILL)   # keeps the agent's background jobs alive; see the module docstring
            except ProcessLookupError:
                pass   # gone already: relaunching it is all that is left to do
            if not claude_session.wait_dead(pid):
                return False, "pid %d did not exit after SIGKILL%s" % (pid, jobs_done)
        registry.remove_dead_entry(sess)   # claude removes its own on exit; left, it is a second entry for this id
        detail = ""
        try:
            if not same_login:   # a model or effort change on the same login keeps the transcript where it is
                claude_session.copy_transcript(src, registry.claude_home(dst_dir), sess["id"])
            cwd = sess.get("cwd") or ""
            if trust and cwd and os.path.isdir(cwd):
                trust(cwd, dst_dir)
            if dst_dir and cwd and os.path.isdir(cwd):
                accounts.link_memory(commands.repo_root(cwd), dst_dir, registry.DEFAULT_CLAUDE_HOME)
        except Exception as e:  # noqa: BLE001 - relaunch where it was rather than leave the agent stopped
            detail = "moving it to %s failed (%s: %s); relaunched on %s instead" % (to, type(e).__name__, e,
                                                                                   sess.get("account"))
            to, dst_dir = sess.get("account"), sess.get("config_dir")
            # as asked, but for the model of a login it did not reach that runs one model only
            back = {k: v for k, v in (overrides or {}).items()
                    if not (k == "--model" and v and v == accounts.fixed_model(acct))}
            flags = override_flags(base, back)
        if bg:
            launched, said = bg_launch(claude, sess, flags, dst_dir, prompt, cfg)
            if not launched:
                return False, "%sclaude --resume --bg on %s failed: %s%s" % ((detail + "; ") if detail else "", to,
                                                                             said, jobs_done)
        else:
            try:
                launch.type_line(pane, launch.claude_line(claude, sess["id"], sess.get("name"), flags, dst_dir, prompt,
                                                          None, s), dst_dir)
            except commands.CommandError as e:
                return False, "%srelaunching it in pane %s failed: %s%s" % ((detail + "; ") if detail else "", pane, e,
                                                                            jobs_done)
            finally:
                keys.close()

        def live():
            return any(x["id"] == sess["id"] for x in registry.live_on(dst_dir))
        if launch.wait_for(live, f["launch_wait_seconds"]):
            return not detail, (detail or "moved %s to %s%s" % (sess.get("name") or sess["id"][:8], to,
                                                                (" (%s)" % said) if bg else "")) + jobs_done
        shows = ("claude said: " + said) if bg else \
            "pane shows: " + " | ".join(
                commands.tmux("capture-pane", "-p", "-t", pane, check=False).stdout.strip().splitlines()[-3:])
        return False, "%srelaunched on %s but not live after %ds; %s%s" % (
            (detail + "; ") if detail else "", to, f["launch_wait_seconds"], shows[:300], jobs_done)

    finally:
        keys.close()

# ------------------------------------------------------------------ resuming an offline session
def resume_login(newest, model, owner, snaps, cfg, now=None):
    """(account, why): the login an offline session resumes on, on the model it runs (usage.recommend for that model
    alone, so a resume never changes its model): the first login in usage.order with headroom for it and allowed
    for its repo's owner, or with every login low, the one with the most left. With no usage known, or every login
    used up, it stays on `newest`, the login holding its newest copy."""
    rec = usage.recommend(snaps, cfg, models={"claude": model}, owner=owner, only_cli="claude", now=now)
    pick = next((c for c in rec["candidates"] if c["target"] == rec["use"]), None)
    if pick and pick["status"] in ("ok", "low"):
        return pick["account"], rec["why"]
    return newest, "no login with known headroom, so where its newest copy is (%s)" % (rec["why"] or "nothing to pick")


def place_resume(sess, cfg, trust=None, snaps=None, now=None, dry_run=False):
    """Get an offline Claude session ready to resume on the best login (resume_login): (config_dir, note). Reads
    the logins' usage (usage.snapshots: a login whose read fails counts as usage unknown, and the note says so).
    Unless the chosen login holds the newest copy of the conversation, the copy goes there (with its session
    folder), and the folder is trusted and its memory linked there, as a move does. A session with no transcript
    stays where it is. Raises, so that nothing is resumed: commands.CommandError when git cannot say who owns the
    session's repo (accounts.repo_owner), ValueError or OSError for a usage cache that cannot be read or written,
    and OSError, RuntimeError or ValueError when the copy, the trust or the memory link fails."""
    found = registry.transcript_copies(sess["id"])
    if not found:
        return sess.get("config_dir"), ""
    _, src_acct, src_dir, src = found[0]
    if pinned(sess, settings(cfg)):
        return src_dir, "on login %s: pinned (failover.pinned), so where its newest copy is" % src_acct
    if accounts.manual(accounts.account(usage.settings(cfg), src_acct)):
        return src_dir, "on login %s, where its newest copy is: a manual login, which nothing moves it off" % src_acct
    cwd = sess.get("cwd") or ""
    has_cwd = bool(cwd) and os.path.isdir(cwd)
    model = (claude_session.session_model(jsonfile.read_tail(src, settings(cfg)["tail_bytes"]))
             or cfg.get("spawn_claude_model"))
    owner = accounts.folder_owner(cwd)
    snaps = usage.snapshots(cfg) if snaps is None else snaps
    acct, why = resume_login(src_acct, model, owner, snaps, cfg, now)
    dst_dir = src_dir if acct == src_acct else accounts.expand(accounts.account(usage.settings(cfg), acct).get("config_dir"))
    note = "on login %s: %s" % (acct, why)
    if (dst_dir or None) == (src_dir or None):
        return dst_dir, note
    if dry_run:
        return dst_dir, note + "; would copy its conversation from %s" % src_acct
    claude_session.copy_transcript(src, registry.claude_home(dst_dir), sess["id"])
    if trust and has_cwd:
        trust(cwd, dst_dir)
    if dst_dir and has_cwd:
        accounts.link_memory(commands.repo_root(cwd), dst_dir, registry.DEFAULT_CLAUDE_HOME)
    return dst_dir, note + "; conversation copied from %s" % src_acct
