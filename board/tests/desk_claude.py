"""Stands in for the claude CLI as the desk runs it (`claude -p --input-format stream-json --output-format stream-json
--include-partial-messages ...`, agentboard/desk.py), run as a process of its own: tests/test_desk.py's only stand-in
for Claude. As the CLI does, it starts the MCP servers its --mcp-config file names, each with its "env" (and the test's
DESK_CLAUDE_MCP_ENV over it), asks each for its tools, and calls them over their stdio.

For each user message on stdin it prints that turn's events as Claude Code does: the events of the plan's "before"
for it, its init (its tools: --tools and each server's, as mcp__<server>__<tool>; its MCP servers, connected; its
permission mode), then each of the turn's steps, then its result. The plan is the JSON file DESK_CLAUDE_PLAN:
{"turns": [[step, ...], ...], "init": {i: fields over turn i's init}, "before": {i: [events]}}, i counting the user
messages of every run of it so far (so a daemon's first primer is 0, and a restarted process's primer is a turn too).
A step is {"text": T} (T streamed in pieces, then the whole reply), {"call": TOOL, "args": {...}} (a call of the desk's
server's TOOL), {"event": E} (printed as it is), {"result": R} (R's fields over the turn's result, as an error's),
{"sleep": S}, {"exit": CODE} or {"hang": true}. A turn the plan has no steps for replies "OK". Each user message and
each call, with its result, is appended to DESK_CLAUDE_LOG, the environment's DESK_CLAUDE_MCP_UNSET left out of its
servers' and DESK_CLAUDE_MCP_ENV added."""
import itertools
import json
import os
import subprocess
import sys
import time

argv = sys.argv[1:]
plan = json.load(open(os.environ["DESK_CLAUDE_PLAN"]))
log = open(os.environ["DESK_CLAUDE_LOG"], "a")
ids = itertools.count(1)


def note(**kw):
    log.write(json.dumps(kw) + "\n")
    log.flush()


def flag(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


class Server:
    """One MCP server the --mcp-config names, started and asked for its tools."""

    def __init__(self, name, spec):
        unset = json.loads(os.environ.get("DESK_CLAUDE_MCP_UNSET") or "[]")
        env = dict({k: v for k, v in os.environ.items() if k not in unset}, **spec.get("env", {}))
        env.update(json.loads(os.environ.get("DESK_CLAUDE_MCP_ENV") or "{}"))
        self.name = name
        self.proc = subprocess.Popen([spec["command"]] + spec.get("args", []), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, env=env)
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fake"}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.tools = [t["name"] for t in self.rpc("tools/list", {})["result"]["tools"]]

    def rpc(self, method, params):
        mid = next(ids)
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        for line in self.proc.stdout:
            msg = json.loads(line)
            if msg.get("id") == mid:
                return msg
        raise SystemExit("the MCP server %s ended" % self.name)


servers = [Server(n, s) for n, s in json.loads(open(flag("--mcp-config")).read())["mcpServers"].items()]
SESSION = "5e550000-0000-4000-8000-000000000001"
api_ms = 0


def init(n):
    tools = [t for t in flag("--tools", "").split(",") if t]
    tools += ["mcp__%s__%s" % (s.name, t) for s in servers for t in s.tools]
    event = {"type": "system", "subtype": "init", "session_id": SESSION, "model": flag("--model"), "tools": tools,
             "mcp_servers": [{"name": s.name, "status": "connected", "source": "dynamic"} for s in servers],
             "permissionMode": flag("--permission-mode")}
    return dict(event, **plan.get("init", {}).get(str(n), {}))


def message(content, n, k):
    return {"type": "assistant", "session_id": SESSION, "parent_tool_use_id": None, "message": {
        "id": "msg_%d_%d" % (n, k), "role": "assistant", "model": flag("--model"), "content": content,
        "usage": {"input_tokens": 3, "cache_read_input_tokens": 9000 + 100 * n, "cache_creation_input_tokens": 50,
                  "output_tokens": 20}}}


def stream(event):
    emit({"type": "stream_event", "session_id": SESSION, "parent_tool_use_id": None, "event": event})


def turn(n, text):
    global api_ms
    for e in plan.get("before", {}).get(str(n), []):
        emit(e)
    emit(init(n))
    emit({"type": "system", "subtype": "status", "status": "requesting", "session_id": SESSION})
    steps = plan["turns"][n] if n < len(plan.get("turns", [])) else [{"text": "OK"}]
    said, over = [], {}
    for k, step in enumerate(steps):
        if "exit" in step:
            sys.exit(step["exit"])
        if step.get("hang"):
            time.sleep(600)
        if "sleep" in step:
            time.sleep(step["sleep"])
        if "event" in step:
            emit(step["event"])
        elif "result" in step:
            over.update(step["result"])
        elif "text" in step:
            stream({"type": "message_start", "message": {"id": "msg_%d_%d" % (n, k)}})
            stream({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            words = step["text"].split(" ")
            for i in range(0, len(words), 3):
                stream({"type": "content_block_delta", "index": 0, "delta": {
                    "type": "text_delta", "text": (" " if i else "") + " ".join(words[i:i + 3])}})
            stream({"type": "content_block_stop", "index": 0})
            emit(message([{"type": "text", "text": step["text"]}], n, k))
            said.append(step["text"])
        elif "call" in step:
            use = "toolu_%d_%d" % (n, k)
            stream({"type": "message_start", "message": {"id": "msg_%d_%d" % (n, k)}})
            stream({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": use}})
            emit(message([{"type": "tool_use", "id": use, "name": "mcp__desk__" + step["call"],
                           "input": step["args"]}], n, k))
            res = servers[0].rpc("tools/call", {"name": step["call"], "arguments": step["args"]})
            out = res.get("result") or {}
            note(call=step["call"], args=step["args"], text=(out.get("content") or [{}])[0].get("text"),
                 is_error=out.get("isError"), error=res.get("error"))
            emit({"type": "user", "session_id": SESSION, "parent_tool_use_id": None, "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": use, "content": out.get("content"),
                                             "is_error": bool(out.get("isError"))}]}})
    api_ms += 1500
    emit(dict({"type": "result", "subtype": "success", "is_error": False, "result": said[-1] if said else "",
               "session_id": SESSION, "duration_api_ms": api_ms, "num_turns": 1,
               "usage": {"input_tokens": 3, "output_tokens": 20}}, **over))


note(argv=argv, env_config_dir=os.environ.get("CLAUDE_CONFIG_DIR"))
for line in sys.stdin:
    msg = json.loads(line)
    n = sum('"user"' in x for x in open(os.environ["DESK_CLAUDE_LOG"]))
    note(user=msg["message"]["content"])
    turn(n, msg["message"]["content"])
