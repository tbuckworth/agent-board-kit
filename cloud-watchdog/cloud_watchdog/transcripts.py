"""Find the session that launched a resource by searching Claude and Codex transcripts.

Only *tool* lines count as launch evidence (an assistant tool_use, or the tool_result that came
back), so a session that was merely told about the resource (including by our own ping) does not
look like its launcher.
"""
import json
import os
import re
from pathlib import Path

from . import config, util

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MARKER = "[cloud-watchdog"


def _files_since(roots, since, exclude):
    out = []
    for root in roots:
        root = config.expand(root)
        if not root.exists():
            continue
        for dirpath, _dirs, files in os.walk(root):
            if any(x in dirpath for x in exclude):
                continue
            for fn in files:
                if fn.endswith(".jsonl"):
                    p = os.path.join(dirpath, fn)
                    try:
                        if os.stat(p).st_mtime >= since:
                            out.append(p)
                    except OSError:
                        pass
    return out


def _grep_files(files, terms):
    hits = []
    args = []
    for t in terms:
        args += ["-e", t]
    for i in range(0, len(files), 200):
        code, out, _ = util.run(["grep", "-lF"] + args + files[i:i + 200], timeout=120)
        hits += [l for l in out.splitlines() if l]
    return hits


def _session_of(path):
    p = Path(path)
    if p.parent.name == "subagents":
        return p.parent.parent.name, "claude"
    if "/.codex/" in path:
        m = UUID_RE.findall(p.name)
        return (m[-1] if m else p.stem), "codex"
    return p.stem, "claude"


def _is_tool_line(obj):
    """True for an assistant tool call or a tool result (Claude), or a function call/output (Codex)."""
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("tool_use", "tool_result"):
                return True
    payload = obj.get("payload") or {}
    return payload.get("type") in ("function_call", "function_call_output", "custom_tool_call",
                                   "custom_tool_call_output", "local_shell_call", "exec_command_end")


def search(terms, since, cfg, launched_at=None):
    tcfg = cfg["transcripts"]
    terms = [t for t in terms if t and len(str(t)) >= 6]
    if not terms:
        return []
    files = _files_since(tcfg["roots"], since, tcfg["exclude"])
    matched = _grep_files(files, terms)
    verbs = [v.lower() for v in tcfg["launch_verbs"]]
    by_session = {}
    for path in matched:
        sid, cli = _session_of(path)
        code, out, _ = util.run(["grep", "-nF"] + sum([["-e", t] for t in terms], []) + [path], timeout=60)
        cand = by_session.get(sid) or {"session_id": sid, "cli": cli, "cwd": None, "first_ts": None,
                                       "launch_ts": None, "launch_hit": False, "hits": 0, "snippet": "",
                                       "files": set()}
        cand["files"].add(path)
        if cli == "codex" and cand["cwd"] is None:
            try:
                with open(path) as f:
                    first = json.loads(f.readline())
                cand["cwd"] = (first.get("payload") or {}).get("cwd")
            except (OSError, ValueError):
                pass
        for line in out.splitlines()[:400]:
            _, _, raw = line.partition(":")
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if MARKER in raw:
                continue
            ts = util.parse_time(obj.get("timestamp"))
            cand["cwd"] = cand["cwd"] or obj.get("cwd")
            cand["hits"] += 1
            if ts and (cand["first_ts"] is None or ts < cand["first_ts"]):
                cand["first_ts"] = ts
            if _is_tool_line(obj) and any(v in raw.lower() for v in verbs):
                if cand["launch_ts"] is None or (ts and ts < cand["launch_ts"]):
                    cand["launch_ts"] = ts
                    cand["launch_hit"] = True
                    cand["snippet"] = _snippet(raw, terms, verbs)
        if cand["hits"]:  # files that only mention it in our own pings do not make a candidate
            by_session[sid] = cand
    out = []
    for c in by_session.values():
        c["files"] = sorted(c["files"])
        out.append(c)
    return out


def _snippet(raw, terms, verbs):
    low = raw.lower()
    for needle in verbs + [t.lower() for t in terms]:
        i = low.find(needle)
        if i >= 0:
            return raw[max(0, i - 60): i + 140].replace("\\n", " ").replace("\\\"", '"')[:200]
    return ""
