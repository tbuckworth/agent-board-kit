"""Helpers shared by the command-line scripts in bin/ and the library code they run."""
import os
import sys

# Set in the commands an agent runs: Claude Code, Codex, a headless orchestrator run. A command that acts as the
# owner refuses to run when any is set (refuse_in_agent), since only the board and the mailroom act as the owner.
AGENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "AGENT_ORCHESTRATOR")


def die(prog, msg, code=1):
    """Print `prog: msg` to stderr and exit with `code`: the way the scripts stop on a usage or setup error."""
    print("%s: %s" % (prog, msg), file=sys.stderr)
    sys.exit(code)


def chosen_session(prog, spec, found):
    """The session --session spec names, from registry.resolve_exact's answer found (session, code, candidates).
    Stops `prog` with exit code 4 when it names none, 3 when it names several."""
    sess, code, cands = found
    if code == 4:
        die(prog, "no session matches --session %r (see `agent-send --list`)" % spec, 4)
    if code == 3:
        die(prog, "--session %r is ambiguous: %s" % (spec, ", ".join("%s %s [%s]" % (s["cli"], s["name"], s["id"][:8])
                                                                     for s in cands)), 3)
    return sess


def refuse_in_agent(prog, what, owner_name):
    """Stop `prog` with exit code 2 when it runs inside an agent session (an AGENT_ENV variable is set): `what` (as
    the message names it, e.g. "--as owner") acts as owner_name, which only the board and the mailroom do."""
    inside = [v for v in AGENT_ENV if os.environ.get(v)]
    if inside:
        die(prog, "refusing %s inside an agent session (%s set): only the board and the mailroom act as %s"
            % (what, ", ".join(inside), owner_name), 2)
