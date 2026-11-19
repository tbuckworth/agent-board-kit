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
from agentboard import actions, chats, collect, config, gmailstate, google, sources  # noqa: E402


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
        self.assertIn("This is the owner writing", actions.orch_header(empty))
        self.assertIn("--to owner ", actions.orch_header(empty))              # the mailroom resolves the alias

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
        orch = collect.orchestrator_dir()
        self.assertEqual(orch, os.path.realpath(str(root / "code" / "orchestrator")))
        self.assertTrue(collect.is_orchestrator("x", "", str(root / "code" / "orchestrator"), orch))
        self.assertEqual(sources.REPOS_ROOT, root / "code")          # the worktree scan's root


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
                self.assertEqual(sources.mailroom_root(), d)

    def test_no_mailroom_is_a_clear_error(self):
        with Env(AGENT_MAILROOM_DIR="/nonexistent/mailroom"):
            with self.assertRaises(RuntimeError) as e:
                sources.mailroom("close")
            self.assertIn("mailroom isn't installed", str(e.exception))
            self.assertIsNone(sources.optional_mailroom("failover"))


class BoardConfig:
    """The board config file at `path` holding `cfg` for one block (config.BOARD_CONFIG is read at import)."""
    def __init__(self, path, **cfg):
        self.path, self.cfg = Path(path), cfg

    def __enter__(self):
        self.path.write_text(json.dumps(self.cfg))
        self.real = config.BOARD_CONFIG
        config.BOARD_CONFIG = self.path

    def __exit__(self, *exc):
        config.BOARD_CONFIG = self.real


class GmailOptionalTest(unittest.TestCase):
    def test_off_by_default_and_on_with_a_credentials_file(self):
        self.assertIsNone(config.board_config()["gmail_credentials"])
        self.assertIsNone(google.gmail_login())
        self.assertIsNone(gmailstate.for_owner(sync=True))               # default: no read-check, no Gmail sync
        with tempfile.TemporaryDirectory() as d:
            creds = Path(d) / "creds.json"
            with BoardConfig(Path(d) / "board.json", gmail_credentials=str(creds)):
                self.assertIsNone(gmailstate.for_owner(sync=True))       # named but missing: still off
                creds.write_text("{}")
                g = gmailstate.for_owner(sync=True)
                self.assertIsNotNone(g)
                self.assertEqual((g.sender, g.owners, g.creds_path), ("agents@example.com", ["owner@example.com"], creds))
                self.assertIsNone(gmailstate.for_owner(sync=True, owners=[]))   # no owner address to check it against
                self.assertIsNone(gmailstate.for_owner(sync=True, sender=""))   # no agents' account to look for
            with Env(AGENT_BOARD_GMAIL_CREDS=str(creds)):
                self.assertEqual(google.gmail_login(), creds)            # the environment names it too


class GoogleTokenTest(unittest.TestCase):
    def test_off_by_default_and_on_with_a_token_file(self):
        self.assertIsNone(config.board_config()["google_token"])
        self.assertIsNone(config.google_token())
        self.assertIsNone(google.docs_login())
        with tempfile.TemporaryDirectory() as d:
            token = Path(d) / "token.json"
            with BoardConfig(Path(d) / "board.json", google_token="%s/token.json" % d):
                self.assertEqual(config.google_token(), token)
                self.assertIsNone(google.docs_login())                   # named, but no file there
                token.write_text("{}")
                self.assertEqual(google.docs_login(), token)
            with Env(AGENT_BOARD_GOOGLE_TOKEN=str(token)):
                self.assertEqual(config.google_token(), token)
            self.assertEqual(config.google_token({"google_token": "~/t.json"}), Path(os.path.expanduser("~/t.json")))


class TasksRepoTest(unittest.TestCase):
    def test_the_board_config_then_the_to_do_config_then_none(self):
        self.assertIsNone(config.board_config()["tasks_repo"])
        with tempfile.TemporaryDirectory() as d:
            todo = Path(d) / "todo-config.json"
            self.assertIsNone(config.tasks_repo({}, todo_config=todo))  # neither: the to-do source is off
            todo.write_text(json.dumps({"repo": " example/tasks "}))
            self.assertEqual(config.tasks_repo({}, todo_config=todo), "example/tasks")
            self.assertEqual(config.tasks_repo({"tasks_repo": "example/mine"}, todo_config=todo), "example/mine")
            todo.write_text("{not json")
            self.assertIsNone(config.tasks_repo({"tasks_repo": ""}, todo_config=todo))


class OwnerTextTest(unittest.TestCase):
    def test_text_agents_read_names_the_owner_from_the_config(self):
        self.assertTrue(actions.close_text().startswith("[relayed from Alex via agent-board] close\nAlex asked you"))
        self.assertIn("use agent-mail send to Alex.)", actions.send_footer())
        self.assertIn("agent-mail send --to owner --subject", actions.orch_header())     # the alias, no address
        self.assertEqual(chats.as_owner(), "owner")
        self.assertIn("(Alex posted this from the agent board as message k3f9.", chats.footer("k3f9"))
        brief = actions.SPAWN_BRIEF.format(owner=actions.owner(), path="/p", repo="r", branch="b", state="s", text="t")
        self.assertTrue(brief.startswith("Alex started you from the agent board"))
        self.assertIn("Instructions from Alex:", brief)


if __name__ == "__main__":
    unittest.main()
