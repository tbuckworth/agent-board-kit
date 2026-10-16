"""Mailroom alerts to the owner: plain mail from the agents account, no routing tag, deduped."""
import time
from email.message import EmailMessage
from email.utils import formataddr

from . import config, gmail

DEDUPE_SECONDS = 6 * 3600


def send_alert(svc, st, cfg, kind, key, subject, body):
    """Send at most one alert per (kind, key) per DEDUPE_SECONDS. Returns True if sent."""
    if not cfg.get("alerts_enabled", True):
        return False
    meta_key = "alert:%s:%s" % (kind, key)
    last = st.get_meta(meta_key)
    now = time.time()
    if last and now - float(last) < DEDUPE_SECONDS:
        return False
    to = cfg.get("alert_to") or config.owner_email(cfg)
    if not to:
        return False
    msg = EmailMessage()
    msg["From"] = formataddr(("%s (mailroom)" % cfg["from_name"], config.require_account(cfg)))
    msg["To"] = to
    msg["Subject"] = "[mailroom] " + subject
    msg["X-Agent-Mailroom"] = "alert"
    msg.set_content(body + "\n\nReply to this email to reach the orchestrator.\n")
    receipt = gmail.send_raw(svc, msg.as_bytes())
    st.set_meta(meta_key, now)
    st.set_meta(meta_key + ":message_id", receipt["id"])
    st.set_meta(meta_key + ":thread_id", receipt["threadId"])
    return True
