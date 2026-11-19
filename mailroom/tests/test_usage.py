import argparse
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import fakes  # noqa: E402
from homes import Homes, config_file, dead_pid  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import accounts, commands, config, launch, registry, send, trust, usage  # noqa: E402

NOW = 1791300000.0  # 2026-10-06
# Shapes as Claude Code 2.1.291 and codex-cli 0.160.0 return them (identities removed).
CLAUDE_RESP = {
    "session": {"total_cost_usd": 0}, "subscription_type": "team", "rate_limits_available": True,
    "rate_limits": {
        "five_hour": {"utilization": 100, "resets_at": "2026-10-06T13:49:59.808156+00:00"},
        "seven_day": {"utilization": 57, "resets_at": "2026-10-12T02:59:59.808182+00:00"},
        "seven_day_opus": None, "seven_day_sonnet": None,
        "extra_usage": {"is_enabled": True, "monthly_limit": None, "used_credits": 21141, "utilization": None,
                        "currency": "USD", "decimal_places": 2, "spend_limit_reached": False},
        "limits": [{"kind": "session", "percent": 100, "resets_at": "2026-10-06T13:49:59+00:00"}],
        "model_scoped": [{"display_name": "Fable", "utilization": 70, "resets_at": "2026-10-12T02:59:59+00:00"}],
    },
    "behaviors": None,
}
CODEX_RESULT = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": 1791627808},
                   "secondary": None, "credits": {"hasCredits": True, "unlimited": False, "balance": "62500"},
                   "spendControlReached": False, "planType": "pro", "rateLimitReachedType": None},
    "rateLimitResetCredits": {"availableCount": 3, "credits": []},
}


def snap(target, windows, **kw):
    cli = "codex" if target == "codex" else "claude"
    d = {"target": target, "cli": cli, "account": None if cli == "codex" else target.split(":", 1)[1],
         "windows": windows, "source": "server", "observed_at": NOW}
    d.update(kw)
    return d


def win(kind, name, remaining, model=None, resets=NOW + 3600):
    return {"kind": kind, "name": name, "used_pct": 100 - remaining, "remaining_pct": remaining,
            "resets_at": resets, "model": model}


PRO_DIR = tempfile.mkdtemp()   # a set-up second login: its config dir with a login in it
Path(PRO_DIR, ".credentials.json").write_text("{}")
TWO = {"usage": {"claude_accounts": {"pro": {"config_dir": PRO_DIR, "exclude_owners": ["Example-Org"]},
                                     "enterprise": {"config_dir": None}},
                 "order": ["claude:pro", "claude:enterprise", "codex"],
                 "min_remaining": {"session": 15, "weekly": 10}},
       "spawn_claude_model": "claude-opus-5-5", "spawn_codex_model": "gpt-6.1-sol"}
LADDER = dict(TWO, spawn_claude_model="claude-fable-5-2", claude_fallback_models=["claude-opus-5-5"])


class Parsing(unittest.TestCase):
    def test_claude_windows_and_extra_usage(self):
        p = usage.parse_claude(CLAUDE_RESP)
        self.assertEqual(p["plan"], "team")
        names = {w["name"]: w for w in p["windows"]}
        self.assertEqual(names["5-hour"]["remaining_pct"], 0)
        self.assertEqual(names["weekly"]["remaining_pct"], 43)
        self.assertEqual(names["weekly Fable"]["model"], "fable")
        self.assertEqual(names["5-hour"]["resets_at"], usage.iso_epoch("2026-10-06T13:49:59.808156+00:00"))
        self.assertEqual(p["extra_usage"], {"enabled": True, "used": 211.41, "monthly_limit": None,
                                            "currency": "USD", "limit_reached": False})

    def test_claude_without_plan_limits_is_an_error(self):
        with self.assertRaises(usage.UsageError):
            usage.parse_claude({"subscription_type": None, "rate_limits_available": False, "rate_limits": None})

    def test_claude_limits_array_only(self):
        p = usage.parse_claude({"rate_limits_available": True, "rate_limits": {"limits": [
            {"kind": "session", "percent": 20, "resets_at": "2026-10-06T13:00:00Z"},
            {"kind": "weekly_all", "percent": 50, "resets_at": None},
            {"kind": "weekly_scoped", "percent": 90, "scope": {"model": {"display_name": "Opus"}}}]}})
        self.assertEqual([(w["kind"], w["name"], w["remaining_pct"], w["model"]) for w in p["windows"]],
                         [("session", "5-hour", 80, None), ("weekly", "weekly", 50, None),
                          ("model_weekly", "weekly Opus", 10, "opus")])

    def test_codex_windows_credits_and_blocks(self):
        p = usage.parse_codex(CODEX_RESULT)
        self.assertEqual(p["plan"], "pro")
        self.assertEqual([(w["kind"], w["name"], w["remaining_pct"]) for w in p["windows"]], [("weekly", "weekly", 90)])
        self.assertEqual(p["credits"], {"has": True, "unlimited": False, "balance": "62500"})
        self.assertEqual((p["reset_credits"], p["blocked"]), (3, []))
        r = json.loads(json.dumps(CODEX_RESULT))
        r["rateLimits"]["secondary"] = {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": NOW}
        r["rateLimits"]["rateLimitReachedType"] = "rate_limit_reached"
        r["ordinaryUsageAllowed"] = False
        p = usage.parse_codex(r)
        self.assertEqual(p["windows"][1]["name"], "5-hour")
        r["rateLimits"]["secondary"]["windowDurationMins"] = None   # unknown length: judged as a session window
        self.assertEqual(usage.parse_codex(r)["windows"][1]["kind"], "session")
        self.assertEqual(p["windows"][1]["kind"], "session")
        self.assertEqual(len(p["blocked"]), 2)

    def test_codex_rollout_fallback(self):
        home = Path(tempfile.mkdtemp())
        day = home / "sessions" / "2026" / "10" / "05"
        day.mkdir(parents=True)
        old, new = day / "rollout-a.jsonl", day / "rollout-b.jsonl"
        rl = {"primary": {"used_percent": 33.0, "window_minutes": 10080, "resets_at": 1791627808}, "secondary": None,
              "credits": {"has_credits": False, "unlimited": False, "balance": None}, "plan_type": "pro"}
        old.write_text(json.dumps({"timestamp": "2026-10-05T10:00:00Z", "payload": {"rate_limits": dict(rl, primary={"used_percent": 1})}}) + "\n")
        new.write_text(json.dumps({"type": "session_meta"}) + "\n" +
                       json.dumps({"timestamp": "2026-10-05T20:15:42Z", "type": "event_msg",
                                   "payload": {"type": "token_count", "rate_limits": rl}}) + "\n")
        os.utime(old, (time.time() - 1000, time.time() - 1000))
        os.utime(new, (time.time(), time.time()))
        parsed, ts = usage.codex_from_rollouts(home)
        self.assertEqual(parsed["windows"][0]["remaining_pct"], 67)
        self.assertEqual(ts, usage.iso_epoch("2026-10-05T20:15:42Z"))
        with self.assertRaises(usage.UsageError):
            usage.codex_from_rollouts(Path(tempfile.mkdtemp()))


class Reading(unittest.TestCase):
    def fake_run(self, stdout, calls):
        def run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, stdout, "")
        return run

    def test_claude_probe_sends_no_prompt_and_isolates_credentials(self):
        d = tempfile.mkdtemp()
        Path(d, ".credentials.json").write_text("{}")
        out = json.dumps({"type": "system", "subtype": "ui_invalidate"}) + "\n" + json.dumps(
            {"type": "control_response", "response": {"subtype": "success", "request_id": "agent-usage", "response": CLAUDE_RESP}})
        calls = []
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "CLAUDE_CONFIG_DIR": "/elsewhere",
               "CLAUDE_CODE_SESSION_ID": "s", "PATH": "/bin"}
        with mock.patch.dict(os.environ, env, clear=True):
            p = usage.fetch_claude({"config_dir": d}, claude="/bin/claude", run=self.fake_run(out, calls))
            usage.fetch_claude({"config_dir": None}, claude="/bin/claude", run=self.fake_run(out, calls))
        self.assertEqual(p["windows"][0]["name"], "5-hour")
        cmd, kw = calls[0]
        self.assertIn("--safe-mode", cmd)
        self.assertIn("--no-session-persistence", cmd)
        sent = [json.loads(l) for l in kw["input"].splitlines()]
        self.assertEqual([m["type"] for m in sent], ["control_request"])   # no user message: no model call
        self.assertEqual(sent[0]["request"]["subtype"], "get_usage")
        self.assertEqual(kw["env"]["CLAUDE_CONFIG_DIR"], str(Path(d).resolve()))
        for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_SESSION_ID"):
            self.assertNotIn(k, kw["env"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", calls[1][1]["env"])   # default login: never point it at ~/.claude

    def test_claude_errors(self):
        calls = []
        err = json.dumps({"type": "control_response", "response": {"subtype": "error", "error": "not supported"}})
        with self.assertRaisesRegex(usage.UsageError, "not supported"):
            usage.fetch_claude({}, claude="/bin/claude", run=self.fake_run(err, calls))
        with self.assertRaisesRegex(usage.UsageError, "no answer to get_usage"):
            usage.fetch_claude({}, claude="/bin/claude", run=self.fake_run("", calls))
        with self.assertRaisesRegex(usage.UsageError, "does not exist"):
            usage.fetch_claude({"config_dir": "/nonexistent/claude-x"}, claude="/bin/claude", run=self.fake_run("", calls))
        d = tempfile.mkdtemp()
        with mock.patch.object(accounts.sys, "platform", "linux"), self.assertRaisesRegex(usage.UsageError, "no login"):
            usage.fetch_claude({"config_dir": d}, claude="/bin/claude", run=self.fake_run("", calls))
        self.assertEqual(len(calls), 2)   # the not-ready accounts never started claude
        self.assertEqual(accounts.account_ready({"config_dir": d}, platform="darwin"), (True, ""))   # Keychain login

    def test_codex_app_server_protocol(self):
        fake = Path(tempfile.mkdtemp()) / "codex"
        fake.write_text("#!%s\n" % sys.executable + (
            "import json, sys\n"
            "assert sys.argv[1:] == ['app-server', '--listen', 'stdio://'], sys.argv\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    if m.get('method') == 'initialize':\n"
            "        print(json.dumps({'method': 'configWarning', 'params': {}}), flush=True)\n"
            "        print(json.dumps({'id': m['id'], 'result': {'userAgent': 'fake'}}), flush=True)\n"
            "    elif m.get('method') == 'account/rateLimits/read':\n"
            "        print(json.dumps({'id': m['id'], 'result': json.loads(%r)}), flush=True)\n" % json.dumps(CODEX_RESULT)))
        fake.chmod(0o755)
        p = usage.fetch_codex_live(timeout=10, codex=str(fake))
        self.assertEqual(p["windows"][0]["remaining_pct"], 90)


class Deciding(unittest.TestCase):
    def snaps(self, pro, ent, cx=(win("weekly", "weekly", 90),), **kw):
        return {"claude:pro": snap("claude:pro", list(pro)), "claude:enterprise": snap("claude:enterprise", list(ent), **kw),
                "codex": snap("codex", list(cx))}

    def test_pro_first_while_it_has_headroom(self):
        rec = usage.recommend(self.snaps([win("session", "5-hour", 60), win("weekly", "weekly", 70)],
                                         [win("session", "5-hour", 100)]), TWO, now=NOW)
        self.assertEqual((rec["use"], rec["cli"], rec["account"]), ("claude:pro", "claude", "pro"))
        self.assertEqual(rec["avoid"], [])

    def test_pro_under_threshold_moves_to_enterprise(self):
        rec = usage.recommend(self.snaps([win("session", "5-hour", 12), win("weekly", "weekly", 70)],
                                         [win("session", "5-hour", 80), win("weekly", "weekly", 50)]), TWO, now=NOW)
        self.assertEqual(rec["use"], "claude:enterprise")
        self.assertEqual(rec["avoid"][0]["target"], "claude:pro")
        self.assertIn("under 15%", rec["avoid"][0]["why"])

    def test_claude_used_up_goes_to_codex_and_says_extra_usage_bills(self):
        rec = usage.recommend(self.snaps([win("weekly", "weekly", 0)], [win("session", "5-hour", 0)],
                                         extra_usage={"enabled": True}), TWO, now=NOW)
        self.assertEqual(rec["use"], "codex")
        why = {a["target"]: a["why"] for a in rec["avoid"]}
        self.assertIn("used up", why["claude:pro"])
        self.assertIn("bills credits", why["claude:enterprise"])
        self.assertIn("Use codex", rec["line"])

    def test_each_login_on_fable_then_each_on_opus_then_codex_then_extra_usage_on_opus(self):
        """The login and model order: personal Fable, work Fable, personal Opus 5.5, work Opus 5.5, and only then
        work credits on Opus 5.5."""
        def place(pro_fable, ent_fable, pro=90, ent=90, cx=90, **kw):
            s = self.snaps([win("session", "5-hour", pro), win("model_weekly", "weekly Fable", pro_fable, "fable")],
                           [win("session", "5-hour", ent), win("model_weekly", "weekly Fable", ent_fable, "fable")],
                           cx=[win("weekly", "weekly", cx)], **kw)
            rec = usage.recommend(s, LADDER, now=NOW)
            return rec["use"], rec["model"], rec["line"]
        self.assertEqual(place(50, 50)[:2], ("claude:pro", "claude-fable-5-2"))
        self.assertEqual(place(0, 50)[:2], ("claude:enterprise", "claude-fable-5-2"))
        use, model, line = place(0, 0)
        self.assertEqual((use, model), ("claude:pro", "claude-opus-5-5"))
        self.assertIn("Use claude:pro on claude-opus-5-5: ", line)
        self.assertIn("Avoid claude:pro on claude-fable-5-2 (weekly Fable window used up", line)
        self.assertEqual(place(0, 0, pro=0)[:2], ("claude:enterprise", "claude-opus-5-5"))
        self.assertEqual(place(0, 0, pro=0, ent=0)[:2], ("codex", "gpt-6.1-sol"))
        use, model, line = place(0, 0, pro=0, ent=0, cx=0, extra_usage={"enabled": True})
        self.assertEqual((use, model), ("claude:enterprise", "claude-opus-5-5"))   # never Fable on extra usage
        self.assertIn("this one bills its extra usage", line)
        # an explicit model is the only one tried, as before
        s = self.snaps([win("session", "5-hour", 90), win("model_weekly", "weekly Fable", 0, "fable")], [])
        rec = usage.recommend(s, LADDER, models={"claude": "claude-fable-5-2"}, only_cli="claude", now=NOW)
        self.assertEqual((rec["use"], rec["model"]), ("claude:enterprise", "claude-fable-5-2"))

    def test_the_model_order_from_config(self):
        self.assertEqual(config.claude_model_order(LADDER), ["claude-fable-5-2", "claude-opus-5-5"])
        self.assertEqual(config.claude_model_order(dict(LADDER, claude_fallback_models=["claude-fable-5-2", "x"])),
                         ["claude-fable-5-2", "x"])                       # the first is not repeated
        self.assertEqual(config.claude_model_order(dict(LADDER, spawn_claude_model=None)), [None])   # the CLI's own
        self.assertEqual(config.claude_model_order(TWO), ["claude-opus-5-5"])
        for bad in ("claude-opus-5-5", [""], [None]):
            with self.assertRaisesRegex(ValueError, "claude_fallback_models must be a list of model ids"):
                config.claude_model_order(dict(LADDER, claude_fallback_models=bad))
        self.assertEqual(config.claude_model_order(config.DEFAULTS), [None])     # the kit leaves it to Claude Code

    def test_owner_exclusion_keeps_employer_repos_off_pro(self):
        rec = usage.recommend(self.snaps([win("session", "5-hour", 90)], [win("session", "5-hour", 80)]),
                              TWO, owner="example-org", now=NOW)
        self.assertEqual(rec["use"], "claude:enterprise")
        self.assertEqual(rec["avoid"][0]["status"], "excluded")
        self.assertEqual(usage.recommend(self.snaps([win("session", "5-hour", 90)], []), TWO, owner="example-user",
                                         now=NOW)["use"], "claude:pro")

    def test_model_windows_count_only_for_the_model_run(self):
        pro = [win("session", "5-hour", 90), win("model_weekly", "weekly Fable", 0, "fable"),
               win("model_weekly", "weekly Opus", 5, "opus")]
        a = usage.assess(snap("claude:pro", pro), usage.settings(TWO), "claude-opus-5-5", NOW)
        self.assertEqual(a["status"], "low")
        self.assertEqual(len(a["why"]), 1)
        a = usage.assess(snap("claude:pro", pro), usage.settings(TWO), "claude-sonnet-5-5", NOW)
        self.assertEqual(a["status"], "ok")

    def test_per_account_threshold_and_low_fallback(self):
        cfg = json.loads(json.dumps(TWO))
        cfg["usage"]["claude_accounts"]["pro"]["min_remaining"] = {"weekly": 50}
        s = self.snaps([win("weekly", "weekly", 40)], [win("weekly", "weekly", 8)], cx=[win("weekly", "weekly", 9)])
        rec = usage.recommend(s, cfg, now=NOW)
        self.assertEqual(rec["use"], "claude:pro")   # every one is low: the most left wins
        self.assertIn("every account is low", rec["why"])

    def test_unknown_and_order(self):
        s = {"claude:pro": snap("claude:pro", [], error="no login"), "claude:enterprise": snap("claude:enterprise", [win("session", "5-hour", 0)]),
             "codex": snap("codex", [win("weekly", "weekly", 0)])}
        rec = usage.recommend(s, TWO, now=NOW)
        self.assertEqual(rec["use"], "claude:pro")
        self.assertIn("unknown", rec["why"])
        cfg = json.loads(json.dumps(TWO))
        cfg["usage"]["order"] = ["claude:pro", "claude:enterprise"]   # codex never auto-picked
        s["claude:pro"] = snap("claude:pro", [win("session", "5-hour", 0)])
        s["codex"] = snap("codex", [win("weekly", "weekly", 100)])
        self.assertEqual(usage.recommend(s, cfg, now=NOW)["use"], "claude:pro")
        self.assertIn("codex", usage.targets(usage.settings(cfg)))   # still read and shown
        self.assertEqual(usage.recommend(s, cfg, only_cli="codex", now=NOW)["use"], None)

    def test_a_login_not_set_up_is_never_picked(self):
        cfg = json.loads(json.dumps(TWO))
        cfg["usage"]["claude_accounts"]["pro"]["config_dir"] = "/nonexistent/claude-pro"
        s = self.snaps([], [win("session", "5-hour", 0)], cx=[win("weekly", "weekly", 0)])
        rec = usage.recommend(s, cfg, now=NOW)
        self.assertEqual(rec["use"], "claude:enterprise")
        self.assertEqual({a["target"]: a["status"] for a in rec["avoid"]}["claude:pro"], "unavailable")

    def test_parse_target(self):
        s = usage.settings(TWO)
        self.assertEqual(usage.parse_target("pro", s), ("claude", "pro"))
        self.assertEqual(usage.parse_target("claude:enterprise", s), ("claude", "enterprise"))
        self.assertEqual(usage.parse_target("codex", s), ("codex", None))
        with self.assertRaises(ValueError):
            usage.parse_target("max", s)
        self.assertEqual(accounts.default_account(s), "enterprise")
        self.assertEqual(usage.settings({})["order"], ["claude:default", "codex"])
        self.assertEqual(usage.claude_target("pro"), "claude:pro")
        self.assertEqual([usage.account_of(t) for t in ("claude:pro", "codex")], ["pro", None])


class Caching(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp()) / "usage-cache.json"
        self.calls = []

    def reader(self, fail=()):
        def read(t, s):
            self.calls.append(t)
            if t in fail:
                raise usage.UsageError("boom " + t)
            return {"windows": [win("weekly", "weekly", 50)], "plan": "pro", "source": "server", "observed_at": time.time()}
        return read

    def test_cache_reuse_refresh_and_stale_fallback(self):
        out = usage.snapshots(TWO, reader=self.reader(), cache_file=self.cache)
        self.assertEqual(sorted(self.calls), ["claude:enterprise", "claude:pro", "codex"])
        self.assertEqual(oct(stat.S_IMODE(os.stat(self.cache).st_mode)), "0o600")
        self.assertEqual(out["claude:pro"]["account"], "pro")
        usage.snapshots(TWO, reader=self.reader(), cache_file=self.cache)
        self.assertEqual(len(self.calls), 3)              # all fresh: nothing read again
        out = usage.snapshots(TWO, refresh=True, reader=self.reader(fail=("codex",)), cache_file=self.cache)
        self.assertEqual(len(self.calls), 6)
        self.assertTrue(out["codex"]["stale"])
        self.assertIn("boom", out["codex"]["error"])
        self.assertEqual(out["codex"]["windows"][0]["remaining_pct"], 50)
        a = usage.assess(out["codex"], usage.settings(TWO))
        self.assertTrue(any("min old" in w for w in a["why"]))
        self.cache.unlink()
        out = usage.snapshots(TWO, reader=self.reader(fail=("codex",)), cache_file=self.cache)
        self.assertEqual(out["codex"]["windows"], [])
        self.assertEqual(usage.assess(out["codex"], usage.settings(TWO))["status"], "unknown")

    @unittest.skipIf(os.geteuid() == 0, "root writes into a folder whatever its mode")
    def test_a_cache_that_cannot_be_written_raises(self):
        self.cache.parent.chmod(0o500)
        self.addCleanup(self.cache.parent.chmod, 0o700)
        with self.assertRaises(PermissionError):
            usage.snapshots(TWO, reader=self.reader(), cache_file=self.cache)

    def test_a_cache_that_is_not_json_raises_with_its_path(self):
        self.cache.write_text('{"codex": ')
        with self.assertRaisesRegex(ValueError, "%s is not JSON" % self.cache):
            usage.snapshots(TWO, reader=self.reader(), cache_file=self.cache)
        self.assertEqual(self.calls, [])

    def test_times_and_spend_in_words(self):
        self.assertEqual(usage.fmt_reset(NOW + 3600, NOW), time.strftime("%H:%M", time.localtime(NOW + 3600)))
        self.assertEqual(usage.fmt_reset(NOW - 3600, NOW), time.strftime("%H:%M", time.localtime(NOW - 3600)))
        self.assertEqual(usage.fmt_reset(NOW - 30 * 3600, NOW), time.strftime("%a %H:%M", time.localtime(NOW - 30 * 3600)))
        self.assertEqual(usage.fmt_reset(None, NOW), "unknown")
        self.assertEqual(usage.extra_spend({"used": 12.5, "monthly_limit": 500, "currency": "USD"}),
                         "$12.50 used of a $500.00 monthly cap")
        self.assertEqual(usage.extra_spend({"used": None, "monthly_limit": None, "currency": "EUR"}),
                         "? used, no monthly cap")

    def test_text(self):
        out = usage.snapshots(TWO, reader=self.reader(), cache_file=self.cache)
        out["codex"] = snap("codex", [], error="codex binary not found")
        out["claude:pro"]["extra_usage"] = {"enabled": True, "used": 211.41, "monthly_limit": None, "currency": "USD"}
        rec = usage.recommend(out, TWO, now=NOW)
        text = usage.render_text(out, rec, TWO, now=NOW)
        self.assertIn("50% left", text)
        self.assertIn("extra usage ON: $211.41 used, no monthly cap", text)
        self.assertIn("usage unknown: codex binary not found", text)
        self.assertTrue(text.splitlines()[-1].startswith("Recommendation: Use claude:pro"))


class Accounts(unittest.TestCase):
    def setUp(self):
        h = Homes(self, logins=("enterprise", "pro"))
        self.base, self.default, self.pro = h.base, h.default, h.homes["pro"]
        for login, sid in (("enterprise", "11111111-1111-1111-1111-111111111111"),
                           ("pro", "22222222-2222-2222-2222-222222222222")):
            home = h.homes[login]
            h.claude(sid, home.name, login=login, cwd="/work")
            (home / "projects" / "-work").mkdir(parents=True)
            (home / "projects" / "-work" / (sid + ".jsonl")).write_text(json.dumps({"cwd": str(self.base)}) + "\n")

    def test_registry_sees_both_logins(self):
        homes = registry.claude_homes()
        self.assertEqual([(a, c) for a, _, c in homes], [("enterprise", None), ("pro", str(self.pro.resolve()))])
        by = {s["name"]: s for s in registry.claude_sessions()}
        self.assertEqual((by[".claude"]["account"], by[".claude"]["config_dir"]), ("enterprise", None))
        self.assertEqual((by[".claude-pro"]["account"], by[".claude-pro"]["config_dir"]), ("pro", str(self.pro.resolve())))
        gone = registry.claude_transcript_session("22222222-2222-2222-2222-222222222222")
        self.assertEqual(gone["config_dir"], str(self.pro.resolve()))
        self.assertIsNone(registry.claude_transcript_session("11111111-1111-1111-1111-111111111111")["config_dir"])

    def test_live_on_one_login(self):
        self.assertEqual([s["name"] for s in registry.live_on(None)], [".claude"])
        self.assertEqual([s["name"] for s in registry.live_on(str(self.pro))], [".claude-pro"])
        self.assertEqual(registry.live_on(str(self.base / ".claude-other")), [])

    def test_a_resumed_session_is_found_on_the_login_it_was_resumed_under(self):
        # The memory cap waits for the resumed session's pid: the registry entry with its id on that login only.
        pro_sid, waits = "22222222-2222-2222-2222-222222222222", []
        conf = json.loads(config.CONFIG_PATH.read_text())
        config.CONFIG_PATH.write_text(json.dumps(dict(conf, failover={"pinned": ["a"]})))   # resume where it ran
        # claude itself and the systemd scope are the boundaries: the resume command and the cap call are stubbed.
        with mock.patch.object(send, "claude_bin", return_value="/bin/claude"), \
                mock.patch.object(send, "agent_caps", return_value=({"MemoryMax": 1}, None)), \
                mock.patch.object(send.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")), \
                mock.patch.object(send.memcap, "cap_bg_session",
                                  side_effect=lambda *a, **k: waits.append(k["session_pid"]) or "capped"):
            send.resume_claude({"id": pro_sid, "name": "a", "cwd": "", "config_dir": str(self.pro)}, "hi", True, lambda: None)
            newer = self.default / "projects" / "-work" / (pro_sid + ".jsonl")   # now resumed on the default login
            newer.write_text(json.dumps({"cwd": str(self.base)}) + "\n")
            os.utime(newer, (time.time() + 60, time.time() + 60))
            send.resume_claude({"id": pro_sid, "name": "a", "cwd": "", "config_dir": str(self.pro)}, "hi", True, lambda: None)
        self.assertEqual(waits[0](), os.getpid())     # live on pro, the login it was resumed under
        self.assertIsNone(waits[1]())                 # resumed on the default login, where no session has that id

    def test_inside_a_pro_agent_the_default_login_is_still_seen(self):
        with mock.patch.object(registry, "CLAUDE_HOME", self.pro):
            self.assertEqual(sorted(a for a, _, _ in registry.claude_homes()), ["enterprise", "pro"])

    def test_resume_uses_the_sessions_own_login(self):
        seen = []
        # claude (its binary and its run) and the memory cap are the boundary
        with mock.patch.object(send, "claude_bin", return_value="/bin/claude"), \
                mock.patch.object(send, "agent_caps", return_value=(None, "uncapped: test")), \
                mock.patch.object(send.subprocess, "run", side_effect=lambda cmd, **kw: seen.append(kw["env"]) or
                                  subprocess.CompletedProcess(cmd, 0, "ok", "")), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/somewhere/else"}):
            send.resume_claude({"id": "x", "name": "a", "cwd": "", "config_dir": str(self.pro)}, "hi", True, lambda: None)
            send.resume_claude({"id": "y", "name": "b", "cwd": "", "config_dir": None}, "hi", True, lambda: None)
        self.assertEqual(seen[0]["CLAUDE_CONFIG_DIR"], str(self.pro.resolve()))   # the Mac's temp dir is a symlink
        self.assertNotIn("CLAUDE_CONFIG_DIR", seen[1])

    def test_a_resume_that_cannot_choose_a_login_fails(self):
        """git cannot say who owns the session's repo, so its owner rules cannot pick a login. Nothing is started
        and agent-send fails with git's error, the path the mailroom holds the mail and alerts on."""
        repo = self.base / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        with open(repo / ".git" / "config", "a") as fh:
            fh.write("[broken\n")
        sid = "33333333-3333-3333-3333-333333333333"   # exited: a transcript and no registry entry
        (self.pro / "projects" / "-work" / (sid + ".jsonl")).write_text(json.dumps({"cwd": str(repo)}) + "\n")
        bins = fakes.install(self)   # the claude CLI is the boundary
        sender = load_script("agent-send")
        # the memory cap (systemd) is the boundary too
        with mock.patch.object(send, "agent_caps", return_value=(None, "uncapped: test")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = sender.main(["--exact-id", "--raw", "--cli", "claude", "--offline", "resume", sid, "hi"])
        self.assertEqual((rc, bins.calls("claude")), (1, []))
        self.assertRegex(err.getvalue(), r"agent-send: delivery to claude .* failed: git ls-remote --get-url origin "
                                         r"in .*repo failed \(exit 128\): fatal: bad config line")
        # a dry run reports the same failure, not a traceback
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = sender.main(["--exact-id", "--raw", "--cli", "claude", "--offline", "resume", "--dry-run", sid, "hi"])
        self.assertEqual(rc, 1)
        self.assertRegex(err.getvalue(), r"agent-send: delivery to claude .* failed: git ls-remote --get-url origin ")

    def test_link_memory(self):
        repo = self.base / "repo"
        mem = self.default / "projects" / accounts.project_slug(repo.resolve()) / "memory"
        mem.mkdir(parents=True)
        link = accounts.link_memory(repo, self.pro, default_home=self.default)
        self.assertTrue(Path(link).is_symlink())
        self.assertEqual(os.readlink(link), str(mem))
        self.assertIsNone(accounts.link_memory(repo, self.pro, default_home=self.default))   # already there
        self.assertIsNone(accounts.link_memory(self.base / "other", self.pro, default_home=self.default))
        blocked = self.base / "blocked"
        blocked.write_text("")                    # a file where the login's projects folder would be made
        with self.assertRaises(OSError):
            accounts.link_memory(repo, blocked, default_home=self.default)
        self.assertEqual(accounts.project_slug("/srv/x/py/a.b_c d"), "-srv-x-py-a-b-c-d")

    def test_repo_owner_is_none_only_without_an_origin_and_a_git_failure_raises(self):
        """The owner rules (exclude_owners, only_owners) need the owner: git failing must not read as "no owner"."""
        repo = self.base / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.assertIsNone(accounts.repo_owner(repo))                    # no origin remote
        self.assertIsNone(accounts.repo_owner(self.default))         # no repo
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Example-Org/x.git"],
                       check=True)
        self.assertEqual(accounts.repo_owner(repo), "Example-Org")
        with open(repo / ".git" / "config", "a") as fh:
            fh.write("[broken\n")
        with self.assertRaisesRegex(commands.CommandError, r"git ls-remote --get-url origin in .* failed \(exit 128\): "
                                                           r"fatal: bad config line"):
            accounts.repo_owner(repo)

    def test_init_account_shares_customisations_not_credentials(self):
        (self.default / "CLAUDE.md").write_text("# mine")
        (self.default / "settings.json").write_text("{}")
        (self.default / "skills").mkdir()
        (self.default / ".credentials.json").write_text("secret")
        new = self.base / ".claude-two"
        src = self.base / ".claude.json"   # the default login's; only copying MCP servers reads it
        self.assertTrue(accounts.init_account(new, self.default, src, dry_run=True))
        self.assertFalse(new.exists())
        done = accounts.init_account(new, self.default, src)
        self.assertEqual(oct(stat.S_IMODE(new.stat().st_mode)), "0o700")
        self.assertTrue((new / "settings.json").is_symlink())
        self.assertTrue((new / "skills").is_symlink())
        self.assertFalse((new / ".credentials.json").exists())
        self.assertFalse((new / "projects").exists())
        self.assertEqual(accounts.init_account(new, self.default, src), [])   # idempotent
        self.assertTrue(done)
        with self.assertRaises(ValueError):
            accounts.init_account(self.default, self.default, src)
        src.write_text(json.dumps({"mcpServers": {"gmail": {"command": "g"}, "work": {"command": "w"}}, "oauthAccount": {"x": 1}}))
        with self.assertRaisesRegex(ValueError, "log in"):
            accounts.init_account(new, default_home=self.default, copy_mcp=["gmail"], default_json=src)
        (new / ".claude.json").write_text(json.dumps({"oauthAccount": {"y": 2}}))
        accounts.init_account(new, default_home=self.default, copy_mcp=["gmail"], default_json=src)
        d = json.loads((new / ".claude.json").read_text())
        self.assertEqual(d, {"oauthAccount": {"y": 2}, "mcpServers": {"gmail": {"command": "g"}}})
        with self.assertRaises(ValueError):
            accounts.init_account(new, default_home=self.default, copy_mcp=["nope"], default_json=src)


class StaleEntry(unittest.TestCase):
    """An agent moved from the work login back to personal. The move SIGKILLs claude, so its work entry stays (pid
    gone, socket file still there) beside the live personal one, and every agent-send --exact-id to it would fail
    with exit 3. The fixture is such a dead entry."""

    def setUp(self):
        self.h = Homes(self, logins=("personal", "work"))
        self.base, self.personal, self.work = self.h.base, self.h.homes["personal"], self.h.homes["work"]
        dead = json.loads((ROOT / "tests" / "fixtures" / "stale-registry" / "4242424.json").read_text())
        self.sid, self.name = dead["sessionId"], dead["name"]
        self.cwd = str(self.base)   # a folder that exists, as the agent's did (a resume refuses a missing one)
        self.fields = {k: v for k, v in dead.items() if k not in ("pid", "sessionId", "name", "cwd", "messagingSocketPath")}
        old_sock = self.base / "4242424.sock"   # the killed claude's socket file stays behind
        old_sock.write_text("")
        self.dead = self.entry("work", live=False, messagingSocketPath=str(old_sock))
        self.pid = json.loads(self.dead.read_text())["pid"]
        self.key = self.work / "sessions" / ("%d.%s.key" % (self.pid, "a7" * 32))
        self.key.write_text("k")
        # dated before the dead one, so only liveness can pick it
        self.live = self.entry("personal", procStart=None, statusUpdatedAt=dead["statusUpdatedAt"] - 1)

    def entry(self, login, live=True, **fields):
        """The fixture's registry entry on login, with fields changed (None: left out)."""
        fields = {k: v for k, v in dict(self.fields, **fields).items() if v is not None}
        return self.h.claude(self.sid, self.name, login=login, live=live, cwd=self.cwd, **fields)

    def entries(self):
        return [s for a, h, c in registry.claude_homes() for s in registry._claude_sessions_in(h / "sessions", a, c)
                if s["id"] == self.sid]

    def send(self, *args):
        sender = load_script("agent-send")
        out, err = io.StringIO(), io.StringIO()
        # the session's inbox socket and starting claude are the boundary
        with mock.patch.object(sender, "send_claude_socket", return_value="") as sock, \
                mock.patch.object(sender, "resume_claude", return_value=(True, "resumed")) as resume, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sender.main(["--exact-id", "--raw", "--cli", "claude"] + list(args) + [self.sid, "hello"])
        return rc, out.getvalue() + err.getvalue(), sock, resume

    def test_exact_id_reaches_the_live_entry_beside_a_dead_one(self):
        self.assertEqual(sorted((s["account"], s["live"]) for s in self.entries()),
                         [("personal", True), ("work", False)])
        self.assertEqual([(s["account"], s["live"]) for s in registry.claude_sessions() if s["id"] == self.sid],
                         [("personal", True)])
        rc, out, sock, _ = self.send("--offline", "fail")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sock.call_args.args[0]["socket"], str(self.h.socket))
        self.assertEqual(registry.by_id(self.entries(), self.sid)[0]["account"], "personal")

    def test_dead_entries_alone_are_one_offline_session(self):
        self.live.unlink()
        self.entry("personal", live=False, messagingSocketPath=json.loads(self.dead.read_text())["messagingSocketPath"],
                   updatedAt=1, statusUpdatedAt=1)
        self.assertEqual(len(self.entries()), 2)
        rc, out, sock, resume = self.send("--offline", "fail")
        self.assertEqual(rc, 2, out)
        self.assertIn("is not live", out)
        rc, out, sock, resume = self.send("--offline", "resume")
        self.assertEqual(rc, 0, out)
        self.assertEqual(resume.call_args.args[0]["account"], "work")   # the latest entry stands for the session
        sock.assert_not_called()

    def test_two_live_entries_are_ambiguous(self):
        self.entry("work", procStart=None, statusUpdatedAt=json.loads(self.live.read_text())["statusUpdatedAt"])  # live on work too
        rc, out, sock, resume = self.send("--offline", "resume")
        self.assertEqual(rc, 3, out)
        self.assertEqual(out.count("\n  claude "), 2)
        sock.assert_not_called()
        resume.assert_not_called()

    def resume(self, personal_left=90, saved_on_personal=False):
        """The agent offline, its newest conversation on work (where the move left it), an older copy on personal;
        a reply arrives and the mailroom resumes it."""
        self.live.unlink()
        d = json.loads(self.dead.read_text())
        self.dead.write_text(json.dumps(dict(d, cwd=str(self.base))))
        copies = {}
        for home, text, age in ((self.personal, "before the move", 7200), (self.work, "after the move", 0)):
            p = home / "projects" / "-work" / (self.sid + ".jsonl")
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"type": "user", "message": {"content": text}}) + "\n" + json.dumps(
                {"type": "assistant", "message": {"model": "claude-fable-5-1", "content": "ok"}}) + "\n")
            os.utime(p, (time.time() - age, time.time() - age))
            copies[home] = p
        if saved_on_personal:   # personal's daemon has run it before: it keeps saved options there
            (self.personal / "jobs" / self.sid[:8]).mkdir(parents=True)
            (self.personal / "jobs" / self.sid[:8] / "state.json").write_text(json.dumps({"respawnFlags": ["--model", "fable"]}))
        snaps = {"claude:personal": snap("claude:personal", [win("session", "5-hour", personal_left)]),
                 "claude:work": snap("claude:work", [win("session", "5-hour", 90)])}
        sender, runs, trusted = load_script("agent-send"), [], []
        real_run = subprocess.run

        def run(cmd, **kw):
            if cmd[0] != "/bin/claude":
                return real_run(cmd, **kw)
            runs.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, "backgrounded \u00b7 %s" % self.sid[:8], "")
        out = io.StringIO()
        # the usage probes (Anthropic, OpenAI), the folder trust in the user's files, and claude are the boundary
        with mock.patch.object(usage, "snapshots", return_value=snaps), \
                mock.patch.object(send, "spawn_trust", side_effect=lambda c, d: trusted.append((c, d))), \
                mock.patch.object(send, "claude_bin", return_value="/bin/claude"), \
                mock.patch.object(send, "agent_caps", return_value=(None, "uncapped: test")), \
                mock.patch.object(subprocess, "run", side_effect=run), contextlib.redirect_stdout(out):
            rc = sender.main(["--exact-id", "--raw", "--cli", "claude", "--offline", "resume", self.sid, "hello"])
        return rc, out.getvalue(), runs, trusted, copies

    def test_a_resume_takes_the_newest_conversation_to_the_best_login(self):
        rc, out, runs, trusted, copies = self.resume()
        self.assertEqual(rc, 0, out)
        (cmd, kw), = runs
        self.assertEqual(cmd, ["/bin/claude", "--resume", self.sid, "--bg", "--name", self.name,
                               "--dangerously-skip-permissions", "hello"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", kw["env"])                 # personal, the default login
        self.assertEqual(kw["cwd"], str(self.base))
        self.assertIn("after the move", copies[self.personal].read_text())
        self.assertEqual(trusted, [(str(self.base), None)])
        self.assertIn("on login personal", out)
        self.assertIn("conversation copied from work", out)

    def test_a_login_whose_daemon_ran_it_before_gets_no_flags(self):
        rc, out, runs, *_ = self.resume(saved_on_personal=True)   # with flags it would start a copy under a new id
        self.assertEqual(rc, 0, out)
        self.assertEqual(runs[0][0], ["/bin/claude", "--resume", self.sid, "--bg", "hello"])

    def test_with_personal_used_up_it_resumes_on_work_where_the_conversation_is(self):
        rc, out, runs, trusted, copies = self.resume(personal_left=0)
        self.assertEqual(rc, 0, out)
        self.assertEqual(runs[0][1]["env"]["CLAUDE_CONFIG_DIR"], str(self.work))
        self.assertIn("before the move", copies[self.personal].read_text())   # nothing copied
        self.assertEqual(trusted, [])

    def test_the_dead_entry_is_removed_and_the_live_one_never(self):
        dead = next(s for s in self.entries() if not s["live"])
        live = next(s for s in self.entries() if s["live"])
        self.assertEqual(registry.remove_dead_entry(dict(dead, id="another-session")), [])
        self.assertEqual(registry.remove_dead_entry(dict(dead, pid=dead_pid())), [])   # no entry for that pid
        self.assertEqual(registry.remove_dead_entry(live), [])                         # pid alive
        self.assertEqual(registry.remove_dead_entry(dead), [str(self.dead), str(self.key)])
        self.assertTrue(self.live.exists())
        self.assertEqual([s["account"] for s in self.entries()], ["personal"])
        self.assertEqual(registry.remove_dead_entry(dead), [])


class Spawn(unittest.TestCase):
    def setUp(self):
        self.sp = load_script("agent-spawn")
        self.pro = Path(tempfile.mkdtemp()) / ".claude-pro"
        self.pro.mkdir()
        (self.pro / ".credentials.json").write_text("{}")
        self.cfg = json.loads(json.dumps(TWO))
        self.cfg["usage"]["claude_accounts"]["pro"]["config_dir"] = str(self.pro)

    def args(self, **kw):
        d = {"cli": None, "account": None, "model": None}
        d.update(kw)
        return argparse.Namespace(**d)

    def reader(self, pro_left=90):
        """Stands in for usage.snapshots, whose probes ask Anthropic and OpenAI: pro with pro_left percent left."""
        return lambda cfg: {"claude:pro": snap("claude:pro", [win("session", "5-hour", pro_left)]),
                            "claude:enterprise": snap("claude:enterprise", [win("session", "5-hour", 80)]),
                            "codex": snap("codex", [win("weekly", "weekly", 90)])}

    def test_explicit_flags_unchanged(self):
        boom = mock.Mock(side_effect=AssertionError("usage must not be read"))
        self.cfg["spawn_route"] = "auto"
        r = self.sp.choose_route(self.args(cli="claude"), self.cfg, None, read=boom)
        self.assertEqual((r["cli"], r["account"], r["config_dir"], r["routed"]), ("claude", "enterprise", None, False))
        r = self.sp.choose_route(self.args(cli="codex"), self.cfg, None, read=boom)
        self.assertEqual((r["cli"], r["account"]), ("codex", None))
        r = self.sp.choose_route(self.args(account="pro"), self.cfg, None, read=boom)
        self.assertEqual((r["cli"], r["account"], r["config_dir"]), ("claude", "pro", str(self.pro.resolve())))
        r = self.sp.choose_route(self.args(model="claude-sonnet-5-5"), self.cfg, None, read=boom)
        self.assertEqual((r["cli"], r["account"]), ("claude", "enterprise"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.sp.choose_route(self.args(cli="codex", account="pro"), self.cfg, None, read=boom)

    def test_routing_is_opt_in(self):
        r = self.sp.choose_route(self.args(), self.cfg, None, read=mock.Mock(side_effect=AssertionError))
        self.assertEqual((r["cli"], r["account"], r["routed"]), ("claude", "enterprise", False))
        self.cfg["spawn_route"] = "auto"
        r = self.sp.choose_route(self.args(), self.cfg, None, read=self.reader())
        self.assertEqual((r["cli"], r["account"], r["routed"]), ("claude", "pro", True))
        r = self.sp.choose_route(self.args(), self.cfg, None, read=self.reader(pro_left=5))
        self.assertEqual(r["account"], "enterprise")
        self.assertIn("Avoid claude:pro", r["why"])
        r = self.sp.choose_route(self.args(), self.cfg, "Example-Org", read=self.reader())
        self.assertEqual(r["account"], "enterprise")
        r = self.sp.choose_route(self.args(account="auto", cli="codex"), config.load(), None, read=self.reader())
        self.assertEqual(r["cli"], "codex")

    def test_routing_picks_the_model_too_and_the_plan_runs_it(self):
        self.cfg.update(spawn_route="auto", spawn_claude_model="claude-fable-5-2", spawn_claude_effort="xhigh",
                        claude_fallback_models=["claude-opus-5-5"])

        def no_fable(cfg):   # stands in for usage.snapshots: both logins have room, but no Fable left
            fable = win("model_weekly", "weekly Fable", 0, "fable")
            return {"claude:pro": snap("claude:pro", [win("session", "5-hour", 90), fable]),
                    "claude:enterprise": snap("claude:enterprise", [win("session", "5-hour", 80), fable]),
                    "codex": snap("codex", [win("weekly", "weekly", 90)])}
        r = self.sp.choose_route(self.args(), self.cfg, None, read=no_fable)
        self.assertEqual((r["account"], r["model"]), ("pro", "claude-opus-5-5"))
        a = argparse.Namespace(cli=None, account=None, model=None, effort=None, new_repo="x", org="example-user")
        with mock.patch.object(self.sp.usage, "snapshots", no_fable):
            plan = self.sp.plan_agent(a, self.cfg, {})
        self.assertEqual((plan["account"], plan["model"], plan["effort"]), ("pro", "claude-opus-5-5", "xhigh"))
        r = self.sp.choose_route(self.args(account="pro"), self.cfg, None, read=no_fable)
        self.assertNotIn("model", r)   # an explicit login runs spawn_claude_model; failover moves it on a limit

    def test_auto_routing_stops_when_usage_or_no_login_allows_it_and_unready_account_refused(self):
        """No silent fall back to the default login, which may be the one an owner rule excludes."""
        self.cfg["spawn_route"] = "auto"
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            self.sp.choose_route(self.args(), self.cfg, None, read=mock.Mock(side_effect=OSError("down")))
        self.assertIn("reading usage failed (OSError: down)", err.getvalue())
        self.cfg["usage"]["claude_accounts"]["enterprise"]["exclude_owners"] = ["Example-Org"]
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            self.sp.choose_route(self.args(cli="claude", account="auto"), self.cfg, "Example-Org",
                                 read=self.reader())
        self.assertIn("No usable account: claude:pro (repos owned by Example-Org are excluded from this account); "
                      "claude:enterprise (repos owned by Example-Org are excluded", err.getvalue())
        (self.pro / ".credentials.json").unlink()
        with mock.patch.object(accounts.sys, "platform", "linux"), contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit):
            self.sp.choose_route(self.args(account="pro"), self.cfg, None)
        self.assertIn("no login", err.getvalue())

    def test_launch_sets_the_login_for_the_pane_and_its_session(self):
        calls = []
        # tmux is the boundary: record what would be run in the pane
        with mock.patch.object(launch.commands, "tmux", side_effect=lambda *a, **k: calls.append(a)), \
                mock.patch.object(launch.time, "sleep"):
            launch.start_claude("%5", "/bin/claude", "n", str(self.pro), "m", "high", "/b.md")
            launch.start_claude("%6", "/bin/claude", "n2", None, "m", "high", "/b.md")
        # another login: the pane's session gets its CLAUDE_CONFIG_DIR, and the pane's shell exports it
        self.assertEqual(calls[0], ("set-environment", "-t", "%5", "CLAUDE_CONFIG_DIR", str(self.pro)))
        self.assertEqual(calls[1][-1], "stty sane; export CLAUDE_CONFIG_DIR=%s; /bin/claude --name n "
                                       "--dangerously-skip-permissions --model m --effort high \"$(cat /b.md)\""
                         % self.pro)
        # the default login: removed from the session's environment and from the pane's shell
        self.assertEqual(calls[3], ("set-environment", "-t", "%6", "-r", "CLAUDE_CONFIG_DIR"))
        self.assertTrue(calls[4][-1].startswith("stty sane; unset CLAUDE_CONFIG_DIR; /bin/claude "))

    def test_codex_launch_skips_the_update_menu(self):
        calls = []
        # tmux and Codex's lock files are the boundary: no thread appears, and the start says so
        with mock.patch.object(launch.registry, "codex_live_ids", return_value=set()), \
                mock.patch.object(launch.commands, "tmux", side_effect=lambda *a, **k: calls.append(a)), \
                mock.patch.object(launch, "CODEX_THREAD_SECONDS", 0), mock.patch.object(launch.time, "sleep"):
            started = launch.start_codex("=n:", "/bin/codex", "/work", "gpt-6.1-sol", "xhigh", "brief", "n")
        typed = next(c[-1] for c in calls if c[:2] == ("send-keys", "-t") and "-l" in c)
        self.assertTrue(typed.startswith("stty sane; unset CLAUDE_CONFIG_DIR; /bin/codex -s danger-full-access -a never "
                                         "-C /work -c check_for_update_on_startup=false "), typed)
        self.assertIn("did not appear", started["error"])

    def test_trust_goes_to_the_accounts_own_claude_json(self):
        home_json = Path(tempfile.mkdtemp()) / ".claude.json"
        home_json.write_text("{}")
        acct_json = trust.claude_json_for(str(self.pro))
        acct_json.write_text(json.dumps({"oauthAccount": {}}))
        d = tempfile.mkdtemp()
        with mock.patch.object(registry, "CLAUDE_JSON", home_json), mock.patch.object(trust, "trust_codex", return_value=False):
            trust.ensure_trust(d, acct_json)
            self.assertEqual(json.loads(home_json.read_text()), {})
            self.assertTrue(json.loads(acct_json.read_text())["projects"][str(Path(d).resolve())]["hasTrustDialogAccepted"])
            acct_json.unlink()
            trust.ensure_trust(d, acct_json)   # a login that is not set up gets no half-made .claude.json
            self.assertFalse(acct_json.exists())

    def test_claude_trust_says_whether_it_wrote_and_raises_when_it_cannot(self):
        """True when it writes the trust, False when the folder is trusted already, and an error when it cannot read
        or write the file."""
        d = Path(tempfile.mkdtemp())
        cj = d / ".claude.json"
        self.assertTrue(trust.trust_claude("/w", cj))
        self.assertFalse(trust.trust_claude("/w", cj))   # already trusted
        self.assertEqual(oct(cj.stat().st_mode & 0o777), "0o600")
        cj.write_text("{not json")
        with mock.patch.object(trust, "trust_codex", return_value=False):
            with self.assertRaises(ValueError):
                trust.ensure_trust(str(d), cj)
        (d / "a-file").write_text("")
        with self.assertRaises(OSError):
            trust.trust_claude("/w", d / "a-file" / ".claude.json")   # its folder is a file: the write fails

    def test_codex_trust_escapes_the_path_and_raises_when_it_cannot_write(self):
        conf = Path(tempfile.mkdtemp()) / "config.toml"
        conf.write_text('model = "gpt-6.1-sol"')
        odd = '/w/a "quoted" \\ name'
        with mock.patch.object(trust, "CODEX_CONFIG", conf):
            self.assertTrue(trust.trust_codex(odd))
            self.assertFalse(trust.trust_codex(odd))   # already trusted
            self.assertTrue(trust.trust_codex("/plain"))
        self.assertEqual(conf.read_text(), 'model = "gpt-6.1-sol"\n'
                                           '\n[projects."/w/a \\"quoted\\" \\\\ name"]\ntrust_level = "trusted"\n'
                                           '\n[projects."/plain"]\ntrust_level = "trusted"\n')
        with mock.patch.object(trust, "CODEX_CONFIG", conf.parent / "config.toml" / "config.toml"), \
                self.assertRaises(OSError):
            trust.trust_codex("/w")   # its folder is a file: the write fails


class Cli(unittest.TestCase):
    def test_pick_and_json(self):
        au = load_script("agent-usage")
        s = {"claude:default": snap("claude:default", [win("session", "5-hour", 0)]),
             "codex": snap("codex", [win("weekly", "weekly", 90)])}
        config_file(self, {})                                              # the defaults
        with mock.patch.object(au.usage, "snapshots", return_value=s):   # the usage probes ask Anthropic and OpenAI
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(au.main(["--pick"]), 0)
            self.assertEqual(out.getvalue().strip(), "codex")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                au.main(["--json"])
            d = json.loads(out.getvalue())
            self.assertEqual(d["recommendation"]["use"], "codex")
            self.assertEqual([a["target"] for a in d["accounts"]], ["claude:default", "codex"])


if __name__ == "__main__":
    unittest.main()
