"""Mail out of the agents account, for agent-mail: what its commands work with (Context), a mail's content (Mail)
and the MIME message made of it (the text, an HTML part, inline images, attachments), kept in the message store and
then sent, or, with config mailbox.owner_delivery "local" (MAILBOX_DEFAULTS) or with Gmail off (config "gmail":
false), kept for the Mail tab alone when only to the owner (transmit), with Gmail off mail to anyone else refused
(refuse_without_gmail) and each security alert kept also recorded for the board (record_security_alert), the
recipient a --to or --cc value names (recipient), loading the images and files it
carries, sending a new mail or a reply from a session once the pre-send review lets it go (send_mail, reply_mail),
withdrawing a held draft, and sending the held drafts whose time ran out (the daemon runs `agent-mail
flush-drafts`). The send, reply and withdraw functions return what agent-mail prints and raise MailError where it
stops, so a command proxied from another machine (rpc.py) runs the same code; flush_drafts, which only the daemon
runs, prints its own lines."""
import base64
import functools
import html as htmllib
import json
import os
import re
import secrets
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import NamedTuple, Optional

from . import config, gmail, imagefile, messages, replyfiles, review, routing
from .compose import NOTE, html_to_text, image_html, placed_images, text_to_html
from .store import Store


class MailError(Exception):
    """A mail or command that cannot go ahead as asked; nothing was sent. exit_code is the code agent-mail stops
    with."""

    def __init__(self, message, exit_code):
        super().__init__(message)
        self.exit_code = exit_code


# The exit code of agent-mail when what it was asked needs Gmail and Gmail is off in config ("gmail": false): mail to
# anyone but the owner (refuse_without_gmail), a command that reads or changes Gmail, or anything else that would
# call it (gmail.GmailOff).
NEEDS_GMAIL = 5


class Context:
    """What agent-mail's commands work with: the config, and the store and the Gmail service, each opened on first
    use, so a command that needs neither (one proxied to another machine) opens neither. With Gmail off in config the
    service is gmail.OFF, which loads no token and raises gmail.GmailOff if anything uses it."""

    def __init__(self, cfg):
        self.cfg = cfg

    @classmethod
    def opened(cls, cfg, st, svc):
        """A Context on a store and a Gmail service already open (the daemon's)."""
        ctx = cls(cfg)
        ctx.__dict__.update(st=st, svc=svc)   # in place of what the cached properties would open
        return ctx

    @functools.cached_property
    def st(self):
        return Store()

    @functools.cached_property
    def svc(self):
        return gmail.service() if config.gmail_on(self.cfg) else gmail.OFF


def session_label(sess, host_id):
    """How mail and agent-mail's output name a session: its name (or id prefix) and CLI, with "@host" for a session
    on another machine than host_id (config host_id)."""
    if sess["cli"] == "orchestrator":
        return "orchestrator"
    if sess["cli"] == routing.GROUP_CLI:   # a group's routing row: the group's title
        return "group " + (sess.get("session_name") or "")[:40]
    name = sess.get("name") or sess.get("session_name") or ""
    sid = sess.get("id") or sess.get("session_id") or ""
    host = sess.get("host") or sess.get("origin_host")
    return (name[:40] if name else sid[:8]) + " (%s%s)" % (sess["cli"], "@" + host if host and host != host_id else "")


MAX_IMAGES_BYTES = 20 * 1024 * 1024
IMAGE_NAME_MAX = 64
IMAGE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,%d}" % IMAGE_NAME_MAX)   # an --image NAME, which becomes a cid: in the HTML


def check_image(name, data):
    """Raise MailError (exit 1) unless name can be the image's cid: (IMAGE_NAME_RE) and data is a PNG, JPEG, GIF or
    WebP file (imagefile.subtype) whose header gives its size (imagefile.size), which the review reads."""
    if not IMAGE_NAME_RE.fullmatch(name):
        raise MailError("image name %r: use 1 to 64 letters, digits, _ . -" % name, 1)
    kind = imagefile.subtype(data)
    if not kind:
        raise MailError("image %s is not a PNG, JPEG, GIF or WebP file" % name, 1)
    if imagefile.size(data) is None:
        raise MailError("image %s is a %s file whose header gives no size: is it damaged?" % (name, kind.upper()), 1)


def load_images(specs):
    """--image NAME=PATH values as [(name, bytes)], checked: unique names, real image files (check_image), size cap.
    Reads the files; raises MailError."""
    out, total = [], 0
    for spec in specs or ():
        name, sep, path = spec.partition("=")
        if not sep:
            raise MailError("--image wants NAME=PATH (got %r)" % spec, 1)
        if any(n == name for n, _ in out):
            raise MailError("--image name %r given twice" % name, 1)
        try:
            data = Path(os.path.expanduser(path)).read_bytes()
        except OSError as e:
            raise MailError("--image %s: %s" % (name, e), 1)
        check_image(name, data)
        total += len(data)
        out.append((name, data))
    if total > MAX_IMAGES_BYTES:
        raise MailError("images total %.1f MB; keep them under %d MB (Gmail's limit is 25 MB)"
                        % (total / 1e6, MAX_IMAGES_BYTES >> 20), 1)
    return out


def load_files(paths):
    """--attach PATH values as [(filename, bytes)] for ordinary attachments (PDF, zip, CSV...). Reads the files;
    raises MailError."""
    out = []
    for path in paths or ():
        f = Path(os.path.expanduser(path))
        try:
            out.append((f.name, f.read_bytes()))
        except OSError as e:
            raise MailError("--attach %s: %s" % (path, e), 1)
    if sum(len(d) for _, d in out) > MAX_IMAGES_BYTES:
        raise MailError("attachments total over %d MB; share a Drive link instead" % (MAX_IMAGES_BYTES >> 20), 1)
    return out


def plain_text(text, html_body):
    """A mail's plain text: text, unless it is None or blank and there is HTML, which gives it (html_to_text)."""
    return html_to_text(html_body) if html_body and not (text or "").strip() else text


class Mail(NamedTuple):
    """One mail's content: the plain text, the HTML part (None: rendered from the text), the inline images and
    attachments as [(name, bytes)], and the widgets its blocks became ({"kind", "spec"} each; widgets.prepare)."""
    text: str
    html: Optional[str]
    images: list
    files: list
    widgets: tuple = ()

    def as_json(self):
        """The content as JSON-able fields, images and files base64: how a held draft keeps it and a proxied request
        carries it (rpc.py). Widgets are added only when there are any, so a mail without them keeps the format a
        mailroom from before widgets reads."""
        return dict({"html": self.html, "text": self.text,
                     "images": [{"name": n, "data": base64.b64encode(d).decode()} for n, d in self.images],
                     "files": [{"name": n, "data": base64.b64encode(d).decode()} for n, d in self.files]},
                    **({"widgets": list(self.widgets)} if self.widgets else {}))

    @classmethod
    def from_json(cls, d):
        return cls(d["text"], d.get("html"), [(i["name"], base64.b64decode(i["data"])) for i in d.get("images") or ()],
                   [(i["name"], base64.b64decode(i["data"])) for i in d.get("files") or ()],
                   tuple(d.get("widgets") or ()))


def check_mail(mail):
    """Raise MailError when the mail cannot go as it is: its images and attachments together pass MAX_IMAGES_BYTES, an
    image line of its text names no image of the mail's (compose.placed_images), which would show a broken image, or
    it has nothing to read (no text, which a mail of HTML alone is given from its HTML; no image; no attachment), which
    would reach its reader as a blank card, the review not always stopping it (the send after a held mail goes
    whatever the review says). agent-mail checks every mail it builds (send, reply, unrouted), and the desktop every
    mail proxied from the Mac (rpc), before anything else is done with it."""
    if sum(len(d) for _, d in mail.images + mail.files) > MAX_IMAGES_BYTES:
        raise MailError("images and attachments total over %d MB" % (MAX_IMAGES_BYTES >> 20), 1)
    have = [name for name, _ in mail.images]
    missing = [name for name in placed_images(mail.text) if name not in have]
    if missing:
        raise MailError("![...](cid:%s) names no --image %s (the mail's: %s)" % (missing[0], missing[0],
                                                                              ", ".join(have) or "none"), 1)
    if not mail.text.strip() and not mail.images and not mail.files:
        raise MailError("nothing to send: the mail is empty; give it text (--body-file FILE), an image or an "
                        "attachment", 1)


def at_end(html_body, more):
    """html_body with the HTML `more` as the last thing in its body."""
    i = html_body.lower().rfind("</body>")
    return html_body[:i] + more + html_body[i:] if i >= 0 else html_body + more


def place_images(html_body, images):
    """html_body with each of images ([(name, bytes)]) that it does not place (no cid:NAME) shown at its end."""
    unplaced = [n for n, _ in images if not re.search(r"""cid:%s["'\s>]""" % re.escape(n), html_body)]
    return at_end(html_body, "".join(image_html(n, n) + "\n" for n in unplaced))


def compose(cfg, from_label, to, cc, subject, text, html_body=None, reply_to_tag=None,
            in_reply_to=None, references=None, images=(), files=(), alert=False):
    """One multipart/alternative message: the text as given, plus html_body or HTML rendered from the text.
    Images ride as multipart/related parts of the HTML, each with Content-ID <NAME>. A routed mail carries its reply
    tag in routing.MAILROOM_HEADER, an alert (alert, with no tag) routing.ALERT_MARK there. Raises config.ConfigError
    when config has no agents account."""
    account = config.require_account(cfg)
    msg = EmailMessage()
    msg["From"] = formataddr(("%s (%s)" % (cfg["from_name"], from_label), account))
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if reply_to_tag:
        msg["Reply-To"] = formataddr((cfg["from_name"], routing.tagged_address(account, reply_to_tag)))
        msg[routing.MAILROOM_HEADER] = reply_to_tag
    elif alert:
        msg[routing.MAILROOM_HEADER] = routing.ALERT_MARK
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(text)
    msg.add_alternative(place_images(html_body or text_to_html(text), images), subtype="html")
    if images:
        html_part = msg.get_payload()[1]
        for name, data in images:
            html_part.add_related(data, maintype="image", subtype=imagefile.subtype(data), cid="<%s>" % name,
                                  filename="%s.%s" % (name, imagefile.subtype(data).replace("jpeg", "jpg")),
                                  disposition="inline")
    attach(msg, files)
    return msg


def attach(msg, files):
    """Attach files ([(name, bytes)]) to msg (an EmailMessage), each with the content type its name suggests
    (replyfiles.content_type)."""
    for name, data in files:
        maintype, subtype = replyfiles.content_type(name).split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)


# config "mailbox": where agent mail to the owner goes. owner_delivery "gmail": through Gmail, with a copy kept in
# the message store; "local": kept in the message store alone, for the board's Mail tab (transmit, kept_here), but for
# the alerts alert_through_gmail sends through Gmail all the same. nudge_minutes, with "local": at most one
# content-free mail through Gmail every this many minutes while kept agent mail is unread in the tab (alerts.nudge);
# None for none. gmail_alerts, with "local": the kinds of alert (ALERT_KINDS) that go through Gmail besides
# SECURITY_ALERTS and PERSONAL_MAIL. fast_minutes: how long fast mode on a thread (agentmail/fastmode.py) lasts after it is
# switched on, or after the owner's last message in the thread. With Gmail off (config "gmail": false) the first three are taken as
# "local", None and [] whatever config says (mailbox_settings; gmail_ignored lists what it says in vain).
MAILBOX_DEFAULTS = {"owner_delivery": "gmail", "nudge_minutes": None, "gmail_alerts": [], "fast_minutes": 5}
OWNER_DELIVERIES = ("gmail", "local")

# Every kind of alert or notice the owner is sent, and what it tells: the mailroom's own and agent-failover's
# (alerts.send_alert), and those other tools send with `agent-mail send --unrouted --alert KIND`.
ALERT_KINDS = {
    "dkim": "mail claiming an allowlisted or collaborator sender failed DKIM and is held",
    "collab-thread": "a collaborator's agent started a new agent-to-agent thread",
    "flood": "a collaborator sent more than collaborator_hourly_cap mails in an hour; its mail is held",
    "loop": "a possible agent loop with a collaborator; the thread is held",
    "reply-recovery": "a reply of the owner's that may not have reached its session; it is held",
    "orchestrator-fail": "the orchestrator run on the owner's mail failed; the mail is held",
    "process-error": "a mail hit an error in the mailroom and is held",
    "group-post": "the owner's reply to a group's mail was not posted",
    "remarkable": "the agent for a reMarkable document did not start",
    "quota": "an agent hit a usage limit",
    "history-gone": "Gmail lost the mailroom's place in the inbox history, and the inbox was searched again",
    "extra-usage-spend": "agent-failover: agents began billing extra usage on a Claude login, or spent another step",
    "extra-usage-end": "agent-failover: agents stopped billing extra usage, with the total spent",
    "phone-enrolment": "agent-board: a passkey for the phone's mail page was enrolled",
    "memory": "agent-memwatch: the desktop's memory is nearly full",
    "cloud-idle": "cloud-watchdog: a paid resource is idle, crash-looping or unowned, and its owner was not reached",
    "cloud-stop": "cloud-watchdog: it stopped a crash-looping resource, or failed to",
    "cloud-cost": "cloud-watchdog: a resource's cost passed a threshold",
    "cloud-colleague": "cloud-watchdog: a colleague's app is crash-looping",
    "cloud-blind": "cloud-watchdog: it cannot see an account's resources",
    "cloud-heartbeat": "cloud-watchdog: no check has succeeded for a while",
    "cloud-check": "cloud-watchdog: a check failed",
    "fast-mode": "fast mode on Mail-tab threads ended, and the agent's effort could not be put back",
    "fast-keys": "a key fast mode pressed in an agent's pane may have moved the highlight of a dialog opening then",
    "voicemail": "a voicemail tool: a message left on the owner's phone, with its transcript and audio",
    "voicemail-alert": "a voicemail tool: a step keeps failing for a voicemail, the phone provider cannot be polled (or "
                       "can again), or its balance is low",
}
# The kinds that go through Gmail whatever config says: news of who may reach the agents or the owner's mail (a
# passkey enrolled for the phone's mail page among them), and of what fast mode's keys may have left wrong in an
# agent's pane without the owner knowing (agentmail/fastmode.py). With Gmail off they are kept for the Mail tab as
# every alert is, and each is also recorded in config.SECURITY_ALERTS_PATH (record_security_alert), which the agent
# board turns into a notification on the Mac that the phone app cannot dismiss.
SECURITY_ALERTS = frozenset({"dkim", "collab-thread", "flood", "phone-enrolment", "fast-mode", "fast-keys"})
# The kinds that carry the owner's own mail rather than agent mail, which go through Gmail whatever config says too,
# with Gmail on: a caller's voicemail, which the owner reads in their Gmail with the rest of their mail. With Gmail
# off it is kept for the Mail tab, as every alert is.
PERSONAL_MAIL = frozenset({"voicemail"})


def whole_minutes(n):
    """Whether n is a whole number of minutes, 1 or more."""
    return not isinstance(n, bool) and isinstance(n, int) and n >= 1


def mailbox_settings(cfg):
    """MAILBOX_DEFAULTS with config "mailbox" over them, as they take effect: with Gmail off (config.gmail_on),
    owner_delivery "local", nudge_minutes None and gmail_alerts [] whatever config says. Raises ValueError on an
    owner_delivery that does not exist, a nudge_minutes that is neither null nor a whole number of minutes, a
    fast_minutes that is not one, or a gmail_alerts that is not a list of ALERT_KINDS, Gmail on or off."""
    s = dict(MAILBOX_DEFAULTS, **(cfg.get("mailbox") or {}))
    if s["owner_delivery"] not in OWNER_DELIVERIES:
        raise ValueError("config mailbox.owner_delivery must be one of %s, not %r"
                         % (", ".join(OWNER_DELIVERIES), s["owner_delivery"]))
    n = s["nudge_minutes"]
    if n is not None and not whole_minutes(n):
        raise ValueError("config mailbox.nudge_minutes must be null or a whole number of minutes, not %r" % (n,))
    if not whole_minutes(s["fast_minutes"]):
        raise ValueError("config mailbox.fast_minutes must be a whole number of minutes, not %r" % (s["fast_minutes"],))
    kinds = s["gmail_alerts"]
    if not isinstance(kinds, list) or not all(isinstance(k, str) and k in ALERT_KINDS for k in kinds):
        raise ValueError("config mailbox.gmail_alerts must be a list of alert kinds (%s), not %r"
                         % (", ".join(ALERT_KINDS), kinds))
    if not config.gmail_on(cfg):
        s.update(owner_delivery="local", nudge_minutes=None, gmail_alerts=[])
    return s


def gmail_ignored(cfg):
    """The settings config gives that take no effect because Gmail is off in it, each as a line saying what happens
    instead; [] with Gmail on. The daemon warns of them as it starts, and agent-mail status lists them."""
    if config.gmail_on(cfg):
        return []
    box, out = cfg.get("mailbox") or {}, []
    if box.get("owner_delivery") == "gmail":
        out.append('mailbox.owner_delivery "gmail": mail to the owner is kept for the Mail tab')
    if box.get("nudge_minutes") is not None:
        out.append("mailbox.nudge_minutes: no nudge, which is a mail through Gmail")
    if box.get("gmail_alerts"):
        out.append("mailbox.gmail_alerts: every alert is kept for the Mail tab")
    if cfg.get("alert_to") and cfg["alert_to"].strip().lower() not in review.owner_addresses(cfg):
        out.append("alert_to: alerts go to the owner's address, kept for the Mail tab")
    if cfg.get("collaborators"):
        out.append("collaborators: no mail comes from or goes to them without Gmail")
    if (cfg.get("remarkable") or {}).get("address"):
        out.append("remarkable.address: the reMarkable route takes the tablet's mail from the agents Gmail account, so "
                   "no document starts an agent")
    return out


def alert_through_gmail(cfg, kind):
    """Whether an alert of kind goes through Gmail even when mail to the owner is kept for the Mail tab (kept_here):
    Gmail is on (config.gmail_on), and it is one of SECURITY_ALERTS or PERSONAL_MAIL, or config mailbox.gmail_alerts
    names it. Raises ValueError on a kind that is not in ALERT_KINDS, and as mailbox_settings does."""
    if kind not in ALERT_KINDS:
        raise ValueError("%r is no alert kind; the kinds are %s" % (kind, ", ".join(ALERT_KINDS)))
    return config.gmail_on(cfg) and (kind in SECURITY_ALERTS or kind in PERSONAL_MAIL
                                     or kind in mailbox_settings(cfg)["gmail_alerts"])


def kept_here(cfg, to, cc):
    """Whether a mail to to and cc stays in the message store, not sent: mail to the owner is kept for the Mail tab
    (config mailbox.owner_delivery "local", or Gmail off: mailbox_settings) and every recipient is an owner address."""
    addrs = review.addresses(to, cc)
    return mailbox_settings(cfg)["owner_delivery"] == "local" and bool(addrs) and addrs <= review.owner_addresses(cfg)


def refuse_without_gmail(cfg, to, cc):
    """Raise MailError (exit NEEDS_GMAIL) when Gmail is off in config (config.gmail_on) and a mail to to and cc would
    need it: it has a recipient who is not an owner address (config allowlist), as only mail to the owner is kept.
    send_mail and reply_mail ask this before the pre-send review, so a mail that cannot go costs no model run;
    transmit asks it again before it keeps anything. Nothing is checked with Gmail on."""
    if config.gmail_on(cfg):
        return
    addrs = review.addresses(to, cc)
    others = sorted(addrs - review.owner_addresses(cfg)) if addrs else [to or "(no recipient)"]
    if others:
        raise MailError('Gmail is off (config "gmail": false), so agent mail goes only to %s, kept for the board\'s '
                        "Mail tab; %s %s not an owner address (config allowlist). Nothing was sent or kept."
                        % (cfg["owner_name"], ", ".join(others), "is" if len(others) == 1 else "are"), NEEDS_GMAIL)


def record_security_alert(kind, subject, thread_id, now=None):
    """Append one line for a security alert (SECURITY_ALERTS) kept for the Mail tab rather than sent through Gmail to
    config.SECURITY_ALERTS_PATH: {"at": unix seconds (now, else the time), "kind", "subject": the alert's subject,
    "thread_id": its thread}, as one O_APPEND write to a file of mode 600, which is never rewritten. The agent board
    reads it (its snapshot's security_alerts) and notifies the owner on the Mac. Raises OSError when it cannot."""
    line = json.dumps({"at": time.time() if now is None else float(now), "kind": kind, "subject": subject,
                       "thread_id": thread_id}) + "\n"
    path = config.SECURITY_ALERTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        data = line.encode()
        if os.write(fd, data) != len(data):
            raise OSError("a short write to %s" % path)
    finally:
        os.close(fd)


def local_thread_id():
    """A new thread id for mail kept in the mailroom alone: "L" and 16 hex digits, which no Gmail thread id has."""
    return "L" + secrets.token_hex(8)


def transmit(ctx, row, to, cc, subject, mail, inb, new_thread=False, alert=False, through_gmail=False, kind=None):
    """Compose, keep, deliver and record one mail from the session behind row (a sessions row), in reply to inbound
    mail inb (None: not a reply) and in inb's thread, unless new_thread: then the sent row alone ties it to inb. row
    None sends it unrouted: from the mailroom, with no reply tag; alert marks it as a mailroom alert (alerts.py), and
    kind is the kind of alert or notice it is (ALERT_KINDS), or None.

    With Gmail off in config, a mail that is not kept_here (to anyone but the owner) is refused before anything is
    done (refuse_without_gmail: MailError). The message's bytes are kept in the message store (messages.keep) first,
    so a mail that cannot be kept goes no further (OSError). A mail kept_here, unless through_gmail with Gmail on (an
    alert alert_through_gmail sends through Gmail), then stays there: it gets its own Message-ID and Date before it is
    kept, the thread of the mail it answers or a new one (local_thread_id), and no Gmail id. Any other mail is sent,
    as those exact bytes, through the Gmail API, in the thread of the mail it answers when Gmail holds that thread
    (Store.thread_in_gmail), else in a new Gmail thread: one kept for the tab alone has no Gmail thread to go on. Then
    the sent row is written, and a kept mail of a kind in SECURITY_ALERTS is recorded for the board
    (record_security_alert). Returns ({"id": the Gmail id or None, "threadId": the thread id}, the real
    Message-ID)."""
    gmail_on = config.gmail_on(ctx.cfg)
    if not gmail_on:
        refuse_without_gmail(ctx.cfg, to, cc)
    thread = None if new_thread else inb
    msg = compose(ctx.cfg, session_label(dict(row), ctx.cfg["host_id"]) if row else "mailroom", to, cc, subject,
                  mail.text, mail.html, reply_to_tag=row["tag"] if row else None,
                  in_reply_to=thread["message_id"] if thread else None,
                  references=references(ctx.st, thread) if thread else None, images=mail.images, files=mail.files,
                  alert=alert)
    here = not (through_gmail and gmail_on) and kept_here(ctx.cfg, to, cc)
    if here:
        msg["Message-ID"] = make_msgid(domain=routing.LOCAL_DOMAIN)
        msg["Date"] = formatdate(localtime=True)
    raw = msg.as_bytes()
    eml = messages.keep(raw)
    if here:
        res = {"id": None, "threadId": thread["thread_id"] if thread and thread["thread_id"] else local_thread_id()}
        mid = str(msg["Message-ID"])
    else:
        res = gmail.send_raw(ctx.svc, raw, thread_id=thread["thread_id"] if thread and thread["thread_id"]
                             and ctx.st.thread_in_gmail(thread["thread_id"]) else None)
        mid = gmail.headers(gmail.get_metadata(ctx.svc, res["id"], ["Message-ID"])).get("message-id")
    # compose makes each file an attachment and each image an inline part
    ctx.st.record_sent(row["tag"] if row else "", res["id"], res["threadId"], mid, to, subject, cc_addr=cc,
                       body=mail.text, eml=eml, attachments=len(mail.files),
                       widgets=json.dumps(list(mail.widgets)) if mail.widgets else None,
                       in_reply_to_inbound=inb["id"] if inb else None)
    if here and kind in SECURITY_ALERTS:
        record_security_alert(kind, subject, res["threadId"])
    return res, mid


def delivered(res, what):
    """What a send's output says of where it went: what ("sent to X") as it is for mail sent through Gmail, and with
    " in the Mail tab" for mail transmit kept there alone (no Gmail id)."""
    return what if res["id"] else what + " in the Mail tab"


def references(st, inb):
    """The References header of an answer to inbound mail inb (RFC 5322 3.6.4): the Message-IDs of the mail of its
    thread up to it that the store holds, oldest first (Store.thread_message_ids), ending with inb's own. So an answer
    to a reply written in the Mail tab, whose Message-ID only the mailroom knows, still names the agent mail it
    answered, which Gmail threads it by."""
    earlier = st.thread_message_ids(inb["thread_id"], inb["received_at"]) if inb["thread_id"] else []
    return " ".join([m for m in earlier if m != inb["message_id"]] + [inb["message_id"]])


def recipient(cfg, value):
    """The recipient a --to or --cc value names: the owner's address (config.owner_address) when the value is
    "owner" or the owner's name (config owner_name), in any case (config.is_owner_alias), so an agent mails the owner
    without typing the address; an address as given, and an empty value (no --cc) unchanged. Raises MailError on a
    value that is neither an address nor the owner, or on the owner when config has no owner address."""
    if not value:
        return value
    if config.is_owner_alias(cfg, value):
        addr = config.owner_address(cfg)
        if not addr:
            raise MailError("%r names %s, but config has no owner address (owner_email or allowlist)"
                            % (value, cfg["owner_name"]), 1)
        return addr
    if "@" not in value:
        raise MailError("%r is neither an address nor %r (or %r, %s's name), for the owner's address"
                        % (value, config.OWNER_ALIAS, cfg["owner_name"].lower(), cfg["owner_name"]), 1)
    return value


def sender_row(ctx, sess):
    """The sessions row (with its reply tag) of a registry session, or of the origin session another machine named
    (its "host"); made the first time that session sends. Writes the store."""
    return ctx.st.session_for(sess["cli"], sess["id"], sess.get("name", ""), sess.get("cwd", ""),
                              origin_host=sess.get("host", ctx.cfg["host_id"]))


class Outcome(NamedTuple):
    """What send_mail or reply_mail did. held: the pre-send review kept the mail back and this is its NOT SENT
    message (review.pushback, which starts with review.NOT_SENT); nothing was sent. Else summary says where the mail
    went, and tips holds the review's notes for the next mail (None: none)."""
    held: Optional[str]
    summary: Optional[str]
    tips: Optional[str]

    def text(self):
        """What agent-mail prints, one line per part; a machine that proxied the command prints the same."""
        return "".join(part + "\n" for part in ((self.held,) if self.held else (self.summary, self.tips)) if part)


def send_mail(ctx, row, to, cc, subject, mail, no_review):
    """Send a new mail to to and cc (each as recipient reads it) from the session behind row (sender_row) once the
    pre-send review lets it go (review.gate, which may run a model; no_review: why it is skipped). With Gmail off, a
    mail to anyone but the owner is refused first (refuse_without_gmail). Writes the store (the review, the sent mail)
    and sends through the Gmail API, or keeps the mail for the Mail tab (transmit). Returns an Outcome."""
    to, cc = recipient(ctx.cfg, to), recipient(ctx.cfg, cc)
    refuse_without_gmail(ctx.cfg, to, cc)
    tag = row["tag"]
    rid, stop, tips = review.gate(ctx.cfg, ctx.st, tag, [to, cc], subject, mail.text, mail.html, mail.images,
                                  no_review=no_review,
                                  draft=dict({"op": "send", "to": to, "cc": cc, "subject": subject}, **mail.as_json()))
    if stop:
        return Outcome(stop, None, None)
    subject = routing.tag_subject(subject, tag)
    res, mid = transmit(ctx, row, to, cc, subject, mail, None)
    if rid:
        ctx.st.review_sent(rid, mid)
    return Outcome(None, "%s | subject %r | thread %s | tag %s | from %s"
                   % (delivered(res, "sent to " + to), subject, res["threadId"], tag,
                      session_label(dict(row), ctx.cfg["host_id"])), tips)


def reply_target(st, inbound_id):
    """(the inbound mail, the sessions row it was routed to or None). Raises MailError when there is no such mail."""
    inb = st.inbound(inbound_id)
    if not inb:
        raise MailError("no inbound mail with id %s (see `agent-mail inbox`)" % inbound_id, 4)
    return inb, st.session_by_tag(inb["matched_tag"]) if inb["matched_tag"] else None


def reply_mail(ctx, row, inb, mail, no_review, subject, caller):
    """Answer inbound mail inb from the session behind row, once the pre-send review lets it go, as send_mail does;
    the reviewer is shown inb's text (review.gate's answers). subject None answers in inb's thread under its
    subject; a subject sends the answer as a new thread under that subject (send_reply), and a blank one raises
    MailError. caller is (host, cli, session id) of the session running the command, or None when unknown: an
    answer in the thread skips the review while fast mode is on there for that session (fastmode.skips_review). With
    Gmail off, an answer to anyone but the owner is refused before the review (refuse_without_gmail). Returns an
    Outcome."""
    tag = row["tag"]
    if subject is not None and not subject.strip():
        raise MailError("--subject is empty: give the new thread's subject, or leave --subject out to answer in the "
                        "thread", 1)
    refuse_without_gmail(ctx.cfg, inb["from_addr"], None)
    from . import fastmode   # fastmode imports this module
    fast = subject is None and fastmode.skips_review(ctx.st, ctx.cfg, row, inb, caller, time.time())
    rid, stop, tips = review.gate(ctx.cfg, ctx.st, tag, [inb["from_addr"]],
                                  (inb["subject"] or "") if subject is None else subject, mail.text,
                                  mail.html, mail.images, no_review=no_review, answers=inb["body"],
                                  draft=dict({"op": "reply", "inbound_id": inb["id"], "subject": subject},
                                             **mail.as_json()), fast=fast)
    if stop:
        return Outcome(stop, None, None)
    sent_as, res, mid = send_reply(ctx, row, inb, mail, subject)
    if rid:
        ctx.st.review_sent(rid, mid)
    return Outcome(None, "%s | subject %r | %sthread %s | from %s%s"
                   % (delivered(res, "replied to " + inb["from_addr"]), sent_as, "" if subject is None else "new ",
                      res["threadId"], session_label(dict(row), ctx.cfg["host_id"]),
                      " | fast mode: not reviewed" if fast else ""), tips)


def send_reply(ctx, row, inb, mail, subject):
    """Transmit the answer to inbound mail inb from the session behind row: in inb's thread under its subject
    (reply_subject), or, given a subject, as a new thread under it whose last line links back to inb (continued).
    Returns (the subject it went out under, the Gmail result, the real Message-ID)."""
    if subject is None:
        sent_as = reply_subject(inb, row["tag"])
        return (sent_as,) + transmit(ctx, row, inb["from_addr"], None, sent_as, mail, inb)
    sent_as = routing.tag_subject(subject, row["tag"])
    return (sent_as,) + transmit(ctx, row, inb["from_addr"], None, sent_as, continued(mail, inb), inb, new_thread=True)


def reply_subject(inb, tag):
    return routing.tag_subject(routing.answer_subject(inb["subject"] or ""), tag)


def continued(mail, inb):
    """mail ending with a line that names the thread it continues, inbound mail inb's, and links to inb, the mail
    it answers, in its sender's Gmail, where that mail sits in the old thread (gmail.message_search); the line
    alone when inb has no Message-ID or was written in the Mail tab, which no Gmail holds. The HTML is made explicit
    (rendered from the text when the mail has none, images placed) so that the line comes last."""
    url = (gmail.web_url(inb["from_addr"], gmail.message_search(inb["message_id"]))
           if inb["message_id"] and not routing.is_local(inb["gmail_id"]) else None)
    # agent-board's agentboard/mailforward.py strips this from forwarded mail: change both together.
    line = 'Continues the thread "%s".' % routing.bare_subject(inb["subject"] or "")
    link = ' <a href="%s">Open the mail this answers in Gmail</a>' % htmllib.escape(url) if url else ""
    html_body = at_end(place_images(mail.html or text_to_html(mail.text), mail.images),
                       "<hr>" + NOTE % (htmllib.escape(line) + link))
    return mail._replace(text="%s\n\n--\n%s%s\n" % (mail.text.rstrip(), line,
                                                     " The mail this answers: " + url if url else ""),
                         html=html_body)


def withdraw(st, tag):
    """Drop the mail of reply tag `tag` that the review sent back, so the mailroom does not send it later (recorded
    as verdict "withdrawn"). Writes the store; returns what agent-mail prints."""
    last = st.held_draft(tag)
    if last is None:
        return "nothing to withdraw: no mail from this session is waiting on a revision\n"
    st.clear_draft(last["id"])
    st.record_review(tag, last["round"], "withdrawn", last["subject"], kind=last["kind"], audience=last["audience"])
    return "withdrawn: %r will not be sent\n" % last["subject"]


def flush_drafts(ctx):
    """Send, unchanged, every held draft whose session neither revised nor withdrew it in time (the daemon
    runs this). The mail says so at the top. A draft whose session, or the mail it replies to, is no longer in the
    store, or that would need Gmail while it is off in config (refuse_without_gmail: held before Gmail was turned off,
    to anyone but the owner), is dropped instead: recorded as verdict "dropped" with the reason, and printed."""
    st = ctx.st
    s = review.settings(ctx.cfg)
    for r in st.stale_drafts(time.time() - 60 * s["fallback_minutes"]):
        d = json.loads(r["draft"])
        row = st.session_by_tag(r["tag"])
        inb = st.inbound(d["inbound_id"]) if d.get("op") == "reply" else None
        why = None
        if row is None or (d.get("op") == "reply" and inb is None):
            why = ("its session (tag %s) is not in the store" % r["tag"] if row is None
                   else "the mail it replies to (#%s) is not in the store" % d["inbound_id"])
        else:
            try:
                refuse_without_gmail(ctx.cfg, *((inb["from_addr"], None) if inb else (d["to"], d.get("cc"))))
            except MailError as e:
                why = str(e)
        if why:
            st.clear_draft(r["id"])
            st.record_review(r["tag"], r["round"], "dropped", r["subject"], kind=r["kind"], reasons=why,
                             audience=r["audience"])
            print("dropped held draft %r: %s" % (r["subject"], why))
            continue
        mail = Mail.from_json(d)
        if (r["audience"] or "owner") == "owner":  # the note is for the owner; anyone else just gets the mail
            note = review.EXPIRED_NOTE % s["fallback_minutes"]
            mail = mail._replace(text=note + "\n\n" + mail.text, html=review.prepend_note(mail.html, note))
        if inb:
            _sent_as, res, mid = send_reply(ctx, row, inb, mail, d.get("subject"))
        else:
            res, mid = transmit(ctx, row, d["to"], d.get("cc"), routing.tag_subject(d["subject"], r["tag"]), mail, None)
        rid = st.record_review(r["tag"], r["round"], "expired", r["subject"], kind=r["kind"], audience=r["audience"])
        st.review_sent(rid, mid)
        print("sent held draft %r from %s unchanged (no revision in %d min)"
              % (r["subject"], session_label(dict(row), ctx.cfg["host_id"]), s["fallback_minutes"]))
