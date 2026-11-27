"""Voice desk: the owner asks the agent that did a piece of work about it, by voice, without interrupting it. The voice
page (voiceweb.py: Gemini Live in the owner's browser) calls these through its tools; the Mac's ask box (mac
AskBox.swift) through the board's actions voice_looking and voice_ask (actions.py); and so do these commands:

  agent-board agents --json            the live Claude and Codex agents one can ask
  agent-board ask AGENT QUESTION       answered by a READ-ONLY copy ("fork") of the agent's conversation
  agent-board looking                  the Mac's front window, from ActivityWatch there, and the agent behind it

A Claude agent's fork is `claude -p --resume ID --fork-session`, run in the session's folder under the login that holds
the newest copy of its transcript (CLAUDE_CONFIG_DIR), on the session's own model. It is read-only by construction
(fork_argv): only Read, Grep and Glob exist in it (--tools), everything else is disallowed by name, the permission mode
is dontAsk with nobody to ask, and it loads no MCP server. Permission mode alone is not enough: in dontAsk, a fork that
also had Bash ran `ls` (read-only commands need no approval), so Bash must not exist at all. Nor are the reading tools
allowed outright (no --allowedTools): Claude Code then lets them read only in the session's folder, and dontAsk refuses
a read anywhere else (checked on Claude Code 2.1.330), so a question cannot have a fork read the desktop's other files,
its keys among them, for its answer to carry to Google.

A fork must never pass for the original agent (checked on Claude Code 2.1.330). Left alone it registers in
<login>/sessions/<pid>.json as a live session with its own inbox socket (agent-send --list shows it; the board skips it
as entrypoint sdk-cli); its transcript copies the original's name (custom-title, agent-name); it runs every hook,
agent-status's SessionStart (an event under its id) and agent-group's UserPromptSubmit, which hands a raised notice flag
to whichever name the registry gives the session; and it takes agent-send deliveries and answers them. So a fork is
named voice-copy-of-<agent> (--name: registry and transcript), runs no hooks (disableAllHooks) and refuses every
cross-session message (crossSessionInbound "refuse"). asks.jsonl in the board's state folder records each ask.

A copy of an agent on a login with a settings file of its own (a per-token login, a model through OpenRouter; the
mailroom's accounts.account_by_dir) runs on that login's one model, whatever its transcript or the caller names, since a
Claude model fails there. A copy's read-only settings (FORK_SETTINGS: no hooks, no cross-session messages, no
secret-looking files) go in its one --settings, which Claude Code takes once, so such a copy's is a file of its own
(settings_file: private, in SETTINGS_DIR, removed once the copy exits): the login's settings, its apiKeyHelper, env and
model, with the read-only settings laid over them, which win on every key they set (fork_settings). Other copies take
them inline.

A login at its usage limit answers with its limit's reply ("You've hit your weekly limit · ..."), which usage_limit
reads (the mailroom's failover.limit_record). Then the question goes to the mailroom's other Claude logins in turn, in
its order (fallback_logins: headless.logins, which the orchestrator and the pre-send review fall through; never a manual
login), the transcript copied to each first as the mailroom copies one for a resume on another login (copy_transcript),
and the kept copy (below) made there. An agent on a manual login (Gemini's, billed per token) is asked there alone.

The answer is bounded by the question's own preamble (PREAMBLE): Claude Code re-sends a resumed conversation's recorded
system prompt verbatim (--system-prompt-snapshot), so an --append-system-prompt never reaches the model.

Prompt caching: the original's cache cannot help, since the fork's tool list differs and tools come first in the cached
prefix. Nor does a second fork of the original read the first fork's cache: that was written at the end of the first
question, which is not a prefix of the second (measured: 12.6k of 93k tokens read). So a fork asked only PRIMER is
kept (a "copy", with its transcript, named as above), and each question forks that copy instead, keeping nothing: the
conversation and the primer are then a cached prefix, and no question (nor anything one carried) stays in a copy for
the next to meet. A copy is reused for COPY_SECONDS while the original's transcript has not grown and the model is the
same (copies.json); otherwise a new one is made.

Read-only is checked on the running fork too (run_fork): its init, before any event but its title and Claude Code's
notices of the original's background agents (PRE_INIT), lists its tools, MCP servers and permission mode, and a fork
with anything but Read, Grep and Glob, any MCP server, or a mode but dontAsk is stopped there, before the model can
answer. A fork may not read secret-looking files even in its folder (SECRET_READS), nor run in the home folder or above
it, nor in a folder that holds another live agent's (too_broad).

The ask box hears an answer while it is being written (ask's on_text; actions.voice_ask speaks it a sentence at a time,
speech.py): the question's fork then runs with --include-partial-messages, and each text delta after its init, so
after the check, is passed on. A fork that keeps nothing is not waited for once its result is in: claude takes ~0.9 s
more to exit (measured on Claude Code 2.1.331), which run_copy's `leave` spends in a thread instead.

A Codex agent's copy is `codex exec fork --ephemeral` (codex_argv), in the agent's folder under its CODEX_HOME, on its
own model at EFFORT. It holds no tool that can act or read (checked on Codex 0.171.0, with strace and a
stand-in model): --ignore-user-config leaves out config.toml's MCP servers, plugins and hook trust, and with them the
folder's own .codex layer (its MCP server and hooks did not run); --disable turns off hooks, the shell, apps (ChatGPT
connectors: Slack, sites), plugins, image generation, view_image, goals, sub-agents (with agents.enabled=false) and the
shell snapshot (a login shell, run at start); web search is "disabled"; and a model catalog of its own (codex_catalog:
CODEX_MODEL over the model's entry in Codex's list) leaves out code mode (`exec`, which nests the shell and
apply_patch), apply_patch and the shell. All it is offered is request_user_input (CODEX_TOOLS), which answers
"unavailable in Default mode". A stand-in model that called exec_command, shell, apply_patch, exec, spawn_agent and
list_mcp_resources got "unsupported call" for each, and the copy ran nothing but Codex's own start-up reads (git status
and config, lsb_release; GIT_OPTIONAL_LOCKS keeps git from touching the agent's index). Codex reads files only through
its shell, whose read-only sandbox may read any file (and does not run on this desktop), so a Codex copy has no file
tool at all and answers from the conversation alone (CODEX_PREAMBLE). A folder too broad (too_broad) is refused too.

That is checked on the running copy (Gate). Every request the copy makes goes to a gate on 127.0.0.1: its model
provider is the voice desk's own (the built-in one cannot be changed), ChatGPT's address (its account check, plugins,
analytics) is the gate's too, and its metrics export is off, so it connects to nothing else (strace). The gate turns
away all but model requests, and reads each of those before the model sees it: it sends one on to CODEX_UPSTREAM, the
copy's own login headers and all, only when the tools the copy offers in it are CODEX_TOOLS at most and any tool result
it carries for a call of the copy's is Codex's refusal (CODEX_REFUSED); else it refuses the request and the copy is
stopped. After a compaction Codex re-sends the tool list the agent's conversation recorded (rollout_tools: the agent's
own tools) and declares none of its own; that list goes to the model as recorded, and the copy refuses those tools
(each was "unsupported" to a stand-in model), but there the gate cannot see the copy's own list: what it checks is that
the copy declares nothing more, and that whatever a tool gave would never reach the model. The copy is stopped too at
any event but its answer, its reasoning and Codex's warnings (CODEX_ITEMS), and an answer whose requests never passed
the gate is not used.

A Codex copy's answer comes whole: `codex exec --json` gives no deltas of it (checked on Codex 0.172.0). A Codex copy
keeps nothing and never passes for the agent (checked on Codex 0.171.0): --ephemeral writes no rollout and no row in
Codex's threads, history or log tables, its name index or its thread-writer locks, so neither the board nor agent-send
(which finds Codex sessions by those) sees it; with hooks off it records no agent-status event. There is no kept copy:
each question forks the agent's live conversation, so a copy's request begins as the copy before's did and may be read
from the cache that one left.

`looking` maps the front window to an agent, passing over the voice page's own window and the ask box's (VOICE_TITLE
starts both titles: the owner talks to them about the window that was in front before): a mailroom tag [#xxxxxxxx] in
its title or address (Gmail, a thread) is the session that owns it; a mail subject alone (a Mail-tab window) is the
session that sent it; a GitHub pull request is the live session whose folder has the PR's head branch checked out; the
board's own window names what it shows in its title (BOARD_SESSION: the selected tile's agent, or the agent that joined
the Mail tab's open thread last); a kitty window (TERMINALS) is the desktop tmux session most recently typed in
(tmux_front: kitty windows attach to desktop tmux); else a live agent's name in the title. Anything else is "no agent
found", with the title. The Mac is read over ssh by the alias mac_host() gives (VOICE_DESK_MAC, else the board's
config.mac_host: the mailroom config's remote_hosts.mac, which the kit installer writes from MAC_HOST); with no Mac
configured, `looking` says so and reaches for nothing.
"""
import http.client
import http.server
import json
import os
import re
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

from . import collect, config, relaunch, sources

ASKS = sources.BOARD_STATE / "voice-desk" / "asks.jsonl"
COPIES = sources.BOARD_STATE / "voice-desk" / "copies.json"     # {agent's session id: the copy a later question reuses}
COPY_SECONDS = 50 * 60      # a copy is reused this long: its prompt cache lives an hour
MAC_SCRIPT = Path(__file__).resolve().parent.parent / "mac" / "voice-desk" / "aw_front.py"
VOICE_TITLE = "Agent Front Desk"          # the voice page's title (web/voice/index.html) and the ask box's (mac
                                          # AskBox.swift, AskRules.titlePrefix): front_window passes over their windows
NO_MAC = ("no Mac is configured for the desktop to read the front window from (MAC_HOST in the kit's kit.env, which "
          "becomes the mailroom config's remote_hosts.mac; or VOICE_DESK_MAC)")

COPY_PREFIX = "voice-copy-of-"            # a fork's session name: never an agent's
READ_TOOLS = ("Read", "Grep", "Glob")
# --tools may not cover every tool: on Claude Code 2.1.330 a fork named ListAgents, ToolSearch,
# AskUserQuestion, ReportFindings, ScheduleWakeup, SendFeedback and ShareOnboardingGuide among its tools besides Read,
# Grep and Glob, so they are denied by name too; with them denied, it named only those three
DENIED_TOOLS = ("Bash", "BashOutput", "KillShell", "Edit", "MultiEdit", "Write", "NotebookEdit", "WebFetch", "WebSearch",
                "Agent", "Task", "SendMessage", "Skill", "Workflow", "Monitor", "TaskStop", "CronCreate",
                "EnterWorktree", "ExitWorktree", "Artifact", "ArtifactComments", "ArtifactData", "ListAgents",
                "ToolSearch", "AskUserQuestion", "ReportFindings", "ScheduleWakeup", "SendFeedback",
                "ShareOnboardingGuide", "mcp__*")
# files a copy may not read even in its agent's folder, whatever the login's own settings say: an answer goes to
# Google, and a question may carry text that agents or web pages wrote (Read rules hold for Grep and Glob too)
SECRET_READS = ("Read(**/.env)", "Read(**/.env.*)", "Read(**/*.pem)", "Read(**/*.key)", "Read(**/*.p12)",
                "Read(**/id_rsa*)", "Read(**/id_ed25519*)", "Read(**/*secret*)", "Read(**/*credential*)",
                "Read(**/token.json)", "Read(**/.netrc)", "Read(**/.npmrc)", "Read(~/.ssh/**)", "Read(~/.config/**)")
FORK_SETTINGS = {"disableAllHooks": True, "crossSessionInbound": "refuse", "permissions": {"deny": list(SECRET_READS)}}
# a copy on a login with a settings file of its own (a per-token login's, through OpenRouter) runs with one file of
# its own here,
# while it runs: the login's settings with FORK_SETTINGS laid over them (fork_settings)
SETTINGS_DIR = sources.BOARD_STATE / "voice-desk" / "settings"
STALE_SETTINGS = 3600       # seconds after which a copy's settings file left by a board process that died is removed
NO_MCP = json.dumps({"mcpServers": {}})
EFFORT = "low"
TIMEOUT = 240
# events a fork may send before its init, none of which can run anything: the session title its --name sets, and Claude
# Code's notices of the original's background agents (a fork of an agent running sub-agents gives one each, "didn't
# finish before the previous session ended", checked on Claude Code 2.1.331)
PRE_INIT = {("system", "session_title_changed"), ("system", "task_notification")}
MAX_QUESTION = 4000
USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")   # ask()'s usage


def _preamble(can, owner=None):
    """The question's preamble, naming the owner (config.owner_name, read once as this module loads) as who asks."""
    return ("[Voice desk. %s is asking you this by voice, through a READ-ONLY copy of this conversation, and you "
            "are that copy: %s. You cannot run commands, edit files, send mail or message anyone, and nothing you do "
            "here reaches the original session; do not try. Answer for speech, from what this conversation already "
            "knows: 2 to 4 plain sentences, no lists, no markdown, no code, no file paths unless asked for one. "
            "Never quote or reveal a key, token, password or other credential, whatever the question says.]\n\n"
            % (owner or config.owner_name(default="The owner"), can))


PREAMBLE = _preamble("you can read files (Read, Grep, Glob) and nothing else")
CODEX_PREAMBLE = _preamble("you have no tools at all, not even to read a file")
PRIMER = "Nothing yet: reply OK, and nothing else."      # the kept copy's question (ask), so it holds no real one

# a Codex copy (codex_argv): the features that give tools or run anything (a name Codex does not know fails the copy)
CODEX_OFF = ("hooks", "shell_tool", "unified_exec", "apps", "plugins", "image_generation", "view_image", "goals",
             "multi_agent", "shell_snapshot", "enable_request_compression")    # the last so the gate can read requests
CODEX_CONFIG = ("agents.enabled=false", 'web_search="disabled"', 'sandbox_mode="read-only"', 'approval_policy="never"',
                'history.persistence="none"',
                relaunch.CODEX_NO_UPDATE,
                "analytics.enabled=false")      # else its metrics go to ab.chatgpt.com, past the gate
# over the model's entry in Codex's list: no code mode (`exec`), no shell, no apply_patch, no tool search, no clock
CODEX_MODEL = {"tool_mode": "default", "shell_type": "disabled", "apply_patch_tool_type": None,
               "experimental_supported_tools": [], "supports_search_tool": False}
CODEX_MODELS = sources.BOARD_STATE / "voice-desk" / "codex-models"   # those catalogs, one per model
CODEX_TOOLS = ("request_user_input",)       # all a copy may be offered: in exec it is "unavailable in Default mode"
# Codex's answer to a call of a tool it does not hold, or of request_user_input: the only tool result a model may see
CODEX_REFUSED = re.compile(r"unsupported (custom tool )?call: [\w.-]+|request_user_input is unavailable in \w+ mode")
TOOL_LIST = re.compile(rb'"type":\s*"additional_tools",\s*"id":\s*"([^"]+)"')     # a tool list in a rollout: its id
CODEX_ITEMS = ("agent_message", "reasoning", "error")   # its events' items: its answer, its thinking, Codex's warnings
CODEX_UPSTREAM = "https://chatgpt.com/backend-api/codex"    # where Codex sends a ChatGPT login's model requests
# the copy's model provider, at the gate: its login's headers, by plain posts (a WebSocket is tried 5 times, 6 s)
CODEX_PROVIDER = 'wire_api="responses", requires_openai_auth=true, supports_websockets=false'
CODEX_LEAKED = ("CODEX_THREAD_ID", "CODEX_SESSION_ID")      # the thread of the Codex process the board may run under
HOP = {"host", "content-length", "connection", "keep-alive", "proxy-connection", "te", "trailer", "transfer-encoding",
       "upgrade", "accept-encoding"}        # not sent on by the gate (no accept-encoding: it reads the answer's usage)

TAG = re.compile(r"\[#([a-z0-9]{6,12})\]")
PR_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
PR_TITLE = re.compile(r"Pull Request #(\d+) · ([\w.-]+)/([\w.-]+)")
SUBJECT_PREFIX = re.compile(r"^\s*((re|fwd?|aw)\s*:\s*)+", re.I)
GMAIL_SUFFIX = re.compile(r"\s+-\s+[^-]*@[^-]*\s+-\s+Gmail$")
# the board window's title names its agent so (mac App.swift, BoardTitle)
BOARD_SESSION = re.compile(r"\[session ([0-9A-Za-z-]{8,64})\]")
BOARD_MAIL = "Agent Board — Mail: "        # the board window's title in the Mail tab, before the thread's subject
TERMINALS = ("kitty",)                     # apps whose windows attach to the desktop's tmux (mac Kitty.swift)
BROWSERS = ("Google Chrome", "Chrome", "Safari", "Arc", "Firefox", "Brave Browser", "Microsoft Edge", "Chromium")


class VoiceError(Exception):
    """What went wrong, said for the owner to hear."""


class LimitReached(VoiceError):
    """A copy's login is at its usage limit: Claude Code gave its limit's reply (usage_limit's record, `hit`) in
    place of an answer."""

    def __init__(self, name, hit):
        super().__init__("%s's copy got no answer: its login is at its usage limit (%s)" % (name, hit.get("text")))
        self.hit = hit


# ------------------------------------------------------------------ the agents

def agents(claude=sources.claude_sessions_all, codex=None, orch_dir=None):
    """The live agents one can ask, by name: [{cli, name, id, status, cwd, folder, account, config_dir, tmux, model}],
    config_dir the Claude login or the CODEX_HOME. Not `claude -p` runs (probes, the orchestrator, these forks:
    entrypoint sdk-cli), not a Codex sub-agent (it lives in its parent's process), not the orchestrator's sessions, not
    a copy. `codex` reads the Codex threads (sources.CodexReader() by default). Raises VoiceError when they cannot be
    read, rather than leave every Codex agent out unsaid."""
    orch = orch_dir if orch_dir is not None else collect.orchestrator_dir()
    reader = codex if codex is not None else sources.CodexReader()
    codex_rows = reader.sessions()
    if reader.error:
        raise VoiceError("could not read Codex's threads in %s (%s), so no agent is listed" % (reader.home,
                                                                                             reader.error))
    out, seen = [], set()
    for s in sorted(claude(), key=lambda s: -(s.get("status_at") or 0)) + list(codex_rows):
        cli = s.get("cli") or "claude"
        if not s.get("live") or s.get("entrypoint") == "sdk-cli" or s.get("is_subagent") or not s.get("id") or \
                s["id"] in seen:
            continue
        if (s.get("name") or "").startswith(COPY_PREFIX) or collect.is_orchestrator(s["id"], s.get("name"),
                                                                                    s.get("cwd"), orch):
            continue
        seen.add(s["id"])
        out.append({"cli": cli, "name": s.get("name") or "", "id": s["id"], "status": s.get("raw_status") or "",
                    "cwd": s.get("cwd") or "", "folder": os.path.basename((s.get("cwd") or "").rstrip("/")),
                    "account": s.get("account") or ("codex" if cli == "codex" else ""),
                    "config_dir": s.get("config_dir") or (str(reader.home) if cli == "codex" else ""),
                    "tmux": s.get("tmux") or "", "model": s.get("model") or ""})
    return sorted(out, key=lambda a: a["name"] or a["id"])


def _spoken(name):
    return re.sub(r"[\s_]+", "-", (name or "").strip().lower())


def resolve(target, rows):
    """The agent `target` names among rows (agents()): its exact name, its id or a prefix of 6+ of it, its name as said
    (case, spaces for hyphens), then a unique name prefix or a unique part of a name. Raises VoiceError."""
    t = (target or "").strip()
    said = _spoken(t)
    picks = [lambda a: a["name"] == t, lambda a: a["id"] == t, lambda a: len(t) >= 6 and a["id"].startswith(t),
             lambda a: _spoken(a["name"]) == said, lambda a: bool(said) and _spoken(a["name"]).startswith(said),
             lambda a: len(said) >= 4 and said in _spoken(a["name"])]
    for pick in picks:
        hits = [a for a in rows if pick(a)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise VoiceError("'%s' could be %s: say which" % (t, ", ".join(sorted(a["name"] or a["id"] for a in hits))))
    raise VoiceError("no live agent called '%s'" % t)


# ------------------------------------------------------------------ its transcript

def transcript_facts(path, nbytes=4_000_000):
    """{model, context_tokens, cwd} from the end of a transcript: the model and prompt size (input, cache read and
    cache write tokens) of its last assistant reply, and the folder it last ran in."""
    facts = {"model": None, "context_tokens": None, "cwd": None}
    for line in reversed(sources.transcript_tail(path, nbytes)):
        if facts["model"] and facts["cwd"]:
            break
        if '"cwd"' not in line and '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if not facts["cwd"] and d.get("cwd"):
            facts["cwd"] = d["cwd"]
        model = sources.reply_model(d) if d.get("type") == "assistant" else None
        if not facts["model"] and model:
            u = d["message"].get("usage") or {}
            facts["model"] = model
            facts["context_tokens"] = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                                  "cache_creation_input_tokens")) or None
    return facts


def rollout_facts(path, nbytes=4_000_000):
    """{model, context_tokens, cwd} from the end of a Codex rollout: the model and folder of its last turn
    (turn_context), and the prompt size of its last model request (token_count's last_token_usage)."""
    facts = {"model": None, "context_tokens": None, "cwd": None}
    for line in reversed(sources.transcript_tail(path, nbytes)):
        if facts["model"] and facts["context_tokens"]:
            break
        if '"turn_context"' not in line and '"token_count"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get("payload") if isinstance(d, dict) else None
        if not isinstance(p, dict):
            continue
        if d.get("type") == "turn_context" and not facts["model"]:
            facts["model"], facts["cwd"] = p.get("model") or None, p.get("cwd") or None
        elif p.get("type") == "token_count" and not facts["context_tokens"]:
            last = (p.get("info") or {}).get("last_token_usage") or {}
            facts["context_tokens"] = last.get("input_tokens") or None
    return facts


# ------------------------------------------------------------------ running a copy (both CLIs)

def run_copy(argv, prompt, name, on_event, *, cwd, env, timeout, popen, stderr=subprocess.STDOUT, leave=False,
             ended=None):
    """Run one copy, `argv`, with `prompt` on its stdin, handing each JSON event it prints to on_event, which returns
    True to stop reading or raises VoiceError to stop the copy there: (its exit code, its other output). `stderr` is
    where its stderr goes (merged into that output by default). With `leave`, a copy on_event stopped is left to end by
    itself (a claude fork takes ~0.9 s to exit after its result) and the code is 0: a thread waits for it, and kills it
    at `timeout`, and the process does not exit before it. `ended` is called once the copy has exited (its settings
    file removed). Raises VoiceError when it takes over `timeout` seconds. `popen` is subprocess.Popen or a test's
    stand-in (sources.run_json_lines runs it)."""
    code, other, late = sources.run_json_lines(argv, prompt, on_event, timeout=timeout, stderr=stderr, cwd=cwd, env=env,
                                               popen=popen, leave=leave, ended=ended)
    if late:
        raise VoiceError("%s's copy took over %g seconds to answer" % (name, timeout))
    return code, other


def _stopped(name, why):
    return VoiceError("%s's copy was stopped before it could use a tool: %s" % (name, why))


def _failed(name, code, why):
    return VoiceError("%s's copy failed (exit %s): %s" % (name, code, str(why).strip()[-300:]))


# ------------------------------------------------------------------ a Claude agent's fork

def fork_argv(claude, sid, name, model=None, effort=EFFORT, keep=False, partial=False, settings=None):
    """The command that answers one question from a read-only copy of session sid (the question goes on its stdin):
    `name` is the agent's. With `keep` the copy keeps its transcript, for later questions to fork; with `partial` it
    streams its answer as it is written (stream_event text deltas). Never a bypass mode: dontAsk, with nobody to ask,
    denies whatever Claude Code would ask about, a read outside the session's folder among them. Its one --settings is
    FORK_SETTINGS, or `settings`, the file of them laid over its login's own (settings_file)."""
    argv = [claude, "-p", "--resume", sid, "--fork-session"] + ([] if keep else ["--no-session-persistence"]) + [
            "--name", COPY_PREFIX + (name or sid[:8]),
            "--tools", ",".join(READ_TOOLS), "--disallowedTools", ",".join(DENIED_TOOLS),
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--strict-mcp-config", "--mcp-config", NO_MCP, "--settings", str(settings or json.dumps(FORK_SETTINGS)),
            "--disable-slash-commands", "--no-chrome", "--output-format", "stream-json", "--verbose"]
    if partial:
        argv += ["--include-partial-messages"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    return argv


def fork_base():
    """The board's own environment for a claude it starts, before the mailroom's scrub: without another process's
    session id, messaging socket and token (relaunch.LEAKED), and tmux and SSH variables."""
    return {k: v for k, v in relaunch.agent_env().items() if not k.startswith("TMUX")}


def fork_env(config_dir):
    """The fork's environment: fork_base's, without what the mailroom keeps from any claude (relaunch.claude_env:
    another login's, a per-token login's OpenRouter settings among them), under the login whose transcript it resumes.
    Raises VoiceError when the mailroom's install or config fails to load: its scrub is the one rule for that
    environment."""
    try:
        return relaunch.claude_env(config_dir, fork_base())
    except (RuntimeError, ImportError, OSError, ValueError) as e:
        raise VoiceError("no environment for the copy, as the mailroom's scrub failed: %s" % sources.error_text(e)) \
            from None


def fallback_logins(cmd):
    """[(name, config dir, command, env)]: the Claude logins, in the mailroom's order, that a claude the board starts
    falls back through when one is at its usage limit (relaunch.headless_logins: never a manual one such as Gemini's),
    `cmd` as each takes it and fork_base's environment scrubbed for it. None without a mailroom that lists them.
    Raises VoiceError when the mailroom fails to load."""
    try:
        return relaunch.headless_logins(cmd, fork_base())
    except (RuntimeError, ImportError, OSError, ValueError, KeyError) as e:
        raise VoiceError("can't list the Claude logins from the mailroom: %s" % sources.error_text(e)) from None


def login_key(home):
    """A Claude login's folder, resolved, to tell logins apart by: None (the default login) is ~/.claude."""
    return Path(home or sources.DEFAULT_CLAUDE_HOME).resolve()


def usage_limit(event):
    """The usage limit a claude's stream-json `event` reports, when it is the reply Claude Code gives in place of an
    answer once its login's plan limit (5-hour, weekly) or a model's own allowance is used up ("You've hit your weekly
    limit · resets Dec 3, 5am (UTC)"): the mailroom's record of it (failover.limit_record: {"window",
    "resets_at", "text", ...}), read from the event as from the transcript entry Claude Code writes for that reply,
    whose fields the event carries under snake_case names (is_api_error_message, api_error, api_error_params'
    rate_limit_info for quotaLimits; checked 27 Nov 2026 on Claude Code 2.1.332). None for any other event, and
    without a mailroom, or one older than limit_record. Raises VoiceError when the mailroom fails to load: limits
    are then not read at all, which is said, not taken for no limit."""
    if event.get("type") != "assistant" or not event.get("is_api_error_message"):
        return None
    try:
        failover = sources.optional_mailroom("failover")
    except (RuntimeError, ImportError, OSError, ValueError) as e:
        raise VoiceError("can't tell whether the reply is a usage limit's, as the mailroom failed to load: %s"
                         % sources.error_text(e)) from None
    if failover is None or not hasattr(failover, "limit_record"):
        return None
    params = event.get("api_error_params") if isinstance(event.get("api_error_params"), dict) else {}
    quota = params.get("rate_limit_info") if isinstance(params.get("rate_limit_info"), dict) else {}
    return failover.limit_record({"type": "assistant", "isApiErrorMessage": True, "error": event.get("error"),
                                  "apiError": event.get("api_error"), "quotaLimits": quota,
                                  "message": event.get("message"), "timestamp": event.get("timestamp")})


def fork_settings(base):
    """The one settings object a copy runs with: its login's own (`base`: a per-token login's apiKeyHelper, env and
    model, which reach OpenRouter) with the copy's read-only settings (FORK_SETTINGS) laid over it, which win on every
    key they set, whole: the login's permissions give way to the read-only ones, never merged with them."""
    return dict(base, **json.loads(json.dumps(FORK_SETTINGS)))


def login_settings(config_dir):
    """(its settings object or None, its model or None) for the login whose folder is config_dir, from the mailroom
    (relaunch.login_account): a per-token login's settings file, read, and its one model; (None, None) for a plan login.
    Raises VoiceError when the mailroom fails to load, or the file cannot be read or holds no JSON object (its
    contents are never said)."""
    try:
        accounts, acct = relaunch.login_account(config_dir)
    except (RuntimeError, ImportError, OSError, ValueError) as e:
        raise VoiceError("can't read the login's settings from the mailroom: %s" % sources.error_text(e)) from None
    if not accounts:
        return None, None
    path, model = accounts.expand(acct.get("settings")), accounts.fixed_model(acct)
    if not path:
        return None, model
    try:
        base = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise VoiceError("can't read the login's settings file %s: %s" % (path, type(e).__name__)) from None
    if not isinstance(base, dict):
        raise VoiceError("the login's settings file %s holds no JSON object" % path)
    return base, model


def settings_file(settings, folder=None):
    """(path, remove) of a new private file (0600, in a 0700 folder: SETTINGS_DIR) holding `settings`, for one copy's
    --settings; remove() deletes it, and may be called again. Files there older than STALE_SETTINGS, left by a board
    process that died, are removed first."""
    folder = Path(folder or SETTINGS_DIR)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    for old in folder.glob("copy-*.json"):
        try:
            if time.time() - old.stat().st_mtime > STALE_SETTINGS:
                old.unlink()
        except FileNotFoundError:           # its copy has just ended and removed it
            pass
    fd, path = tempfile.mkstemp(dir=str(folder), prefix="copy-", suffix=".json")     # 0600
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(settings))

    def remove():
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return path, remove


def kept_copy(copies, sid, path, model, now, max_age=COPY_SECONDS):
    """The copy of session sid a question may fork (its id), or None: one made under `max_age` seconds ago on the same
    model, whose transcript is still beside the original's (`path`, on the login asked: so a copy is forked only on
    the login it was made on, which copies.json records), while the original has not grown since."""
    c = copies.get(sid) if isinstance(copies, dict) else None
    if not isinstance(c, dict) or not c.get("copy") or c.get("model") != model or now - (c.get("made") or 0) > max_age:
        return None
    try:
        if path.stat().st_size != c.get("size") or not (path.parent / ("%s.jsonl" % c["copy"])).exists():
            return None
    except OSError:
        return None
    return c["copy"]


def init_refusal(init, tools, servers=(), tools_as=None):
    """(why, fatal) when a claude process's init event `init` (stream-json's system/init: the tools, MCP servers and
    permission mode it runs with) shows other than `tools`, the MCP servers `servers` (each once, connected) and the
    mode dontAsk, else (None, False). Fatal: it holds a tool or server beyond these, or another mode; not fatal: it
    lacks one of them (a server that failed to start, say). `tools_as` names `tools` in the message (their list by
    default). The forks' check (unsafe), and the desk's (desk.py)."""
    got, listed = init.get("tools"), init.get("mcp_servers")
    names = [str(m.get("name") if isinstance(m, dict) else m) for m in listed] if isinstance(listed, list) else None
    extra = sorted(set(map(str, got if isinstance(got, list) else [])) - set(tools))
    if not isinstance(got, list) or extra:
        return "it has tools beyond %s: %s" % (tools_as or ", ".join(tools), ", ".join(extra) or "?"), True
    if names is None or [n for n in names if n not in servers] or len(names) != len(set(names)):
        return "it has MCP servers%s: %s" % (" beyond " + ", ".join(servers) if servers else "",
                                             ", ".join(names or ["?"])), True
    if init.get("permissionMode") != "dontAsk":
        return "its permission mode is %r, not dontAsk" % init.get("permissionMode"), True
    missing = sorted(set(tools) - set(got)) + [s for s in servers if s not in names]
    if missing:
        return "it lacks %s" % ", ".join(missing), False
    down = [m.get("name") for m in listed if isinstance(m, dict) and m.get("status") != "connected"]
    if down:
        return "its MCP server %s is not connected" % ", ".join(map(str, down)), False
    return None, False


def unsafe(init):
    """Why a fork whose init event is `init` is not read-only, or None (init_refusal): a tool beyond READ_TOOLS, any
    MCP server, a mode but dontAsk. A fork may hold fewer tools."""
    why, fatal = init_refusal(init, READ_TOOLS)
    return why if fatal else None


def run_fork(argv, prompt, name, *, cwd, env, timeout, popen=subprocess.Popen, on_text=None, leave=False, ended=None):
    """The result event (stream-json's "result") of one fork, `argv` (fork_argv), asked `prompt` on its stdin. Its first
    event but those in PRE_INIT (the session title its --name sets, notices of the original's background agents) must
    be its init, which is checked (unsafe) before the model can answer, so before any tool runs: a fork that is not
    read-only, or whose first such event is anything else, is stopped there, whatever the claude CLI did with the flags.
    on_text, when given, gets each piece of the answer's text as it comes (fork_argv's `partial`), all after that
    check. With `leave`, it returns at the result, leaving the fork to exit by itself (run_copy): for a fork that keeps
    nothing. `ended` is called once the fork has exited (run_copy). `popen` is subprocess.Popen or a test's stand-in.
    Raises VoiceError, saying `name`'s copy: LimitReached when its reply was its login's usage limit's (usage_limit).
    """
    seen = {"result": None, "checked": False, "limit": None}

    def on_event(event):
        if seen["checked"]:
            if event.get("type") == "result":
                seen["result"] = event
                return leave
            if event.get("type") == "assistant" and not seen["limit"]:
                seen["limit"] = usage_limit(event)
            delta = (event.get("event") or {}).get("delta") if event.get("type") == "stream_event" else None
            if on_text and isinstance(delta, dict) and delta.get("type") == "text_delta" and \
                    isinstance(delta.get("text"), str):
                on_text(delta["text"])
            return False
        if event.get("type") == "result":
            seen["result"] = event                  # it ended before it began (a usage limit): said below, as a failure
            return True
        if (event.get("type"), event.get("subtype")) in PRE_INIT:
            return False
        is_init = event.get("type") == "system" and event.get("subtype") == "init"
        kind = "%r" % event.get("type") + (" (%r)" % event["subtype"] if event.get("subtype") else "")
        why = unsafe(event) if is_init else "its first event was %s, not its init" % kind
        if why:
            raise _stopped(name, why)
        seen["checked"] = True
        return False

    code, other = run_copy(argv, prompt, name, on_event, cwd=cwd, env=env, timeout=timeout, popen=popen, leave=leave,
                           ended=ended)
    result = seen["result"]
    if seen["limit"] and (not result or result.get("is_error")):
        raise LimitReached(name, seen["limit"])
    if code != 0 or not result or result.get("is_error") or not seen["checked"]:
        raise _failed(name, code, (result or {}).get("result") or other)
    return result                                   # a result came only after an init that passed


# ------------------------------------------------------------------ a Codex agent's fork

def codex_catalog(home, model, folder=None):
    """The model catalog a Codex copy runs with (its model_catalog_json): `model`'s entry in Codex's own list (home's
    models_cache.json) with CODEX_MODEL over it, written to `folder` (CODEX_MODELS): its path. Raises VoiceError when
    the list cannot be read or has no such model."""
    try:
        models = json.loads((Path(home) / "models_cache.json").read_text()).get("models")
    except (OSError, ValueError, AttributeError) as e:
        raise VoiceError("could not read Codex's model list in %s: %s" % (home, e)) from None
    entry = next((m for m in models or [] if isinstance(m, dict) and m.get("slug") == model), None)
    if not entry:
        raise VoiceError("Codex's model list in %s has no %s" % (home, model))
    path = Path(folder or CODEX_MODELS) / ("%s.json" % re.sub(r"[^\w.-]", "_", model))
    sources.write_json(path, {"models": [dict(entry, **CODEX_MODEL)]})
    return path


def codex_argv(codex, tid, model, catalog, gate, effort=EFFORT):
    """The command that answers one question from a copy of Codex thread tid that holds no tool (the question goes on
    its stdin), every request it makes sent to the gate at address `gate` (Gate): its model's, by a model provider of
    its own (the built-in one cannot be changed), and ChatGPT's (its account check, plugins, analytics), which the gate
    turns away. Never --trust nor a --dangerously flag."""
    argv = [codex, "exec", "fork", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check"]
    for feature in CODEX_OFF:
        argv += ["--disable", feature]
    provider = '{name="voice desk", base_url=%s, %s}' % (json.dumps(gate + "/v1"), CODEX_PROVIDER)
    for setting in CODEX_CONFIG + ("model_catalog_json=%s" % json.dumps(str(catalog)),
                                   'model_provider="voice-desk"', "model_providers.voice-desk=" + provider,
                                   "chatgpt_base_url=%s" % json.dumps(gate + "/backend-api/")):
        argv += ["-c", setting]
    argv += ["-m", model]
    if effort:
        argv += ["-c", "model_reasoning_effort=%s" % json.dumps(effort)]
    return argv + [tid, "-"]


def codex_env(home):
    """A Codex copy's environment: fork_env's, without the thread of a Codex process the board runs under
    (CODEX_LEAKED), under the agent's CODEX_HOME, and with Codex's own `git status` leaving the agent's index alone."""
    env = {k: v for k, v in fork_env(None).items() if k not in CODEX_LEAKED}
    return dict(env, CODEX_HOME=str(home), GIT_OPTIONAL_LOCKS="0")


def rollout_tools(path):
    """The ids of the tool lists (additional_tools items) a Codex rollout holds: those its compactions recorded among
    them, which a fork of it re-sends as its own conversation's. Raises OSError."""
    ids, tail = set(), b""
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 24), b""):
            ids.update(m.decode() for m in TOOL_LIST.findall(tail + chunk))
            tail = chunk[-200:]
    return ids


def codex_tools(body, history=()):
    """The tools a Codex model request offers the model, as the copy itself declares them: its `tools`, and those of
    each input item that lists tools but the agent's own conversation's (`history`: their ids, rollout_tools). A tool
    in a namespace but Codex's own ("functions") is namespace.name; a hosted tool without a name is its type."""
    names = []

    def walk(tools, space=""):
        for t in tools if isinstance(tools, list) else [tools]:
            if not isinstance(t, dict):
                names.append(repr(t))
            elif t.get("type") == "namespace":
                walk(t.get("tools") or [], "" if t.get("name") == "functions" else "%s." % t.get("name"))
            else:
                names.append(space + str(t.get("name") or t.get("type")))
    walk(body.get("tools") or [])
    for item in body.get("input") or []:
        if isinstance(item, dict) and "tools" in item and item.get("id") not in history:
            walk(item["tools"])
    return names


def codex_results(body):
    """{call id: its result as text} of the tool results a Codex model request carries."""
    out = {}
    for item in body.get("input") or []:
        if isinstance(item, dict) and str(item.get("type", "")).endswith("_output") and "call_id" in item:
            o = item.get("output")
            if isinstance(o, dict):
                o = o.get("content") if isinstance(o.get("content"), (str, list)) else json.dumps(o)
            if isinstance(o, list):
                o = "".join(str(x.get("text") or "") if isinstance(x, dict) else str(x) for x in o)
            out[str(item["call_id"])] = str(o)
    return out


def codex_unsafe(body, history=(), known=None):
    """Why a Codex copy's model request `body` may not reach the model, or None: the copy offers a tool beyond
    CODEX_TOOLS (codex_tools), or the request carries a result of a call the copy made (one not among `known`, those
    of the requests before it; None for the first, whose results are all its conversation's) that is not Codex's
    refusal (CODEX_REFUSED): a tool ran, and what it gave never reaches the model."""
    if not isinstance(body, dict) or not isinstance(body.get("input", []), list):
        return "its request is not one the voice desk knows"
    extra = sorted(set(codex_tools(body, history)) - set(CODEX_TOOLS))
    if extra:
        return "it was offered tools beyond %s: %s" % (", ".join(CODEX_TOOLS), ", ".join(extra))
    ran = sorted(c for c, text in codex_results(body).items()
                 if known is not None and c not in known and not CODEX_REFUSED.fullmatch(text.strip()))
    return "a tool ran in it (call %s), and its result was kept from the model" % ", ".join(ran) if ran else None


class Gate:
    """Where a Codex copy sends every request (codex_argv): a server on 127.0.0.1 for one ask, at `url`. It turns away
    all but model requests (POST /v1/responses), reads each of those and refuses it, before the model sees it, when
    the copy offers a tool or a tool ran in it (codex_unsafe; `history`, the tool lists of the agent's own
    conversation); else sends it on to `upstream` as it came, the copy's own login headers and all, and streams the
    answer back. `refused` says why it refused a request, `passed` counts those sent on, `usage` holds each answer's
    token usage."""

    def __init__(self, upstream, timeout=TIMEOUT, history=()):
        self.upstream, self.timeout, self.history = urllib.parse.urlsplit(upstream), timeout, set(history)
        self.refused, self.passed, self.usage, self.known = None, 0, [], None
        self.lock = threading.Lock()                # one request checked at a time: `known` grows with each
        gate = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):                       # its model list (it has its own: codex_catalog), ChatGPT's
                gate.answer(self, 404, "not here")

            def do_POST(self):
                gate.post(self)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def answer(h, status, message):
        data = json.dumps({"error": {"message": message}}).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def post(self, h):
        if h.path != "/v1/responses":
            return self.answer(h, 404, "not here")
        with self.lock:
            try:
                if h.headers.get("Content-Encoding", "identity") != "identity":
                    raise ValueError("it is %s-encoded" % h.headers["Content-Encoding"])
                raw = h.rfile.read(int(h.headers.get("Content-Length") or 0))
                body = json.loads(raw)
                why = codex_unsafe(body, self.history, self.known)
            except ValueError as e:
                why = "its request could not be read (%s)" % e
            if why:
                self.refused = self.refused or why
            else:
                self.known = (self.known or set()) | set(codex_results(body))
                self.passed += 1
        if why:
            return self.answer(h, 403, "voice desk: " + why)
        self.forward(h, raw)

    def forward(self, h, raw):
        u, started = self.upstream, False
        conn = (http.client.HTTPSConnection(u.netloc, timeout=self.timeout, context=ssl.create_default_context())
                if u.scheme == "https" else http.client.HTTPConnection(u.netloc, timeout=self.timeout))
        try:
            conn.request("POST", u.path.rstrip("/") + "/responses", body=raw,
                         headers={k: v for k, v in h.headers.items() if k.lower() not in HOP})
            r = conn.getresponse()
            h.send_response(r.status)
            h.send_header("Content-Type", r.getheader("Content-Type") or "text/event-stream")
            h.end_headers()
            started, rest = True, b""
            while True:
                chunk = r.read1(65536)
                if not chunk:
                    break
                h.wfile.write(chunk)
                h.wfile.flush()
                *lines, rest = (rest + chunk).split(b"\n")
                for line in lines:
                    self._usage(line)
            self._usage(rest)
        except (OSError, http.client.HTTPException) as e:     # the model's end, or the copy's (it was stopped)
            if not started:
                self.answer(h, 502, "voice desk: the model could not be reached: %s" % e)
        finally:
            conn.close()

    def _usage(self, line):
        """An answer's token usage, from its response.completed event; None when that cannot be read."""
        if line.startswith(b"data:") and b'"response.completed"' in line:
            try:
                usage = json.loads(line[5:])["response"]["usage"]
            except (ValueError, KeyError, TypeError):
                usage = None
            self.usage.append(usage if isinstance(usage, dict) else None)


def run_codex(argv, prompt, name, gate, *, cwd, env, timeout, popen=subprocess.Popen):
    """The answer of one Codex copy, `argv` (codex_argv, its requests sent to `gate`), asked `prompt` on its stdin:
    {"answer", "fork_id"}. The gate checks each request before the model sees it, and a copy it refused is stopped
    at its next event; so is a copy whose events hold anything but CODEX_ITEMS. An answer whose requests never passed
    the gate is not used. Raises VoiceError, saying `name`'s copy."""
    seen = {"answer": None, "done": False, "error": "", "fork_id": None}

    def on_event(event):
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if gate.refused:
            raise _stopped(name, gate.refused)
        if str(event.get("type")).startswith("item.") and item.get("type") not in CODEX_ITEMS:
            raise VoiceError("%s's copy was stopped: it began %r, and may only answer" % (name, item.get("type")))
        if event.get("type") == "thread.started":
            seen["fork_id"] = event.get("thread_id")
        elif event.get("type") == "item.completed" and item.get("type") == "agent_message":
            seen["answer"] = item.get("text") or ""
        elif event.get("type") == "turn.completed":
            seen["done"] = True
        elif event.get("type") in ("turn.failed", "error"):     # an error alone may be a retry: turn.failed is the end
            seen["error"] = (event.get("error") or {}).get("message") or event.get("message") or event["type"]
        return False

    with tempfile.TemporaryFile("w+") as err:      # apart from its events: Codex writes logs there as it goes
        code, other = run_copy(argv, prompt, name, on_event, cwd=cwd, env=env, timeout=timeout, popen=popen,
                               stderr=err)
        err.seek(0)
        other += err.read()[-2000:]
    if gate.refused:
        raise _stopped(name, gate.refused)
    if code != 0 or not seen["done"] or seen["answer"] is None:
        raise _failed(name, code, seen["error"] or other)
    if not gate.passed:
        raise VoiceError("%s's copy answered without its model requests passing the voice desk's check, so its answer "
                         "is not used" % name)
    return seen


# ------------------------------------------------------------------ asking

def too_broad(cwd, agent, rows):
    """Why a copy may not run in `agent`'s folder `cwd`, or None: it is the home folder or above it, or it holds another
    live agent's folder (among `rows`), so a copy there could read all of it or their work."""
    here = Path(cwd).resolve()
    if here in (sources.HOME.resolve(), *sources.HOME.resolve().parents):
        return "the home folder or above it: a copy there could read all of it"
    inside = sorted(a["name"] or a["id"][:8] for a in rows if a["id"] != agent["id"] and a.get("cwd")
                    and here in Path(a["cwd"]).resolve().parents)
    if inside:
        return "which holds other agents' folders (%s): a copy there could read their work" % ", ".join(inside)
    return None


def _folder(agent, name, recorded, rows):
    """The folder a copy of `agent` runs in: its own, else the one its conversation `recorded`. Raises VoiceError when
    it is gone or too broad (too_broad)."""
    cwd = agent.get("cwd") or recorded
    if not cwd or not os.path.isdir(cwd):
        raise VoiceError("%s's folder %s is gone" % (name, cwd or "(unknown)"))
    why = too_broad(cwd, agent, rows)
    if why:
        raise VoiceError("%s runs in %s, %s" % (name, cwd, why))
    return cwd


def _other_logins(home):
    """[(name, config dir)] a copy moves to, in turn, when the login at `home` (the agent's: it holds its newest
    transcript) is at its usage limit: the mailroom's other logins, in its order (fallback_logins); none when `home`
    is not among them (a manual login, such as Gemini's, which bills per token: its agent is asked there alone), or
    without a mailroom that lists them. Raises VoiceError."""
    found = fallback_logins(["claude"]) or []
    if login_key(home) not in [login_key(d) for _, d, _, _ in found]:
        return []
    return [(name, d) for name, d, _, _ in found if login_key(d) != login_key(home)]


def copy_transcript(src, home, sid):
    """Session sid's transcript `src`, with its session folder, copied to the login whose folder is `home` (None: the
    default), as the mailroom copies one for a resume on another login (claude_session.copy_transcript, which refuses
    to overwrite a newer copy): the copy's path. Raises VoiceError."""
    try:
        return Path(sources.mailroom("claude_session").copy_transcript(
            Path(src), Path(home) if home else sources.DEFAULT_CLAUDE_HOME, sid))
    except (RuntimeError, ImportError, OSError, ValueError) as e:
        raise VoiceError("could not copy the conversation to %s: %s" % (home or sources.DEFAULT_CLAUDE_HOME,
                                                                        sources.error_text(e))) from None


def _ask_claude(agent, name, question, rows, *, popen, claude_bin, homes, model, effort, timeout, copies_path, started,
                on_text):
    """The question to a copy on the login holding the agent's newest transcript; when that login is at its usage
    limit, on each of the mailroom's other logins in turn (_other_logins), the transcript copied there first
    (copy_transcript). Its answer says the login ("login") and those at their limit before it ("limited")."""
    own, config_dir, path = sources.newest_transcript(agent["id"], homes)
    if path is None:
        raise VoiceError("%s has no transcript to copy yet" % name)
    facts = transcript_facts(path)
    cwd = _folder(agent, name, facts["cwd"], rows)
    claude = claude_bin or sources.find_bin("claude")
    if not claude:
        raise VoiceError("the claude CLI is not installed here")

    def on(login, home, transcript):
        return dict(_ask_on(agent, name, question, home, transcript, facts, claude=claude, cwd=cwd, popen=popen,
                            model=model, effort=effort, timeout=timeout, copies_path=copies_path, started=started,
                            on_text=on_text), login=login)
    try:
        return on(own, config_dir, path)
    except LimitReached as e:
        limited = ["%s: %s" % (own, e.hit.get("text"))]
    for login, home in _other_logins(config_dir):
        try:
            return dict(on(login, home, copy_transcript(path, home, agent["id"])), limited=limited)
        except LimitReached as e:
            limited.append("%s: %s" % (login, e.hit.get("text")))
    raise VoiceError("%s's copy got no answer: %s login is at its usage limit (%s)" % (
        name, "its" if len(limited) == 1 else "each", "; ".join(limited)))


def _ask_on(agent, name, question, config_dir, path, facts, *, claude, cwd, popen, model, effort, timeout, copies_path,
            started, on_text):
    """The question to a copy of the agent on the login whose folder is config_dir, which holds its transcript `path`
    (facts: transcript_facts of it), through a copy kept there (kept_copy). Raises VoiceError, LimitReached when that
    login is at its usage limit."""
    base, pinned = login_settings(config_dir)          # a per-token login: its settings, and its one model
    use_model = pinned or model or facts["model"]
    size = path.stat().st_size
    env = fork_env(config_dir)

    def fork(sid, prompt, keep=False):          # a kept copy must exit by itself: its transcript is for later forks
        settings, remove = settings_file(fork_settings(base)) if base is not None else (None, None)
        try:
            return run_fork(fork_argv(claude, sid, agent["name"], use_model, effort, keep=keep,
                                      partial=bool(on_text) and not keep, settings=settings),
                            PREAMBLE + prompt, name, cwd=cwd, env=env, timeout=timeout, popen=popen,
                            on_text=None if keep else on_text, leave=not keep, ended=remove)
        except BaseException:
            if remove:
                remove()                        # stopped: the copy is not running any more
            raise
    copies = sources.read_cache(copies_path, {}, lambda m: None) if copies_path else {}
    copy = kept_copy(copies, agent["id"], path, use_model, started)
    made = copies_path and not copy
    if made:
        copy = fork(agent["id"], PRIMER, keep=True).get("session_id")
        if not copy:
            raise VoiceError("%s's new copy gave no session id to fork" % name)
        copies[agent["id"]] = {"copy": copy, "made": started, "size": size, "model": use_model,
                               "login": str(config_dir or sources.DEFAULT_CLAUDE_HOME)}
        sources.write_json(copies_path, copies)
    res = fork(copy or agent["id"], question)
    usage = res.get("usage") or {}
    first = (usage.get("iterations") or [usage])[0]
    return {"model": use_model, "copy": "%s %s" % ("made" if made else "reused", copy[:8]) if copy else "none",
            "context_tokens": facts["context_tokens"], "answer": (res.get("result") or "").strip(),
            "usage": {k: usage.get(k) for k in USAGE},
            "first_request": {k: first.get(k) for k in USAGE[1:3]},
            "denials": len(res.get("permission_denials") or []), "turns": res.get("num_turns"),
            "fork_id": res.get("session_id")}           # not its total_cost_usd: that carries on the original's


def _codex_usage(u):
    """A Codex model answer's usage as a Claude reply's (USAGE): its input_tokens count its cached ones too."""
    d = u.get("input_tokens_details") or {}
    cached = d.get("cached_tokens") or 0
    return {"input_tokens": (u.get("input_tokens") or 0) - cached, "cache_creation_input_tokens":
            d.get("cache_write_tokens"), "cache_read_input_tokens": cached, "output_tokens": u.get("output_tokens")}


def _ask_codex(agent, name, question, rows, *, popen, codex_bin, model, effort, timeout, upstream):
    home = agent.get("config_dir") or sources.CODEX_HOME
    try:
        rollout = sources.codex_thread(agent["id"], home).get("rollout_path")
    except sqlite3.Error as e:
        raise VoiceError("could not read Codex's threads in %s: %s" % (home, e)) from None
    path = Path(rollout) if rollout else None
    if path is None or not path.exists():
        raise VoiceError("%s has no conversation to copy yet" % name)
    facts = rollout_facts(path)
    cwd = _folder(agent, name, facts["cwd"], rows)
    codex = codex_bin or sources.find_bin("codex")
    if not codex:
        raise VoiceError("the codex CLI is not installed here")
    use_model = model or agent.get("model") or facts["model"]
    if not use_model:
        raise VoiceError("%s's model is not known" % name)
    catalog = codex_catalog(home, use_model)
    try:
        history = rollout_tools(path)
    except OSError as e:
        raise VoiceError("could not read %s's conversation: %s" % (name, e)) from None
    with Gate(upstream, timeout, history) as gate:
        res = run_codex(codex_argv(codex, agent["id"], use_model, catalog, gate.url, effort), CODEX_PREAMBLE + question,
                        name, gate, cwd=cwd, env=codex_env(home), timeout=timeout, popen=popen)
    per = [_codex_usage(u) if u else dict.fromkeys(USAGE) for u in gate.usage]     # an unread one: unknown, None
    return {"model": use_model, "copy": "none", "context_tokens": facts["context_tokens"],
            "answer": res["answer"].strip(),
            "usage": {k: None if not per or any(p[k] is None for p in per) else sum(p[k] for p in per) for k in USAGE},
            "first_request": {k: per[0][k] if per else None for k in USAGE[1:3]},
            "denials": None, "turns": gate.passed, "fork_id": res["fork_id"]}


def ask(target, question, *, popen=subprocess.Popen, claude_bin=None, rows=None, homes=None, model=None,
        effort=EFFORT, timeout=TIMEOUT, log=ASKS, copies_path=COPIES, now=time.time, codex_bin=None,
        upstream=CODEX_UPSTREAM, on_text=None):
    """Ask agent `target` (resolve) `question` through a read-only fork: {"ok", "agent", "id", "cli", "model", "copy",
    "context_tokens", "answer", "seconds", "usage", "first_request", "denials", "turns", "fork_id"}, and for a Claude
    agent "login" (the login that answered) and, when it is not the agent's own, "limited" (["login: the limit's
    text"] for each at its usage limit before it); or {"ok": False, "error"}. `popen` is subprocess.Popen or a
    stand-in for the claude or codex CLI. Each ask is appended to `log`.
    on_text, when given, gets a Claude copy's answer piece by piece as it is written (run_fork); a Codex copy's comes
    whole, in the answer only, as `codex exec --json` gives no pieces (Codex 0.172.0).

    A Claude agent's question goes to run_fork. With `copies_path`, it forks a kept copy of the agent's conversation
    (kept_copy), so the conversation is read from the prompt cache. A new copy is made by a fork asked only PRIMER, so a
    copy holds none of the owner's questions (nor anything a question carried) for later questions to meet; copies_path
    lists the copies. With None, the question forks the agent's conversation itself and nothing is kept. When the
    login holding the agent's newest transcript is at its usage limit, the question goes to each of the mailroom's other
    logins in turn (_ask_claude): the agent's transcript, with its session folder, is copied into that login's projects
    folder (copy_transcript), and the kept copy is made there and recorded in copies_path with that login.

    A Codex agent's question forks its conversation itself (run_codex), its model requests checked by a Gate that sends
    them on to `upstream`: there is no kept copy, and no denials (None). It reads the agent's CODEX_HOME (its threads,
    its rollout, Codex's model list) and writes the copy's model catalog to CODEX_MODELS (codex_catalog)."""
    started = now()
    try:
        question = (question or "").strip()
        if not question:
            raise VoiceError("no question to ask")
        if len(question) > MAX_QUESTION:
            raise VoiceError("the question is over %d characters" % MAX_QUESTION)
        rows = rows if rows is not None else agents()
        agent = resolve(target, rows)
        name = agent["name"] or agent["id"]
        if agent.get("cli") == "codex":
            res = _ask_codex(agent, name, question, rows, popen=popen, codex_bin=codex_bin, model=model,
                             effort=effort, timeout=timeout, upstream=upstream)
        else:
            res = _ask_claude(agent, name, question, rows, popen=popen, claude_bin=claude_bin, homes=homes,
                              model=model, effort=effort, timeout=timeout, copies_path=copies_path, started=started,
                              on_text=on_text)
        out = dict({"ok": True, "agent": agent["name"], "id": agent["id"], "cli": agent.get("cli") or "claude"}, **res,
                   seconds=round(now() - started, 1))
    except VoiceError as e:
        out = {"ok": False, "error": str(e), "seconds": round(now() - started, 1)}
    if log:
        sources.append_jsonl(log, dict(out, ts=started, target=target, question=question, effort=effort))
    return out


# ------------------------------------------------------------------ what the owner is looking at

def mac_host():
    """The ssh alias of the owner's Mac: VOICE_DESK_MAC when set (empty: none), else config.mac_host() (the mailroom
    config's remote_hosts.mac); None when no Mac is configured."""
    env = os.environ.get("VOICE_DESK_MAC")
    if env is not None:
        return env.strip() or None
    return config.mac_host()


def mac_front(run=sources.run, host=None, script=MAC_SCRIPT, timeout=25):
    """The Mac's latest windows as its ActivityWatch has them (aw_front.py --aw, run there over ssh, by the alias `host`
    or mac_host(), from this checkout's copy of the script): its dict, {"ok": False, "error"} when the Mac cannot be
    reached (asleep, lid shut) or none is configured (NO_MAC, and then no ssh is run)."""
    host = host or mac_host()
    if not host:
        return {"ok": False, "error": NO_MAC}
    code, out, err = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, "python3", "-", "--aw"],
                         input=Path(script).read_text(), timeout=timeout)
    if code == 0:
        try:
            return json.loads(out)
        except ValueError:
            pass
    return {"ok": False, "error": "could not read the Mac's front window (ssh %s, exit %s): %s" % (
        host, code, (err or out).strip()[-200:] or "no answer: it may be asleep")}


def voice_page(event):
    """Whether an ActivityWatch window or tab event is the voice page's own."""
    return (event.get("title") or "").startswith(VOICE_TITLE)


def front_window(seen):
    """{app, title, url, at, afk} from what aw_front gave: the newest window that is not the voice page's (its app and
    title) and, for a browser, the newest tab that is not the voice page's (its address; the window watcher gives one
    for some browsers itself)."""
    w = next((e for e in seen.get("windows") or [] if not voice_page(e)), {})
    web = next((e for e in seen.get("web") or [] if not voice_page(e)), {})
    app, title, url = w.get("app") or "", w.get("title") or "", w.get("url") or ""
    if not url and web.get("url") and any(b.lower() in app.lower() for b in BROWSERS):
        url = web["url"]
    return {"app": app, "title": title, "url": url, "at": w.get("timestamp"), "afk": seen.get("afk")}


def _mail_db(path):
    """A read-only connection to the mailroom's store at `path`, or None when there is none. Raises VoiceError when
    it cannot be read."""
    if not Path(path).exists():
        return None
    try:
        return sources.ro_sqlite(path)
    except sqlite3.Error as e:
        raise _unreadable(path, e) from None


def _unreadable(path, e):
    return VoiceError("could not read the mailroom's store %s: %s" % (path, e))


def by_tag(tags, db=sources.MAIL_DB):
    """The session that owns the first of these mailroom tags: {"cli", "id", "name", "host", "tag"} or None (None too
    when there is no mailroom store). Raises VoiceError when the store cannot be read."""
    con = _mail_db(db)
    if con is None:
        return None
    try:
        for tag in tags:
            r = con.execute("select cli, session_id, session_name, origin_host from sessions where tag = ?",
                            (tag,)).fetchone()
            if r:
                return {"cli": r["cli"], "id": r["session_id"], "name": r["session_name"] or "",
                        "host": r["origin_host"] or "", "tag": tag}
    except sqlite3.Error as e:
        raise _unreadable(db, e) from None
    finally:
        con.close()
    return None


def bare_subject(s):
    return TAG.sub("", SUBJECT_PREFIX.sub("", GMAIL_SUFFIX.sub("", s or ""))).strip()


def by_subject(title, db=sources.MAIL_DB, days=60):
    """The session that last sent agent mail with this subject (a Mail-tab window is titled with the subject alone):
    as by_tag gives it, or None. Only an exact match, Re: and the tag aside. Raises VoiceError as by_tag does."""
    want = bare_subject(title)
    con = _mail_db(db) if len(want) >= 8 else None
    if con is None:
        return None
    try:
        rows = con.execute("select t.tag, t.subject from sent t where t.sent_at >= ? order by t.id desc",
                           (int(time.time() - days * 86400),)).fetchall()
        tag = next((r["tag"] for r in rows if bare_subject(r["subject"]) == want), None)
    except sqlite3.Error as e:
        raise _unreadable(db, e) from None
    finally:
        con.close()
    return by_tag([tag], db) if tag else None


def pull_request(url, title):
    """(owner/repo, number) of the GitHub pull request in the address or the tab title, or None."""
    m = PR_URL.search(url or "")
    if m:
        return "%s/%s" % (m.group(1), m.group(2)), int(m.group(3))
    m = PR_TITLE.search(title or "")
    return ("%s/%s" % (m.group(2), m.group(3)), int(m.group(1))) if m else None


def pr_branch(slug, number, run=sources.run):
    """The PR's head branch and title, from gh: (branch, title), or (None, why)."""
    gh = sources.find_bin("gh")
    if not gh:
        return None, "gh is not installed"
    code, out, err = run([gh, "pr", "view", str(number), "--repo", slug, "--json", "headRefName,title"], timeout=20)
    try:
        d = json.loads(out) if code == 0 else None
    except ValueError:
        d = None
    if not d or not d.get("headRefName"):
        return None, "gh pr view failed: %s" % (err or out).strip()[-200:]
    return d["headRefName"], d.get("title") or ""


def checkout(cwd, run=sources.run):
    """(owner/repo, branch) checked out in a folder, from git: ("", "") when it is not a git checkout."""
    code, out, _ = run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    branch = out.strip() if code == 0 else ""
    code, out, _ = run(["git", "-C", cwd, "config", "--get", "remote.origin.url"], timeout=10)
    return (sources.github_slug(out.strip()) if code == 0 else ""), branch


def tmux_front(run=sources.run):
    """(session name, "session:@window.%pane") of the desktop tmux client most recently typed in, from `tmux
    list-clients` (read-only), or None without tmux, a tmux server or a client. Raises VoiceError when tmux fails
    otherwise or does not answer in 3 s."""
    tmux = sources.find_bin("tmux")
    if not tmux:
        return None
    code, out, err = run([tmux, "list-clients", "-F", "#{client_activity}\t#{session_name}\t#{window_id}\t#{pane_id}"],
                         timeout=3)
    if code != 0:
        if any(w in err for w in sources.TMUX_NO_SERVER):
            return None
        raise VoiceError("could not ask the desktop's tmux which session was typed in last (exit %d): %s" % (
            code, err.strip()[-200:]))
    best = None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[0].isdigit() and (best is None or int(parts[0]) > best[0]):
            best = (int(parts[0]), parts[1], "%s:%s.%s" % tuple(parts[1:]))
    return best[1:] if best else None


def by_tmux(rows, run=sources.run):
    """The live agent in the tmux pane, else alone in the tmux session, most recently typed in (tmux_front): (agent
    row, session name) or (None, why). Raises VoiceError as tmux_front does."""
    front = tmux_front(run)
    if not front:
        return None, "no tmux client on the desktop"
    session, pane = front
    hits = [a for a in rows if a.get("tmux") == pane]
    if not hits:
        hits = [a for a in rows if sources.tmux_name(a.get("tmux")) == session] or \
               [a for a in rows if a["name"] and a["name"] == session]
    if len(hits) == 1:
        return hits[0], session
    return None, ("tmux session %s holds %d live agents" % (session, len(hits)) if hits
                  else "tmux session %s has no live agent" % session)


def who_is_behind(win, rows, db=sources.MAIL_DB, run=sources.run, host=None):
    """The agent behind a front window (front_window) among the live agents rows (agents()): {"name", "id", "live",
    "how", ...} or None, and a note on what was looked for. It reads the mailroom's store `db` (by_tag, by_subject), for
    a pull request runs `gh pr view` (GitHub) and git in each live agent's folder, and for a kitty window asks tmux
    (tmux_front). Raises VoiceError when the store or tmux cannot be read."""
    host = host or sources.mail_host()
    live = {a["id"]: a for a in rows}
    shown = BOARD_SESSION.search(win.get("title") or "")
    if shown:
        a = live.get(shown.group(1))
        if a:
            return {"cli": a.get("cli") or "claude", "id": a["id"], "name": a["name"], "live": True,
                    "how": "selection on the board"}, ""
        # not live here (ended, or on the Mac): its id still goes out, so an excluded agent's is refused (voiceweb)
        return {"cli": "", "id": shown.group(1), "name": "", "live": False, "how": "selection on the board",
                "note": "it is not a live agent here"}, ""
    text = " ".join([win.get("title") or "", win.get("url") or ""])
    tags = TAG.findall(text)
    found = by_tag(tags, db) if tags else None
    how = "mail tag [#%s]" % found["tag"] if found else None
    if not found and not PR_URL.search(win.get("url") or ""):
        found = by_subject((win.get("title") or "").removeprefix(BOARD_MAIL), db)
        how = "mail subject" if found else None
    if found:
        here = found["host"] in ("", host)
        return dict(found, live=here and found["id"] in live, how=how,
                    note="" if here else "that agent runs on %s, not the desktop" % found["host"]), ""
    pr = pull_request(win.get("url"), win.get("title"))
    if pr:
        slug, number = pr
        branch, why = pr_branch(slug, number, run)
        if not branch:
            return None, "pull request %s#%d: %s" % (slug, number, why)
        for a in rows:
            here = checkout(a["cwd"], run) if a["cwd"] else ("", "")
            if here[1] == branch and here[0].lower() == slug.lower():
                return {"cli": a.get("cli") or "claude", "id": a["id"], "name": a["name"], "live": True,
                        "how": "pull request %s#%d on branch %s" % (slug, number, branch)}, ""
        return None, "pull request %s#%d is on branch %s, and no live agent has it checked out" % (slug, number, branch)
    title = (win.get("title") or "").lower()
    named = [a for a in rows if len(a["name"]) >= 6 and re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(a["name"].lower()),
                                                                   title)]
    if len(named) == 1:
        a = named[0]
        return {"cli": a.get("cli") or "claude", "id": a["id"], "name": a["name"], "live": True,
                "how": "name in the title"}, ""
    if any(t in (win.get("app") or "").lower() for t in TERMINALS):
        a, why = by_tmux(rows, run)
        if a:
            return {"cli": a.get("cli") or "claude", "id": a["id"], "name": a["name"], "live": True,
                    "how": "tmux session %s, the one last typed in" % why}, ""
        return None, why
    return None, ""


def looking(seen=None, *, rows=None, db=sources.MAIL_DB, run=sources.run, mac=mac_front):
    """What the owner is looking at and who is behind it: {"ok", "window", "agent", "said", "warnings"}, `said` one or
    two sentences for speech, `warnings` what ActivityWatch failed to read besides the window; or {"ok": False,
    "error"}. `seen` is what aw_front gave on the Mac (a test gives its own); None reads it from the Mac here, over ssh
    (mac_front: none when no Mac is configured). It reads and runs what who_is_behind does."""
    started = time.time()
    seen = seen if seen is not None else mac(run=run)
    if not seen.get("ok"):
        return {"ok": False, "error": seen.get("error") or "no front window", "said": seen.get("error") or "",
                "seconds": round(time.time() - started, 1)}
    win = front_window(seen)
    try:
        rows = rows if rows is not None else agents()
        agent, note = who_is_behind(win, rows, db=db, run=run)
    except VoiceError as e:
        return {"ok": False, "error": str(e), "said": str(e), "seconds": round(time.time() - started, 1)}
    shown = bare_subject(BOARD_SESSION.sub("", win["title"]))   # for speech: no tags, no "- address - Gmail"
    where = win["app"] + (": " + shown if shown else "")
    if agent and agent.get("live"):
        said = "You're looking at %s. Behind it: %s (matched by its %s)." % (where, agent["name"] or agent["id"],
                                                                            agent["how"])
    elif agent:
        said = "You're looking at %s. It belongs to %s, which is not live here%s." % (
            where, agent["name"] or agent["id"], (": " + agent["note"]) if agent.get("note") else "")
    else:
        said = "You're looking at %s. No agent found behind it%s." % (where, (" (" + note + ")") if note else "")
    return {"ok": True, "window": win, "agent": agent, "note": note, "said": said,
            "warnings": seen.get("errors") or [], "seconds": round(time.time() - started, 1)}


def show_agents(rows):
    if not rows:
        print("no live agents")
    for a in rows:
        print("%-40s %-6s %-5s %s  %s" % (a["name"] or "(unnamed)", a.get("cli") or "claude", a["status"], a["id"][:8],
                                          a["cwd"]))


def show_answer(res):
    if not res["ok"]:
        print("could not ask: %s" % res["error"])
        return
    print(res["answer"])
    u = res["first_request"]
    print("\n-- %s's read-only copy, %s, context %s tokens, %.1f s (cache read %s, written %s)" % (
        res["agent"], res["model"], res["context_tokens"], res["seconds"], u.get("cache_read_input_tokens"),
        u.get("cache_creation_input_tokens")))


def show_looking(res):
    print(res["said"] if res["ok"] else "could not tell: %s" % res["error"])
    for warning in res.get("warnings") or []:
        print("(ActivityWatch: %s)" % warning)
