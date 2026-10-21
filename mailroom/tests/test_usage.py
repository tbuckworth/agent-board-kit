import argparse
import contextlib
import importlib.machinery
import importlib.util
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

from agentmail import config, registry, usage  # noqa: E402

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


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def snap(target, windows, **kw):
    cli = "codex" if target == "codex" else "claude"
    d = {"target": target, "cli": cli, "account": None if cli == "codex" else target.split(":", 1)[1],
         "windows": windows, "source": "server", "observed_at": NOW}
    d.update(kw)
    return d


def win(kind, name, remaining, model=None, resets=NOW + 3600):
    return {"kind": kind, "name": name, "used_pct": 100 - remaining, "remaining_pct": remaining,
            "resets_at": resets, "model": model}


PERSONAL_DIR = tempfile.mkdtemp()   # a set-up second login: its config dir with a login in it
Path(PERSONAL_DIR, ".credentials.json").write_text("{}")
TWO = {"usage": {"claude_accounts": {"personal": {"config_dir": PERSONAL_DIR, "exclude_owners": ["Employer-Org"]},
                                     "work": {"config_dir": None}},
                 "order": ["claude:personal", "claude:work", "codex"],
                 "min_remaining": {"session": 15, "weekly": 10}},
       "spawn_claude_model": "claude-opus-5-5", "spawn_codex_model": "gpt-6.1-sol"}


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
        with self.assertRaisesRegex(usage.UsageError, "no usage answer"):
            usage.fetch_claude({}, claude="/bin/claude", run=self.fake_run("", calls))
        with self.assertRaisesRegex(usage.UsageError, "does not exist"):
            usage.fetch_claude({"config_dir": "/nonexistent/claude-x"}, claude="/bin/claude", run=self.fake_run("", calls))
        d = tempfile.mkdtemp()
        with mock.patch.object(usage.sys, "platform", "linux"), self.assertRaisesRegex(usage.UsageError, "no login"):
            usage.fetch_claude({"config_dir": d}, claude="/bin/claude", run=self.fake_run("", calls))
        self.assertEqual(len(calls), 2)   # the not-ready accounts never started claude
        self.assertEqual(usage.account_ready({"config_dir": d}, platform="darwin"), (True, ""))   # Keychain login

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
    def snaps(self, personal_w, work_w, cx=(win("weekly", "weekly", 90),), **kw):
        return {"claude:personal": snap("claude:personal", list(personal_w)), "claude:work": snap("claude:work", list(work_w), **kw),
                "codex": snap("codex", list(cx))}

    def test_pro_first_while_it_has_headroom(self):
        rec = usage.recommend(self.snaps([win("session", "5-hour", 60), win("weekly", "weekly", 70)],
                                         [win("session", "5-hour", 100)]), TWO, now=NOW)
        self.assertEqual((rec["use"], rec["cli"], rec["account"]), ("claude:personal", "claude", "personal"))
        self.assertEqual(rec["avoid"], [])

    def test_personal_under_threshold_moves_to_work(self):
        rec = usage.recommend(self.snaps([win("session", "5-hour", 12), win("weekly", "weekly", 70)],
                                         [win("session", "5-hour", 80), win("weekly", "weekly", 50)]), TWO, now=NOW)
        self.assertEqual(rec["use"], "claude:work")
        self.assertEqual(rec["avoid"][0]["target"], "claude:personal")
        self.assertIn("under 15%", rec["avoid"][0]["why"])

    def test_claude_used_up_goes_to_codex_and_says_extra_usage_bills(self):
        rec = usage.recommend(self.snaps([win("weekly", "weekly", 0)], [win("session", "5-hour", 0)],
                                         extra_usage={"enabled": True}), TWO, now=NOW)
        self.assertEqual(rec["use"], "codex")
        why = {a["target"]: a["why"] for a in rec["avoid"]}
        self.assertIn("used up", why["claude:personal"])
        self.assertIn("bills credits", why["claude:work"])
        self.assertIn("Use codex", rec["line"])

    def test_owner_exclusion_keeps_employer_repos_off_personal(self):
        rec = usage.recommend(self.snaps([win("session", "5-hour", 90)], [win("session", "5-hour", 80)]),
                              TWO, owner="employer-org", now=NOW)
        self.assertEqual(rec["use"], "claude:work")
        self.assertEqual(rec["avoid"][0]["status"], "excluded")
        self.assertEqual(usage.recommend(self.snaps([win("session", "5-hour", 90)], []), TWO, owner="example-user",
                                         now=NOW)["use"], "claude:personal")

    def test_model_windows_count_only_for_the_model_run(self):
        personal_w = [win("session", "5-hour", 90), win("model_weekly", "weekly Fable", 0, "fable"),
               win("model_weekly", "weekly Opus", 5, "opus")]
        a = usage.assess(snap("claude:personal", personal_w), usage.settings(TWO), "claude-opus-5-5", NOW)
        self.assertEqual(a["status"], "low")
        self.assertEqual(len(a["why"]), 1)
        a = usage.assess(snap("claude:personal", personal_w), usage.settings(TWO), "claude-sonnet-5-5", NOW)
        self.assertEqual(a["status"], "ok")

    def test_per_account_threshold_and_low_fallback(self):
        cfg = json.loads(json.dumps(TWO))
        cfg["usage"]["claude_accounts"]["personal"]["min_remaining"] = {"weekly": 50}
        s = self.snaps([win("weekly", "weekly", 40)], [win("weekly", "weekly", 8)], cx=[win("weekly", "weekly", 9)])
        rec = usage.recommend(s, cfg, now=NOW)
        self.assertEqual(rec["use"], "claude:personal")   # every one is low: the most left wins
        self.assertIn("every account is low", rec["why"])

    def test_unknown_and_order(self):
        s = {"claude:personal": snap("claude:personal", [], error="no login"), "claude:work": snap("claude:work", [win("session", "5-hour", 0)]),
             "codex": snap("codex", [win("weekly", "weekly", 0)])}
        rec = usage.recommend(s, TWO, now=NOW)
        self.assertEqual(rec["use"], "claude:personal")
        self.assertIn("unknown", rec["why"])
        cfg = json.loads(json.dumps(TWO))
        cfg["usage"]["order"] = ["claude:personal", "claude:work"]   # codex never auto-picked
        s["claude:personal"] = snap("claude:personal", [win("session", "5-hour", 0)])
        s["codex"] = snap("codex", [win("weekly", "weekly", 100)])
        self.assertEqual(usage.recommend(s, cfg, now=NOW)["use"], "claude:personal")
        self.assertIn("codex", usage.targets(usage.settings(cfg)))   # still read and shown
        self.assertEqual(usage.recommend(s, cfg, only_cli="codex", now=NOW)["use"], None)

    def test_a_login_not_set_up_is_never_picked(self):
        cfg = json.loads(json.dumps(TWO))
        cfg["usage"]["claude_accounts"]["personal"]["config_dir"] = "/nonexistent/claude-personal"
        s = self.snaps([], [win("session", "5-hour", 0)], cx=[win("weekly", "weekly", 0)])
        rec = usage.recommend(s, cfg, now=NOW)
        self.assertEqual(rec["use"], "claude:work")
        self.assertEqual({a["target"]: a["status"] for a in rec["avoid"]}["claude:personal"], "unavailable")

    def test_parse_target(self):
        s = usage.settings(TWO)
        self.assertEqual(usage.parse_target("personal", s), ("claude", "personal"))
        self.assertEqual(usage.parse_target("claude:work", s), ("claude", "work"))
        self.assertEqual(usage.parse_target("codex", s), ("codex", None))
        with self.assertRaises(ValueError):
            usage.parse_target("max", s)
        self.assertEqual(usage.default_account(s), "work")
        self.assertEqual(usage.settings({})["order"], ["claude:default", "codex"])


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
        self.assertEqual(sorted(self.calls), ["claude:personal", "claude:work", "codex"])
        self.assertEqual(oct(stat.S_IMODE(os.stat(self.cache).st_mode)), "0o600")
        self.assertEqual(out["claude:personal"]["account"], "personal")
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

    def test_only_and_text(self):
        out = usage.snapshots(TWO, only="claude", reader=self.reader(), cache_file=self.cache)
        self.assertEqual(sorted(out), ["claude:personal", "claude:work"])
        out["codex"] = snap("codex", [], error="codex binary not found")
        out["claude:personal"]["extra_usage"] = {"enabled": True, "used": 211.41, "monthly_limit": None, "currency": "USD"}
        rec = usage.recommend(out, TWO, now=NOW)
        text = usage.render_text(out, rec, TWO, now=NOW)
        self.assertIn("50% left", text)
        self.assertIn("extra usage ON: $211.41 used, no monthly cap", text)
        self.assertIn("usage unknown: codex binary not found", text)
        self.assertTrue(text.splitlines()[-1].startswith("Recommendation: Use claude:personal"))


class Accounts(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.default = self.base / "home" / ".claude"
        self.second = self.base / "home" / ".claude-personal"
        for h, sid in ((self.default, "11111111-1111-1111-1111-111111111111"), (self.second, "22222222-2222-2222-2222-222222222222")):
            (h / "sessions").mkdir(parents=True)
            (h / "projects" / "-work").mkdir(parents=True)
            (h / "sessions" / ("%d.json" % os.getpid())).write_text(json.dumps(
                {"pid": os.getpid(), "sessionId": sid, "name": h.name, "cwd": "/work"}))
            (h / "projects" / "-work" / (sid + ".jsonl")).write_text(json.dumps({"cwd": str(self.base)}) + "\n")
        conf = self.base / "config.json"
        conf.write_text(json.dumps({"usage": {"claude_accounts": {"personal": {"config_dir": str(self.second)},
                                                                   "work": {"config_dir": None}}}}))
        self.patches = [mock.patch.object(config, "CONFIG_PATH", conf),
                        mock.patch.object(registry, "DEFAULT_CLAUDE_HOME", self.default),
                        mock.patch.object(registry, "CLAUDE_HOME", self.default)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_registry_sees_both_logins(self):
        homes = registry.claude_homes()
        self.assertEqual([(a, c) for a, _, c in homes], [("work", None), ("personal", str(self.second.resolve()))])
        by = {s["name"]: s for s in registry.claude_sessions()}
        self.assertEqual((by[".claude"]["account"], by[".claude"]["config_dir"]), ("work", None))
        self.assertEqual((by[".claude-personal"]["account"], by[".claude-personal"]["config_dir"]), ("personal", str(self.second.resolve())))
        gone = registry.claude_transcript_session("22222222-2222-2222-2222-222222222222")
        self.assertEqual(gone["config_dir"], str(self.second.resolve()))
        self.assertIsNone(registry.claude_transcript_session("11111111-1111-1111-1111-111111111111")["config_dir"])

    def test_inside_a_pro_agent_the_default_login_is_still_seen(self):
        with mock.patch.object(registry, "CLAUDE_HOME", self.second):
            self.assertEqual(sorted(a for a, _, _ in registry.claude_homes()), ["personal", "work"])

    def test_resume_uses_the_sessions_own_login(self):
        send = load_script("agent-send")
        seen = []
        with mock.patch.object(send, "claude_bin", return_value="/bin/claude"), \
                mock.patch.object(send, "agent_caps", return_value=(None, "uncapped: test")), \
                mock.patch.object(send.subprocess, "run", side_effect=lambda cmd, **kw: seen.append(kw["env"]) or
                                  subprocess.CompletedProcess(cmd, 0, "ok", "")), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/somewhere/else"}):
            send.resume_claude({"id": "x", "name": "a", "cwd": "", "config_dir": str(self.second)}, "hi", True)
            send.resume_claude({"id": "y", "name": "b", "cwd": "", "config_dir": None}, "hi", True)
        self.assertEqual(seen[0]["CLAUDE_CONFIG_DIR"], str(self.second.resolve()))   # the Mac's temp dir is a symlink
        self.assertNotIn("CLAUDE_CONFIG_DIR", seen[1])

    def test_link_memory(self):
        repo = self.base / "repo"
        mem = self.default / "projects" / usage.project_slug(repo.resolve()) / "memory"
        mem.mkdir(parents=True)
        link = usage.link_memory(repo, self.second, default_home=self.default)
        self.assertTrue(Path(link).is_symlink())
        self.assertEqual(os.readlink(link), str(mem))
        self.assertIsNone(usage.link_memory(repo, self.second, default_home=self.default))   # already there
        self.assertIsNone(usage.link_memory(self.base / "other", self.second, default_home=self.default))
        self.assertEqual(usage.project_slug("/media/x/py/a.b_c d"), "-media-x-py-a-b-c-d")

    def test_init_account_shares_customisations_not_credentials(self):
        (self.default / "CLAUDE.md").write_text("# mine")
        (self.default / "settings.json").write_text("{}")
        (self.default / "skills").mkdir()
        (self.default / ".credentials.json").write_text("secret")
        new = self.base / "home" / ".claude-two"
        self.assertTrue(usage.init_account(new, default_home=self.default, dry_run=True))
        self.assertFalse(new.exists())
        done = usage.init_account(new, default_home=self.default)
        self.assertEqual(oct(stat.S_IMODE(new.stat().st_mode)), "0o700")
        self.assertTrue((new / "settings.json").is_symlink())
        self.assertTrue((new / "skills").is_symlink())
        self.assertFalse((new / ".credentials.json").exists())
        self.assertFalse((new / "projects").exists())
        self.assertEqual(usage.init_account(new, default_home=self.default), [])   # idempotent
        self.assertTrue(done)
        with self.assertRaises(ValueError):
            usage.init_account(self.default, default_home=self.default)
        src = self.base / "home" / ".claude.json"
        src.write_text(json.dumps({"mcpServers": {"gmail": {"command": "g"}, "work": {"command": "w"}}, "oauthAccount": {"x": 1}}))
        with self.assertRaisesRegex(ValueError, "log in"):
            usage.init_account(new, default_home=self.default, copy_mcp=["gmail"], default_json=src)
        (new / ".claude.json").write_text(json.dumps({"oauthAccount": {"y": 2}}))
        usage.init_account(new, default_home=self.default, copy_mcp=["gmail"], default_json=src)
        d = json.loads((new / ".claude.json").read_text())
        self.assertEqual(d, {"oauthAccount": {"y": 2}, "mcpServers": {"gmail": {"command": "g"}}})
        with self.assertRaises(ValueError):
            usage.init_account(new, default_home=self.default, copy_mcp=["nope"], default_json=src)


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class StaleEntry(unittest.TestCase):
    """An agent moved from the work login back to personal. The move SIGKILLs claude, so its work entry stays
    (pid gone, socket file still there) beside the live personal one; unhandled, every agent-send --exact-id to it
    would fail with exit 3. The fixture is such a dead entry."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.personal, self.work = self.base / ".claude", self.base / ".claude-work"
        dead = json.loads((ROOT / "tests" / "fixtures" / "stale-registry" / "4242424.json").read_text())
        self.sid = dead["sessionId"]
        self.pid = dead_pid()
        old_sock, live_sock = self.base / "4242424.sock", self.base / "4343434.sock"
        old_sock.write_text("")
        live_sock.write_text("")
        dead.update(pid=self.pid, messagingSocketPath=str(old_sock))
        # dated before the dead one, so only liveness can pick it
        live = dict(dead, pid=os.getpid(), messagingSocketPath=str(live_sock), statusUpdatedAt=dead["statusUpdatedAt"] - 1)
        live.pop("procStart")
        for h in (self.personal, self.work):
            (h / "sessions").mkdir(parents=True)
        self.dead = self.work / "sessions" / ("%d.json" % self.pid)
        self.dead.write_text(json.dumps(dead))
        self.key = self.work / "sessions" / ("%d.%s.key" % (self.pid, "a7" * 32))
        self.key.write_text("k")
        self.live = self.personal / "sessions" / ("%d.json" % os.getpid())
        self.live.write_text(json.dumps(live))
        conf = self.base / "config.json"
        conf.write_text(json.dumps({"usage": {"claude_accounts": {"personal": {"config_dir": None},
                                                                   "work": {"config_dir": str(self.work)}}}}))
        self.patches = [mock.patch.object(config, "CONFIG_PATH", conf),
                        mock.patch.object(registry, "DEFAULT_CLAUDE_HOME", self.personal),
                        mock.patch.object(registry, "CLAUDE_HOME", self.personal),
                        mock.patch.object(registry, "CODEX_HOME", self.base / ".codex")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def entries(self):
        return [s for a, h, c in registry.claude_homes() for s in registry._claude_sessions_in(h / "sessions", a, c)
                if s["id"] == self.sid]

    def send(self, *args):
        sender = load_script("agent-send")
        out, err = io.StringIO(), io.StringIO()
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
        self.assertEqual(sock.call_args.args[0]["socket"], str(self.base / "4343434.sock"))
        self.assertEqual(registry.by_id(self.entries(), self.sid)[0]["account"], "personal")

    def test_dead_entries_alone_are_one_offline_session(self):
        self.live.unlink()
        older = dict(json.loads(self.dead.read_text()), pid=dead_pid(), updatedAt=1, statusUpdatedAt=1)
        (self.personal / "sessions" / ("%d.json" % older["pid"])).write_text(json.dumps(older))
        self.assertEqual(len(self.entries()), 2)
        rc, out, sock, resume = self.send("--offline", "fail")
        self.assertEqual(rc, 2, out)
        self.assertIn("is not live", out)
        rc, out, sock, resume = self.send("--offline", "resume")
        self.assertEqual(rc, 0, out)
        self.assertEqual(resume.call_args.args[0]["account"], "work")   # the latest entry stands for the session
        sock.assert_not_called()

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
        with mock.patch.object(usage, "snapshots", return_value=snaps), \
                mock.patch.object(sender, "spawn_trust", side_effect=lambda c, d: trusted.append((c, d))), \
                mock.patch.object(sender, "claude_bin", return_value="/bin/claude"), \
                mock.patch.object(sender, "agent_caps", return_value=(None, "uncapped: test")), \
                mock.patch.object(subprocess, "run", side_effect=run), contextlib.redirect_stdout(out):
            rc = sender.main(["--exact-id", "--raw", "--cli", "claude", "--offline", "resume", self.sid, "hello"])
        return rc, out.getvalue(), runs, trusted, copies

    def test_a_resume_takes_the_newest_conversation_to_the_best_login(self):
        rc, out, runs, trusted, copies = self.resume()
        self.assertEqual(rc, 0, out)
        (cmd, kw), = runs
        self.assertEqual(cmd, ["/bin/claude", "--resume", self.sid, "--bg", "--name", "nightly report r7k2m9xq",
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
        self.second = Path(tempfile.mkdtemp()) / ".claude-personal"
        self.second.mkdir()
        (self.second / ".credentials.json").write_text("{}")
        self.cfg = json.loads(json.dumps(TWO))
        self.cfg["usage"]["claude_accounts"]["personal"]["config_dir"] = str(self.second)

    def args(self, **kw):
        d = {"cli": None, "account": None, "model": None}
        d.update(kw)
        return argparse.Namespace(**d)

    def reader(self, personal_left=90):
        return lambda cfg: {"claude:personal": snap("claude:personal", [win("session", "5-hour", personal_left)]),
                            "claude:work": snap("claude:work", [win("session", "5-hour", 80)]),
                            "codex": snap("codex", [win("weekly", "weekly", 90)])}

    def test_explicit_flags_unchanged(self):
        boom = mock.Mock(side_effect=AssertionError("usage must not be read"))
        self.cfg["spawn_route"] = "auto"
        r = self.sp.choose_route(self.args(cli="claude"), self.cfg, None, read=boom)
        self.assertEqual((r["cli"], r["account"], r["config_dir"], r["routed"]), ("claude", "work", None, False))
        r = self.sp.choose_route(self.args(cli="codex"), self.cfg, None, read=boom)
        self.assertEqual((r["cli"], r["account"]), ("codex", None))
        r = self.sp.choose_route(self.args(account="personal"), self.cfg, None, read=boom)
        self.assertEqual((r["cli"], r["account"], r["config_dir"]), ("claude", "personal", str(self.second.resolve())))
        r = self.sp.choose_route(self.args(model="claude-sonnet-5-5"), self.cfg, None, read=boom)
        self.assertEqual((r["cli"], r["account"]), ("claude", "work"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.sp.choose_route(self.args(cli="codex", account="personal"), self.cfg, None, read=boom)

    def test_routing_is_opt_in(self):
        r = self.sp.choose_route(self.args(), self.cfg, None, read=mock.Mock(side_effect=AssertionError))
        self.assertEqual((r["cli"], r["account"], r["routed"]), ("claude", "work", False))
        self.cfg["spawn_route"] = "auto"
        r = self.sp.choose_route(self.args(), self.cfg, None, read=self.reader())
        self.assertEqual((r["cli"], r["account"], r["routed"]), ("claude", "personal", True))
        r = self.sp.choose_route(self.args(), self.cfg, None, read=self.reader(personal_left=5))
        self.assertEqual(r["account"], "work")
        self.assertIn("Avoid claude:personal", r["why"])
        r = self.sp.choose_route(self.args(), self.cfg, "Employer-Org", read=self.reader())
        self.assertEqual(r["account"], "work")
        r = self.sp.choose_route(self.args(account="auto", cli="codex"), {}, None, read=self.reader())
        self.assertEqual(r["cli"], "codex")

    def test_usage_failure_falls_back_and_unready_account_refused(self):
        self.cfg["spawn_route"] = "auto"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            r = self.sp.choose_route(self.args(), self.cfg, None, read=mock.Mock(side_effect=OSError("down")))
        self.assertEqual((r["cli"], r["account"], r["routed"]), ("claude", "work", False))
        self.assertIn("usage read failed", err.getvalue())
        (self.second / ".credentials.json").unlink()
        with mock.patch.object(usage.sys, "platform", "linux"), contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit):
            self.sp.choose_route(self.args(account="personal"), self.cfg, None)
        self.assertIn("no login", err.getvalue())

    def test_launch_sets_the_login_for_the_tmux_session(self):
        calls = []
        with mock.patch.object(self.sp.registry, "claude_bin", return_value="/bin/claude"), \
                mock.patch.object(self.sp, "tmux", side_effect=lambda *a, **k: calls.append(a)), \
                mock.patch.object(self.sp.time, "sleep"):
            self.sp.launch_claude("n", "/work", "/b.md", "m", "high", config_dir=str(self.second))
            self.sp.launch_claude("n2", "/work", "/b.md", "m", "high")
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(self.second), calls[0])
        self.assertFalse(calls[1][-1].startswith("unset"))
        self.assertFalse(any("CLAUDE_CONFIG_DIR" in str(x) for x in calls[3]))
        # the default login: removed from the session's environment and from the pane's shell
        self.assertEqual(calls[4], ("set-environment", "-t", "n2", "-r", "CLAUDE_CONFIG_DIR"))
        self.assertTrue(calls[5][-1].startswith("unset CLAUDE_CONFIG_DIR; /bin/claude "))

    def test_codex_launch_skips_the_update_menu(self):
        calls = []
        with mock.patch.object(self.sp.registry, "codex_bin", return_value="/bin/codex"), \
                mock.patch.object(self.sp.registry, "codex_live_ids", return_value=set()), \
                mock.patch.object(self.sp, "tmux", side_effect=lambda *a, **k: calls.append(a)), \
                mock.patch.object(self.sp, "wait_for", return_value=None), \
                mock.patch.object(self.sp.time, "sleep"):
            self.sp.launch_codex("n", "/work", "/b.md", "gpt-6.1-sol", "xhigh")
        typed = next(c[-1] for c in calls if c[:2] == ("send-keys", "-t") and "-l" in c)
        self.assertTrue(typed.startswith("/bin/codex -s danger-full-access -a never -C /work "
                                         "-c check_for_update_on_startup=false "), typed)

    def test_trust_goes_to_the_accounts_own_claude_json(self):
        home_json = Path(tempfile.mkdtemp()) / ".claude.json"
        home_json.write_text("{}")
        acct_json = self.sp.claude_json_for(str(self.second))
        acct_json.write_text(json.dumps({"oauthAccount": {}}))
        d = tempfile.mkdtemp()
        with mock.patch.object(self.sp, "CLAUDE_JSON", home_json), mock.patch.object(self.sp, "trust_codex", return_value=False):
            self.sp.ensure_trust(d, acct_json)
            self.assertEqual(json.loads(home_json.read_text()), {})
            self.assertTrue(json.loads(acct_json.read_text())["projects"][str(Path(d).resolve())]["hasTrustDialogAccepted"])
            acct_json.unlink()
            self.sp.ensure_trust(d, acct_json)   # a login that is not set up gets no half-made .claude.json
            self.assertFalse(acct_json.exists())


class Cli(unittest.TestCase):
    def test_pick_and_json(self):
        au = load_script("agent-usage")
        s = {"claude:default": snap("claude:default", [win("session", "5-hour", 0)]),
             "codex": snap("codex", [win("weekly", "weekly", 90)])}
        with mock.patch.object(au.config, "load", return_value={}), mock.patch.object(au.usage, "snapshots", return_value=s):
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
