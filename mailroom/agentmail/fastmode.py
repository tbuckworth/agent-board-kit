"""Fast mode on a thread of the Mail tab (README.md, "Fast mode"), for quick back-and-forths between the owner and
the agent that owns the thread. Switched on from the board (`agent-mail mailbox fast THREAD on`: mailbox.fast, which
finds the agent and calls start), it

- puts the agent's effort to low with its own keys (agentmail/paneeffort.py), keeping the level to put back;
- puts a line on each of the owner's messages to the agent in the thread telling it to answer at once in a few plain lines
  (reminder, which mailroom.owner_reminder gives instead of the usual reminders);
- lets that agent's `agent-mail reply` to the owner in the thread skip the pre-send review (skips_review).

It ends when the owner switches it off (end), or when its minutes (config mailbox.fast_minutes, kept on the row) have passed
since it was switched on or since their last message in the thread, whichever is later (until). Ending tells the agent
that fast mode is over and the usual mail rules apply again, and, when it was the agent's last fast thread, puts its
effort back.

The board's command only records the switch (start, end) and returns at once; the daemon does the rest at the start of
its next poll (tick, within config poll_seconds), or before a Mail-tab reply it routes between polls: it ends the
threads switched off or past their time, tells their agents, and presses every key, so no command waits on an agent's
pane or holds the keys lock for long.

The effort belongs to the agent, not the thread: a mailbox_fast row per thread names its agent (tag), with ended set
once it is switched off and not yet ended by the daemon, and one fast_agents row per agent holds what fast mode does to
its effort, lowered once whatever its number of fast threads and put back when the last ends. Its effort is one of
EFFORTS:
  lowering   the keys are to lower it, at the daemon's next pass; they wait while paneeffort.ready gives a reason
  low        lowered, or perhaps partly (keys went and the level was not read back; note says so): put back at the end
  unchanged  fast mode changes only how the agent answers; note says why
  raising    its last fast thread has ended and the keys are to put it back to was; tried at each pass, and after
             RESTORE_TRIES polls that did not finish it (a pass between polls counts none), given up and the owner
             alerted (kind fast-mode, through Gmail)
  unreported the restore was given up and the alert has not gone out yet (Gmail failed): tried again at each poll
level is the level the agent is believed to run at now (None: not known), was the one to put back, pressed how many
keys may have gone (counted before each is sent, so a pass cut off mid-way still restores), tries the polls a restore
has taken. One process changes the rows at a time (STATE_LOCK: the board's command or the daemon);
paneeffort.set_level holds the keys lock (commands.keys_lock) while it presses keys. When a key is followed at once by
the agent's prompt going (paneeffort.Wait.risk: a dialog opened as it was pressed), the owner is alerted (kind fast-keys,
through Gmail) to check that dialog's highlight before answering it."""
import subprocess
import time

from . import commands, config, deliver, outbound, paneeffort, registry, review, routing
from .commands import CommandError

EFFORTS = ("lowering", "low", "unchanged", "raising", "unreported")
STATE_LOCK = config.STATE_DIR / "fast.lock"
RESTORE_TRIES = 10   # polls a restore (or a row's work that keeps failing) is tried before it is given up and reported
# The line on each of the owner's messages to the agent while fast mode is on, in place of routing.reply_reminder's.
REMIND = ("[Fast mode, which %(owner)s switched on in this thread: answer at once in a few plain lines, with no "
          "headings, lists or tables. Your agent-mail reply here skips the pre-send review while it lasts.]\n")
# What the agent is told when a thread's fast mode ends: why, what becomes of its effort, and that the rules are back.
ENDED = {"off": "%(owner)s switched it off",
         "expired": "%(owner)s wrote nothing in it for %(minutes)d minutes"}
NOTICE = ('[mailroom: fast mode is over in the thread "%(subject)s": %(why)s. %(effort)sWrite mail as usual again '
          '(agent-mail guide); your agent-mail reply goes through the pre-send review again.]')
EFFORT_BACK = "Your effort is going back to %(was)s. "
EFFORT_STAYS = {"low": "Fast mode is still on in %(others)d other thread%(s)s, so your effort stays low. ",
                "lowering": "Fast mode is still on in %(others)d other thread%(s)s, so your effort is still to go to "
                            "low. "}
# A key pressed as a dialog opened (paneeffort.Wait.risk): the note on the thread, and the alert to the owner.
RISK_NOTE = "A key fast mode pressed may have moved the highlight of a dialog that opened just then: check its pane."
RISK_ALERT = ("Fast mode pressed shift+%(key)s in %(label)s's pane as a dialog opened there (%(why)s), so the key may "
              "have moved that dialog's highlight. Look at the agent's pane before you answer the dialog.")
GAVE_UP = ("[mailroom: your effort could not be put back to %(was)s after fast mode (%(error)s), so it may still be "
           "low. %(told)s]")
TOLD = "%(owner)s has been told."
NOT_TOLD = "%(owner)s could not be told: %(why)s."


def state_lock():
    """commands.file_lock on STATE_LOCK, waited for."""
    return commands.file_lock(STATE_LOCK, True)


def label(st, cfg, tag):
    """How the agent of reply tag tag is named to the owner: its session's label (outbound.session_label), or its tag
    when the mailroom no longer knows its session."""
    sess_row = st.session_by_tag(tag)
    return outbound.session_label(dict(sess_row), cfg["host_id"]) if sess_row else "agent [#%s]" % tag


def alert_key(tag):
    """The key one fast mode event's alert about the agent of reply tag tag is sent under (alerts.send_alert's dedupe
    key): its tag and the time now, so that each event's alert goes out once, however often it is retried, and a
    later event's goes out too."""
    return "%s:%.6f" % (tag, time.time())


def minutes(cfg):
    """How long fast mode lasts: config mailbox.fast_minutes (outbound.MAILBOX_DEFAULTS)."""
    return outbound.mailbox_settings(cfg)["fast_minutes"]


def until(st, cfg, row):
    """When fast mode row ends unless the owner writes in its thread again: its minutes after it was switched on, or after
    their last message in the thread (from an owner address, config allowlist), whichever is later."""
    last = st.newest_from(row["thread_id"], sorted(a.lower() for a in cfg["allowlist"]))
    return max(row["since"], last or 0) + 60 * row["minutes"]


def active(st, cfg, thread_id, now):
    """Thread thread_id's fast mode row while it is on at now (not switched off, its time not passed), else None."""
    row = st.fast_row(thread_id)
    return row if row is not None and row["ended"] is None and until(st, cfg, row) > now else None


def item(st, cfg, row):
    """What `mailbox list` and `show` give of fast mode row (None: off) as a thread's "fast": null when it is off,
    else {"since", "until", "minutes", "agent", "effort", "was", "note"}, the effort as its agent's fast_agents row has
    it. A row past its until is shown until the daemon ends it, on its next poll; one switched off is not. While a
    fast-keys alert about its agent has not gone out (fast_alerts), the note carries RISK_NOTE."""
    if row is None or row["ended"] is not None:
        return None
    agent = st.fast_agent(row["tag"])
    note = agent["note"] if agent else "Effort unchanged: what fast mode did to it is no longer recorded."
    pending = any(a["tag"] == row["tag"] for a in st.fast_alerts().values())
    if pending and RISK_NOTE not in note:   # its alert has not gone out: the note says it
        note += " " + RISK_NOTE
    return {"since": row["since"], "until": until(st, cfg, row), "minutes": row["minutes"],
            "agent": label(st, cfg, row["tag"]), "effort": agent["effort"] if agent else "unchanged",
            "was": agent["was"] if agent else None, "note": note}


def reminder(st, cfg, inb, now):
    """The fast mode line for the owner's mail inb (an inbound row) to its session, when fast mode is on in its thread for
    that session at now; "" otherwise."""
    row = active(st, cfg, inb["thread_id"], now) if inb["thread_id"] else None
    return REMIND % {"owner": cfg["owner_name"]} if row is not None and row["tag"] == inb["matched_tag"] else ""


def skips_review(st, cfg, row, inb, caller, now):
    """Whether a reply in its thread from the session behind sessions row row to inbound mail inb, run by caller ((host,
    cli, session id) of the session running agent-mail, None when unknown), skips the pre-send review: inb is the owner's
    (from an owner address, so the reply goes to them alone), fast mode is on at now in its thread for that session,
    and caller is that session."""
    fast = active(st, cfg, inb["thread_id"], now) if inb["thread_id"] else None
    return (fast is not None and fast["tag"] == row["tag"]
            and review.audience(cfg, inb["from_addr"])[0] == "owner"
            and caller == (row["origin_host"], row["cli"], row["session_id"]))


def live_session(cfg, sess_row):
    """(the registry's live session for sessions row sess_row on this machine, "") or (None, why there is none)."""
    if sess_row["origin_host"] != cfg["host_id"]:
        return None, "the agent runs on %s, where the mailroom presses no keys" % sess_row["origin_host"]
    sess, code, _ = registry.by_id(registry.all_sessions(sess_row["cli"]), sess_row["session_id"])
    if code != 0 or not sess["live"]:
        return None, "the agent is not running"
    return sess, ""


def where(cfg, sess_row):
    """(live session, its paneeffort.Pane, "") for the agent of sessions row sess_row, or (session or None, None,
    why the keys cannot reach it: no pane, or a version whose keys were not tried, paneeffort.untested)."""
    sess, why = live_session(cfg, sess_row)
    if sess is None:
        return None, None, why
    pane, why = paneeffort.place(sess)
    if pane is not None:
        why = paneeffort.untested(sess, pane)
    return sess, (None if why else pane), why


def plan(cfg, sess_row):
    """(effort, was, note) for fast mode switched on for the agent of sessions row sess_row: "lowering" from the level
    it runs at (paneeffort.current) when that is one of paneeffort.KEYED, else "unchanged" and why."""
    sess, pane, why = where(cfg, sess_row)
    if pane is None:
        return "unchanged", None, "Effort unchanged: %s." % why
    try:
        level = paneeffort.current(sess, pane)
    except CommandError as e:
        return "unchanged", None, "Effort unchanged: its level could not be read (%s)." % e
    if level is None:
        return "unchanged", None, "Effort unchanged: nothing says what level it runs at."
    if level not in paneeffort.ORDER:
        return "unchanged", None, "Effort unchanged: it runs at %s, a level its keys do not step through." % level
    if paneeffort.ORDER.index(level) <= paneeffort.ORDER.index(paneeffort.LOW):
        return "unchanged", None, "Effort unchanged: it runs at %s already." % level
    if level not in paneeffort.KEYED:
        return "unchanged", None, "Effort unchanged: it runs at %s, which its keys cannot put back." % level
    return "lowering", level, "Back to %s when fast mode ends." % level


def start(ctx, thread_id, sess_row, now):
    """Switch fast mode on in thread thread_id for the agent of sessions row sess_row: record it, and, when it is the
    agent's first fast thread, plan what to do to its effort, for the daemon to do (tick). A thread switched off
    whose end the daemon has not reached is on again, its agent never told; an agent whose effort is being put back
    is to be lowered again. On already changes nothing. Returns the lines to log."""
    st, cfg = ctx.st, ctx.cfg
    tag = sess_row["tag"]
    with state_lock():
        row = st.fast_row(thread_id)
        if row is not None and row["ended"] is None:
            return []
        if row is not None:
            st.reopen_fast(thread_id, now, minutes(cfg))
            return ["fast   %s: on again before the daemon ended it" % thread_id]
        st.start_fast(thread_id, tag, now, minutes(cfg))
        agent = st.fast_agent(tag)
        if agent is None:
            effort, was, note = plan(cfg, sess_row)
            st.start_fast_agent(tag, effort, was, note)
            return ["fast   %s: on for %s (%s)" % (thread_id, sess_row["session_name"] or tag, note)]
        if agent["effort"] not in ("raising", "unreported"):
            return ["fast   %s: on for %s, whose effort fast mode has already (%s)" % (thread_id, tag,
                                                                                    agent["effort"])]
        st.update_fast_agent(tag, effort="lowering", tries=0, note="Back to %s when fast mode ends." % agent["was"])
        return ["fast   %s: on again before %s's effort was put back" % (thread_id, tag)]


def end(ctx, thread_id, why):
    """Switch fast mode off in thread thread_id, why being a key of ENDED: record it, for the daemon to end (tick):
    tell its agent, and put its effort back when this was its last fast thread (a restore that cannot finish is tried
    at each pass and, after RESTORE_TRIES polls, given up with a fast-mode alert to the owner, _give_up). Ending a thread
    where it is not on changes nothing. Returns the lines to log."""
    with state_lock():
        row = ctx.st.fast_row(thread_id)
        if row is None or row["ended"] is not None:
            return []
        ctx.st.end_fast(thread_id, why)
        return ["fast   %s: switched off, for the daemon to end" % thread_id]


def tick(ctx, now, counting):
    """The daemon's pass at the start of each poll, and before it routes the Mail tab's replies between polls: end
    each fast mode switched off or past its until (_end, which tells the agent), and press the keys each agent waits on
    (_press). A thread or agent whose work raises is logged and passed over; an agent whose work keeps raising is given
    up after RESTORE_TRIES polls, the owner alerted when its effort may be low (_give_up): a pass counts its failures as
    tries only when `counting` (at a poll). A fast-keys alert that did not go out is tried again first (_send_risk).
    Returns the lines to log."""
    st, cfg = ctx.st, ctx.cfg
    lines = []
    with state_lock():
        for thread_id, row in st.fast_rows().items():
            try:
                if row["ended"] is not None:
                    lines += _end(ctx, thread_id, row["ended"])
                elif until(st, cfg, row) <= now:
                    lines += _end(ctx, thread_id, "expired")
            except Exception as e:   # noqa: BLE001 - one thread must not stop the others
                lines.append("fast   %s: ending it failed: %s: %s" % (thread_id, type(e).__name__, e))
        for key in st.fast_alerts():
            lines += _send_risk(ctx, key)   # catches its own failure: the alert stays for the next poll
        for tag, agent in st.fast_agents().items():
            if agent["effort"] == "unreported":
                lines += _report(ctx, tag)   # catches its own failure: the row stays for the next poll
                continue
            if agent["effort"] not in ("lowering", "raising"):
                continue
            try:
                lines += _press(ctx, tag, counting)
            except Exception as e:   # noqa: BLE001 - one agent must not stop the others
                lines += _tried(ctx, tag, "%s: %s" % (type(e).__name__, e), counting)
    return lines


def _end(ctx, thread_id, why):
    """End thread thread_id's fast mode, with STATE_LOCK held: its row goes first, so that nothing after it can leave
    fast mode on; then its agent is told, and its effort is to go back when no fast thread of its own is left (the
    keys are pressed by tick, after)."""
    st, cfg = ctx.st, ctx.cfg
    row = st.fast_row(thread_id)
    if row is None:
        return []
    st.drop_fast(thread_id)
    tag = row["tag"]
    agent, others = st.fast_agent(tag), len(st.fast_threads_of(tag))
    lines, effort = ["fast   %s: ended (%s)" % (thread_id, why)], ""
    if agent is not None and others and agent["effort"] in EFFORT_STAYS:
        effort = EFFORT_STAYS[agent["effort"]] % {"others": others, "s": "" if others == 1 else "s"}
    elif agent is not None and not others:
        if agent["effort"] == "low" or agent["pressed"]:
            st.update_fast_agent(tag, effort="raising", tries=0)
            effort = EFFORT_BACK % {"was": agent["was"]}
        else:   # unchanged, or never lowered: nothing to put back
            st.drop_fast_agent(tag)
    answered = st.sent_in_thread(thread_id)
    fill = {"owner": cfg["owner_name"], "minutes": row["minutes"],
            "subject": routing.bare_subject(answered["subject"] or "") if answered else thread_id}
    return lines + _tell(ctx, tag, NOTICE % dict(fill, why=ENDED[why] % fill, effort=effort))


def _press(ctx, tag, counting):
    """Press the keys agent tag's fast_agents row waits on, with STATE_LOCK held: lower its effort to low
    ("lowering"), or put it back to was ("raising"). Each key is counted in the row before it goes
    (paneeffort.set_level's sent). When paneeffort.ready gives a reason, the row waits, the reason in its note; a
    restore counts the pass as a try when `counting` (_tried); a key that may have landed in a dialog opening as it
    went alerts the owner (_risk). An agent no longer running has no effort to put back. Returns the lines to log."""
    st, cfg = ctx.st, ctx.cfg
    agent = st.fast_agent(tag)
    if agent is None or agent["effort"] not in ("lowering", "raising"):
        return []
    lowering = agent["effort"] == "lowering"
    doing = "lower" if lowering else "put back"
    target, start_at = (paneeffort.LOW, agent["level"] or agent["was"]) if lowering else \
        (agent["was"], agent["level"] or paneeffort.LOW)
    sess, pane, error = where(cfg, st.session_by_tag(tag))
    if sess is None and not lowering:
        st.drop_fast_agent(tag)
        return ["fast   %s: no effort to put back: %s" % (tag, error)]
    level = agent["level"]
    if pane is not None:
        try:
            paneeffort.set_level(sess, pane, target, start_at, lambda: st.count_fast_key(tag))
            level, error = target, None
        except paneeffort.Wait as e:
            st.update_fast_agent(tag, level=None if e.pressed else level,
                                 note=("Waiting: %s.%s" if lowering else "Waiting to put back its effort: %s.%s")
                                 % (e, (" " + RISK_NOTE) if e.risk else ""))
            lines = ["fast   %s: waiting to %s its effort: %s" % (tag, doing, e)]
            if e.risk:
                lines += _risk(ctx, tag, target, e)
            return lines + ([] if lowering else _tried(ctx, tag, str(e), counting))
        except paneeffort.EffortError as e:
            level, error = e.level, str(e)
        except CommandError as e:
            level, error = None, str(e)
    pressed = st.fast_agent(tag)["pressed"]
    if not lowering:
        if error is None:
            st.drop_fast_agent(tag)
            return ["fast   %s: effort back to %s" % (tag, agent["was"])]
        st.update_fast_agent(tag, level=level, note="Not put back yet: %s." % error)
        return ["fast   %s: effort not put back yet: %s" % (tag, error)] + _tried(ctx, tag, error, counting)
    if error is None:
        st.update_fast_agent(tag, effort="low", level=level,
                             note="Effort low until fast mode ends, then %s again." % agent["was"])
        return ["fast   %s: effort low (was %s)" % (tag, agent["was"])]
    if pressed:   # keys may have gone, so it may have changed: put it back at the end all the same
        st.update_fast_agent(tag, effort="low", level=level,
                             note="Effort may be partly lowered (it reads %s: %s); back to %s when fast mode ends."
                             % (level or "no level", error, agent["was"]))
        return ["fast   %s: effort may be partly lowered: %s" % (tag, error)]
    st.update_fast_agent(tag, effort="unchanged", was=None, note="Effort unchanged: it could not be lowered (%s)."
                         % error)
    return ["fast   %s: effort not lowered: %s" % (tag, error)]


def _risk(ctx, tag, target, e):
    """Alert the owner (kind fast-keys, through Gmail: outbound.SECURITY_ALERTS) that the key just pressed in agent tag's
    pane, toward target, may have moved the highlight of a dialog that opened as it went (paneeffort.Wait e, with
    e.risk). The alert is kept (fast_alerts, under this event's alert_key) until it goes out (_send_risk). Returns the
    lines to log."""
    st, cfg = ctx.st, ctx.cfg
    who, key = label(st, cfg, tag), alert_key(tag)
    st.keep_fast_alert(key, tag, "fast mode pressed a key in %s's pane as a dialog opened" % who,
                       RISK_ALERT % {"key": "down" if target == paneeffort.LOW else "up", "label": who, "why": e})
    return _send_risk(ctx, key)


def _send_risk(ctx, key):
    """Send the kept fast-keys alert of event key, and drop it unless sending fails (Gmail), which leaves it, and
    RISK_NOTE on the thread, for the next poll. send_alert sends nothing with alerts off in config or with no owner
    address, each said in the log, or when this event's alert went out already. Returns the lines to log."""
    st, cfg = ctx.st, ctx.cfg
    from . import alerts   # alerts imports mailbox, which imports this module
    kept = st.fast_alerts()[key]
    tag = kept["tag"]
    try:
        sent = alerts.send_alert(ctx.svc, st, cfg, "fast-keys", key, kept["subject"], kept["body"])
    except Exception as err:   # noqa: BLE001 - Gmail may fail: the alert stays and the next poll tries again
        return ["fast   %s: ALERT about a key in a dialog NOT SENT, tried again at the next poll: %s: %s"
                % (tag, type(err).__name__, err)]
    st.drop_fast_alert(key)
    why = unalerted(cfg)
    return ["fast   %s: %s %s about a key that may have landed in a dialog" % (
        tag, cfg["owner_name"], "alerted" if sent else "NOT alerted: " + why if why else "alerted already")]


def unalerted(cfg):
    """Why config lets no alert reach the owner (alerts.send_alert sends nothing then), or ""."""
    return ("alerts are off (config alerts_enabled)" if not cfg["alerts_enabled"] else
            "config has no owner address" if not config.owner_address(cfg) else "")


def _tried(ctx, tag, error, counting):
    """Count a poll in which agent tag's row did not get its work done (error: why), when `counting` (a pass between
    polls counts nothing); at RESTORE_TRIES, give it up (_give_up). Returns the lines to log."""
    agent = ctx.st.fast_agent(tag)
    if agent is None:
        return []
    if not counting:
        return ["fast   %s: failed between polls, not counted as a try: %s" % (tag, error)]
    tries = agent["tries"] + 1
    ctx.st.update_fast_agent(tag, tries=tries)
    if tries < RESTORE_TRIES:
        return ["fast   %s: try %d of %d failed: %s" % (tag, tries, RESTORE_TRIES, error)]
    return _give_up(ctx, tag, error)


def _give_up(ctx, tag, error):
    """Stop trying agent tag's effort work. A lowering is left as it is: low if keys were sent (put back at the end),
    else unchanged. A restore becomes "unreported", its error in the note, and the owner is alerted (_report). Returns the
    lines to log."""
    st = ctx.st
    agent = st.fast_agent(tag)
    if agent["effort"] == "lowering":
        if agent["pressed"]:
            st.update_fast_agent(tag, effort="low", note="Effort may be partly lowered (%s); back to %s when fast "
                                                         "mode ends." % (error, agent["was"]))
        else:
            st.update_fast_agent(tag, effort="unchanged", was=None, note="Effort unchanged: %s." % error)
        return ["fast   %s: gave up lowering its effort: %s" % (tag, error)]
    st.update_fast_agent(tag, effort="unreported", note=error, alert_key=alert_key(tag))
    return ["fast   %s: gave up putting its effort back to %s: %s" % (tag, agent["was"], error)] + _report(ctx, tag)


def _report(ctx, tag):
    """Alert the owner that agent tag's effort was not put back (kind fast-mode, through Gmail: outbound.SECURITY_ALERTS),
    tell the agent whether the owner was, and drop the row. send_alert sends nothing when alerts are off in config, when
    config has no owner address, or when this give-up's alert (its row's alert_key) went out already: the first two
    are said, to the agent and in the log, and the last counts as told. When sending fails (Gmail), it is logged and
    the row stays "unreported", for the next poll. Returns the lines to log."""
    st, cfg = ctx.st, ctx.cfg
    agent = st.fast_agent(tag)
    from . import alerts   # alerts imports mailbox, which imports this module
    who = label(st, cfg, tag)
    try:
        sent = alerts.send_alert(ctx.svc, st, cfg, "fast-mode", agent["alert_key"],
                                 "fast mode could not put %s's effort back" % who,
                                 "Fast mode ended, and %s's effort could not be put back to %s after %d tries: %s.\n\n"
                                 "It may still run at low effort. Set it in its pane (shift+up) or with the board's "
                                 "switch." % (who, agent["was"], RESTORE_TRIES, agent["note"]))
    except Exception as e:   # noqa: BLE001 - Gmail may fail: the row stays and the next poll tries again
        return ["fast   %s: ALERT NOT SENT, tried again at the next poll: %s: %s" % (tag, type(e).__name__, e)]
    why = unalerted(cfg)
    told = NOT_TOLD % {"owner": cfg["owner_name"], "why": why} if why else TOLD % {"owner": cfg["owner_name"]}
    st.drop_fast_agent(tag)
    lines = ["fast   %s: %s %s" % (tag, cfg["owner_name"], "alerted" if sent else
                                    "NOT alerted: " + why if why else "alerted already")]
    return lines + _tell(ctx, tag, GAVE_UP % {"was": agent["was"], "error": agent["note"], "told": told})


def _tell(ctx, tag, text):
    """Hand text to the session of tag, never resuming one that is not running (deliver.deliver_session with resume
    off). Returns the lines to log: told (and, for a Codex agent that holds it in its queue, why), or why not."""
    sess_row = ctx.st.session_by_tag(tag)
    if sess_row is None:
        return ["fast   %s: no session to tell" % tag]
    who = label(ctx.st, ctx.cfg, tag)
    try:
        rc, out = deliver.deliver_session(sess_row, text, ctx.cfg, resume=False)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        rc, out = 1, str(e)
    if rc == deliver.HELD:
        return ["fast   %s: told %s, held in Codex's queue: %s" % (tag, who, (out.splitlines() or [""])[-1])]
    return ["fast   %s: told %s" % (tag, who) if rc == 0 else
            "fast   %s: telling %s failed (%d): %s" % (tag, who, rc, out)]
