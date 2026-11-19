"""Stand-ins the test modules share for the commands the board finds on PATH (sources.find_bin) and for the mailroom
checkout it borrows modules from (sources.mailroom_root: config.mailroom_dir, AGENT_MAILROOM_DIR)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
from agentboard import sources  # noqa: E402

MAILROOM = Path(tempfile.mkdtemp())          # a mailroom checkout: bin/agent-spawn beside an (empty) agentmail package
(MAILROOM / "bin").mkdir()
(MAILROOM / "agentmail").mkdir()
(MAILROOM / "bin" / "agent-spawn").write_text("")


def find_bin(name):
    """Every command is "/bin/<name>", except agent-spawn, which sits in MAILROOM; the tests stand in for the
    mailroom's modules (another component, with its own tests) themselves."""
    return str(MAILROOM / "bin" / name) if name == "agent-spawn" else "/bin/" + name


def use(test):
    """For one test: find_bin above, and MAILROOM as the mailroom checkout (AGENT_MAILROOM_DIR), both put back by the
    test's cleanup."""
    saved_find, saved_dir = sources.find_bin, os.environ.get("AGENT_MAILROOM_DIR")
    sources.find_bin = find_bin
    os.environ["AGENT_MAILROOM_DIR"] = str(MAILROOM)

    def undo():
        sources.find_bin = saved_find
        if saved_dir is None:
            os.environ.pop("AGENT_MAILROOM_DIR", None)
        else:
            os.environ["AGENT_MAILROOM_DIR"] = saved_dir
    test.addCleanup(undo)


class Command:
    """Stands in for a command the board runs through sources.run (agent-group, agent-mail): answers each call with
    the next (code, stdout, stderr), the last one again once the others are used, and keeps (argv, stdin) of each."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers) or [(0, "", "")], []

    def __call__(self, cmd, timeout=60, env=None, input=None, cwd=None):
        self.calls.append((cmd, input))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
