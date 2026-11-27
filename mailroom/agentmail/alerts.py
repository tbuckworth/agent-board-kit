"""Mailroom alerts to the owner: unrouted mail from the mailroom marked as an alert (routing.ALERT_MARK),
deduplicated, and the nudge: with mail to the owner kept for the Mail tab alone and Gmail on, a content-free mail
through Gmail saying how much is waiting there. A reply to either reaches the orchestrator as a reply to a mailroom
notice (routing.match). With Gmail off (config "gmail": false) every alert is kept for the Mail tab, and a security
alert is also recorded for the board (outbound.record_security_alert); there is no nudge."""
import time

from . import config, gmail, mailbox, outbound, routing

DEDUPE_SECONDS = 6 * 3600
FOOTER = "\n\nReply to this email to reach the orchestrator.\n"
NUDGE = "alert:nudge:last"   # the meta key of the last nudge's time, with :thread_id like an alert's
NUDGE_UPTO = "nudge:upto"    # the meta key of the highest kept sent row when the last nudge went
NUDGE_TEXT = "New agent mail since the last notice: %d. It is on the Agent Board, not in Gmail: open its Mail tab."


def remember(st, meta_key, now, thread_id):
    """Record an alert sent at now under meta_key, with its thread, which Store.is_alert_thread reads so that a reply
    in its thread goes to the orchestrator."""
    st.set_meta(meta_key, now)
    st.set_meta(meta_key + ":thread_id", thread_id)


def send_alert(svc, st, cfg, kind, key, subject, body):
    """Send at most one alert of kind (outbound.ALERT_KINDS) per (kind, key) per DEDUPE_SECONDS, as unrouted mail
    through outbound.transmit, so it goes where other agent mail to the owner goes (config mailbox.owner_delivery; with
    Gmail off, kept for the Mail tab, and recorded for the board when of a security kind), or through Gmail all the
    same when outbound.alert_through_gmail says so. Returns True if sent. Raises ValueError on a kind ALERT_KINDS
    does not hold."""
    if not cfg["alerts_enabled"]:
        return False
    gmail_too = outbound.alert_through_gmail(cfg, kind)
    meta_key = "alert:%s:%s" % (kind, key)
    last = st.get_meta(meta_key)
    now = time.time()
    if last and now - float(last) < DEDUPE_SECONDS:
        return False
    to = config.alert_address(cfg)
    if not to:
        return False
    res, _mid = outbound.transmit(outbound.Context.opened(cfg, st, svc), None, to, None, routing.ALERT_PREFIX + subject,
                                  outbound.Mail(body + FOOTER, None, [], []), None, alert=True,
                                  through_gmail=gmail_too, kind=kind)
    remember(st, meta_key, now, res["threadId"])
    return True


def nudge(svc, st, cfg, now):
    """With config mailbox.owner_delivery "local" and nudge_minutes set: when agent mail kept for the Mail tab alone
    since the previous nudge (after NUDGE_UPTO) is still unread in the tab's inbox (mailbox.unread_kept), and that nudge
    was nudge_minutes or more before now, mail the owner through Gmail how many there are and nothing else of them: no
    subject, sender, thread or text. Records it in meta (NUDGE, its time; NUDGE_UPTO, the newest mail kept by then).
    Returns how many it told of, 0 when it sent nothing, as always with Gmail off (config.gmail_on). Calls the Gmail
    API."""
    if not config.gmail_on(cfg):
        return 0
    s = outbound.mailbox_settings(cfg)
    to = config.owner_address(cfg)
    if (s["owner_delivery"] != "local" or s["nudge_minutes"] is None or not to
            or now - float(st.get_meta(NUDGE, 0)) < 60 * s["nudge_minutes"]):
        return 0
    count, upto = mailbox.unread_kept(st, cfg, int(st.get_meta(NUDGE_UPTO, 0)), now)
    if not count:
        return 0
    msg = outbound.compose(cfg, "mailroom", to, None,
                           routing.ALERT_PREFIX + "%d new agent message%s" % (count, "" if count == 1 else "s"),
                           NUDGE_TEXT % count + FOOTER, alert=True)
    res = gmail.send_raw(svc, msg.as_bytes())
    remember(st, NUDGE, now, res["threadId"])
    st.set_meta(NUDGE_UPTO, upto)
    return count
