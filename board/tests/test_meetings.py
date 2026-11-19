"""The meetings the Mac app alerts for: which calendar events count, what each carries, a calendar that fails, the
config, the snapshot's meetings section, and the alerts being off without the owner's Google login."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fixtures  # noqa: E402
from fixtures import OWN, WORK, FakeCalendar, config_file, event  # noqa: E402
from fixtures import meetings_reader as reader  # noqa: E402
from agentboard import config, google, meetings  # noqa: E402

NOW = 1_800_000_000.0          # Friday 15 Jan 2027 08:00 UTC


class LocalTime(unittest.TestCase):
    """Run in one fixed zone (Chicago), whatever the machine's: the calendar day view is the meeting's local day."""

    def setUp(self):
        self.saved_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Chicago"
        time.tzset()

    def tearDown(self):
        if self.saved_tz is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = self.saved_tz
        time.tzset()


class WhichEvents(LocalTime):
    def test_keeps_timed_meetings_the_login_has_not_declined(self):
        meet = "https://meet.google.com/abc-defg-hij"
        zoom = {"entryPoints": [{"entryPointType": "phone", "uri": "tel:+1"},
                                {"entryPointType": "video", "uri": "https://zoom.us/j/1"}]}
        primary = [
            event("standup", NOW + 600, summary="Standup", eventType="default", hangoutLink=meet,
                  attendees=[{"email": "x@example.com"}, {"self": True, "responseStatus": "accepted"}]),
            event("zoomcall", NOW + 1200, summary="Zoom call", conferenceData=zoom),          # no eventType: default
            event("invite", NOW + 1800, summary="Flight", eventType="fromGmail"),
            event("untitled", NOW + 2400),
            dict(event("allday", NOW), start={"date": "2027-01-15"}, end={"date": "2027-01-16"}, summary="Holiday"),
            event("cancelled", NOW + 3000, status="cancelled", summary="Off"),
            event("declined", NOW + 3600, summary="No", attendees=[{"self": True, "responseStatus": "declined"}]),
            event("office", NOW, eventType="workingLocation", summary="Home"),
            event("away", NOW, eventType="outOfOffice", summary="Away"),
            event("focus", NOW, eventType="focusTime", summary="Focus"),
            event("bday", NOW, eventType="birthday", summary="Ann"),
        ]
        fake = FakeCalendar({"primary": primary})
        got = reader(fake).read(NOW)
        self.assertEqual(got["failed"], [])
        self.assertEqual((got["notify_minutes"], got["fullscreen_minutes"]), (10, 1))
        self.assertEqual([m["id"] for m in got["events"]], ["standup", "zoomcall", "invite", "untitled"])
        self.assertEqual(got["events"][0], {
            "calendar": "Personal", "calendar_id": "primary", "id": "standup", "title": "Standup", "start": NOW + 600,
            "end": NOW + 2400, "join_url": meet, "account": OWN, "location": None, "invited": 2,
            "open_url": "https://calendar.google.com/calendar/r/day/2027/1/15?authuser=me%40example.com"})
        self.assertEqual([m["join_url"] for m in got["events"][1:]], ["https://zoom.us/j/1", None, None])
        self.assertIsNone(got["events"][3]["title"])
        events = [c for c in fake.calls if c[0].endswith("/events")]
        self.assertEqual(events[0][0], google.CALENDAR_API + "primary/events")
        self.assertEqual({k: events[0][1][k] for k in ("timeMin", "timeMax", "singleEvents", "orderBy")},
                         {"timeMin": "2027-01-15T08:00:00Z", "timeMax": "2027-01-16T08:00:00Z", "singleEvents": "true",
                          "orderBy": "startTime"})
        self.assertTrue(all("EXAMPLE-TOKEN" not in base + json.dumps(params) for base, params, _ in fake.calls))

    def test_a_free_busy_calendar_opens_in_its_own_account_and_a_shared_meeting_is_listed_once(self):
        fake = FakeCalendar({"primary": [event("sync", NOW + 900, summary="Sync")],
                             WORK: [event("sync", NOW + 900), event("review", NOW + 10800)]})   # free/busy only
        cfg = config_file(calendars=[{"id": "primary", "label": "Personal"},
                                     {"id": WORK, "label": "Work", "account": WORK}])
        m = reader(fake, cfg)
        got = m.read(NOW)
        self.assertEqual([(e["calendar"], e["id"]) for e in got["events"]], [("Personal", "sync"), ("Work", "review")])
        review = got["events"][1]
        self.assertEqual((review["title"], review["join_url"], review["account"]), (None, None, WORK))
        self.assertEqual(review["open_url"],
                         "https://calendar.google.com/calendar/r/day/2027/1/15?authuser=work%40example.org")
        self.assertIn(google.CALENDAR_API + "work%40example.org/events", [c[0] for c in fake.calls])
        m.read(NOW + 60)
        self.assertEqual([c[0] for c in fake.calls].count(google.CALENDAR_API + "primary"), 1)     # its account: asked once

    def test_the_day_view_is_the_local_day_the_meeting_starts_on(self):
        late_evening = meetings.sources.iso_ts("2026-07-02T04:30:00Z")                    # 23:30 on 1 July in Chicago
        self.assertEqual(meetings.open_url(WORK, late_evening),
                         "https://calendar.google.com/calendar/r/day/2026/7/1?authuser=work%40example.org")


class Failures(unittest.TestCase):
    def test_a_calendar_that_fails_keeps_its_last_meetings_and_is_named(self):
        work = [event("early", NOW + 600), event("later", NOW + 7200)]
        fake = FakeCalendar({"primary": [event("lunch", NOW + 3600, summary="Lunch")], WORK: work})
        cfg = config_file(calendars=[{"id": "primary", "label": "Personal"},
                                     {"id": WORK, "label": "Work", "account": WORK}])
        m = reader(fake, cfg)
        m.read(NOW)
        fake.calendars[WORK] = google.GoogleError("Google API answered HTTP 503", 503)
        fake.calendars["primary"] = []                                      # lunch cancelled meanwhile
        got = m.read(NOW + 2400)                                            # "early" has ended by now
        self.assertEqual([e["id"] for e in got["events"]], ["later"])
        self.assertEqual(got["failed"], [{"calendar": "Work", "error": "GoogleError: Google API answered HTTP 503"}])

    def test_no_login_or_a_bad_config_fails_the_read(self):
        missing = reader(FakeCalendar({}), creds=False)
        self.assertIsNone(missing.maybe_refresh(NOW))
        self.assertTrue(missing.error.startswith("RuntimeError: no Google login at "), missing.error)
        for bad, why in [({"calendars": []}, "calendars must be"),
                         ({"calendars": [{"id": "primary"}]}, "calendars must be"),
                         ({"calendars": [{"id": "primary", "label": "P", "acount": "x"}]}, "calendars must be"),
                         ({"notify_minutes": "10"}, "notify_minutes must be a number 0 or over"),
                         ({"fullscreen_minutes": -1}, "fullscreen_minutes must be a number 0 or over"),
                         ({"refresh_minutes": 0}, "refresh_minutes must be a number over 0"),
                         ({"lookahead_hours": True}, "lookahead_hours must be a number over 0")]:
            m = reader(FakeCalendar({"primary": []}), config_file(**bad))
            self.assertIsNone(m.maybe_refresh(NOW))
            self.assertTrue(m.error.startswith("ValueError: ") and why in m.error, (bad, m.error))

    def test_the_config_sets_the_lead_times_and_the_refresh(self):
        m = reader(FakeCalendar({"primary": []}), config_file(notify_minutes=5, fullscreen_minutes=0, refresh_minutes=2))
        got = m.maybe_refresh(NOW)
        self.assertEqual((got["notify_minutes"], got["fullscreen_minutes"], m.timer.interval), (5, 0, 120))


class Snapshot(unittest.TestCase):
    def test_the_snapshot_carries_the_meetings_and_names_what_failed(self):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeCalendar({"primary": [event("standup", NOW + 600, summary="Standup")],
                                 WORK: google.GoogleError("Google API answered HTTP 404", 404)})
            cfg = config_file(calendars=[{"id": "primary", "label": "Personal"},
                                         {"id": WORK, "label": "Work", "account": WORK}])
            snap = fixtures.collector(d, meetings=reader(fake, cfg)).snapshot(NOW)
            self.assertEqual(set(snap["meetings"]), {"notify_minutes", "fullscreen_minutes", "events"})
            self.assertEqual([e["id"] for e in snap["meetings"]["events"]], ["standup"])
            self.assertEqual(snap["problems"], [{"source": "meetings: Work calendar",
                                                 "error": "GoogleError: Google API answered HTTP 404"}])
            broken = fixtures.collector(d, meetings=reader(fake, config_file(calendars="primary")))
            snap = broken.snapshot(NOW)
            self.assertNotIn("meetings", snap)                      # the Mac keeps the meetings it has
            self.assertEqual([p["source"] for p in snap["problems"]], ["meetings"])


class Optional(unittest.TestCase):
    """Meeting alerts need the board config's meeting_alerts and the owner's Google login (google_token): without
    either the collector has no meetings reader, and the snapshot no meetings section and no problem line."""

    def env(self, key, value):
        saved = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.pop(key) if saved is None else os.environ.__setitem__(key, saved))

    def test_off_without_a_google_token_and_on_with_one(self):
        self.assertIsNone(config.board_config()["google_token"])
        self.env("AGENT_BOARD_MEETING_ALERTS", "yes")
        self.assertIsNone(meetings.make())
        with tempfile.TemporaryDirectory() as d:
            snap = fixtures.collector(d, meetings=meetings.make(sync=True)).snapshot(NOW)
            self.assertNotIn("meetings", snap)
            self.assertEqual(snap["problems"], [])
            token = Path(d) / "token.json"
            self.env("AGENT_BOARD_GOOGLE_TOKEN", str(token))
            self.assertIsNone(meetings.make())                     # named, but no file there: still off
            token.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
            m = meetings.make(sync=True)
            self.assertEqual(m.creds_path, token)

    def test_a_google_token_alone_alerts_nothing(self):
        self.assertIs(config.board_config()["meeting_alerts"], False)
        self.assertFalse(config.meeting_alerts())
        with tempfile.TemporaryDirectory() as d:
            token = Path(d) / "token.json"
            token.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
            self.env("AGENT_BOARD_GOOGLE_TOKEN", str(token))
            self.assertIsNone(meetings.make())                     # the Time page may use the token; no alerts
            for value, on in (("no", False), ("off", False), ("", False), ("YES", True), ("true", True), ("1", True)):
                self.env("AGENT_BOARD_MEETING_ALERTS", value)
                self.assertEqual(meetings.make() is not None, on, value)

    def test_the_board_config_turns_them_on(self):
        self.assertFalse(config.meeting_alerts({"meeting_alerts": False}))
        self.assertFalse(config.meeting_alerts({"meeting_alerts": "yes"}))          # JSON true only
        self.assertTrue(config.meeting_alerts({"meeting_alerts": True}))
        saved = os.environ.pop("AGENT_BOARD_MEETING_ALERTS", None)
        self.addCleanup(lambda: saved is not None and os.environ.__setitem__("AGENT_BOARD_MEETING_ALERTS", saved))
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"meeting_alerts": True}))
            self.assertTrue(config.meeting_alerts(config.board_config(cfg)))


if __name__ == "__main__":
    unittest.main()
