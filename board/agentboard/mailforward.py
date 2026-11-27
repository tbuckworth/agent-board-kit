"""Forwarding an agent's mail from the Mail tab (the mail_forward action): one mail's content, its inline images and
its attachments as a fresh mail, which gmailstate.create_draft puts in the owner's Gmail as a draft for the owner to
send to anyone.

The mail is read as the mailroom sent it, from its message store (sources.MailStore.sent_message), and made anew
(draft): with no sender, recipients or reply headers, under its thread's subject (subject), with its text and HTML less
the mailroom's own words in them (HTML_PLUMBING, TEXT_PLUMBING), and its images and attachments as they were.
"""
import email
import email.policy
import re
from email.message import EmailMessage
from typing import NamedTuple

from . import sources

# The mailroom's own words in an agent's mail, which a forward leaves out, as the mailroom writes them in the HTML and
# in the text. Each copies the markup of the mailroom function named beside it: a change there must be made here too.
HTML_PLUMBING = (
    # a paragraph in the mailroom's small grey note style, "the mailroom's word, not the sender's" (compose.NOTE): the
    # line after an <hr> that ends a mail answering in a new thread, naming the old thread and linking to the mail it
    # answers in the owner's Gmail (outbound.continued); and the line that starts a mail the pre-send review held and
    # then sent unchanged (review.prepend_note)
    re.compile(r'(?:<hr>)?<p style="color:#6e7781;font-size:13px">.*?</p>\n?'),
    # that review's line on a mail with no HTML of its own, whose HTML is rendered from its text (review.EXPIRED_NOTE)
    re.compile(r"<p>\[Sent unchanged by the mailroom: [^<]*\]</p>\n?"),
    # how to answer a widget's options by mail, the last line of its fallback (widgets.fallback_html, REPLY_LINE)
    re.compile(r"<p>Reply with the numbers?\.</p>(?=\n</div>)"),
    # the mark on a widget's fallback that shows the Mail tab where to draw the widget (mailview.WIDGET_MARKER)
    re.compile(r' data-agentmail-widget="[0-9]+"'),
)
TEXT_PLUMBING = (
    # the same in the text: outbound.continued's last line, review.EXPIRED_NOTE and widgets.fallback_text's REPLY_LINE
    re.compile(r'\n\n--\nContinues the thread "[^\n]*"\.(?: The mail this answers: \S+)?(?=\n\Z)'),
    re.compile(r"\A\[Sent unchanged by the mailroom: [^\n]*\]\n\n"),
    re.compile(r"^Reply with the numbers?\.\n", re.M),
)
# The Re:, Fw: and Fwd: run in front of a subject (the mailroom's routing._PREFIXES)
REPLY_PREFIXES = re.compile(r"^\s*(?:(?:re|fwd?)\s*:\s*)*", re.I)


class Forward(NamedTuple):
    """A forward made by draft: its MIME bytes, and for the record how many inline images it has and its attachments'
    names."""
    raw: bytes
    images: int
    files: list


def subject(sent_as):
    """The subject a forward goes under: `sent_as`, a subject the mailroom sent agent mail under, without the Re:, Fw:
    and Fwd: run in front of it, the reply tag the mailroom puts after it (" [#TAG]", sources.REPLY_TAG) or the
    spaces around it. That is the subject the Mail tab heads its thread with (the mailroom's routing.bare_subject)."""
    return sources.REPLY_TAG.sub("", REPLY_PREFIXES.sub("", sent_as, count=1)).strip()


def without(text, plumbing):
    """`text` with every match of the patterns in `plumbing` taken out."""
    for pattern in plumbing:
        text = pattern.sub("", text)
    return text


def _leaves(part):
    """The MIME leaves under `part`, depth first; an attached mail (message/*) is one leaf (the mailroom's
    mailview._leaves)."""
    if part.get_content_maintype() == "multipart" and part.is_multipart():
        for sub in part.iter_parts():
            yield from _leaves(sub)
    else:
        yield part


def draft(raw, subject_line):
    """The forward of the mail `raw` (MIME bytes, as the mailroom sent it) under `subject_line`: a new message with no
    From, To, Cc, Reply-To, In-Reply-To, References or other header of the old one but its subject. Its body is the old
    one's text and HTML, without the mailroom's words in them (HTML_PLUMBING, TEXT_PLUMBING), as multipart/alternative
    (a mail with only one of them, as some older agent mail has, keeps that one alone). With HTML, its inline images
    (each image part with a Content-ID, as the mailroom's mailview.parse counts them) go beside it in
    multipart/related, so that its cid: images show; every other part is attached as it was. Raises ValueError for a
    mail with neither a text nor an HTML body."""
    old = email.message_from_bytes(raw, policy=email.policy.default)
    text, html = old.get_body(("plain",)), old.get_body(("html",))
    if text is None and html is None:
        raise ValueError("the mail has neither a text nor an HTML body to forward")
    images, files = [], []
    for leaf in _leaves(old):
        if leaf is not text and leaf is not html:
            inline = html is not None and leaf.get_content_maintype() == "image" and leaf["Content-ID"]
            (images if inline else files).append(leaf)
    new = EmailMessage()
    new["Subject"] = subject_line
    if text is not None:
        new.set_content(without(text.get_content(), TEXT_PLUMBING))
    if html is not None:
        body = without(html.get_content(), HTML_PLUMBING)
        if text is None:
            new.set_content(body, subtype="html")
        else:
            new.add_alternative(body, subtype="html")
    if images:
        shown = new.get_body(("html",))
        shown.make_related()
        for image in images:
            shown.attach(image)
    if files:
        new.make_mixed()
        for f in files:
            new.attach(f)
    return Forward(new.as_bytes(), len(images), [f.get_filename() or f.get_content_type() for f in files])


def forward(store, thread, sent_id):
    """The Forward of agent mail `sent_id` of thread `thread`, read from `store` (sources.MailStore). Raises ValueError
    when the mail is not in that thread or cannot be forwarded (MailStore.sent_message, draft), OSError or
    sqlite3.Error when the store cannot be read."""
    in_thread, sent_as, raw = store.sent_message(sent_id)
    if in_thread != thread:
        raise ValueError("agent mail s%d is not in thread %s" % (sent_id, thread))
    return draft(raw, subject(sent_as))
