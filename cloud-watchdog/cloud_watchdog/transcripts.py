"""Find the session that launched a resource by searching Claude and Codex transcripts.

Only *tool* lines count as launch evidence (an assistant tool_use, or the tool_result that came
back), so a session that was merely told about the resource (including by our own ping) does not
look like its launcher.
"""
import json
import os
import re
from pathlib import Path

from . import config, investigator, messages, util

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MARKER = messages.PREFIX   # in the watchdog's own messages, which name resources but never launch them


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


def _grep_failed(code, err):
    """None for grep's exit status 0 (a match) or 1 (no match); for any other (2 an error, and util.run's 124 timeout
    or 127 not found) a description of the failed search."""
    return "grep exited %s: %s" % (code, err.strip()[-200:]) if code not in (0, 1) else None


def _grep_files(files, terms):
    """(files that contain any of the terms, failed searches). A failed batch keeps whatever matches it printed."""
    hits, failures = [], []
    args = []
    for t in terms:
        args += ["-e", t]
    for i in range(0, len(files), 200):
        code, out, err = util.run(["grep", "-lF"] + args + files[i:i + 200], timeout=120)
        failed = _grep_failed(code, err)
        if failed:
            failures.append(failed)
        hits += [l for l in out.splitlines() if l]
    return hits, failures


def _first_record(path):
    """The transcript's first line as JSON, or {} when it is not JSON (an empty or half-written file)."""
    with open(path) as f:
        line = f.readline()
    try:
        return json.loads(line)
    except ValueError:
        return {}


def _session_of(path, first):
    """(session id, cli) of a transcript, given its first record: a Codex rollout begins with a session_meta record."""
    p = Path(path)
    if p.parent.name == "subagents":
        return p.parent.parent.name, "claude"
    if first.get("type") == "session_meta":
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


def search(terms, since, cfg):
    """(candidate sessions, failed searches): the sessions whose transcripts under transcripts.roots, modified since
    `since`, mention any of the terms, leaving out the dirs transcripts.exclude names and the investigator's (its
    transcripts mention every resource it is asked about). Runs grep; a search that failed is described in the
    second list."""
    tcfg = cfg["transcripts"]
    terms = [t for t in terms if t and len(str(t)) >= 6]
    if not terms:
        return [], []
    files = _files_since(tcfg["roots"], since, tcfg["exclude"] + [investigator.WORKDIR_NAME])
    matched, failures = _grep_files(files, terms)
    verbs = [v.lower() for v in tcfg["launch_verbs"]]
    by_session = {}
    for path in matched:
        code, out, err = util.run(["grep", "-nF"] + sum([["-e", t] for t in terms], []) + [path], timeout=60)
        failed = _grep_failed(code, err)
        try:
            first = _first_record(path)
        except OSError as e:
            failed = failed or "cannot read %s: %s" % (path, e)
        if failed:
            failures.append(failed)
            continue
        sid, cli = _session_of(path, first)
        cand = by_session.get(sid) or {"session_id": sid, "cli": cli, "cwd": None, "first_ts": None,
                                       "launch_ts": None, "launch_hit": False, "hits": 0, "snippet": ""}
        if cli == "codex" and cand["cwd"] is None:
            cand["cwd"] = (first.get("payload") or {}).get("cwd")
        for line in out.splitlines()[:400]:
            _, _, raw = line.partition(":")
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if MARKER in raw:
                continue
            try:
                ts = util.parse_time(obj.get("timestamp"))
            except ValueError:   # transcripts are written by many tools; a line's time is a hint, not a requirement
                ts = None
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
    return list(by_session.values()), failures


def _snippet(raw, terms, verbs):
    low = raw.lower()
    for needle in verbs + [t.lower() for t in terms]:
        i = low.find(needle)
        if i >= 0:
            return raw[max(0, i - 60): i + 140].replace("\\n", " ").replace("\\\"", '"')[:200]
    return ""
