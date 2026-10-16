"""Where the board reads identity and paths from: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
from agentboard import actions, collect, config, gmailread  # noqa: E402


class Env:
    """Set (or with None, unset) environment variables for one block."""
    def __init__(self, **kw):
        self.kw, self.saved = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class IdentityTest(unittest.TestCase):
    def test_owner_from_the_mailroom_config(self):
        self.assertEqual(config.owner_name(), "Alex")
        self.assertEqual(config.owner_email(), "owner@example.com")
        self.assertEqual(actions.header(), "[relayed from Alex via agent-board]")

    def test_neutral_without_config(self):
        empty = {}
        self.assertEqual(config.owner_name(cfg=empty), "the owner")
        self.assertIsNone(config.owner_name(default=None, cfg=empty))
        self.assertIsNone(config.owner_email(empty))
        self.assertEqual(config.owner_addresses(empty), [])
        self.assertIn("<the owner's address>", actions.orch_header(empty))

    def test_owner_email_falls_back_to_the_allowlist(self):
        cfg = {"allowlist": ["Owner@Example.com", "second@example.com"]}
        self.assertEqual(config.owner_email(cfg), "Owner@Example.com")
        self.assertEqual(config.owner_addresses(dict(cfg, owner_email="alex@example.org")),
                         ["alex@example.org", "owner@example.com", "second@example.com"])

    def test_repos_root_and_orchestrator_dir(self):
        with Env(AGENT_BOARD_REPOS=None):
            self.assertEqual(config.repos_root({}), Path(os.path.expanduser("~/code")))
            self.assertEqual(config.repos_root({"repos_root": "/srv/repos"}), Path("/srv/repos"))
            self.assertEqual(config.orchestrator_dir({"repos_root": "/srv/repos"}), Path("/srv/repos/orchestrator"))
            self.assertEqual(config.orchestrator_dir({"orchestrator_dir": "/o"}), Path("/o"))
        with Env(AGENT_BOARD_REPOS="/elsewhere"):
            self.assertEqual(config.repos_root({"repos_root": "/srv/repos"}), Path("/elsewhere"))
        root = Path(os.environ["AGENT_BOARD_TEST_ROOT"])
        self.assertIn(os.path.realpath(str(root / "code" / "orchestrator")), collect.ORCHESTRATOR_DIRS)
        self.assertTrue(collect.is_orchestrator("x", "", str(root / "code" / "orchestrator")))


class MailroomDirTest(unittest.TestCase):
    def test_sibling_by_default_and_env_override(self):
        with Env(AGENT_MAILROOM_DIR=None):
            sibling = config.KIT_DIR / "mailroom"
            self.assertEqual(config.mailroom_dir(), sibling if (sibling / "agentmail").is_dir() else None)
        with tempfile.TemporaryDirectory() as d:
            with Env(AGENT_MAILROOM_DIR=d):
                self.assertIsNone(config.mailroom_dir())                 # no agentmail package there
                (Path(d) / "agentmail").mkdir()
                self.assertEqual(config.mailroom_dir(), Path(d))
                self.assertEqual(actions.mailroom_root(), d)

    def test_no_mailroom_is_a_clear_error(self):
        with Env(AGENT_MAILROOM_DIR="/nonexistent/mailroom"):
            with self.assertRaises(RuntimeError) as e:
                actions.mailroom("close")
            self.assertIn("mailroom isn't installed", str(e.exception))


class GmailOptionalTest(unittest.TestCase):
    def test_off_by_default_and_on_with_a_credentials_file(self):
        self.assertIsNone(config.board_config()["gmail_credentials"])
        self.assertIsNone(gmailread.make())                              # default: no read-check
        with tempfile.TemporaryDirectory() as d:
            creds = Path(d) / "creds.json"
            cfg = Path(d) / "board.json"
            cfg.write_text(json.dumps({"gmail_credentials": str(creds)}))
            with Env(AGENT_BOARD_CONFIG=str(cfg)):
                real = config.BOARD_CONFIG
                config.BOARD_CONFIG = cfg
                try:
                    self.assertIsNone(gmailread.make())                  # named but missing: still off
                    creds.write_text("{}")
                    g = gmailread.make()
                    self.assertIsNotNone(g)
                    self.assertEqual((g.sender, g.owners), ("agents@example.com", ["owner@example.com"]))
                    self.assertIsNone(gmailread.make(owners=[]))         # no owner address to check the login against
                finally:
                    config.BOARD_CONFIG = real


if __name__ == "__main__":
    unittest.main()
