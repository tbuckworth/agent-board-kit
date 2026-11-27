"""Optional: the agents' mail in the owner's own Gmail, from the owner's login: whether it has been read; and a draft
there when the owner forwards an agent's mail from the Mail tab.

Off unless the board config (~/.config/agent-board/config.json) names an OAuth file in "gmail_credentials" (or
AGENT_BOARD_GMAIL_CREDS is set; google.gmail_login): an "authorized_user" JSON with client_id, client_secret and
refresh_token for the owner's own Gmail. The read-check needs the gmail.readonly scope (gmail.metadata is not enough:
it searches); the Mail tab's Forward makes a draft, so it needs gmail.compose (or gmail.modify) as well, and with a
read-only login Gmail refuses it and the Forward's answer says so. The file a Gmail MCP server keeps works. Without
it the board shows "read-check off" and the Forward says the desktop has no Gmail login.

Once a minute: one `messages.list` of the *unread* mail from the agents' address in the last 8 days, and, only for
mails the board newly cares about (each agent's latest unanswered mail), one `messages.list q=rfc822msgid:...` to
find it in the owner's mailbox (cached); never for mail the mailroom keeps for the Mail tab alone, which Gmail has no
copy of (sources.tab_only). On request, create_draft(), which makes the draft of a forward (mailforward.py). It never
reads a body, never looks at another sender's mail, and changes nothing there but the drafts it makes. The login is
checked to belong to an owner address (mailroom config owner_email / allowlist) first; the access token stays in
memory and is never put in a URL or an error message.
"""
from pathlib import Path

from . import google, sources

API = "https://gmail.googleapis.com/gmail/v1/users/me"
WINDOW_DAYS = 8
INTERVAL = 60       # seconds between checks
CHECK_PAGES = 5     # result pages a check reads: 500 mails, far more than 8 days of agent mail
UPLOAD = "https://gmail.googleapis.com/upload/gmail/v1/users/me"
MAX_DRAFT_BYTES = 35 << 20   # the largest message Gmail's drafts.create takes through its upload endpoint
DRAFT_SECONDS = 300          # allowed for sending a draft's bytes: the largest at 1 Mbit/s


def for_owner(sync, sender=None, owners=None):
    """A GmailState of the agents' mail in the owner's Gmail, if the board config turns the owner's Gmail login on and
    its file exists, the mailroom names the agents' account and the owner's addresses (else None: the check is off);
    checked in the caller's thread when `sync` is set. sender, owners: the agents' address and the owner's (default:
    the mailroom config's)."""
    creds = google.gmail_login()
    sender = sender if sender is not None else sources.mail_account()
    owners = owners if owners is not None else sources.owner_addresses()
    if not creds or not sender or not owners:
        return None
    return GmailState(sender, owners, creds_path=creds, sync=sync)


class GmailState:
    """The read state, checked every INTERVAL seconds as a Periodic (in the background unless `sync`), and on
    request a forward's draft (create_draft)."""

    def __init__(self, sender, owners, creds_path, http=None, sync=False):
        self.sender = sender
        self.owners = [o.lower() for o in owners]
        self.creds_path = Path(creds_path)
        self.client = google.Client(self.creds_path, http or google.http)
        self.verified = False
        self.found = {}          # normalised Message-ID -> Gmail ids of that mail in the owner's mailbox
        self.threads = {}        # normalised Message-ID -> its thread id there (for an "open in Gmail" link)
        self.wanted = set()      # Message-IDs the board asks about (set by the collector each poll)
        self.unread_ids = set()
        self.timer = sources.Periodic(self.poll_once, INTERVAL, None, sync)

    def _get(self, path, **params):
        return self.client.get(API + path, params)

    def _list(self, q):
        """The mail Gmail search q finds, {id, threadId} each, 100 to a page, at most CHECK_PAGES pages."""
        return list(self.client.pages(API + "/messages", {"q": q, "maxResults": 100}, "messages",
                                      max_pages=CHECK_PAGES))

    def _ids(self, q):
        return [m["id"] for m in self._list(q)]

    def verify(self):
        """Check, once, that the login exists and is the owner's. Raises RuntimeError otherwise."""
        if not self.creds_path.exists():
            raise RuntimeError("no Gmail login at %s" % self.creds_path)
        if not self.verified:
            who = (self._get("/profile").get("emailAddress") or "").lower()
            if who not in self.owners:
                raise RuntimeError("the Gmail login there is not the owner's (it is %s); not used" % (who or "unknown"))
            self.verified = True

    def create_draft(self, raw):
        """A draft in the owner's Gmail of the message `raw` (its MIME bytes, which name no sender: Gmail fills in the
        owner's), sent whole as message/rfc822 to the upload endpoint (uploadType=media), which takes up to
        MAX_DRAFT_BYTES: Gmail's Draft, {"id", "message": {"id", ...}}. Raises ValueError for a message over that,
        before sending anything; RuntimeError when the login is not the owner's (verify) or Gmail refuses
        (google.GoogleError)."""
        if len(raw) > MAX_DRAFT_BYTES:
            raise ValueError("the draft would be %.1f MB, more than the %d MB Gmail takes in one, so none was made"
                             % (len(raw) / (1 << 20), MAX_DRAFT_BYTES >> 20))
        self.verify()
        return self.client.upload(UPLOAD + "/drafts?uploadType=media", raw, "message/rfc822", DRAFT_SECONDS)

    def poll_once(self):
        self.verify()
        self.unread_ids = set(self._ids("from:%s newer_than:%dd is:unread" % (self.sender, WINDOW_DAYS)))
        wanted, self.wanted = self.wanted, set()     # the board re-asks every few seconds; stale wants lapse
        for m in wanted:
            if not self.found.get(m):          # not looked up yet, or not delivered last time
                hits = self._list("rfc822msgid:%s" % m)
                self.found[m] = [h["id"] for h in hits]
                if hits and hits[0].get("threadId"):
                    self.threads[m] = hits[0]["threadId"]

    @property
    def status(self):
        """{"ok": whether the last check worked (None before the first), "checked_at", "error"}."""
        t = self.timer
        return {"ok": None if t.checked_at is None else t.error is None, "checked_at": t.checked_at, "error": t.error}

    @property
    def error(self):
        """Why the last check failed, or None."""
        return self.timer.error

    def maybe_refresh(self, now=None, force=False):
        """status, after starting a check when INTERVAL has passed or `force` is set."""
        self.timer.maybe_refresh(now, force)
        return self.status

    def invalidate(self):
        """The next maybe_refresh() checks (a manual refresh)."""
        self.timer.invalidate()

    def thread_of(self, message_id):
        """The mail's thread id in the owner's mailbox, once looked up (for a direct Gmail link), else None."""
        return self.threads.get(sources.norm_id(message_id))

    def is_read(self, message_id):
        """True read, False unread, None not known (not in the owner's mailbox yet, or Gmail unavailable).
        Asking also marks the mail as wanted, so the next poll looks it up; but for mail in the Mail tab alone
        (sources.tab_only), which no search of the owner's mailbox would ever find, the answer is None and nothing is
        looked up."""
        m = sources.norm_id(message_id)
        if not m or sources.tab_only(m):
            return None
        self.wanted.add(m)
        gids = self.found.get(m)
        if not self.status.get("ok") or not gids:
            return None
        return not any(g in self.unread_ids for g in gids)
