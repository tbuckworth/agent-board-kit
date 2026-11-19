"""The owner's upcoming meetings, for the Mac app's meeting alerts (mac/Sources/AgentBoard/Meetings.swift): a
notification notify_minutes before each start, and a full-screen overlay fullscreen_minutes before.

Optional: on only when the board config turns "meeting_alerts" on and names the owner's Google login in
"google_token" (google.docs_login, the login the Time page reads the owner's calendar with); make() gives None
otherwise, and the snapshot then has no meetings section. Every refresh_minutes it lists each configured calendar's events from now to lookahead_hours ahead.
An event of a calendar shared with that login as free/busy only has no title and no join link: the Mac names it after
its calendar's label and offers the calendar instead. It only reads. ~/.config/agent-board/meetings.json (or
AGENT_BOARD_MEETINGS_CONFIG) overrides DEFAULTS.
"""
import os
import time
import urllib.parse
from pathlib import Path

from . import config as board_config, google, sources

CONFIG = Path(os.environ.get("AGENT_BOARD_MEETINGS_CONFIG") or sources.HOME / ".config/agent-board/meetings.json")
DEFAULTS = {
    # the calendars to read, in order: "id" (Google's calendar id; "primary" is the login's own calendar), "label"
    # (an event with no title is called "<label> meeting") and, optionally, "account": the Google account the Mac
    # opens the calendar's meetings in, through the Chrome profile signed in to it (Browser.swift). Without one, the
    # login's own account, the id of its primary calendar.
    "calendars": [{"id": "primary", "label": "Personal"}],
    "lookahead_hours": 24,            # how far ahead meetings are listed
    "notify_minutes": 10,             # the Mac's notification comes this long before a meeting starts
    "fullscreen_minutes": 1,          # and its full-screen overlay this long before
    "refresh_minutes": 5,             # how often the calendars are read
}
SPANS = ("lookahead_hours", "refresh_minutes")         # numbers over 0
LEADS = ("notify_minutes", "fullscreen_minutes")       # numbers, 0 or more (0: at the start)
# the event types that are meetings; an event without one is a default event. Not workingLocation, outOfOffice,
# focusTime or birthday.
MEETING_TYPES = ("default", "fromGmail")
FIELDS = ("nextPageToken,items(id,iCalUID,status,summary,start,end,eventType,hangoutLink,location,"
          "conferenceData(entryPoints(entryPointType,uri)),attendees(self,responseStatus,resource))")


def config(path):
    """DEFAULTS with the file at `path`'s overrides. Raises ValueError for calendars that are not a non-empty list of
    {"id", "label", "account"?} with text values, or a setting in SPANS or LEADS that is not a number in its range."""
    cfg = sources.load_config(DEFAULTS, path)
    cals = cfg["calendars"]
    if not (isinstance(cals, list) and cals and all(
            isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("label"), str)
            and isinstance(c.get("account", ""), str) and set(c) <= {"id", "label", "account"} for c in cals)):
        raise ValueError('%s: calendars must be a list of {"id", "label", "account"?} with text values' % path)
    for k in SPANS + LEADS:
        v = cfg[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (v == 0 and k in SPANS):
            raise ValueError("%s: %s must be a number %s" % (path, k, "over 0" if k in SPANS else "0 or over"))
    return cfg


def is_meeting(e):
    """A timed event that is not cancelled, not declined by the login's account, and of a type in MEETING_TYPES."""
    return e.get("status") != "cancelled" and google.attending(e, MEETING_TYPES)


def join_url(e):
    """The meeting's video link: Meet's hangoutLink, else the video entry point of its conference (Zoom, Teams...),
    else None."""
    if e.get("hangoutLink"):
        return e["hangoutLink"]
    points = (e.get("conferenceData") or {}).get("entryPoints", [])
    return next((p["uri"] for p in points if p.get("entryPointType") == "video" and p.get("uri")), None)


def open_url(account, start):
    """Google Calendar's day view, in `account`, of the day (local time) the meeting starts on: unlike an event's own
    page, it shows a free/busy event too."""
    day = time.localtime(start)
    return ("https://calendar.google.com/calendar/r/day/%d/%d/%d?" % (day.tm_year, day.tm_mon, day.tm_mday)
            + urllib.parse.urlencode({"authuser": account}))


def meeting(e, cal, account):
    """One event of the configured calendar `cal` as the snapshot carries it; the title is None for a free/busy
    event. "invited" counts the people asked to it, the owner included (1 for an event with no guest list), not the
    rooms a guest list may name."""
    start, end = sources.iso_ts(e["start"]["dateTime"]), sources.iso_ts(e["end"]["dateTime"])
    return {"calendar": cal["label"], "calendar_id": cal["id"], "id": e["id"], "title": e.get("summary") or None,
            "start": start, "end": end, "join_url": join_url(e), "open_url": open_url(account, start),
            "account": account, "location": e.get("location") or None,
            "invited": sum(1 for a in e.get("attendees", []) if not a.get("resource")) or 1}


def make(sync=False):
    """A Meetings on the owner's Google login, or None when the board config leaves meeting alerts off
    (meeting_alerts) or has no such login (google_token): meeting alerts off, silently."""
    if not board_config.meeting_alerts():
        return None
    creds = google.docs_login()
    return Meetings(creds_path=creds, sync=sync) if creds else None


class Meetings:
    """The snapshot's meetings section, read every refresh_minutes as a sources.Periodic (in the background unless
    `sync`). The config file is read at every read, so a change to it needs no restart."""

    def __init__(self, config_path=CONFIG, creds_path=None, http=google.http, sync=False):
        self.config_path = Path(config_path)
        self.creds_path = Path(creds_path or google.docs_login() or "/nonexistent/google-token.json")
        self.client = google.Client(self.creds_path, http)
        self.own_account = None       # the login's account, once asked (the id of its primary calendar)
        self.last = {}                # calendar id -> [(iCalUID, meeting)] from its last read that worked
        self.timer = sources.Periodic(self.read, DEFAULTS["refresh_minutes"] * 60, None, sync)

    def _account(self, cal):
        if cal.get("account"):
            return cal["account"]
        if self.own_account is None:
            self.own_account = self.client.get(google.CALENDAR_API + "primary", {"fields": "id"})["id"]
        return self.own_account

    def _calendar(self, cal, now, until):
        """[(iCalUID, meeting)] of one calendar, by start. Raises google.GoogleError when Google cannot be asked."""
        account = self._account(cal)
        params = {"timeMin": sources.rfc3339(now), "timeMax": sources.rfc3339(until), "singleEvents": "true",
                  "orderBy": "startTime", "maxResults": 250, "fields": FIELDS}
        events = self.client.pages(google.calendar_events_url(cal["id"]), params, "items")
        return [(e["iCalUID"], meeting(e, cal, account)) for e in events if is_meeting(e)]

    def read(self, now=None, until=None):
        """{"notify_minutes", "fullscreen_minutes", "events": the meetings starting or going on from `now` (default
        the time now) to `until` (default lookahead_hours after `now`), by start, "failed": [{"calendar": label,
        "error"}]}. A calendar that cannot be read keeps the meetings of its last good read that have not ended, and
        is named in failed. A meeting on two calendars (one iCalUID and start) is listed once, from the first calendar
        in the config. Raises with no login, or with a config file that config() refuses."""
        cfg = config(self.config_path)
        self.timer.interval = cfg["refresh_minutes"] * 60
        if not self.creds_path.exists():
            raise RuntimeError("no Google login at %s" % self.creds_path)
        now = now or time.time()
        until = until or now + cfg["lookahead_hours"] * 3600
        events, failed, seen = [], [], set()
        for cal in cfg["calendars"]:
            try:
                got = self.last[cal["id"]] = self._calendar(cal, now, until)
            except google.GoogleError as e:
                failed.append({"calendar": cal["label"], "error": sources.error_text(e)})
                got = [(uid, m) for uid, m in self.last.get(cal["id"], []) if m["end"] > now]
            for uid, m in got:
                if (uid, m["start"]) not in seen:
                    seen.add((uid, m["start"]))
                    events.append(m)
        events.sort(key=lambda m: m["start"])
        return {"notify_minutes": cfg["notify_minutes"], "fullscreen_minutes": cfg["fullscreen_minutes"],
                "events": events, "failed": failed}

    def maybe_refresh(self, now=None, force=False):
        """The last read's value (None before the first read ends), after starting a read for `now` when one is due
        or `force` is set."""
        return self.timer.maybe_refresh(now, force, args=(now,))

    @property
    def error(self):
        """Why the last read failed, or None."""
        return self.timer.error

    def invalidate(self):
        """The next maybe_refresh() reads (a manual refresh)."""
        self.timer.invalidate()
