"""Imported first by every test module (directly, or through fixtures.py): point HOME and every config and state path
the board reads at a scratch directory, with an example owner, so tests never read or write the real ~/.config or
~/.local/state, and no personal login (Gmail, Google, a to-do repo) is configured unless a test sets one up."""
import json
import os
import tempfile
from pathlib import Path

OWNER_NAME = "Alex"
OWNER = "owner@example.com"
AGENTS = "agents@example.com"

if not os.environ.get("AGENT_BOARD_TEST_ROOT"):
    root = Path(tempfile.mkdtemp(prefix="agent-board-tests-"))
    os.environ["AGENT_BOARD_TEST_ROOT"] = str(root)
    (root / "home").mkdir()
    os.environ["HOME"] = str(root / "home")         # sources.HOME, BOARD_STATE and every ~ default follow it
    (root / "mail-config").mkdir()
    (root / "mail-config" / "config.json").write_text(json.dumps({
        "owner_name": OWNER_NAME, "owner_email": OWNER, "allowlist": [OWNER], "account": AGENTS,
        "repos_root": str(root / "code"), "orchestrator_dir": str(root / "code" / "orchestrator")}))
    os.environ["AGENT_MAIL_CONFIG_DIR"] = str(root / "mail-config")
    os.environ["AGENT_MAIL_STATE_DIR"] = str(root / "mail-state")
    os.environ["AGENT_STATUS_DIR"] = str(root / "status")
    os.environ["AGENT_BOARD_CONFIG"] = str(root / "board-config.json")
    os.environ["AGENT_MAILROOM_DIR"] = str(root / "no-mailroom")
    os.environ["TODO_DESK_CONFIG_DIR"] = str(root / "todo-config")
    for k in ("AGENT_BOARD_REPOS", "AGENT_BOARD_GMAIL_CREDS", "AGENT_BOARD_GOOGLE_TOKEN", "AGENT_BOARD_TIME_CONFIG",
              "AGENT_BOARD_MEETINGS_CONFIG", "AGENT_BOARD_ALLOWANCE_CONFIG", "AGENT_BOARD_COLOURS",
              "AGENT_BOARD_DRAFTS_CONFIG", "AGENT_BOARD_MAILWEB_CONFIG", "AGENT_BOARD_KEYRING_UNLOCK",
              "AGENT_BOARD_VOICE_CONFIG", "AGENT_BOARD_DESK_CONFIG", "AGENT_BOARD_SPEECH_CONFIG",
              "AGENT_BOARD_TRANSCRIPTS_CONFIG", "AGENT_BOARD_MAC", "VOICE_DESK_MAC", "GEMINI_API_KEY",
              "CLAUDE_CONFIG_DIR", "CODEX_HOME", "TODO_REPO"):
        os.environ.pop(k, None)
