"""Helpers shared by the command-line scripts in bin/ and the library code they run."""
import os
import sys

from . import procs

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


# The programs whose processes are agents: a command run by one has it among its ancestors (agent_ancestor).
AGENT_PROGRAMS = ("claude", "codex")


def agent_ancestor(pid, stop=None):
    """The name (AGENT_PROGRAMS) of the first agent among pid's ancestors, its own process not counted, up to stop
    (excluded; None: to the first process) by /proc, or None. A process counts by its argv[0]'s base name, or, for a
    node one, by the script it runs."""
    for pid in procs.ancestors(pid):
        if pid == stop:
            return None
        argv = procs.cmdline(pid)
        names = [os.path.basename(argv[0])] if argv else []
        if names == ["node"] and len(argv) > 1:   # .../@anthropic-ai/claude-code/cli.js, .../@openai/codex/bin/codex.js
            names += [os.path.basename(os.path.dirname(argv[1])), os.path.splitext(os.path.basename(argv[1]))[0]]
        for name in names:
            for agent in AGENT_PROGRAMS:
                if name == agent or name == agent + "-code":
                    return agent
    return None


def refuse_in_agent(prog, what, owner_name, tree=False):
    """Stop `prog` with exit code 2 when it runs inside an agent session (an AGENT_ENV variable is set), or, with
    tree, under an agent's process (agent_ancestor), which an agent cannot unset as it can a variable: `what` (as the
    message names it, e.g. "--as owner") acts as owner_name, which only the board and the mailroom do."""
    inside = [v for v in AGENT_ENV if os.environ.get(v)]
    if inside:
        die(prog, "refusing %s inside an agent session (%s set): only the board and the mailroom act as %s"
            % (what, ", ".join(inside), owner_name), 2)
    # The process tree, like the variables, stops an agent's ordinary commands, not a determined agent: a command
    # started through something that reparents it (setsid with a double fork, systemd-run --user, a new tmux window,
    # at) is no longer under the agent's process. This is known and accepted.
    agent = agent_ancestor(os.getpid()) if tree else None
    if agent:
        die(prog, "refusing %s under a %s process: only the board and the mailroom act as %s" % (what, agent,
                                                                                                owner_name), 2)
