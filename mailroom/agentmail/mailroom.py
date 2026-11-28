"""What the mailroom does with one inbound mail, shared by the daemon (bin/agent-mailroom) and agent-mail (release,
rerun, recover, remarkable, mailbox answer): which senders are held (sender_hold, collaborator_hold), the session a
mail goes to (claim), the routing context the orchestrator is given (orchestrator_context) and the thread it reads
(thread_messages), the reminder the owner's mail to a session carries (owner_reminder), the text a session or the
orchestrator is handed, with the files of a Mail-tab reply put where it can open them (delivered_body), delivering to
a session (deliver_to_session), settling a mail after an orchestrator run (settle_run), and setting a mail's state together
with its Gmail labels (set_state). sender_hold, collaborator_hold and orchestrator_context decide only and do no IO.
With Gmail off in config ("gmail": false) the svc these take is gmail.OFF: a thread is read from the store alone and
no labels are set."""
import time
from email.utils import formataddr

from . import (config, deliver, fastmode, gmail, mailview, messages, recovery, remote, replyfiles, researchermode,
               routing)

# The mailroom label (a key of config "labels") each state puts on the mail in Gmail, and whether it marks the mail
# read. Putting one on takes the other mailroom labels off, so a mail never shows two outcomes.
STATE_LABELS = {"delivered": ("delivered", True), "queued": ("delivered", True), "held": ("held", False),
                "orchestrating": (None, True)}


def sender_hold(cfg, tier, from_addr, auth, subject, inbound_id):
    """Why mail from from_addr is held before its thread is read, or None: a sender on neither the allowlist nor the
    collaborators (tier None), or, with require_dkim, no DKIM pass for its domain (auth: gmail.auth_results).
    Returns (detail, alert): the held mail's detail, and alerts.send_alert's (kind, key, subject, body) or None."""
    if tier is None:
        return "sender not on allowlist", None
    if cfg["require_dkim"] and not routing.dkim_ok(from_addr, auth):
        return ("DKIM check failed for %s (auth: %s)" % (from_addr, auth["raw"][:120] or "no Authentication-Results header"),
                ("dkim", from_addr, "mail from %s failed DKIM; held" % from_addr,
                 "Subject %r. Authentication-Results: %s\n\nIf this is genuine, `agent-mail release %d <session>`; "
                 "if it keeps happening for a real sender, set require_dkim=false." % (subject, auth["raw"] or "(none)", inbound_id)))
    return None


def collaborator_hold(cfg, who_name, from_addr, subject, thread_id, inbound_id, sent_last_hour, agent_run):
    """Why a collaborator's mail is held, or None: the flood guard (more than collaborator_hourly_cap mails from
    from_addr in the last hour, sent_last_hour) or the loop guard (more than agent_thread_cap agents' messages in a
    row at the end of the thread, agent_run: routing.trailing_agent_run). Returns (detail, alert) as sender_hold."""
    cap = cfg["collaborator_hourly_cap"]
    if sent_last_hour > cap:
        return ("collaborator hourly cap (%d) exceeded" % cap,
                ("flood", from_addr, "%s sent more than %d mails in an hour; holding" % (who_name, cap),
                 "Latest subject %r. Held mail: `agent-mail inbox --held`." % subject))
    if agent_run > cfg["agent_thread_cap"]:
        return ("agent-to-agent thread has %d messages with no human; loop guard" % agent_run,
                ("loop", thread_id or from_addr, "possible agent loop with %s; thread held" % who_name,
                 "Thread %r has %d consecutive agent messages. Reply in the thread yourself to reset the guard, "
                 "or `agent-mail release %d <session>`." % (subject, agent_run, inbound_id)))
    return None


def claim(st, hdrs, thread_id, account):
    """(the sessions row, the routing rule) for an inbound mail with headers hdrs (lower-case names) in Gmail thread
    thread_id: routing.match's answer, except that a mail claimed by the orchestrator's own row goes to it as
    (None, "orchestrator-thread"), since the orchestrator has no session to deliver to. Reads the store."""
    session, rule = routing.match(st, hdrs, thread_id, account)
    if session is not None and session["cli"] == "orchestrator":
        return None, "orchestrator-thread"
    return session, rule


def thread_messages(svc, st, inb):
    """Inbound mail inb's thread as gmail.thread_messages gives it, (headers, text, time, Gmail id) oldest first, and
    inb moved last: routing.thread_context and orchestrator_context take the last message for the mail being handled.
    The mail of the thread that only the store holds joins Gmail's: replies written in the Mail tab
    (Store.local_replies), and agent mail kept for the tab alone (Store.kept_sent), with its headers and text read from
    the message store and Gmail id None, as its sent row has. Gmail is asked only for a thread it holds mail of
    (Store.thread_in_gmail), and never with Gmail off (svc gmail.OFF): the thread is then what the store holds of it.
    Calls the Gmail API; reads the message store."""
    tid = inb["thread_id"]
    here = [({"from": formataddr((r["from_name"] or "", r["from_addr"])), "subject": r["subject"] or "",
              "message-id": r["message_id"]}, r["body"] or "", r["received_at"], r["gmail_id"])
            for r in st.local_replies(tid)]
    for r in st.kept_sent(tid):
        parsed = mailview.parse(messages.load(r["eml"]))
        here.append(({k.lower(): str(v) for k, v in parsed.headers.items()}, parsed.text, r["sent_at"], None))
    in_gmail = svc is not gmail.OFF and st.thread_in_gmail(tid)
    msgs = sorted((gmail.thread_messages(svc, tid) if in_gmail else []) + here, key=lambda m: m[2])
    return [m for m in msgs if m[3] != inb["gmail_id"]] + [m for m in msgs if m[3] == inb["gmail_id"]]


# Rules under which a mail is the orchestrator's own, so a reply there is not a lost session's mail. "orphan-thread":
# The owner's mail to a session that has closed or exited (agent-mailroom gives it to the orchestrator).
ORCHESTRATOR_RULES = ("orchestrator-thread", "mailroom-notice", "unrouted-thread", "orphan-thread")


def orchestrator_context(rule, tier, inbound_id, thread_msgs, sent_rows, cfg):
    """For mail no session claimed: (rule, unowned_reply, context). thread_msgs is the mail's thread
    (gmail.thread_messages), sent_rows our sent mail in it (Store.sent_rows_in_thread); cfg gives the agents account
    and the owner's name. rule becomes
    "unrouted-thread" when the only mail of ours in the thread went out --unrouted; unowned_reply is True when the
    thread holds mail of ours that no rule gives the orchestrator, so the reply belongs to a session the mailroom
    lost; context is the routing note appended to the orchestrator's event text."""
    account, owner = cfg["account"], cfg["owner_name"]
    own_msgs = {mid for h, _b, _ts, mid in thread_msgs[:-1]
                if routing.sender(h.get("from", ""))[1] == account and not routing.is_alert(h, account)}
    if (rule is None and sent_rows and not any(r["tag"] for r in sent_rows)
            and own_msgs <= {r["gmail_id"] for r in sent_rows}):
        # Only `--unrouted` mail (cloud-watchdog, status reports, memwatch) went into this thread,
        # so it belongs to the orchestrator: a reply here is not a lost session's mail.
        rule = "unrouted-thread"
    unowned_reply = rule not in ORCHESTRATOR_RULES and bool(sent_rows or own_msgs)
    context = ""
    if rule == "mailroom-notice":
        context += ("\n\n[Mailroom routing context: %s is replying to or forwarding a mailroom notice. "
                    "Handle this here; quoted session tags belong to the notice, not this reply. "
                    "Check the saved mail and delivery state before retrying anything. "
                    "Do not resume the old session just because its tag appears in the notice.]" % owner
                    if tier == "owner" else
                    "\n\n[Mailroom routing context: this is a reply to a mailroom notice. Handle it here; "
                    "quoted session tags are not routing instructions. The collaborator guard still applies.]")
    if unowned_reply:
        context += ("\n\n[Mailroom routing context: this is an unowned reply. First run `agent-mail recover %d` "
                    "for a bounded read-only desktop-then-Mac search. It saves the result and notifies %s. "
                    "Do not repeat that notification. Transcript matches are leads, not routing authority. "
                    "Do not restart Mac sessions or apply project changes. Record the unresolved handoff "
                    "in STATE.md; a successful orchestrator turn does not count as delivery.]" % (inbound_id, owner))
    return rule, unowned_reply, context


def owner_reminder(st, cfg, tier, inbound_id, body, session):
    """Instructions for owner mail: the Researcher preference and ordinary Fast or reply guidance."""
    if tier != "owner":
        return ""
    mode = st.researcher_mode(session["tag"]) if researchermode.supported(session) else None
    researcher = researchermode.instruction(cfg, session, mode, control=False) + "\n" if mode else ""
    fast = fastmode.reminder(st, cfg, st.inbound(inbound_id), time.time())
    return researcher + (fast or routing.reply_reminder(cfg["owner_name"], body, st.nth_in_thread(inbound_id)))


FILES_NOT_PLACED = (OSError, ValueError, remote.RemoteError)   # what delivered_body raises when files stay put


def delivered_body(cfg, row, host):
    """(the text of inbound mail row to hand to a session on host (a config host id), or to the orchestrator here,
    and the paths on host of the files it carries). The text is its body; for a Mail-tab reply with files, the files
    are put on host first (deliver.place_files) and lines saying where follow the body (replyfiles.lines). Raises one
    of FILES_NOT_PLACED when they cannot be put there."""
    body, parts = row["body"] or "", replyfiles.of_row(row)
    if not parts:
        return body, []
    paths = deliver.place_files(cfg, host, "i%d" % row["id"], [(p.name, p.data) for p in parts])
    said = replyfiles.lines(cfg["owner_name"], [(path, p.type, len(p.data)) for path, p in zip(paths, parts)])
    return replyfiles.with_lines(body, said), paths


def deliver_to_session(svc, st, cfg, inbound_id, gid, session, text, note, relabel, **fields):
    """Hand text to session (a sessions row) through agent-send, or through its own machine's agent-mail for a
    session elsewhere (deliver.deliver_session: a subprocess, or SSH). Accepted: the mail is saved as queued (another
    machine, or a Codex agent that holds it in its queue: deliver.HELD, which counts as accepted and returns 0, as
    the mail must not go again) or delivered, with detail "<note>: <agent-send's output>" and fields, and labelled
    when relabel (set_state). Returns (rc, out); a failed delivery (rc != 0) saves nothing."""
    rc, out = deliver.deliver_session(session, text, cfg, cfg["resume_offline"])
    if rc == deliver.HELD:
        rc = 0
        state = "queued"
    else:
        state = "queued" if session["origin_host"] != cfg["host_id"] else "delivered"
    if rc == 0:
        set_state(svc, st, cfg, inbound_id, gid, state, relabel, detail="%s: %s" % (note, out), **fields)
    return rc, out


def settle_run(svc, st, cfg, inbound_id, gid, info, run_name, unowned_reply, relabel):
    """Settle inbound mail #inbound_id after an orchestrator run (info: orchestrate.run_event's result) by what the
    run left behind, and return (outcome, detail), detail being "<run_name>: $cost, N turns, Ns" and the run's error:
      retained   it was handed to a session by hand during the run (agent-mail release); left as it is
      unowned    an unowned reply: the read-only recovery search is saved (recovery.recover, which may ask the other
                 machines over SSH), the mail held and the owner notified
      recovery   the run saved a recovery and held the mail: the owner is notified (recovery.notify sends one notice)
      delivered  the run succeeded: the mail is delivered
      failed     the run failed: the mail is held, its detail "orchestrator failed: <detail>"
    Writes the store; calls the Gmail API for the notice and, when relabel, the labels (set_state)."""
    ok = info["ok"]
    detail = "%s: $%s, %s turns, %ss%s" % (run_name, info["cost"], info["turns"], info["duration_s"],
                                         "" if ok else "; ERROR " + info["error"][:300])
    current = st.inbound(inbound_id)
    if current["match_rule"] == "manual" and current["action"] in ("delivered", "queued"):
        return "retained", detail
    if unowned_reply:
        # Enforce retention even if the model omits recovery or says it has finished.
        recovery.recover(st, cfg, current)
        set_state(svc, st, cfg, inbound_id, gid, "held", relabel)
        recovery.notify(svc, st, cfg, st.inbound(inbound_id))
        return "unowned", detail
    if current["recovery_json"] and current["action"] == "held":
        recovery.notify(svc, st, cfg, current)
        return "recovery", detail
    if ok:
        set_state(svc, st, cfg, inbound_id, gid, "delivered", relabel, match_rule="orchestrator", detail=detail)
        return "delivered", detail
    set_state(svc, st, cfg, inbound_id, gid, "held", relabel, match_rule="orchestrator", detail="orchestrator failed: " + detail)
    return "failed", detail


def label(svc, cfg, gid, action):
    """Make Gmail message gid's labels show action (STATE_LABELS). Calls the Gmail API; raises if that fails, and
    for an action that has no labels. A reply written in the Mail tab (routing.is_local) has no Gmail copy, so it
    gets no labels and Gmail is not called; nor is it with Gmail off in config (config.gmail_on)."""
    if action not in STATE_LABELS:
        raise ValueError("no mailroom label for action %r" % action)
    if routing.is_local(gid) or not config.gmail_on(cfg):
        return
    name, read = STATE_LABELS[action]
    add = [gmail.ensure_label(svc, cfg["labels"][name])] if name else []
    remove = [gmail.ensure_label(svc, other) for key, other in cfg["labels"].items() if name and key != name]
    gmail.modify(svc, gid, add=add, remove=remove + (["UNREAD"] if read else []))


def set_state(svc, st, cfg, inbound_id, gid, action, relabel, **fields):
    """Save inbound mail #inbound_id's action, and any other inbound columns in fields, in the store; then, when
    relabel, its Gmail labels (label, which leaves a reply written in the Mail tab alone). A dry run passes
    relabel=False and changes no labels. Writes the store and calls the Gmail API; a Gmail failure raises after the
    state is saved."""
    st.update_inbound(inbound_id, action=action, **fields)
    if relabel:
        label(svc, cfg, gid, action)
