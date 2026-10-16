"""Optional: has the owner read an agent's mail? Read-only Gmail metadata from the owner's own Gmail login.

Off unless the board config (~/.config/agent-board/config.json) names an OAuth file in "gmail_credentials" (or
AGENT_BOARD_GMAIL_CREDS is set): an "authorized_user" JSON with client_id, client_secret and refresh_token for the
owner's personal Gmail, with the gmail.readonly scope or a broader one such as gmail.modify (search queries need
it; gmail.metadata is not enough). The file a Gmail MCP server keeps, e.g. ~/.gmail-mcp/credentials.json, works.

Once a minute: one `messages.list` of the *unread* mail from the agents' address in the last 8 days, and, only for
mails the board newly cares about (each agent's latest unanswered mail), one `messages.list q=rfc822msgid:...` to
find it in the owner's mailbox (cached). It never reads a body, never looks at another sender's mail, never
writes. The login is checked to belong to an owner address (mailroom config owner_email / allowlist) first; the
access token stays in memory and is never put in a URL or an error message.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
WINDOW_DAYS = 8


def norm_id(message_id):
    return (message_id or "").strip().strip("<>").strip().lower()


class GmailRead:
    def __init__(self, sender, owners, creds_path, interval=60, http=None):
        self.sender = sender
        self.owners = [o.lower() for o in owners]
        self.creds_path = Path(creds_path)
        self.interval = interval
        self.http = http or self._http
        self.access, self.expires = None, 0
        self.verified = False
        self.found = {}          # normalised Message-ID -> Gmail ids of that mail in the owner's mailbox
        self.threads = {}        # normalised Message-ID -> its thread id there (for an "open in Gmail" link)
        self.wanted = set()      # Message-IDs the board asks about (set by the collector each poll)
        self.unread_ids = set()
        self.status = {"ok": None, "checked_at": None, "error": None}
        self.at = 0
        self.busy = False
        self.lock = threading.Lock()

    @staticmethod
    def _http(method, url, data=None, headers=None, timeout=15):
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:     # message only: never the request, which may carry the token
            raise RuntimeError("Gmail API answered HTTP %d" % e.code) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError("Gmail API unreachable: %s" % type(e).__name__) from None

    def _token(self):
        if self.access and time.time() < self.expires - 60:
            return self.access
        c = json.loads(self.creds_path.read_text())
        r = self.http("POST", TOKEN_URL, data={"client_id": c["client_id"], "client_secret": c["client_secret"],
                                               "refresh_token": c["refresh_token"], "grant_type": "refresh_token"})
        self.access, self.expires = r["access_token"], time.time() + int(r.get("expires_in", 3600))
        return self.access

    def _get(self, path, **params):
        url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        return self.http("GET", url, headers={"Authorization": "Bearer " + self._token()})

    def _list(self, q):
        out, page = [], None
        for _ in range(5):                     # at most 500 messages: 8 days of agent mail is far less
            r = self._get("/messages", q=q, maxResults=100, **({"pageToken": page} if page else {}))
            out += r.get("messages", [])
            page = r.get("nextPageToken")
            if not page:
                break
        return out

    def _ids(self, q):
        return [m["id"] for m in self._list(q)]

    def poll_once(self):
        if not self.creds_path.exists():
            raise RuntimeError("no Gmail login at %s" % self.creds_path)
        if not self.verified:
            who = (self._get("/profile").get("emailAddress") or "").lower()
            if who not in self.owners:
                raise RuntimeError("the Gmail login there is not an owner address (it is %s); not used"
                                   % (who or "unknown"))
            self.verified = True
        self.unread_ids = set(self._ids("from:%s newer_than:%dd is:unread" % (self.sender, WINDOW_DAYS)))
        wanted, self.wanted = self.wanted, set()     # the board re-asks every few seconds; stale wants lapse
        for m in wanted:
            if not self.found.get(m):          # not looked up yet, or not delivered last time
                hits = self._list("rfc822msgid:%s" % m)
                self.found[m] = [h["id"] for h in hits]
                if hits and hits[0].get("threadId"):
                    self.threads[m] = hits[0]["threadId"]

    def refresh(self):
        with self.lock:
            if self.busy:
                return
            self.busy = True
        try:
            self.poll_once()
            self.status = {"ok": True, "checked_at": time.time(), "error": None}
        except Exception as e:   # the board must work without it
            self.status = {"ok": False, "checked_at": time.time(), "error": str(e)[:200]}
        finally:
            self.busy = False

    def maybe_poll(self, now=None, force=False):
        now = now or time.time()
        if force or now - self.at > self.interval:
            self.at = now
            threading.Thread(target=self.refresh, daemon=True).start()
        return self.status

    def thread_of(self, message_id):
        """The mail's thread id in the owner's mailbox, once looked up (for a direct Gmail link), else None."""
        return self.threads.get(norm_id(message_id))

    def is_read(self, message_id):
        """True read, False unread, None not known (not in the owner's mailbox yet, or Gmail unavailable).
        Asking also marks the mail as wanted, so the next poll looks it up."""
        m = norm_id(message_id)
        if not m:
            return None
        self.wanted.add(m)
        gids = self.found.get(m)
        if not self.status.get("ok") or not gids:
            return None
        return not any(g in self.unread_ids for g in gids)


def make(sender=None, owners=None):
    """A GmailRead for the owner's login when the board config turns the check on and the file exists, else None."""
    from . import sources
    creds = config.gmail_credentials()
    sender = sender if sender is not None else sources.mail_account()
    owners = owners if owners is not None else sources.owner_addresses()
    if not creds or not creds.exists() or not sender or not owners:
        return None
    return GmailRead(sender, owners, creds)
