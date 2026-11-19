"""agentmail/config.py: what config.load makes of config.json, and config values reaching the texts that use them."""
import io
import json
import runpy
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import config, launch, mailroom, memwatch, outbound, review, routing, switch  # noqa: E402


class Load(unittest.TestCase):
    def test_a_partial_dict_keeps_the_other_defaults(self):
        homes.config_file(self, {"labels": {"held": "mine/held"}, "poll_seconds": 5})
        cfg = config.load()
        self.assertEqual(cfg["labels"], dict(config.DEFAULTS["labels"], held="mine/held"))
        self.assertEqual(cfg["poll_seconds"], 5)
        self.assertEqual(config.DEFAULTS["labels"]["held"], "mailroom/held")   # the defaults are not changed

    def test_nested_dicts_merge_and_other_values_replace(self):
        with patch.dict(config.DEFAULTS, nested={"a": {"b": 1, "c": 2}, "d": [1, 2]}):
            homes.config_file(self, {"nested": {"a": {"b": 3}, "d": [5]}, "allowlist": ["Me@Example.com"]})
            cfg = config.load()
        self.assertEqual(cfg["nested"], {"a": {"b": 3, "c": 2}, "d": [5]})
        self.assertEqual(cfg["allowlist"], ["me@example.com"])
        cfg["labels"]["held"] = "changed"
        self.assertEqual(config.load()["labels"]["held"], "mailroom/held")   # each load is its own copy


class NeutralDefaults(unittest.TestCase):
    """The kit's DEFAULTS name no one: the installer writes the identity into config.json (isolate_state.IDENTITY
    stands in for it here)."""

    def test_no_identity_in_the_defaults(self):
        d = config.DEFAULTS
        self.assertEqual((d["account"], d["allowlist"], d["owner_email"], d["github_owner"]), (None, [], None, None))
        self.assertEqual((d["owner_name"], d["from_name"], d["repos_root"]), ("the owner", "Agents", "~/code"))
        self.assertIsNone(d["orchestrator_fallback_secret"])
        self.assertIsNone(config.owner_address(d))

    def test_the_account_is_required_to_send(self):
        with self.assertRaisesRegex(config.ConfigError, "config `account` is not set"):
            config.require_account(dict(config.DEFAULTS))
        self.assertEqual(config.require_account(config.load()), "agents@example.com")
        with self.assertRaises(config.ConfigError):
            outbound.compose(dict(config.load(), account=None), "s", "o@example.com", None, "S", "text")

    def test_an_empty_owner_name_reads_as_the_owner(self):
        homes.config_file(self, {"owner_name": " "})
        self.assertEqual(config.load()["owner_name"], "the owner")
        self.assertEqual(config.owner_name({}), "the owner")
        self.assertEqual(config.owner_possessive({"owner_name": "Alex"}), "Alex's")


class OwnerAddress(unittest.TestCase):
    def test_owner_email_else_the_first_allowlist_entry(self):
        cfg = config.load()
        self.assertEqual(config.owner_address(cfg), "owner@example.com")
        self.assertEqual(config.owner_address(dict(cfg, owner_email="Alex@Example.com")), "alex@example.com")
        self.assertEqual(config.owner_address(dict(cfg, alert_to="alerts@example.com")), "owner@example.com")
        self.assertIsNone(config.owner_address(dict(cfg, allowlist=[])))

    def test_alerts_go_to_alert_to_else_the_owner(self):
        cfg = config.load()
        self.assertEqual(config.alert_address(cfg), "owner@example.com")
        self.assertEqual(config.alert_address(dict(cfg, alert_to="alerts@example.com")), "alerts@example.com")
        self.assertIsNone(config.alert_address(dict(cfg, allowlist=[])))

    def test_owner_or_the_owners_name_reads_as_that_address_in_any_case(self):
        cfg = config.load()
        for value in ("owner", "OWNER", "alex", "Alex", " ALEX "):
            self.assertEqual(outbound.recipient(cfg, value), "owner@example.com")
        for value in ("a@example.com", "Alex <alex@example.com>", "a@example.com, b@example.com", None, ""):
            self.assertEqual(outbound.recipient(cfg, value), value)   # an address, or no --cc, as given
        self.assertEqual(outbound.recipient(dict(cfg, owner_name="Robin"), "robin"), "owner@example.com")
        with self.assertRaisesRegex(outbound.MailError, r"'alex' is neither an address nor 'owner' \(or 'robin', Robin's"):
            outbound.recipient(dict(cfg, owner_name="Robin"), "alex")
        with self.assertRaisesRegex(outbound.MailError, "'owner' names Alex, but config has no owner address"):
            outbound.recipient(dict(cfg, allowlist=[]), "owner")

    def test_the_brief_tells_the_agent_to_mail_the_owner_as_owner_in_a_command_without_links(self):
        cfg = config.load()
        pre = launch.preamble(cfg, "n", "/p", "b", "claude", "")
        self.assertIn("agent-mail send --to owner --subject", pre)
        self.assertIn("Write the body file in one command and run the send in the next", pre)
        self.assertNotIn("owner@example.com", pre)
        self.assertIn("--to owner --subject", launch.preamble(dict(cfg, owner_name="Alex Smith"), "n", "/p", "b", "claude", ""))
        with self.assertRaisesRegex(ValueError, "no owner address"):
            launch.preamble(dict(cfg, allowlist=[]), "n", "/p", "b", "claude", "")


class OwnerName(unittest.TestCase):
    """config owner_name reaches every text that names the owner: what agents, the orchestrator and the reviewer
    read. test_collab and test_review pin those texts word for word under the test identity's name ("Alex")."""

    def test_another_owner_name_renders_throughout(self):
        cfg = dict(config.load(), owner_name="Robin", collaborators={"agent@example.com": {"owner": "Sam"}})
        team = {"owner": "Sam", "share_data_with": ["agent@example.com"]}
        thread = [({"from": cfg["account"]}, "question", 1, "m1"), ({"from": "x@example.com"}, "answer", 2, "m2")]
        st = {"name": "a", "id": "x" * 36, "cwd": "/w"}
        g = {"mem": 1 << 30, "top": [("python", 1, 1 << 30)]}
        u = {"ram_pct": 85, "ram_used": 1, "ram_total": 2, "swap_pct": 0, "swap_used": 0, "swap_total": 1}
        texts = {
            "event, collaborator to a session": routing.event_text("session", "collaborator", 7, "", "a@example.com",
                                                                   {}, "S", "b", "", "Robin"),
            "event, owner to the orchestrator": routing.event_text("orchestrator", "owner", 7, "", "o@example.com", {},
                                                                   "S", "b", "", "Robin"),
            "event, collaborator to the orchestrator": routing.event_text(
                "orchestrator", "collaborator", 7, "", "a@example.com", team, "S", "b", "", "Robin"),
            "notice context": mailroom.orchestrator_context("mailroom-notice", "owner", 5, [], [], cfg)[2],
            "unowned context": mailroom.orchestrator_context(None, "owner", 5, thread, [{"tag": "t", "gmail_id": "m1"}],
                                                             cfg)[2],
            "lint": " ".join(review.lint("t", '<img src="data:x"><img src="/a.png"><img src="cid:c">', [],
                                         owner_name="Robin")),
            "hints": " ".join(review.hints("see /home/x/a.png", owner_name="Robin")),
            "audience, collaborator": review.audience(cfg, "agent@example.com")[1],
            "audience, other": review.audience(cfg, "someone@example.com")[1],
            "handoff record": switch.handoff_text(st, "Claude", "Codex", "/t", "B", [], [], "Robin"),
            "handoff brief": switch.handoff_brief(st, "/h", "Claude", "Codex", "Robin"),
            "spawn preamble": launch.preamble(cfg, "n", "/p", "b", "claude", ""),
            "memwatch warning": memwatch.ping_text(u, g, 1, memwatch.settings(cfg)),
            "memwatch urgent": memwatch.ping_text(u, g, 2, memwatch.settings(cfg)),
        }
        for path in (review.PROMPT_PATH, review.OUTSIDE_PROMPT_PATH, review.RULES_PATH):
            texts[path.name] = review.read_prompt(path, cfg)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()) as err:
            load_script("agent-mail").check_send_enabled(dict(cfg, send_enabled=False))
        texts["agent-mail send disabled"] = err.getvalue()
        for name, text in texts.items():
            with self.subTest(name):
                self.assertIn("Robin", text)
                self.assertNotIn("Alex", text)
                self.assertNotRegex(text, r"\b(?i:he|his|him)\b")   # nor a gender for Robin

    def test_the_collaborators_named_in_the_owners_prompt_come_from_config(self):
        cfg = dict(config.load(), collaborators={"b@example.com": {"owner": "Sam"}, "a@example.com": {"name": "A"},
                                                   "c@example.com": {"owner": "Sam"}})
        self.assertIn("those collaborators' agents (Sam, a@example.com).", review.read_prompt(review.PROMPT_PATH, cfg))
        self.assertIn("those collaborators' agents (none configured).",
                      review.read_prompt(review.PROMPT_PATH, dict(cfg, collaborators={})))


class OwnerContext(unittest.TestCase):
    """review.owner_context: a few sentences about the owner that every review prompt carries, and none when empty."""

    def test_the_context_reaches_both_prompts_once_and_nothing_when_empty(self):
        about = "Alex works on protein design and knows the lab's tooling."
        cfg = dict(config.load(), review={"owner_context": "  %s  " % about})
        for path in (review.PROMPT_PATH, review.OUTSIDE_PROMPT_PATH):
            with self.subTest(path.name):
                text = review.read_prompt(path, cfg)
                self.assertEqual(text.count(about), 1)
                self.assertIn("\n\n%s\n\n" % about, text)   # a paragraph of its own
                self.assertNotIn("{owner_context}", text)
                plain = review.read_prompt(path, config.load())
                self.assertNotIn(about, plain)
                self.assertNotIn("\n\n\n", plain)
        self.assertNotIn(about, review.read_prompt(review.RULES_PATH, cfg))   # the guide is for agents


class AuthScript(unittest.TestCase):
    def test_consent_uses_the_configured_account_client_and_token_path(self):
        d = homes.temp_dir(self)
        conf = d / "config.json"
        conf.write_text(json.dumps({"account": "Agents@Example.com", "oauth_client_file": str(d / "client.json")}))
        (d / "client.json").write_text("{}")
        calls = {}

        class Flow:   # the Google OAuth consent flow: a browser and Google's servers, so it is faked
            @staticmethod
            def from_client_secrets_file(path, scopes):
                calls["client"], calls["scopes"] = path, scopes
                return Flow()

            def run_local_server(self, **kw):
                calls["hint"] = kw["login_hint"]
                return SimpleNamespace(client_id="id", client_secret="s", refresh_token="r")

        def build(*a, **kw):   # the Gmail API: Google's servers, so it is faked
            profile = {"emailAddress": "agents@example.com", "messagesTotal": 3}
            return Mock(**{"users.return_value.getProfile.return_value.execute.return_value": profile})

        flow_module = SimpleNamespace(InstalledAppFlow=Flow)
        with patch.dict(sys.modules, {"google_auth_oauthlib": SimpleNamespace(flow=flow_module),
                                      "google_auth_oauthlib.flow": flow_module}), \
                patch("googleapiclient.discovery.build", build), patch.object(config, "CONFIG_PATH", conf), \
                patch.object(config, "TOKEN_PATH", d / "keys" / "token.json"), patch.object(sys, "argv", ["x"]), \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as done:
            runpy.run_path(str(Path(__file__).resolve().parent.parent / "agent_mail_auth.py"), run_name="__main__")
        self.assertEqual(done.exception.code, 0)
        self.assertEqual(calls, {"client": str(d / "client.json"), "scopes": config.SCOPES,
                                 "hint": "agents@example.com"})
        self.assertEqual(json.loads((d / "keys" / "token.json").read_text())["refresh_token"], "r")
        self.assertEqual((d / "keys" / "token.json").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
