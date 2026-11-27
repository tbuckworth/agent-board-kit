"""The desk: the owner's always-running agent, talked to from the Mac's ask box (README, "The desk").

`agent-board desk serve` keeps one long-lived headless Claude process (`claude -p` in stream-json, on a fast model at
low effort: settings()), so nothing starts up per question and the conversation is remembered, and serves requests on a
Unix socket (the settings' `socket`, mode 0600, for the owner's uid only, in a folder of its own made 0700). A request
is one JSON line:

  {"text": T, "looking": L?}    a turn: the owner's words T. L is what is on the Mac's screen: what voicedesk.looking
                                gave (the ask box's voice_looking), or true for the desk to read it itself first
  {"op": "new"}                 a fresh conversation

Its answer is JSON lines: {"type": "waiting"} when it waits behind another turn (one runs at a time), {"type": "delta",
"text"} as the reply streams, {"type": "tool", "name", "input"} as the desk calls a tool, and last {"type": "final",
"ok", "text", "error"?, "screen_error"?, "timings", "tools", "refused", "failed", "sends", "context_tokens"}: timings in
seconds from the request's arrival (first_delta, first_tool, end; waited, behind another turn; api, Claude's own);
refused, the tools Claude refused; failed, those that answered with an error; screen_error, why the owner's screen could
not be read. turns.jsonl beside the socket keeps each turn, with the model and effort; the log (stderr) has a line per
turn, and a warning for each restart and each refused or failed tool. The MCP server logs to MCP_LOG beside the socket.

The Claude process (claude_argv) is held as voicedesk's forks are. Its tools are exactly BUILTIN and the desk's own; the
permission mode is dontAsk with nobody to ask (--permission-prompts none); --restricted ignores the login's settings
files (none of its plugins, hooks, allow rules or CLAUDE.md load) and keeps the file tools to read_dirs (the repos
folder by default) and the socket's folder, where secret-looking files are still refused (voicedesk.SECRET_READS); hooks
are off, cross-session messages refused, and it is named "desk" (a `claude -p` run: the board shows no tile for it, and
voicedesk does not offer it). Each turn's init event must come before the model's first output, and list exactly these
tools, the one MCP server "desk" (connected) and dontAsk (unsafe, through voicedesk.init_refusal); else the process is
stopped there, before its model answers, and the daemon exits (EXIT_UNSAFE) when the process held more than these.
Nothing is persisted: a process that dies is started again with a new conversation, as is one whose context passes
context_tokens, and each start's primer (PRIMER) checks its init before any question. Its system prompt is
desk-prompt.md with the owner's name (the mailroom config's owner_name) and read_dirs filled in, written to PROMPT_FILE
beside the socket at each start.

Its login is one of the mailroom's Claude logins, which the mailroom's own headless runs (the orchestrator, the pre-send
review) fall through in the same order (voicedesk.fallback_logins: headless.logins, never a manual login such as
Gemini's), each with its own options and scrubbed environment; claude_config_dir, when set, names one to try first. A
primer or a turn whose reply is its login's usage limit's (voicedesk.usage_limit: the mailroom's failover.limit_record)
moves the desk to the next login, then round to those before it (_fresh's `after`); that reply is never streamed nor
given as the answer, and a turn that finds no login with room fails with the limits' text. A turn that had done nothing
yet (no tool, no word) is asked again there, so the owner gets an answer; one that had is said
to have failed, as it may have acted (a send). Only a start afresh for another reason (a new conversation, a process
that died, a long context) begins at the first login again, so the desk does not flap between logins.

The desk's own tools are an MCP server over stdio, `agent-board desk mcp` (mcp_serve, Tools), which the daemon hands to
Claude with --strict-mcp-config: agents, recent, ask, looking, send, mail_list and mail_show (TOOLS). All but send only
read; mail goes through the mailroom's CLI (agent-mail inbox, sent and show) and none is ever sent. send goes back to
the daemon over its socket with a token it gave the server (DESK_TOKEN). The token is in the server's environment,
set in the MCP config the daemon writes to MCP_FILE beside its socket (mode 0600) at each start of Claude, whose
settings forbid it to read that file, and not on Claude's command line, which every process can read. The daemon
sends only when send_refusal finds nothing against it:

- the turn is running, is the owner's (a request on the socket, not a primer), and its init was checked;
- the owner's words in it ask for a send (SEND_WORDS: tell, ask, send, message, remind, ...); "ask" arms a send as well
  as the read-only copy, so "ask loader-fix to rebase" can go, and the system prompt steers a question to `ask`; or they
  are a short yes (YES_WORDS) to the desk's answer before, read out to the owner, whose last sentence asked whether to
  send (accepted_offer: "Shall I tell loader-fix?");
- the agent is one the owner's words name, in this request or the last two (mentions), or the offer the owner says yes
  to names, or the one behind the owner's screen in it;
- it has not been sent to in this turn already.

Untrusted text (transcripts, mail, web pages) can still shape what is sent to an agent the owner named in a turn that
asks for a send, or that an offer the owner said yes to named. So the message carries the owner's own words to the desk
(said_line), with the offer they said yes to (Turn.said), under the desk's text, goes through the board's own send path
(actions.dispatch's send: the header, the footer and its ref), is on the record in actions.jsonl and sent.jsonl as
sent by the owner via the desk, and comes back to the caller in the turn's final event ("sends").
"""
import collections
import json
import logging
import os
import queue
import re
import secrets
import signal
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import actions, classify, collect, config, groups, relaunch, sent, sources, voicedesk

log = logging.getLogger("desk")

DEFAULTS = Path(__file__).with_name("desk.json")
CONFIG = Path(os.environ.get("AGENT_BOARD_DESK_CONFIG") or sources.HOME / ".config/agent-board/desk.json")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
NAME = "desk"                         # its session's name (--name)
SERVER = "desk"                       # its MCP server's name: the tools are mcp__desk__<tool>
WEB_TOOLS = ("WebSearch",)            # and not WebFetch: the README's desk section says why
BUILTIN = voicedesk.READ_TOOLS + WEB_TOOLS
DENIED = tuple(t for t in voicedesk.DENIED_TOOLS if t not in WEB_TOOLS + ("mcp__*",))   # WebFetch among them
# the forks' settings (hooks off, cross-session messages refused, secret-looking files denied), and the desk's tools
# allowed: in dontAsk, a tool not allowed is refused
MCP_FILE = "mcp.json"                # its MCP config, holding the token, beside the socket: Claude may not read it
PROMPT_FILE = "prompt.md"            # its system prompt, filled in (prompt_text), beside the socket
SETTINGS = dict(voicedesk.FORK_SETTINGS, permissions={
    "deny": list(voicedesk.SECRET_READS) + ["Read(**/%s)" % MCP_FILE], "allow": ["mcp__" + SERVER] + list(WEB_TOOLS)})
ANSWERING = ("stream_event", "assistant", "user")     # the model's output and tool results: never before the init
PRIMER = "[The desk has just started; this is not the owner. Reply with just: ready]"
EXIT_UNSAFE = 3                       # serve's exit when Claude held more than the desk's tools (the unit: no restart)
MAX_REQUEST = 64 * 1024               # bytes of one request line
MAX_TEXT = actions.MAX_TEXT           # characters of the owner's words in a turn, and of a message sent
SAID_CHARS = 1000                     # characters of the owner's words quoted under a message sent
TOOL_CHARS = 8000                     # characters of one tool's answer
NOTE_CHARS, WHY_CHARS, MESSAGE_CHARS = 90, 60, 700
AGENT_CHARS = 100                     # characters of an agent's name in a call
MCP_LOG = "mcp.log"                   # the MCP server's log, beside the socket
HISTORY = 2                           # the owner's earlier requests whose words may name the agent of a send
START_BACKOFF = (5, 60)               # seconds before the next start after one fails: the first, and at most
MOVES = 3                             # moves to another login, at most, in one turn whose reply was a usage limit's
SEND_WORDS = re.compile(r"\b(tell|ask|send|message|remind|nudge|ping|relay|forward|instruct|reply"
                        r"|let\b.{1,60}\bknow)\b", re.I | re.S)
SAID_FMT = "\n\n({owner} said this to the desk, which wrote the message above: \"%s\")"
OWNER_CHARS = 40                      # characters of the owner's name in a message sent, at most
SAID_ROOM = len(SAID_FMT) + OWNER_CHARS     # what said_line adds to a message, at most, besides the quoted words
YES_WORDS = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|do that|send it|please do)\b", re.I)
YES_CHARS = 40                        # characters at most of words that are a yes to the desk's offer to send
OFFER_CHARS = 600                     # characters of that offer quoted with the yes under the message sent
INIT_WAIT = 5                         # seconds a send waits for its turn's init, printed already, to be checked


class DeskError(Exception):
    """What went wrong with a turn, said for the owner."""


class Unsafe(DeskError):
    """The Claude process held more than the desk's tools: the daemon stops."""


class ToolError(Exception):
    """A tool's failure, given to the model as its result."""


# ------------------------------------------------------------------ settings

def settings(path=None, defaults=DEFAULTS):
    """desk.json (`defaults`) with the file at `path`'s overrides (CONFIG by default), checked, its paths expanded
    (system_prompt is relative to this folder). claude_config_dir null is the default login (None); read_dirs null is
    the repos folder (config.repos_root). Raises ValueError for a bad or unknown setting."""
    path = path or CONFIG
    cfg = sources.load_settings(defaults, path)

    def bad(name, what):
        raise ValueError("%s: %s must be %s" % (path, name, what))

    def whole(name, lo, hi):
        v = cfg[name]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            bad(name, "a whole number from %d to %d" % (lo, hi))

    def a_path(v, absolute=True):
        return isinstance(v, str) and v.strip() and "\x00" not in v and (
            not absolute or os.path.isabs(os.path.expanduser(v)))
    if not (isinstance(cfg["model"], str) and re.fullmatch(r"[a-z0-9][a-z0-9.\[\]-]{1,80}", cfg["model"])):
        bad("model", "a Claude model's name, such as claude-sonnet-5-5")
    if cfg["effort"] not in EFFORTS:
        bad("effort", "one of " + ", ".join(EFFORTS))
    if not a_path(cfg["socket"]):
        bad("socket", "an absolute path (~ is fine)")
    if cfg["claude_config_dir"] is not None and not a_path(cfg["claude_config_dir"]):
        bad("claude_config_dir", "null or a Claude login's folder, an absolute path (~ is fine)")
    if not a_path(cfg["system_prompt"], absolute=False):
        bad("system_prompt", "a path (relative to agentboard/)")
    if cfg["read_dirs"] is None:
        cfg["read_dirs"] = [str(config.repos_root())]
    if not (isinstance(cfg["read_dirs"], list) and len(cfg["read_dirs"]) <= 10 and all(map(a_path, cfg["read_dirs"]))):
        bad("read_dirs", "null (the repos folder) or a list of at most 10 absolute folders")
    whole("context_tokens", 20000, 1000000)
    whole("turn_seconds", 30, 3600)
    if len(os.fsencode(os.path.expanduser(cfg["socket"]))) > 107:
        bad("socket", "a path of at most 107 bytes (a Unix socket's limit)")
    first = cfg["claude_config_dir"]
    return dict(cfg, claude_config_dir=os.path.expanduser(first) if first else None,
                socket=os.path.expanduser(cfg["socket"]),
                system_prompt=str(Path(__file__).parent / os.path.expanduser(cfg["system_prompt"])),
                read_dirs=[os.path.expanduser(d) for d in cfg["read_dirs"]])


def owner():
    """The owner's first name for what the desk and the agents read (the mailroom config's owner_name), cut to
    OWNER_CHARS; "the owner" when none is set."""
    return _cut(config.owner_name(), OWNER_CHARS)


def capital(text):
    """`text` with its first letter a capital ("the owner's screen" at the start of a line)."""
    return text[:1].upper() + text[1:]


def prompt_text(cfg, name=None):
    """The desk's system prompt: the file cfg["system_prompt"] with {owner} the owner's name (owner(), or `name`) and
    {read_dirs} the folders its file tools may read. Raises OSError when the file cannot be read."""
    text = Path(cfg["system_prompt"]).read_text()
    dirs = ", ".join(sources.tilde(d) for d in cfg["read_dirs"]) or "none"
    return text.replace("{owner}", name or owner()).replace("{read_dirs}", dirs)


def said_line(said, name=None):
    """What goes under a message the desk sends: the owner's words to the desk (`said`, cut to SAID_CHARS), naming the
    owner (owner(), or `name`)."""
    return SAID_FMT.replace("{owner}", name or owner()) % _cut(said, SAID_CHARS).replace('"', "'")


# ------------------------------------------------------------------ the agents

def desk_rows(rows, board, where):
    """The live agents the desk knows: voicedesk.agents()'s `rows` (those one can ask), each named as the board names
    it when it has no name of its own, with the board's "state", "why" and "note" (`board`: the collector's rows) and
    its "group" (`where`: groups.members())."""
    by = {b.get("id"): b for b in board}
    out = []
    for a in rows:
        b = by.get(a["id"]) or {}
        out.append(dict(a, name=a["name"] or b.get("display") or "", state=b.get("state") or a.get("status") or "?",
                        why=b.get("reason") or "", note=b.get("last_note") or None, group=where.get(a["id"], "")))
    return out


def _words(s):
    """Text as spoken words joined by hyphens, between hyphens: "Tell loader fix!" gives "-tell-loader-fix-"."""
    return "-%s-" % re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def accepted_offer(words, answer):
    """The desk's `answer` before, read out to the owner, when its last sentence asked whether to send (a question with
    a SEND_WORDS word: "Shall I tell loader-fix?") and the owner's `words` now are a short yes to it (YES_WORDS,
    YES_CHARS); else None."""
    last = re.split(r"(?<=[.!?])\s+", (answer or "").strip())[-1]
    if (last.endswith("?") and SEND_WORDS.search(last) and len(words.strip()) <= YES_CHARS
            and YES_WORDS.search(words)):
        return answer
    return None


def mentions(texts, name):
    """Whether any of `texts` says agent name `name`, a name of 3 letters or more, whatever the case, and with spaces or
    punctuation for its hyphens (voicedesk.resolve takes spaces and underscores for them)."""
    said = _words(name)
    return len(said.replace("-", "")) >= 3 and any(said in _words(t) for t in texts)


# ------------------------------------------------------------------ the tools

def _cut(text, limit):
    text = str(text).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _cut_middle(text, limit):
    """`text` in at most about `limit` characters: its start and its end, which says most (a relayed mail's header
    comes before the owner's words, an agent's conclusion last)."""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    return text[:head].rstrip() + " … " + text[-(limit - head - 3):].lstrip()


def _ago(ts, now):
    return "%s ago" % classify.fmt_age(now - ts) if ts else "at an unknown time"


Tool = collections.namedtuple("Tool", "name description properties required")
TOOLS = (                             # each tool, and each of BUILTIN, has a status line in actions.DESK_STATUS
    Tool("agents", "The owner's live agents: each one's name, CLI, state (busy, waiting for the owner, blocked, "
                   "idle) and why, board group, folder and last status note.", {}, ()),
    Tool("recent", "An agent's latest messages, read at once from its transcript: its own replies and what it was sent "
                   "(the owner's words, mail, other agents' messages), without tool output. Use it for 'what is X "
                   "doing'.",
         {"agent": {"type": "string", "maxLength": AGENT_CHARS, "description": "the agent's name, as agents lists it"},
          "n": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6, "description": "how many messages"}},
         ("agent",)),
    Tool("ask", "Ask an agent a question without interrupting it: a read-only copy of its conversation answers, in 5 "
                "to 30 seconds. Say first that you are asking.",
         {"agent": {"type": "string", "maxLength": AGENT_CHARS, "description": "the agent's name"},
          "question": {"type": "string", "maxLength": voicedesk.MAX_QUESTION, "description": "the question"}},
         ("agent", "question")),
    Tool("looking", "What is on the owner's Mac screen now, and the agent behind it.", {}, ()),
    Tool("send", "Send an agent a message from the owner. Only when the owner's latest message asks you to, with what "
                 "the owner wants said: the desk refuses any other.",
         {"agent": {"type": "string", "maxLength": AGENT_CHARS, "description": "the agent's name"},
          "text": {"type": "string", "maxLength": MAX_TEXT - SAID_CHARS - SAID_ROOM,
                   "description": "the message, as the owner would say it to the agent"}},
         ("agent", "text")),
    Tool("mail_list", "The owner's latest mail with the agents: box inbox, the owner's mails to them (each with its "
                      "number), or sent, theirs to the owner (subjects).",
         {"box": {"type": "string", "enum": ["inbox", "sent"]},
          "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8}}, ("box",)),
    Tool("mail_show", "One of the owner's mails to the agents, by its number in mail_list's inbox.",
         {"id": {"type": "integer", "minimum": 1, "maximum": 10 ** 12}}, ("id",)),
)
BY_NAME = {t.name: t for t in TOOLS}
MCP_TOOLS = tuple("mcp__%s__%s" % (SERVER, t.name) for t in TOOLS)     # as Claude names them


def schema(tool):
    """A tool's inputSchema."""
    return {"type": "object", "properties": tool.properties, "required": list(tool.required),
            "additionalProperties": False}


def checked(tool, args):
    """The arguments of a call of `tool`, checked against its schema, with its defaults. Raises ToolError."""
    if not isinstance(args, dict):
        raise ToolError("the arguments must be an object")
    extra = sorted(set(args) - set(tool.properties))
    missing = [k for k in tool.required if k not in args]
    if extra or missing:
        raise ToolError("%s takes %s" % (tool.name, ", ".join(tool.properties) or "no arguments"))
    out = {k: p["default"] for k, p in tool.properties.items() if "default" in p}
    for k, v in args.items():
        p = tool.properties[k]
        if "enum" in p:
            ok = v in p["enum"]
        elif p["type"] == "string":
            ok = isinstance(v, str) and bool(v.strip()) and len(v) <= p["maxLength"] and "\x00" not in v
        else:
            ok = isinstance(v, int) and not isinstance(v, bool) and p["minimum"] <= v <= p["maximum"]
        if not ok:
            raise ToolError("%s's %s is not right: %s" % (tool.name, k, ", ".join(
                "%s %s" % (a, p[a]) for a in ("type", "enum", "minimum", "maximum", "maxLength") if a in p)))
        out[k] = v.strip() if isinstance(v, str) else v
    return out


class Tools:
    """The desk's tools as its MCP server runs them, each giving text for the model or raising ToolError. Seams: `rows`
    (voicedesk.agents), `board` (the collector's rows: by default one Collector, kept), `ask` (voicedesk.ask), `looking`
    (voicedesk.looking), `run` (sources.run, for agent-mail), `control` (how send reaches the daemon: by default its
    socket, from DESK_SOCKET and DESK_TOKEN), `homes` (the Claude logins) and `now`."""

    def __init__(self, rows=voicedesk.agents, board=None, ask=voicedesk.ask, looking=voicedesk.looking, run=sources.run,
                 control=None, homes=None, now=time.time):
        self._rows, self._board, self._ask, self._looking = rows, board, ask, looking
        self.run, self.control, self.homes, self.now = run, control or control_from_env, homes, now
        self.collector = None

    def board(self):
        """The board's live agents (the collector's rows). Raises OSError, ValueError or sqlite3.Error."""
        if self._board:
            return self._board()
        if self.collector is None:
            self.collector = collect.Collector()
        return [a for a in self.collector.agents(with_git=False)[0] if a.get("live")]

    def rows(self, unread=None):
        """desk_rows of the live agents. What of the board cannot be read (its agents' states, its groups) the rows
        go without, said on `unread` (a list) and in the log. Raises ToolError when the agents cannot be listed."""
        def part(what, read, empty):
            try:
                return read()
            except (OSError, ValueError, sqlite3.Error) as e:
                why = "could not read the board's %s: %s" % (what, e)
                log.warning("%s", why)
                if unread is not None:
                    unread.append(why)
                return empty
        try:
            agents = self._rows()
        except voicedesk.VoiceError as e:
            raise ToolError(str(e)) from None
        return desk_rows(agents, part("agents", self.board, []), part("groups", groups.members, {}))

    def agent(self, name, rows=None):
        try:
            return voicedesk.resolve(name, self.rows() if rows is None else rows)
        except voicedesk.VoiceError as e:
            raise ToolError(str(e)) from None

    def tool_agents(self):
        unread = []
        rows, now = self.rows(unread), self.now()
        lines = ["(%s.)" % why for why in unread]
        if not rows:
            return "\n".join(lines + ["No live agents."])
        lines.append("%d live agents (name, CLI: state, why; group; folder; last note):" % len(rows))
        for a in rows:
            note = a["note"] if isinstance(a.get("note"), dict) else {}
            lines.append("%s, %s: %s%s; %s; %s; %s" % (
                a["name"] or a["id"][:8], a.get("cli") or "claude", a["state"],
                ", " + _cut(a["why"], WHY_CHARS) if a.get("why") else "", a["group"] or "no group", a["folder"] or "?",
                "note %s: %s" % (_ago(note.get("ts"), now), _cut(note["text"], NOTE_CHARS)) if note.get("text")
                else "no note"))
        return "\n".join(lines)

    def tool_recent(self, agent, n):
        a = self.agent(agent)
        name = a["name"] or a["id"][:8]
        try:
            msgs, now = sources.latest_messages(a, n, self.homes), self.now()
        except LookupError as e:
            raise ToolError("%s: %s" % (name, e)) from None
        except (OSError, sqlite3.Error) as e:
            raise ToolError("could not read %s's conversation: %s" % (name, e)) from None
        if not msgs:
            return "%s has no messages yet." % name
        head = "%s (%s, %s now): its last %d messages, oldest first:" % (name, a.get("cli") or "claude", a["state"],
                                                                         len(msgs))
        return "\n".join([head] + ["[%s] %s: %s" % (_ago(ts, now), name if who == "agent" else "to " + name,
                                                    _cut_middle(text, MESSAGE_CHARS)) for ts, who, text, _ in msgs])

    def tool_ask(self, agent, question):
        rows = self.rows()
        a = self.agent(agent, rows)
        res = self._ask(a["id"], question, rows=rows)
        if not res.get("ok"):
            raise ToolError("asking %s failed: %s" % (a["name"], res.get("error")))
        return "%s's read-only copy answered in %.0f s: %s" % (res.get("agent") or a["name"], res.get("seconds") or 0,
                                                               res.get("answer") or "(nothing)")

    def tool_looking(self):
        res = self._looking()
        if not res.get("ok"):
            raise ToolError("could not tell what is on the owner's screen: %s" % res.get("error"))
        return res["said"]

    def tool_send(self, agent, text):
        res = self.control({"op": "send", "agent": agent, "text": text})
        if not res.get("ok"):
            raise ToolError(res.get("error") or "not sent")
        return res.get("said") or "sent"

    def _mail(self, args):
        exe = sources.find_bin("agent-mail")
        if not exe:
            raise ToolError("agent-mail is not installed here")
        code, out, err = self.run([exe] + args, timeout=30, env=relaunch.agent_env())
        if code != 0:
            raise ToolError("agent-mail %s failed: %s" % (args[0], (err or out).strip()[-300:]))
        return out.strip() or "(nothing)"

    def tool_mail_list(self, box, limit):
        return self._mail([box, "--limit", str(limit)])

    def tool_mail_show(self, id):
        return self._mail(["show", str(id)])

    def call(self, name, args):
        """(text, is_error) of one call of tool `name`."""
        try:
            return _cut(getattr(self, "tool_" + name)(**checked(BY_NAME[name], args)), TOOL_CHARS), False
        except ToolError as e:
            return str(e), True
        except Exception as e:          # a bug or a broken source: said to the model, the server goes on
            log.exception("tool %s failed", name)
            return "the %s tool failed: %s: %s" % (name, type(e).__name__, e), True


def control_from_env(req, timeout=90):
    """A request to the daemon that started this MCP server (DESK_SOCKET), with its token (DESK_TOKEN): its answer."""
    path, token = os.environ.get("DESK_SOCKET"), os.environ.get("DESK_TOKEN")
    if not path or not token:
        return {"ok": False, "error": "this desk tool runs only under the desk's daemon"}
    try:
        return next(request(path, dict(req, token=token), timeout))
    except (OSError, ValueError, StopIteration) as e:
        return {"ok": False, "error": "could not reach the desk's daemon: %s" % e}


# ------------------------------------------------------------------ the MCP server (stdio, JSON-RPC 2.0)

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def mcp_answer(msg):
    """The answer to one JSON-RPC request but tools/call (which mcp_serve runs on a thread of its own)."""
    method, mid, params = msg.get("method"), msg["id"], msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER, "version": "1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": t.name, "description": t.description, "inputSchema": schema(t)} for t in TOOLS]}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return _error(mid, -32601, "no method %r here" % (method,))


def mcp_call(msg, tools):
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    name = params.get("name")
    if name not in BY_NAME:
        return _error(msg["id"], -32602, "no tool %r" % (name,))
    text, failed = tools.call(name, params.get("arguments") or {})
    return {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}],
                                                          "isError": failed}}


def mcp_serve(rfile, wfile, tools):
    """Serve MCP on newline-delimited JSON-RPC 2.0 from `rfile` to `wfile` until `rfile` ends: initialize, tools/list,
    ping and tools/call (each call on a thread of its own, so a slow ask holds up nothing); notifications are taken and
    nothing is answered to them; any other method is "not found"."""
    lock, calls = threading.Lock(), []

    def write(obj):
        with lock:
            wfile.write(json.dumps(obj) + "\n")
            wfile.flush()

    def run_call(msg):
        write(mcp_call(msg, tools))
    for line in rfile:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            write(_error(None, -32700, "not JSON"))
            continue
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            write(_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "not a JSON-RPC request"))
            continue
        if "id" not in msg:
            continue                                    # a notification: initialized, cancelled
        if msg["method"] == "tools/call":
            t = threading.Thread(target=run_call, args=(msg,), daemon=True)
            calls.append(t)
            t.start()
        else:
            write(mcp_answer(msg))
    for t in calls:
        t.join(voicedesk.TIMEOUT + 30)


def mcp_main():
    """`agent-board desk mcp`: the MCP server Claude starts for the desk, on stdin and stdout. It logs (what of the
    board it could not read, a tool's crash) to MCP_LOG beside the daemon's socket (DESK_SOCKET), as Claude keeps its
    stderr; run by hand, on stderr."""
    sock = os.environ.get("DESK_SOCKET")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s desk mcp %(levelname)s %(message)s",
                        **({"filename": str(Path(sock).parent / MCP_LOG)} if sock else {"stream": sys.stderr}))
    mcp_serve(sys.stdin, sys.stdout, Tools())
    return 0


# ------------------------------------------------------------------ the Claude process

def claude_argv(claude, cfg, mcp_file, prompt_file=None):
    """The command of the desk's Claude process (the module's docstring says why each flag), `claude` the CLI's argv,
    `mcp_file` the file of its --mcp-config and `prompt_file` its system prompt, filled in (prompt_text; by default
    cfg["system_prompt"] as it is)."""
    argv = list(claude) + [
        "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--include-partial-messages",
        "--verbose", "--model", cfg["model"], "--effort", cfg["effort"], "--name", NAME, "--restricted",
        "--tools", ",".join(BUILTIN), "--disallowedTools", ",".join(DENIED),
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--strict-mcp-config", "--mcp-config", str(mcp_file), "--settings", json.dumps(SETTINGS),
        "--system-prompt-file", str(prompt_file or cfg["system_prompt"]), "--disable-slash-commands", "--no-chrome",
        "--no-session-persistence"]
    for d in cfg["read_dirs"]:
        argv += ["--add-dir", d]
    return argv


def mcp_config(socket_path, token, command=None):
    """The desk's --mcp-config: its one server, `agent-board desk mcp` (or `command`), told the daemon's socket and
    token for send."""
    command = command or [sys.executable, str(sources.BOARD_BIN / "agent-board"), "desk", "mcp"]
    return {"mcpServers": {SERVER: {"type": "stdio", "command": command[0], "args": list(command[1:]),
                                    "env": {"DESK_SOCKET": str(socket_path), "DESK_TOKEN": token}}}}


def unsafe(init):
    """(why, fatal) when a turn's init event shows a process that is not the desk's, else (None, False)
    (voicedesk.init_refusal: fatal when it holds more than BUILTIN, the desk's tools and server, or a mode but dontAsk;
    not fatal when it lacks one of them)."""
    return voicedesk.init_refusal(init, BUILTIN + MCP_TOOLS, (SERVER,), tools_as="the desk's")


class Process:
    """One Claude process: its events read on a thread into a queue (None when its output ends), its stderr's tail
    kept."""

    def __init__(self, argv, env, cwd, popen):
        self.proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          cwd=cwd, env=env, bufsize=1)
        self.events, self.err = queue.Queue(), collections.deque(maxlen=30)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    e = json.loads(line)
                except ValueError:
                    self.err.append(line)
                    continue
                if isinstance(e, dict):
                    self.events.put(e)
        except (OSError, ValueError):
            pass
        finally:
            self.proc.stdout.close()
            self.events.put(None)

    def _stderr(self):
        try:
            for line in self.proc.stderr:
                self.err.append(line)
        except (OSError, ValueError):
            pass
        finally:
            self.proc.stderr.close()

    def say(self, text):
        """The owner's words (or a primer) as a user message. Raises OSError when the process is gone."""
        self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        self.proc.stdin.flush()

    def drain(self):
        """Drop what it printed after its last turn ended; False when its output has ended."""
        while True:
            try:
                if self.events.get_nowait() is None:
                    self.events.put(None)
                    return False
            except queue.Empty:
                return True

    def alive(self):
        return self.proc.poll() is None

    def why(self):
        return "exit %s: %s" % (self.proc.poll(), _cut("".join(self.err), 400) or "no output")

    def stop(self):
        """End it: its input closed, then SIGTERM, then SIGKILL, a few seconds apart."""
        for step in (lambda: self.proc.stdin.close(), self.proc.terminate):
            try:
                step()
                self.proc.wait(3)
                return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        self.proc.kill()
        self.proc.wait()


class Turn:
    """A turn as the send rule sees it: the owner's words (`texts`: this request's, then the earlier ones), whether it
    is the owner's (`by_owner`), the agent behind the owner's screen, the desk's offer to send that the owner's words
    say yes to (accepted_offer), whether its init passed, whether it has ended, and what it sent."""

    def __init__(self, n, texts, by_owner, looking_id=None, offer=None):
        self.n, self.texts, self.by_owner, self.looking_id, self.offer = n, texts, by_owner, looking_id, offer
        self.init_ok = self.over = False
        self.sends = []

    def said(self, name=None):
        """The owner's words, as a message sent in this turn quotes them: with the offer they say yes to, if any (the
        owner named `name`, owner() by default)."""
        if not self.offer:
            return self.texts[0]
        return "%s [%s's answer to the desk, which had said: %s]" % (self.texts[0], name or owner(),
                                                                      _cut_middle(self.offer, OFFER_CHARS))


def send_refusal(turn, agent, text):
    """Why the desk may not send `text` to `agent` (a desk_rows row) in `turn` (the module's docstring), or None."""
    name = agent["name"] or agent["id"][:8]
    if turn is None or turn.over:
        return "no turn of the owner's is running"
    if not turn.by_owner:
        return "this turn is the desk's own, not the owner's"
    if not turn.init_ok:
        return "this turn's start was not checked"
    if not (SEND_WORDS.search(turn.texts[0]) or turn.offer):
        return "the owner's words did not ask to tell, ask or send anything, nor say yes to an offer to"
    if not (mentions(turn.texts + [turn.offer] if turn.offer else turn.texts, name) or agent["id"] == turn.looking_id):
        return "the owner did not name %s (nor is it behind the owner's screen); ask the owner to say its name" % name
    if any(s["id"] == agent["id"] for s in turn.sends):
        return "a message went to %s in this turn already" % name
    if not text.strip() or len(text) > MAX_TEXT - SAID_CHARS - SAID_ROOM or "\x00" in text:
        return "the message is empty or too long"
    return None


class Job:
    """A request in the daemon's queue: a turn ("turn") or a new conversation ("new"), its events for the
    connection that asked."""

    def __init__(self, kind, arrived, text=None, looking=None):
        self.kind, self.arrived, self.text, self.looking = kind, arrived, text, looking
        self.events = queue.Queue()


class Desk:
    """The daemon's core: the Claude process (Process), its turns one at a time (a worker thread over `jobs`), and the
    send rule. Seams: `claude` (the CLI's argv), `popen`, `env` (more of the process's environment), `mcp` (the MCP
    server's command), `rows` and `board` (as Tools takes them), `run` (for agent-send, through actions), `looking`
    and `clock`."""

    def __init__(self, cfg, *, claude=None, popen=subprocess.Popen, env=None, mcp=None, rows=voicedesk.agents,
                 board=None, run=sources.run_merged, looking=voicedesk.looking, clock=time.monotonic):
        self.cfg, self.claude, self.popen, self.extra_env = cfg, claude, popen, env or {}
        self.run, self.looking, self.clock = run, looking, clock
        self.folder = Path(cfg["socket"]).parent
        self.turns_log = self.folder / "turns.jsonl"
        self.token = secrets.token_hex(16)
        self.mcp = mcp_config(cfg["socket"], self.token, mcp)
        self.mcp_file = self.folder / MCP_FILE
        self.prompt_file = self.folder / PROMPT_FILE
        self.tools = Tools(rows=rows, board=board)
        self.lock = threading.Lock()                # the turn and its sends, for the send rule
        self.checked = threading.Condition(self.lock)   # a turn's init checked, or its end: a send waiting may go on
        self.proc, self.turn, self.n, self.context = None, None, 0, 0
        self.login = None                           # the login (its name) Claude runs on
        self.api_ms = 0                             # Claude's time on its API so far: its results count it cumulatively
        self.history = collections.deque(maxlen=HISTORY)
        self.answer = None                          # its last answer, read out: an offer the owner may say yes to
        self.jobs, self.pending = queue.Queue(), 0
        self.failures, self.down_until, self.down_why = 0, 0.0, ""
        self.unsafe, self.stopped = None, threading.Event()
        self.worker = None

    # -- the process
    def start(self):
        """Start the first Claude process (and its primer), then the worker. Raises DeskError (Unsafe)."""
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fresh("the desk is starting", warn=False)
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def _places(self):
        """[(login name, argv, env)] the desk may run on, in order: the mailroom's Claude logins
        (voicedesk.fallback_logins), the one claude_config_dir names first when it names one (one that names none of
        them is an error), each with claude_argv as that login takes it and its scrubbed environment, with `env`'s
        additions; without a mailroom that lists them, claude_config_dir's login (else the default) alone. A login with
        a settings file of its own is passed over: its file would take the place of the desk's (SETTINGS). Raises
        DeskError."""
        argv = claude_argv(self.claude or [sources.find_bin("claude") or "claude"], self.cfg, self.mcp_file,
                           self.prompt_file)
        at = argv.index("--settings")
        own, bare = argv[at:at + 2], argv[:at] + argv[at + 2:]
        first = self.cfg["claude_config_dir"]
        try:
            found = voicedesk.fallback_logins(bare)
            if found is None:
                found = [(str(voicedesk.login_key(first)), first, bare, voicedesk.fork_env(first))]
        except voicedesk.VoiceError as e:           # the mailroom failed to load: its scrub is the one rule for it
            raise DeskError("the desk could not start Claude: %s" % e) from None
        if first:
            ahead = [p for p in found if voicedesk.login_key(p[1]) == voicedesk.login_key(first)]
            if not ahead:
                raise DeskError("the desk could not start Claude: claude_config_dir %s is none of the mailroom's "
                                "logins (%s)" % (first, ", ".join(p[0] for p in found) or "none"))
            found = ahead + [p for p in found if p not in ahead]
        places = []
        for name, _, command, env in found:
            if "--settings" in command:
                log.warning("the desk passes over login %s: its settings file would take the place of the desk's", name)
            else:
                places.append((name, command + own, dict(env, **self.extra_env)))
        if not places:
            raise DeskError("the desk could not start Claude: the mailroom lists no Claude login it may run on")
        return places

    def _fresh(self, why, warn=True, after=None):
        """A new Claude process, hence a new conversation, checked by its primer, on the first login (_places) whose
        primer gets an answer, not a usage limit's reply: from the first, or, `after` a login at its limit ((its name,
        the limit's text)), from the one after it, then round to those before it. Raises DeskError."""
        if self.stopped.is_set():
            raise DeskError("the desk is stopping")
        (log.warning if warn else log.info)("starting Claude afresh: %s", why)
        if self.proc:
            self.proc.stop()
        self.proc, self.context, self.api_ms, self.login = None, 0, 0, None
        self.history.clear()
        self.answer = None
        if self.clock() < self.down_until:
            raise DeskError("the desk could not start Claude (%s); it tries again in %.0f s" % (
                self.down_why, self.down_until - self.clock()))
        places, limited = self._places(), []
        names = [p[0] for p in places]
        if after:
            limited.append("%s (%s)" % after)
            if after[0] in names:
                i = names.index(after[0])
                places = places[i + 1:] + places[:i]
        t0 = self.clock()
        try:
            sources.write_text_atomic(self.prompt_file, prompt_text(self.cfg), mode=0o600)
            for name, argv, env in places:
                sources.write_json(self.mcp_file, self.mcp, mode=0o600)
                self.proc = Process(argv, env, str(self.folder), self.popen)
                res = self._converse(Turn(0, [PRIMER], by_owner=False), PRIMER, lambda e: None, t0)
                if res["limit"]:
                    log.warning("login %s is at its usage limit (%s), so the desk tries the next login", name,
                                res["limit"].get("text"))
                    limited.append("%s (%s)" % (name, res["limit"].get("text")))
                    self.proc.stop()
                    continue
                if res["result"].get("is_error"):
                    raise DeskError(_cut(res["result"].get("result") or res["text"] or "its primer failed", 300))
                self.login = name
                break
            else:
                raise DeskError("each Claude login it may run on is at its usage limit: %s" % "; ".join(limited))
        except Unsafe:
            raise
        except (DeskError, OSError) as e:
            self.failures += 1
            self.down_why = str(e)
            self.down_until = self.clock() + min(START_BACKOFF[1], START_BACKOFF[0] * 2 ** (self.failures - 1))
            if self.proc:
                self.proc.stop()
            self.proc = None
            raise DeskError("the desk could not start Claude: %s" % e) from None
        self.failures, self.api_ms = 0, res["result"].get("duration_api_ms") or 0
        if limited:
            log.warning("the desk moved to login %s, past those at their usage limit: %s", self.login,
                        "; ".join(limited))
        log.info("Claude is ready on login %s (pid %s, %s at %s effort): started and primed in %.2f s, context %d "
                 "tokens", self.login, self.proc.proc.pid, self.cfg["model"], self.cfg["effort"], self.clock() - t0,
                 self.context)

    # -- one exchange with the process
    def _converse(self, turn, text, emit, arrived):
        """Put `text` to the process and read its events to the turn's result: {"text", "timings", "tools", "refused",
        "result"}. `emit` gets the deltas and tool calls. Raises Unsafe or DeskError."""
        p, cfg = self.proc, self.cfg
        if not p.drain():
            raise DeskError("Claude had stopped (%s)" % p.why())
        with self.lock:
            self.turn = turn
        try:
            p.say(text)
            return self._read(p, turn, emit, arrived, arrived + cfg["turn_seconds"])
        except OSError as e:
            raise DeskError("Claude had stopped (%s; %s)" % (e, p.why())) from None
        finally:
            with self.lock:
                turn.over = True
                self.turn = None
                self.checked.notify_all()

    def _read(self, p, turn, emit, arrived, deadline):
        """The turn's events, to its result (_converse). The reply's text goes out as it streams, a new block of it
        after a line break; a reply that came whole (a synthetic one, such as an API error's) goes out whole. A usage
        limit's reply (voicedesk.usage_limit) is no answer: it goes nowhere, and the result says it ("limit")."""
        out = {"parts": [], "tools": [], "refused": [], "failed": [], "first_delta": None, "first_tool": None,
               "limit": None}
        streamed, msg_id, gap, checked = set(), None, False, False
        uses, denied = {}, set()                    # tool_use id: its tool; the ids Claude refused

        def put(piece):
            if out["first_delta"] is None:
                out["first_delta"] = round(self.clock() - arrived, 2)
            emit({"type": "delta", "text": piece})
        while True:
            try:
                e = p.events.get(timeout=max(0.05, deadline - self.clock()))
            except queue.Empty:
                p.stop()
                raise DeskError("Claude took over %d s on this turn, so it was stopped" % self.cfg["turn_seconds"]) \
                    from None
            if e is None:
                raise DeskError("Claude stopped (%s)" % p.why())
            kind, sub = e.get("type"), e.get("subtype")
            if kind == "system" and sub == "init":
                why, fatal = unsafe(e)
                if why:
                    p.stop()
                    raise (Unsafe if fatal else DeskError)("Claude was stopped before it could answer: %s" % why)
                checked = True
                with self.lock:
                    turn.init_ok = True
                    self.checked.notify_all()
                continue
            if not checked and kind in ANSWERING:
                p.stop()
                raise Unsafe("Claude was stopped: it answered before its init (%r)" % kind)
            if kind == "result":
                if not checked:
                    raise DeskError("Claude ended the turn before it began: %s" % _cut(e.get("result") or sub, 300))
                return dict(out, result=e, text="\n".join(out["parts"]))
            if e.get("parent_tool_use_id"):
                continue
            if kind == "stream_event":
                ev = e.get("event") if isinstance(e.get("event"), dict) else {}
                if ev.get("type") == "message_start":
                    msg_id = (ev.get("message") or {}).get("id")
                elif ev.get("type") == "content_block_start":
                    gap = out["first_delta"] is not None    # text before this block: a line break first
                elif ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                    piece = ev["delta"].get("text") or ""
                    if piece:
                        streamed.add(msg_id)
                        put("\n" + piece if gap else piece)
                        gap = False
            elif kind == "assistant":
                try:
                    hit = voicedesk.usage_limit(e)
                except voicedesk.VoiceError as err:
                    raise DeskError(str(err)) from None
                if hit:
                    out["limit"] = hit
                    continue
                msg = e.get("message") if isinstance(e.get("message"), dict) else {}
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text"):
                        if msg.get("id") not in streamed:
                            put(("\n" if out["first_delta"] is not None else "") + b["text"])
                        out["parts"].append(b["text"])
                    elif b.get("type") == "tool_use":
                        name = str(b.get("name") or "").removeprefix("mcp__%s__" % SERVER)
                        uses[b.get("id")] = name
                        if out["first_tool"] is None:
                            out["first_tool"] = round(self.clock() - arrived, 2)
                        out["tools"].append({"name": name, "input": b.get("input")})
                        emit({"type": "tool", "name": name, "input": b.get("input")})
                u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                self.context = sum(u.get(k) or 0 for k in voicedesk.USAGE[:3]) or self.context
            elif kind == "user":                        # tool results: a failed one is on the turn's record
                msg = e.get("message") if isinstance(e.get("message"), dict) else {}
                for b in msg.get("content") if isinstance(msg.get("content"), list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error") \
                            and b.get("tool_use_id") not in denied:
                        name = uses.get(b.get("tool_use_id"), "?")
                        out["failed"].append(name)
                        log.warning("turn %d: the %s tool failed: %s", turn.n, name,
                                    _cut(sources.message_text(b.get("content")), 200))
            elif kind == "system" and sub == "permission_denied":
                denied.add(e.get("tool_use_id"))
                out["refused"].append(e.get("tool_name"))
                log.warning("turn %d: Claude refused %s: %s", turn.n, e.get("tool_name"),
                            _cut(e.get("message") or e.get("decision_reason") or "", 200))

    # -- turns
    def submit(self, job):
        with self.lock:
            ahead = self.pending
            self.pending += 1
        if ahead:
            job.events.put({"type": "waiting", "ahead": ahead})
        self.jobs.put(job)

    def _work(self):
        while not self.stopped.is_set():
            try:
                job = self.jobs.get(timeout=5)
            except queue.Empty:
                if not self.stopped.is_set():
                    self._mend()
                continue
            if job is None:                 # close()
                return
            try:
                final = self._new() if job.kind == "new" else self._turn(job)
            except Exception as e:          # always answer: a connection waits on this job
                log.exception("the desk failed on a request")
                final = {"type": "final", "ok": False, "error": "the desk failed: %s: %s" % (type(e).__name__, e)}
            finally:
                with self.lock:
                    self.pending -= 1
            job.events.put(final)
            if self.unsafe:
                self.stopped.set()
                return
            if job.kind == "turn" and self.proc and self.context > self.cfg["context_tokens"]:
                self._mend("its context passed %d tokens (%d)" % (self.cfg["context_tokens"], self.context))

    def _mend(self, why=None):
        """Start Claude afresh between turns: when `why` says so (planned), or when its process has stopped."""
        planned = why is not None
        if not planned:
            if self.proc is None or self.proc.alive() or self.clock() < self.down_until:
                return
            why = "its process stopped (%s)" % self.proc.why()
        try:
            self._fresh(why, warn=not planned)
        except Unsafe as e:
            self._stop_unsafe(e)
        except DeskError as e:
            log.warning("%s", e)

    def _stop_unsafe(self, e):
        log.error("%s; the desk stops", e)
        self.unsafe = str(e)
        self.stopped.set()

    def _new(self):
        try:
            self._fresh("the owner asked for a new conversation", warn=False)
        except Unsafe as e:
            self._stop_unsafe(e)
            return {"type": "final", "ok": False, "error": str(e)}
        except DeskError as e:
            return {"type": "final", "ok": False, "error": str(e)}
        return {"type": "final", "ok": True, "text": "A new conversation has started."}

    def _move(self, turn, res, text, job):
        """After a turn whose reply was its login's usage limit's (res["limit"]): the desk moves on (_fresh after that
        login), and asks the turn again there when it had done nothing yet, no tool and no word, as else it may have
        acted: (the new result, its Turn). Raises DeskError."""
        left, why = self.login, res["limit"].get("text")
        acted = bool(res["tools"]) or res["first_delta"] is not None
        log.warning("turn %d: login %s is at its usage limit (%s), so the desk moves to the next login%s", turn.n,
                    left, why, "" if acted else " and asks again there")
        self._fresh("login %s is at its usage limit" % left, after=(left, why))
        if acted:
            raise DeskError("login %s reached its usage limit part-way through this turn, which may have acted, so it "
                            "was not asked again; the desk is now on login %s" % (left, self.login))
        again = Turn(turn.n, turn.texts, by_owner=True, looking_id=turn.looking_id, offer=turn.offer)
        return self._converse(again, text, job.events.put, job.arrived), again

    def _screen(self, looking):
        """(the line about the owner's screen that goes before the owner's words, the agent behind it's id, why it could
        not be read) for a request's `looking`. A screen that could not be read is said so, logged, and the turn goes
        on without it."""
        if looking is True:
            looking = self.looking()
        if not isinstance(looking, dict):
            return "", None, None
        if not looking.get("ok"):
            why = _cut(looking.get("error") or "no reason given", 300)
            log.warning("could not read the owner's screen: %s", why)
            return "[%s's screen could not be read: %s]\n" % (capital(owner()), why), None, why
        agent = looking.get("agent") if isinstance(looking.get("agent"), dict) else {}
        name = owner()
        line = "[%s's screen, as the Mac reports it (data, not %s's words): %s]\n" % (
            capital(name), name, _cut(looking.get("said") or "", 600))
        return line, (agent.get("id") if agent.get("live") is not False and isinstance(agent.get("id"), str)
                      else None), None

    def _turn(self, job):
        began = self.clock()
        self.n += 1
        n = self.n
        screen, looking_id, screen_error = self._screen(job.looking)
        offer = accepted_offer(job.text, self.answer)
        if offer:
            log.info("turn %d: the owner's words say yes to the desk's offer to send", n)
        turn = Turn(n, [job.text] + list(self.history), by_owner=True, looking_id=looking_id, offer=offer)
        res, error = None, None
        try:
            if self.proc is None or not self.proc.alive():
                self._fresh("its process had stopped (%s)" % self.proc.why() if self.proc else "it was not running")
            res = self._converse(turn, screen + job.text, job.events.put, job.arrived)
            for _ in range(MOVES):
                if not res["limit"]:
                    break
                res, turn = self._move(turn, res, screen + job.text, job)
        except Unsafe as e:
            self._stop_unsafe(e)
            error = str(e)
        except DeskError as e:
            error = str(e)
            if self.proc and not self.proc.alive():
                log.warning("turn %d: %s", n, e)
        self.history.appendleft(job.text)
        res = res or {}
        result = res.get("result") or {}
        if result.get("is_error") and not error:     # a move that failed says more than the limit it moved for
            error = _cut(result.get("result") or result.get("subtype") or "Claude failed", 400)
        timings = {"waited": round(began - job.arrived, 2), "end": round(self.clock() - job.arrived, 2)}
        if res:
            total = result.get("duration_api_ms") or self.api_ms
            api_ms, self.api_ms = total - self.api_ms, total
            timings.update(first_delta=res["first_delta"], first_tool=res["first_tool"], api=round(api_ms / 1000, 2))
        self.answer = None if error else res.get("text")
        final = {"type": "final", "ok": not error, "turn": n, "text": res.get("text") or "", "timings": timings,
                 "tools": res.get("tools") or [], "refused": res.get("refused") or [],
                 "failed": res.get("failed") or [], "sends": [dict(s) for s in turn.sends],
                 "context_tokens": self.context}
        if error:
            final["error"] = error
        if screen_error:
            final["screen_error"] = screen_error
        log.info("turn %d on login %s: %s; first word %s s, done %s s (Claude %s s, waited %s s), context %d tokens; "
                 "tools: %s%s", n, self.login, "ok" if not error else "failed: " + error, timings.get("first_delta"),
                 timings["end"], timings.get("api"), timings["waited"], self.context,
                 ", ".join("%s(%s)" % (t["name"], _cut(json.dumps(t["input"]), 60)) for t in final["tools"]) or "none",
                 ("; failed: " + ", ".join(final["failed"]) if final["failed"] else "")
                 + ("; sent to " + ", ".join(s["agent"] for s in turn.sends) if turn.sends else ""))
        try:
            sources.append_jsonl(self.turns_log, dict({k: v for k, v in final.items() if k != "type"}, ts=time.time(),
                                                      said=job.text, screen=screen.strip(), model=self.cfg["model"],
                                                      effort=self.cfg["effort"], login=self.login))
        except OSError as e:
            log.warning("could not keep turn %d in %s: %s", n, self.turns_log, e)
        return final

    # -- send, from the MCP server
    def control(self, req):
        """The MCP server's send: {"ok", "said"} or {"ok": False, "error"}."""
        if not isinstance(req, dict) or not secrets.compare_digest(str(req.get("token") or ""), self.token):
            return {"ok": False, "error": "not the desk's own tool"}
        agent_name, text = req.get("agent"), req.get("text")
        if not isinstance(agent_name, str) or not isinstance(text, str):
            return {"ok": False, "error": "send takes an agent and a text"}
        try:
            agent = self.tools.agent(agent_name)
        except ToolError as e:
            return self._refused(agent_name, "not sent: %s" % e)
        with self.lock:
            turn = self.turn
            if turn is not None and not turn.init_ok:   # Claude printed its init before it called the tool: the
                self.checked.wait_for(lambda: turn.init_ok or turn.over, INIT_WAIT)    # reader may not be there yet
            why = "the desk has stopped" if self.unsafe else send_refusal(turn, agent, text)
            if not why:
                record = {"agent": agent["name"] or agent["id"][:8], "id": agent["id"], "text": text.strip(),
                          "ok": None}
                turn.sends.append(record)       # taken before it goes: a second send to it in this turn is refused
        if why:
            return self._refused(agent_name, "not sent: " + why)
        res = self.deliver(agent, text, turn.said())
        with self.lock:
            record.update(ok=bool(res.get("ok")), ref=res.get("ref"), error=res.get("error") or None)
        if not res.get("ok"):
            log.warning("turn %d: the send to %s failed: %s", turn.n, record["agent"], res.get("error"))
            return {"ok": False, "error": "the send to %s failed: %s" % (record["agent"], res.get("error"))}
        log.info("turn %d: sent %s a message (%d characters)", turn.n, record["agent"], len(record["text"]))
        return {"ok": True, "said": "Sent to %s, as message %s." % (record["agent"], res.get("ref") or "?")}

    def _refused(self, agent_name, why):
        log.warning("refused a send to %s: %s", _cut(agent_name, 60), why)
        return {"ok": False, "error": why}

    def deliver(self, agent, text, said):
        """The message, with the owner's words to the desk under it (said_line), through the board's send action: {"ok",
        "ref", "error"}."""
        body = text.strip() + said_line(said)
        ref = sent.new_ref()
        res = actions.dispatch({"action": "send", "id": agent["id"], "text": body, "via": "desk"}, self.tools.board,
                               lambda: [], ref=ref, run=self.run)
        return {"ok": bool(res.get("ok")), "ref": ref,
                "error": res.get("error") or (None if res.get("ok") else _cut(res.get("output") or "", 300))}

    def close(self):
        """Stop: the worker once its turn is over (or after 10 s), then Claude."""
        self.stopped.set()
        self.jobs.put(None)
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(10)
        if self.proc:
            self.proc.stop()


# ------------------------------------------------------------------ the socket

class Handler(socketserver.StreamRequestHandler):
    def put(self, obj):
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        self.wfile.flush()

    def handle(self):
        desk = self.server.desk
        line = self.rfile.readline(MAX_REQUEST + 1)
        try:
            req = json.loads(line) if len(line) <= MAX_REQUEST else None
        except ValueError:
            req = None
        if not isinstance(req, dict):
            return self.put({"type": "final", "ok": False, "error": "a request is one JSON object on one line"})
        if req.get("op") == "send":
            return self.put(desk.control(req))
        job, err = request_job(req, desk.clock())
        if err:
            return self.put({"type": "final", "ok": False, "error": err})
        desk.submit(job)
        while True:
            e = job.events.get()
            try:
                self.put(e)
            except OSError:
                return                      # the caller has gone; the turn still runs to its end
            if e["type"] == "final":
                return


def request_job(req, now):
    """(Job, None) for a client's request, or (None, why it won't do)."""
    if req.get("op") == "new":
        return Job("new", now), None
    if req.get("op") is not None:
        return None, "op must be new or send"
    text, looking = req.get("text"), req.get("looking")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or "\x00" in text:
        return None, "text must be the owner's words, at most %d characters" % MAX_TEXT
    if not (looking is None or looking is True or isinstance(looking, dict)):
        return None, "looking must be what voice_looking gave, or true"
    return Job("turn", now, text.strip(), looking), None


class Server(sources.UnixServer):
    """The desk's socket at `path` (sources.UnixServer: 0600, an old socket there replaced), for connections from this
    uid alone; its folder is made 0700 when it is not there yet."""

    def __init__(self, path, desk):
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.desk = desk
        super().__init__(path, Handler, lambda: {os.getuid()}, "the desk",
                         refused=lambda uid: log.warning("refused a connection from uid %d", uid))


def serve(cfg=None, **seams):
    """`agent-board desk serve`: start the desk and serve its socket until SIGTERM or SIGINT (0), or until Claude holds
    more than the desk's tools (EXIT_UNSAFE). Raises DeskError when Claude cannot start, ValueError for bad settings."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s desk %(levelname)s %(message)s", stream=sys.stderr)
    for k in relaunch.LEAKED:               # agent-send and agent-mail must never take the daemon for an agent
        os.environ.pop(k, None)
    cfg = cfg or settings()
    desk = Desk(cfg, **seams)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *a: desk.stopped.set())
    srv = Server(cfg["socket"], desk)           # first: a primer's send is refused by the rule, not unheard
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        desk.start()
        log.info("serving on %s", cfg["socket"])
        while not desk.stopped.wait(1):
            pass
    except Unsafe as e:
        log.error("%s; the desk stops", e)
        desk.unsafe = str(e)
    finally:
        srv.shutdown()
        srv.server_close()
        desk.close()
        log.info("stopped")
    return EXIT_UNSAFE if desk.unsafe else 0


# ------------------------------------------------------------------ the client

CLIENT_SLACK = 60                     # seconds a client waits on the desk beyond its turn_seconds


def request(path, req, timeout):
    """The events of one request to the desk's socket at `path`, as they come. Raises OSError (the desk is not running,
    or took over `timeout` seconds between events) and ValueError."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall((json.dumps(req) + "\n").encode())
        with s.makefile("rb") as f:
            for line in f:
                yield json.loads(line)


def events(cfg, req):
    """The events of one request to the desk the settings `cfg` name (request), waiting on it as long as a turn may
    take. Raises as request does."""
    return request(cfg["socket"], req, cfg["turn_seconds"] + CLIENT_SLACK)


def new_conversation(cfg):
    """A fresh conversation for the desk `cfg` names: its final event, or None when it gave none. Raises as request
    does."""
    return next((e for e in events(cfg, {"op": "new"}) if e.get("type") == "final"), None)


def say(text, looking=False, as_json=False, cfg=None, out=sys.stdout):
    """`agent-board desk say`: one turn, its reply printed as it streams, then its timings, tools and sends (or each
    event as a JSON line). The exit code: 0 when it answered."""
    cfg = cfg or settings()
    final = None
    try:
        for e in events(cfg, dict({"text": text}, **({"looking": True} if looking else {}))):
            if as_json:
                out.write(json.dumps(e, ensure_ascii=False) + "\n")
            elif e.get("type") == "delta":
                out.write(e["text"])
            elif e.get("type") == "waiting":
                out.write("(waiting for the turn before)\n")
            out.flush()
            if e.get("type") == "final":
                final = e
                break
    except (OSError, ValueError) as e:
        out.write("could not reach the desk on %s: %s\n" % (cfg["socket"], e))
        return 1
    if final and not as_json:
        out.write("\n" + show_final(final) + "\n")
    return 0 if final and final.get("ok") else 1


def show_final(final):
    """The footer `desk say` prints under a reply."""
    t = final.get("timings") or {}
    lines = ["could not answer: %s" % final["error"]] if final.get("error") else []
    if final.get("screen_error"):
        lines.append("-- the owner's screen could not be read: %s" % final["screen_error"])
    if t:
        lines.append("-- first word %s s, first tool %s s, done %s s (Claude %s s, waited %s s); context %s tokens" % (
            t.get("first_delta"), t.get("first_tool"), t.get("end"), t.get("api"), t.get("waited"),
            final.get("context_tokens")))
    if final.get("tools"):
        lines.append("-- tools: " + ", ".join("%s(%s)" % (x["name"], _cut(json.dumps(x["input"], ensure_ascii=False),
                                                                          80)) for x in final["tools"]))
    if final.get("refused"):
        lines.append("-- refused: " + ", ".join(map(str, final["refused"])))
    for s in final.get("sends") or []:
        lines.append("-- sent to %s%s: %s" % (s["agent"], "" if s.get("ok") else " (FAILED: %s)" % s.get("error"),
                                               s["text"]))
    if final.get("ok") and final.get("text") and not t:
        lines.append(final["text"])
    return "\n".join(lines)


def new(cfg=None, out=sys.stdout):
    """`agent-board desk new`: a fresh conversation."""
    cfg = cfg or settings()
    try:
        final = new_conversation(cfg)
    except (OSError, ValueError) as e:
        out.write("could not reach the desk on %s: %s\n" % (cfg["socket"], e))
        return 1
    out.write(show_final(final or {"error": "no answer"}) + "\n")
    return 0 if final and final.get("ok") else 1
