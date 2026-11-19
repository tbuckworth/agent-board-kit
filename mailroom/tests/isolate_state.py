"""Point the mailroom's state and config directories and the status log at fresh temporary directories.

Every test module imports this before anything from agentmail or bin/: agentmail.config reads
AGENT_MAIL_STATE_DIR and AGENT_MAIL_CONFIG_DIR, and agentmail.statuslog AGENT_STATUS_DIR, once, at first
import, and without them uses the live mailroom store, config and status log under HOME. The first import
of this module sets the three variables for the rest of the process and its subprocesses. The config
directory holds only a config.json with IDENTITY, the values an installed kit's config.json always has (the
kit's DEFAULTS leave them unset), so config.load() returns config.DEFAULTS with the test owner Alex and the
agents account agents@example.com; there is no Gmail token.

If agentmail.config is imported already, it may hold the live directories: this module then says so on
stderr and ends the process with exit code 2, since unittest reports an exception raised here as one
module failing to import and runs the other modules.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

if "agentmail.config" in sys.modules:
    sys.stderr.write("isolate_state: agentmail.config was imported before isolate_state, so its directories may be "
                     "the live ones; no test runs. Import isolate_state before agentmail in every test module.\n")
    sys.stderr.flush()
    os._exit(2)

# Each environment variable agentmail reads its live directories from, and the temp subfolder it gets here.
DIRS = (("AGENT_MAIL_STATE_DIR", "state"), ("AGENT_MAIL_CONFIG_DIR", "config"), ("AGENT_STATUS_DIR", "status"))
BASE = Path(tempfile.mkdtemp(prefix="agent-mail-tests-"))
for var, name in DIRS:
    (BASE / name).mkdir()
    os.environ[var] = str(BASE / name)

# The identity the installer writes into config.json from kit.env, here with example values.
IDENTITY = {"account": "agents@example.com", "from_name": "Agents", "allowlist": ["owner@example.com"],
            "owner_name": "Alex", "github_owner": "example-user"}
(BASE / "config" / "config.json").write_text(json.dumps(IDENTITY))
