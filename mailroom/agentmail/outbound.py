"""Mail out of the agents account, for agent-mail: what its commands work with (Context), a mail's content (Mail)
and the MIME message made of it (the text, an HTML part, inline images, attachments), kept in the message store and
then sent (transmit), the recipient a --to or --cc value names (recipient), loading the images and files it carries,
sending a new mail or a reply from a session once the pre-send review lets it go (send_mail, reply_mail), withdrawing
a held draft, and sending the held drafts whose time ran out (the daemon runs `agent-mail flush-drafts`). The send, reply and withdraw functions return what
agent-mail prints and raise MailError where it stops, so a command proxied from another machine (rpc.py) runs the
same code; flush_drafts, which only the daemon runs, prints its own lines."""
import base64
import functools
import html as htmllib
import json
import mimetypes
import os
import re
import time
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import NamedTuple, Optional

from . import config, gmail, messages, review, routing
from .compose import NOTE, html_to_text, text_to_html
from .store import Store


class MailError(Exception):
    """A mail or command that cannot go ahead as asked; nothing was sent. exit_code is the code agent-mail stops
    with."""

    def __init__(self, message, exit_code):
        super().__init__(message)
        self.exit_code = exit_code


class Context:
    """What agent-mail's commands work with: the config, and the store and the Gmail service, each opened on first
    use, so a command that needs neither (one proxied to another machine) opens neither."""

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
        return gmail.service()


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


IMAGE_TYPES = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))
MAX_IMAGES_BYTES = 20 * 1024 * 1024
IMAGE_NAME_MAX = 64
IMAGE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,%d}" % IMAGE_NAME_MAX)   # an --image NAME, which becomes a cid: in the HTML


def image_subtype(data):
    for magic, sub in IMAGE_TYPES:
        if data.startswith(magic):
            return sub
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def check_image(name, data):
    """Raise MailError (exit 1) unless name can be the image's cid: (IMAGE_NAME_RE) and data is a PNG, JPEG, GIF or
    WebP file (image_subtype)."""
    if not IMAGE_NAME_RE.fullmatch(name):
        raise MailError("image name %r: use 1 to 64 letters, digits, _ . -" % name, 1)
    if not image_subtype(data):
        raise MailError("image %s is not a PNG, JPEG, GIF or WebP file" % name, 1)


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


def check_size(mail):
    """Raise MailError when the mail's images and attachments together pass MAX_IMAGES_BYTES."""
    if sum(len(d) for _, d in mail.images + mail.files) > MAX_IMAGES_BYTES:
        raise MailError("images and attachments total over %d MB" % (MAX_IMAGES_BYTES >> 20), 1)


def at_end(html_body, more):
    """html_body with the HTML `more` as the last thing in its body."""
    i = html_body.lower().rfind("</body>")
    return html_body[:i] + more + html_body[i:] if i >= 0 else html_body + more


def place_images(html_body, images):
    """html_body with each of images ([(name, bytes)]) that it does not place (no cid:NAME) shown at its end."""
    unplaced = [n for n, _ in images if not re.search(r"""cid:%s["'\s>]""" % re.escape(n), html_body)]
    return at_end(html_body, "".join('<p><img src="cid:%s" alt="%s" style="max-width:100%%;height:auto"></p>\n' % (n, n)
                                     for n in unplaced))


def compose(cfg, from_label, to, cc, subject, text, html_body=None, reply_to_tag=None,
            in_reply_to=None, references=None, images=(), files=()):
    """One multipart/alternative message: the text as given, plus html_body or HTML rendered from the text.
    Images ride as multipart/related parts of the HTML, each with Content-ID <NAME>. Raises config.ConfigError
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
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(text)
    msg.add_alternative(place_images(html_body or text_to_html(text), images), subtype="html")
    if images:
        html_part = msg.get_payload()[1]
        for name, data in images:
            html_part.add_related(data, maintype="image", subtype=image_subtype(data), cid="<%s>" % name,
                                  filename="%s.%s" % (name, image_subtype(data).replace("jpeg", "jpg")),
                                  disposition="inline")
    attach(msg, files)
    return msg


def attach(msg, files):
    """Attach files ([(name, bytes)]) to msg (an EmailMessage), each with the content type its name suggests, else
    application/octet-stream."""
    for name, data in files:
        maintype, subtype = (mimetypes.guess_type(name)[0] or "application/octet-stream").split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)


def transmit(ctx, row, to, cc, subject, mail, inb, new_thread=False):
    """Compose, keep, send and record one mail from the session behind row (a sessions row), in reply to inbound mail
    inb (None: not a reply) and in inb's thread, unless new_thread: then the sent row alone ties it to inb. row None
    sends it unrouted: from the mailroom, with no reply tag. Keeps the message's bytes in the message store
    (messages.keep) before sending exactly those bytes through the Gmail API, so a mail that cannot be kept is not
    sent (OSError), then writes the sent row. Returns the Gmail result and the real Message-ID."""
    thread = None if new_thread else inb
    msg = compose(ctx.cfg, session_label(dict(row), ctx.cfg["host_id"]) if row else "mailroom", to, cc, subject,
                  mail.text, mail.html, reply_to_tag=row["tag"] if row else None,
                  in_reply_to=thread["message_id"] if thread else None,
                  references=references(ctx.st, thread) if thread else None, images=mail.images, files=mail.files)
    raw = msg.as_bytes()
    eml = messages.keep(raw)
    res = gmail.send_raw(ctx.svc, raw, thread_id=thread["thread_id"] if thread else None)
    meta = gmail.get_metadata(ctx.svc, res["id"], ["Message-ID"])
    mid = gmail.headers(meta).get("message-id")
    # compose makes each file an attachment and each image an inline part
    ctx.st.record_sent(row["tag"] if row else "", res["id"], res["threadId"], mid, to, subject, cc_addr=cc,
                       body=mail.text, eml=eml, attachments=len(mail.files),
                       widgets=json.dumps(list(mail.widgets)) if mail.widgets else None,
                       in_reply_to_inbound=inb["id"] if inb else None)
    return res, mid


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
    pre-send review lets it go (review.gate, which may run a model; no_review: why it is skipped). Writes the store
    (the review, the sent mail) and sends through the Gmail API. Returns an Outcome."""
    to, cc = recipient(ctx.cfg, to), recipient(ctx.cfg, cc)
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
    return Outcome(None, "sent to %s | subject %r | thread %s | tag %s | from %s"
                   % (to, subject, res["threadId"], tag, session_label(dict(row), ctx.cfg["host_id"])), tips)


def reply_target(st, inbound_id):
    """(the inbound mail, the sessions row it was routed to or None). Raises MailError when there is no such mail."""
    inb = st.inbound(inbound_id)
    if not inb:
        raise MailError("no inbound mail with id %s (see `agent-mail inbox`)" % inbound_id, 4)
    return inb, st.session_by_tag(inb["matched_tag"]) if inb["matched_tag"] else None


def reply_mail(ctx, row, inb, mail, no_review, subject):
    """Answer inbound mail inb from the session behind row, once the pre-send review lets it go, as send_mail does;
    the reviewer is shown inb's text (review.gate's answers). subject None answers in inb's thread under its
    subject; a subject sends the answer as a new thread under that subject (send_reply), and a blank one raises
    MailError. Returns an Outcome."""
    tag = row["tag"]
    if subject is not None and not subject.strip():
        raise MailError("--subject is empty: give the new thread's subject, or leave --subject out to answer in the "
                        "thread", 1)
    rid, stop, tips = review.gate(ctx.cfg, ctx.st, tag, [inb["from_addr"]],
                                  (inb["subject"] or "") if subject is None else subject, mail.text,
                                  mail.html, mail.images, no_review=no_review, answers=inb["body"],
                                  draft=dict({"op": "reply", "inbound_id": inb["id"], "subject": subject},
                                             **mail.as_json()))
    if stop:
        return Outcome(stop, None, None)
    sent_as, res, mid = send_reply(ctx, row, inb, mail, subject)
    if rid:
        ctx.st.review_sent(rid, mid)
    return Outcome(None, "replied to %s | subject %r | %sthread %s | from %s"
                   % (inb["from_addr"], sent_as, "" if subject is None else "new ", res["threadId"],
                      session_label(dict(row), ctx.cfg["host_id"])), tips)


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
    store is dropped instead: recorded as verdict "dropped" with the reason, and printed."""
    st = ctx.st
    s = review.settings(ctx.cfg)
    for r in st.stale_drafts(time.time() - 60 * s["fallback_minutes"]):
        d = json.loads(r["draft"])
        row = st.session_by_tag(r["tag"])
        inb = st.inbound(d["inbound_id"]) if d.get("op") == "reply" else None
        if row is None or (d.get("op") == "reply" and inb is None):
            why = ("its session (tag %s) is not in the store" % r["tag"] if row is None
                   else "the mail it replies to (#%s) is not in the store" % d["inbound_id"])
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
