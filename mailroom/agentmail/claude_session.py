"""A Claude Code session, whatever moves it (agent-failover, agent-switch, agent-send's resume): its transcript and
what that records (model, effort, permission mode), its process, tmux pane and background jobs, copying its
transcript to another login, and resuming it there through the claude daemon (claude --bg). The line that resumes
it in its pane is launch.claude_line."""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import accounts, commands, procs, registry

BG = "bg"   # movable()'s handle for a background session: the claude daemon runs it, not a tmux pane


# ------------------------------------------------------------------ reading a session (pure where possible)
def transcript(sess):
    """The session's transcript under its own login, or None."""
    hits = registry.transcripts(registry.claude_home(sess.get("config_dir")), sess["id"])
    return hits[0] if hits else None


def last_reply(lines):
    """(model, timestamp) of the last real reply in these transcript lines ("claude-fable-5-1", its ISO timestamp
    or None), or (None, None). Error records say "<synthetic>"."""
    for line in reversed(lines):
        if '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        model = (d.get("message") or {}).get("model") if d.get("type") == "assistant" else None
        if model and not model.startswith("<"):
            return model, d.get("timestamp")
    return None, None


def session_model(lines):
    """The model of the last real reply in these transcript lines (last_reply), or None."""
    return last_reply(lines)[0]


def transcript_flags(lines):
    """--model, --effort and --permission-mode as the transcript last recorded them: what a background session
    resumes with, since its process (a daemon's spare) has none on its command line."""
    model, effort, mode = session_model(lines), None, None
    for line in reversed(lines):
        if effort and mode:
            break
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not mode and d.get("type") == "permission-mode":
            mode = d.get("permissionMode")
        if not effort and d.get("type") == "assistant":
            effort = d.get("perTurnEffort") or d.get("effort")
    out = []
    for flag, value in (("--model", model), ("--effort", effort), ("--permission-mode", mode)):
        if value:
            out += [flag, value]
    return out


def background_jobs(pid, ppids=None):
    """The Bash-tool commands a claude process is running in the background: [{"pid", "command", "output"}].
    They are its children that eval a command (MCP servers and other helpers are not); stdout is the task's
    output file. An agent between turns has no foreground command, so every one of these is a background job."""
    jobs = []
    for child in procs.children(pid, ppids):
        m = re.search(r"eval '(.*)' < /dev/null", " ".join(procs.cmdline(child)), re.S)
        if not m:
            continue
        try:
            out = os.readlink("/proc/%d/fd/1" % child)
        except OSError:
            out = ""
        cmd = m.group(1).replace("'\"'\"'", "'").replace("'\\''", "'")   # Claude Code's two quote escapes
        jobs.append({"pid": child, "command": cmd[:200],
                     "output": out if out.startswith("/") else ""})
    return jobs


def pane_id(sess):
    """'%17' from the registry's tmux field ('name:@7.%17'), or None."""
    m = re.search(r"(%\d+)$", sess.get("tmux") or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------ acting
def pane_pid(pane):
    r = commands.tmux("display-message", "-p", "-t", pane, "#{pane_pid}", check=False)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def movable(sess):
    """(handle, why not). handle is the tmux pane ('%17') whose shell started the agent's claude, or BG for a
    Claude Code background session, which its login's daemon runs; None (and why) for anything else."""
    if not sess.get("live") or not sess.get("pid"):
        return None, "not live"
    if sess.get("kind") == "bg":
        return BG, ""
    pane = pane_id(sess)
    if not pane:
        return None, "not in tmux"
    shell, parent = pane_pid(pane), procs.ppid(int(sess["pid"]))
    if shell is None or parent is None:
        return None, "pane or process gone"
    if shell != parent:
        return None, "claude is not the pane shell's child (not agent-spawn's layout)"
    return pane, ""


def copy_transcript(src, dst_home, sid):
    """Copy the transcript (and its session folder) to the same project folder under dst_home. Refuses to
    overwrite a newer copy there. Returns the new path."""
    dst = Path(dst_home) / "projects" / src.parent.name / src.name
    if dst.exists() and dst.stat().st_mtime > src.stat().st_mtime + 1:
        raise RuntimeError("%s is newer than the transcript being moved; not overwriting it" % dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))
    folder = src.parent / sid
    if folder.is_dir():
        shutil.copytree(str(folder), str(dst.parent / sid), dirs_exist_ok=True)
    return dst


def wait_dead(pid, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
    return False


def claude_env(config_dir):
    """The environment for a claude command run for one login: that login's config dir (none: the default
    login), and nothing inherited from a session this may run inside (agent-switch from an agent)."""
    return accounts.claude_env({"config_dir": config_dir})


def bg_command(claude, sid, name, flags, prompt):
    return [claude, "--resume", sid, "--bg"] + (["--name", name] if name else []) + list(flags) + \
        ([prompt] if prompt else [])


def bg_job(claude, verb, sid, config_dir):
    """`claude VERB ID` (stop, rm) for a background session or copy under the login config_dir: runs the claude
    CLI. (ok, output). It takes the short id that `claude agents` lists; a full session id is "No job matching"."""
    try:
        r = subprocess.run([claude, verb, sid[:8]], capture_output=True, text=True, timeout=60,
                           env=claude_env(config_dir))
    except (OSError, subprocess.SubprocessError) as e:
        return False, "%s: %s" % (type(e).__name__, e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[:300]


def bg_saved_flags(config_dir, sid):
    """The options a login's daemon saved for a background session it has run there (its jobs/<short id>/
    state.json respawnFlags), or None if it has not run there. Given flags, `claude --resume ID --bg` on such a
    login starts a copy under a new id ("keeps its own saved options"); with none it continues the session."""
    try:
        d = json.loads((registry.claude_home(config_dir) / "jobs" / sid[:8] / "state.json").read_text())
    except (OSError, ValueError):
        return None
    return list(d.get("respawnFlags") or [])


COPY_RE = re.compile(r"started a copy as ([0-9a-f]{8})")


def bg_resume(claude, sess, flags, config_dir, prompt):
    """`claude --resume ID --bg` under config_dir, in the session's folder. (ok, output). A login whose daemon has
    run the session before gets no flags (see bg_saved_flags); if claude starts a copy anyway, the copy is
    stopped and removed (bg_job) and the session resumed once more with no flags, and the output says so. A copy
    that will not stop fails the resume, naming it."""
    cwd = sess.get("cwd") if sess.get("cwd") and os.path.isdir(sess["cwd"]) else None
    name = sess.get("name")
    if bg_saved_flags(config_dir, sess["id"]) is not None:
        flags, name = [], None   # --name is a flag too: the saved options keep its name
    out, dropped = "", ""
    for a_flags, a_name in [(flags, name)] + ([([], None)] if flags or name else []):
        try:
            r = subprocess.run(bg_command(claude, sess["id"], a_name, a_flags, prompt),
                               capture_output=True, text=True, timeout=120, cwd=cwd, env=claude_env(config_dir))
        except (OSError, subprocess.SubprocessError) as e:
            return False, "%s: %s" % (type(e).__name__, e)
        said = " ".join((r.stdout + r.stderr).split())
        copy = COPY_RE.search(said)
        # its "backgrounded · ID · name" and "note: ..." lines; the rest is a usage hint
        out = " ".join(ln.strip() for ln in (r.stdout + r.stderr).splitlines()
                       if ln.strip().startswith(("backgrounded", "note:"))) or said[:300]
        if r.returncode != 0:
            return False, out
        if not copy:
            break
        if a_flags or a_name:   # it started a copy because of the flags: drop the copy, resume the session itself
            cid = copy.group(1)
            stopped, said_stop = bg_job(claude, "stop", cid, config_dir)
            if not stopped:
                return False, "claude started a copy %s and `claude stop %s` failed: %s; the copy may still be " \
                              "running" % (cid, cid, said_stop)
            removed, said_rm = bg_job(claude, "rm", cid, config_dir)
            dropped = "claude started a copy %s, which was stopped %s; " % (
                cid, "and removed" if removed else "but not removed: " + said_rm)
            continue
        return False, "claude resumed it as a copy (%s), which is left running: %s" % (copy.group(1), out)
    return True, dropped + out


# ------------------------------------------------------------------ control requests
class ControlError(Exception):
    """Claude Code answered a control request with an error, or not at all."""


def claude_probe_cmd(claude):
    return [claude, "-p", "--safe-mode", "--no-session-persistence", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose"]


def claude_control(claude, acct, request, timeout, cwd, run):
    """Put one control request (its body, {"subtype": ...}) to Claude Code on login acct and return the body of
    its answer. It runs claude_probe_cmd through run (subprocess.run) and sends no prompt, so no model runs.
    Raises ControlError if claude answers with an error or not at all; a failure to run it (OSError,
    subprocess.TimeoutExpired) is the caller's to handle."""
    req = {"type": "control_request", "request_id": "agent-mailroom", "request": request}
    r = run(claude_probe_cmd(claude), input=json.dumps(req) + "\n", capture_output=True, text=True,
            timeout=timeout, env=accounts.claude_env(acct), cwd=cwd)
    for line in (r.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "control_response":
            continue
        resp = d.get("response") or {}
        if resp.get("subtype") == "error":
            raise ControlError("claude: %s" % str(resp.get("error"))[:200])
        return resp.get("response") or {}
    raise ControlError("claude gave no answer to %s (exit %s): %s" % (
        request["subtype"], r.returncode, (r.stderr or r.stdout or "").strip()[:200]))
