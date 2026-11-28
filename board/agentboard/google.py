"""A small Google API client for an OAuth "authorized_user" login (a refresh token), standard library only, and the
Calendar API helpers its calendar readers share (CALENDAR_API, calendar_events_url, attending).

Both logins are the owner's and optional (agentboard/config.py, the board config): gmail_login() (gmail_credentials)
is the owner's own Gmail, which gmailstate and the Time page's own-email source use; docs_login() (google_token)
reads the owner's calendars (and Drive file metadata), which meetings (the Mac's meeting alerts) and the Time page's
meetings and documents sources use. Each is None while it is off or its file is missing, and what needs it is then
off too. The access token stays in memory and never reaches a URL, a log or an error message. An HTTP error carries
its status (GoogleError.status), so a caller can tell a refusal such as 403 from an outage.
"""
import json
import time
import urllib.parse
from pathlib import Path

from . import config, webapi

TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3/calendars/"
# the login's own entry for a calendar, by the calendar's id: its access role there
CALENDAR_LIST = "https://www.googleapis.com/calendar/v3/users/me/calendarList/"


def _present(path):
    return path if path is not None and Path(path).is_file() else None


def gmail_login():
    """The owner's own Gmail login file (board config gmail_credentials, or AGENT_BOARD_GMAIL_CREDS), or None when it
    is not configured or the file is missing."""
    return _present(config.gmail_credentials())


def docs_login():
    """The owner's Google login file with Calendar (and Drive metadata) read access (board config google_token, or
    AGENT_BOARD_GOOGLE_TOKEN), or None when it is not configured or the file is missing."""
    return _present(config.google_token())


def calendar_events_url(calendar_id):
    """The Calendar API's event list of the calendar `calendar_id` ("primary": the login's own calendar)."""
    return CALENDAR_API + urllib.parse.quote(calendar_id, safe="") + "/events"


def attending(event, types):
    """Whether a Calendar API event is timed, of an eventType in `types` (an event without one is "default"), and not
    declined by the login's account."""
    if "dateTime" not in event.get("start", {}) or event.get("eventType", "default") not in types:
        return False
    me = next((a for a in event.get("attendees", []) if a.get("self")), None)
    return not (me and me.get("responseStatus") == "declined")


class GoogleError(webapi.ApiError):
    """A Google API request that failed (webapi.ApiError: `status` is the HTTP status)."""


def http(method, url, data=None, headers=None, timeout=20, body=None, raw=None):
    """One request, `data` URL-encoded, `body` as JSON or `raw` bytes as they are (webapi.request); JSON back, or
    GoogleError naming the status only, never the request (it may carry the token)."""
    return webapi.request(method, url, "Google API", GoogleError, form=data, body=body, headers=headers,
                          timeout=timeout, raw=raw)


def header(message, name):
    """The value of header `name` of a Gmail API message read with format=metadata, or "" when it has none."""
    return next((h["value"] for h in message.get("payload", {}).get("headers", []) if h["name"].lower() == name.lower()),
                "")


def _with_query(url, params):
    return url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")


class Client:
    def __init__(self, creds_path, http=http):
        self.creds_path = Path(creds_path)
        self.http = http
        self.access, self.expires = None, 0

    def token(self):
        if self.access and time.time() < self.expires - 60:
            return self.access
        c = json.loads(self.creds_path.read_text())
        r = self.http("POST", TOKEN_URL, data={"client_id": c["client_id"], "client_secret": c["client_secret"],
                                               "refresh_token": c["refresh_token"], "grant_type": "refresh_token"})
        self.access, self.expires = r["access_token"], time.time() + int(r.get("expires_in", 3600))
        return self.access

    def get(self, url, params=None):
        return self.http("GET", _with_query(url, params), headers={"Authorization": "Bearer " + self.token()})

    def patch(self, url, body, params=None):
        """Change the fields in `body` (sent as JSON) of the thing at `url`; its new state."""
        return self.http("PATCH", _with_query(url, params), headers={"Authorization": "Bearer " + self.token()},
                         body=body)

    def upload(self, url, raw, content_type, timeout):
        """Send the bytes `raw`, of `content_type`, to an upload endpoint at `url`, allowing `timeout` seconds to send
        them; its answer."""
        return self.http("POST", url, headers={"Authorization": "Bearer " + self.token(), "Content-Type": content_type},
                         raw=raw, timeout=timeout)

    def pages(self, url, params, key, max_pages=None):
        """Every item under `key` across the result pages (nextPageToken), at most `max_pages` pages if given."""
        page, n = None, 0
        while max_pages is None or n < max_pages:
            got = self.get(url, dict(params, **({"pageToken": page} if page else {})))
            n += 1
            yield from got.get(key, [])
            page = got.get("nextPageToken")
            if not page:
                return
