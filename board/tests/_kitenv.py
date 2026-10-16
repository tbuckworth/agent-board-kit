"""Imported first by every test module: point every config and state path the board reads at a scratch
directory, with an example owner, so tests never read the real ~/.config or ~/.local/state."""
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
    (root / "mail-config").mkdir()
    (root / "mail-config" / "config.json").write_text(json.dumps({
        "owner_name": OWNER_NAME, "owner_email": OWNER, "allowlist": [OWNER], "account": AGENTS,
        "repos_root": str(root / "code"), "orchestrator_dir": str(root / "code" / "orchestrator")}))
    os.environ["AGENT_MAIL_CONFIG_DIR"] = str(root / "mail-config")
    os.environ["AGENT_MAIL_STATE_DIR"] = str(root / "mail-state")
    os.environ["AGENT_STATUS_DIR"] = str(root / "status")
    os.environ["AGENT_BOARD_CONFIG"] = str(root / "board-config.json")
    os.environ["AGENT_MAILROOM_DIR"] = str(root / "no-mailroom")
    for k in ("AGENT_BOARD_REPOS", "AGENT_BOARD_GMAIL_CREDS"):
        os.environ.pop(k, None)
