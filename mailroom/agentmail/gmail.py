"""Thin Gmail API layer. Only gmail.modify is ever requested."""
import base64
import html
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import SCOPES, TOKEN_PATH


class HistoryGone(Exception):
    """startHistoryId is too old; caller must fall back to a search."""


class NoToken(RuntimeError):
    """The agents account has not been authorised on this machine yet."""


def service():
    if not TOKEN_PATH.exists():
        raise NoToken("no Gmail token at %s: run `agent-mail-auth` (mailroom/agent_mail_auth.py) once, "
                           "signed in as the agents account" % TOKEN_PATH)
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


def headers(msg):
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def history_since(svc, start_id):
    """Ids of messages added to INBOX since start_id, and the newest history id."""
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
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out, str(newest)


def list_recent(svc, q="in:inbox newer_than:2d", limit=100):
    resp = svc.users().messages().list(userId="me", q=q, maxResults=limit).execute()
    return [m["id"] for m in resp.get("messages", [])]


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


def body_text(msg):
    """text/plain if present, else de-tagged text/html, else the snippet."""
    plain, htmlp = [], []

    def walk(part):
        mt = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mt == "text/plain" and data:
            plain.append(_decode(data))
        elif mt == "text/html" and data:
            htmlp.append(_decode(data))
        for p in part.get("parts", []) or []:
            walk(p)

    walk(msg.get("payload", {}))
    if plain:
        return "\n".join(plain)
    if htmlp:
        t = re.sub(r"(?is)<(script|style).*?</\1>", "", "\n".join(htmlp))
        t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
        t = re.sub(r"<[^>]+>", "", t)
        return html.unescape(t)
    return msg.get("snippet", "")


def thread_messages(svc, thread_id):
    """All messages in a Gmail thread, oldest first, as (headers, body_text, internal_ts)."""
    t = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
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
