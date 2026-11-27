"""Thin Gmail API layer. Only gmail.modify is ever requested. Google's client is imported where it is used, not
here: it is slow to load, and commands that never talk to Gmail, such as `agent-mail mailbox show`, import this
module. With Gmail off in config ("gmail": false) the daemon and agent-mail hold OFF in place of the service, so
nothing loads a token or reaches Google."""
import base64
import html
import re
import urllib.parse
from email.utils import getaddresses

from .config import CONFIG_PATH, SCOPES, TOKEN_PATH

RECIPIENT_HEADERS = ("to", "cc", "delivered-to", "x-original-to")


class HistoryGone(Exception):
    """Gmail no longer has history from startHistoryId (it usually keeps a week of it); the caller searches instead."""


def web_url(account, view):
    """A link that opens Gmail on the web as account, at view: "all/ID" is the thread or mail of that Gmail id,
    "search/QUERY" a search. ?authuser picks the account among those signed in."""
    return "https://mail.google.com/mail/?authuser=%s#%s" % (urllib.parse.quote(account, safe="@.-_"), view)


def message_search(message_id):
    """The web_url view that finds the mail with this Message-ID header in whichever mailbox holds it."""
    return "search/" + urllib.parse.quote("rfc822msgid:" + message_id.strip("<> "), safe="")


class NoToken(RuntimeError):
    """The agents account has not been authorised on this machine yet."""


class GmailOff(RuntimeError):
    """Gmail is off in config ("gmail": false), and something needed it."""


class _Off:
    """The Gmail service while Gmail is off in config (OFF): any use of it raises GmailOff, so a path that would call
    Gmail stops with a clear message instead of reaching Google."""

    def __getattr__(self, name):
        if name.startswith("__"):   # copy, pickle and the like probe for these: not a Gmail call
            raise AttributeError(name)
        raise GmailOff('Gmail is off in config ("gmail": false in %s): nothing is sent through or read from Gmail'
                       % CONFIG_PATH)

    def __repr__(self):
        return "gmail.OFF"


OFF = _Off()


def service():
    if not TOKEN_PATH.exists():
        raise NoToken("no Gmail token at %s: run `agent-mail-auth` (mailroom/agent_mail_auth.py) once, "
                      'signed in as the agents account, or set "gmail": false in %s to run without Gmail'
                      % (TOKEN_PATH, CONFIG_PATH))
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def profile(svc):
    return svc.users().getProfile(userId="me").execute()


def send_raw(svc, raw_bytes, thread_id=None):
    body = {"raw": base64.urlsafe_b64encode(raw_bytes).decode()}
    if thread_id:
        body["threadId"] = thread_id
    return svc.users().messages().send(userId="me", body=body).execute()


def get_metadata(svc, gid, names):
    return svc.users().messages().get(userId="me", id=gid, format="metadata",
                                      metadataHeaders=list(names)).execute()


def get_full(svc, gid):
    return svc.users().messages().get(userId="me", id=gid, format="full").execute()


def get_raw(svc, gid):
    """Message gid's whole RFC 822 bytes, as Gmail keeps them (format=raw)."""
    return decode_bytes(svc.users().messages().get(userId="me", id=gid, format="raw").execute()["raw"])


def headers(msg):
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def all_headers(msg):
    """Every header as (lower-case name, value); repeated headers (Delivered-To) are kept."""
    return [(h["name"].lower(), h["value"]) for h in msg.get("payload", {}).get("headers", [])]


def recipients(msg):
    vals = [v for k, v in all_headers(msg) if k in RECIPIENT_HEADERS]
    return {a.lower() for _n, a in getaddresses(vals) if a}


def payload_parts(msg):
    """Every MIME part of the message, its payload first, depth first in document order."""
    def walk(p):
        yield p
        for c in p.get("parts") or []:
            yield from walk(c)
    return list(walk(msg.get("payload", {})))


def pdf_parts(msg):
    """The message's PDF attachments as payload parts, in order."""
    return [p for p in payload_parts(msg)
            if (p.get("mimeType") == "application/pdf" or (p.get("filename") or "").lower().endswith(".pdf"))
            and ((p.get("body") or {}).get("attachmentId") or (p.get("body") or {}).get("data"))]


def history_since(svc, start_id):
    """Ids of messages added to INBOX since start_id, and the newest history id."""
    from googleapiclient.errors import HttpError
    ids, newest, page = [], start_id, None
    while True:
        try:
            resp = svc.users().history().list(
                userId="me", startHistoryId=start_id, historyTypes=["messageAdded"],
                labelId="INBOX", pageToken=page).execute()
        except HttpError as e:
            if e.resp.status == 404:
                raise HistoryGone()
            raise
        for h in resp.get("history", []):
            for a in h.get("messagesAdded", []):
                m = a["message"]
                if "INBOX" in m.get("labelIds", []):
                    ids.append(m["id"])
        newest = resp.get("historyId", newest)
        page = resp.get("nextPageToken")
        if not page:
            break
    return list(dict.fromkeys(ids)), str(newest)


def search(svc, q, limit):
    """Ids of the messages matching the Gmail search q, newest first, at most limit. Calls the Gmail API once per
    result page."""
    ids, page = [], None
    while len(ids) < limit:
        resp = svc.users().messages().list(userId="me", q=q, maxResults=min(500, limit - len(ids)),  # 500: Gmail's page cap
                                           pageToken=page).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids[:limit]


_labels = {}


def ensure_label(svc, name):
    if name in _labels:
        return _labels[name]
    for lab in svc.users().labels().list(userId="me").execute().get("labels", []):
        _labels[lab["name"]] = lab["id"]
    if name not in _labels:
        lab = svc.users().labels().create(userId="me", body={
            "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute()
        _labels[name] = lab["id"]
    return _labels[name]


def modify(svc, gid, add=(), remove=()):
    svc.users().messages().modify(userId="me", id=gid, body={
        "addLabelIds": list(add), "removeLabelIds": list(remove)}).execute()


def decode_bytes(data):
    """Gmail's base64url payloads may arrive without padding."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode(data):
    return decode_bytes(data).decode("utf-8", "replace")


def bodies(msg, mime_type):
    """The decoded bodies of the message's parts of mime_type ("text/plain", "text/html"), in order."""
    return [_decode(p["body"]["data"]) for p in payload_parts(msg)
            if p.get("mimeType") == mime_type and (p.get("body") or {}).get("data")]


def body_text(msg):
    """text/plain if present, else de-tagged text/html, else the snippet."""
    plain, htmlp = bodies(msg, "text/plain"), bodies(msg, "text/html")
    if plain:
        return "\n".join(plain)
    if htmlp:
        t = re.sub(r"(?is)<(script|style).*?</\1>", "", "\n".join(htmlp))
        t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
        t = re.sub(r"<[^>]+>", "", t)
        return html.unescape(t)
    return msg.get("snippet", "")


def get_thread(svc, thread_id):
    """A Gmail thread with every message in full."""
    return svc.users().threads().get(userId="me", id=thread_id, format="full").execute()


def thread_messages(svc, thread_id):
    """All messages in a Gmail thread, oldest first, as (headers, body_text, internal_ts)."""
    t = get_thread(svc, thread_id)
    out = []
    for m in t.get("messages", []):
        out.append((headers(m), body_text(m), int(m.get("internalDate", "0")) / 1000.0, m.get("id")))
    out.sort(key=lambda x: x[2])
    return out


def auth_results(hdrs):
    """Parse Gmail's Authentication-Results header into {dkim: [domains passing], spf: bool, dmarc: str}."""
    import re
    ar = hdrs.get("authentication-results", "") or ""
    dkim = re.findall(r"dkim=pass[^;]*?header\.(?:i=@|d=)([\w.-]+)", ar)
    spf = bool(re.search(r"\bspf=pass\b", ar))
    m = re.search(r"dmarc=(\w+)", ar)
    return {"dkim": [d.lower() for d in dkim], "spf": spf, "dmarc": m.group(1) if m else "", "raw": ar[:300]}
