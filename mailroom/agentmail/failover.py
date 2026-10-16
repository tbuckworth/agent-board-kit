"""Move a Claude agent to another login when its own runs out mid-task, and back once that one has reset.

Detection. When a limit stops a turn, Claude Code writes its own record into the transcript, an assistant entry
with isApiErrorMessage, in one of two shapes:
  - a plan limit (5-hour, weekly): error "rate_limit" and quotaLimits {status: "rejected", rateLimitType,
    resetsAt}. That is the server's verdict, not a transient 429.
  - a model's own weekly bucket used up, with extra usage off (for example Fable): apiError
    "model_requires_usage_credits", no quotaLimits, and the text "You're out of usage credits. Run /usage-credits
    to keep using Fable 5.1 or /model to switch models." (`claude -p` words it "You're out of usage credits.
    Switch to another model, or manage usage credits at ..."). It carries no reset time; that comes from the
    login's per-model window in agent-usage, which also tells a record from before the window's reset.
A live agent whose transcript ends that way, with the reset still ahead, is stuck. Nothing else reads it: the
watcher checks the transcript tails of live agents. Text that merely quotes such a message, such as a hook's
systemMessage, is not a limit: a hook's own `claude -p` running out says nothing about the agent's login.
Headroom is judged for the model the agent runs (its last reply's model), so a Fable agent only goes to, and
only comes back to, a login with Fable left.

The move (same session id, same tmux pane; the conversation, memory and background jobs are kept):
  1. SIGKILL the agent's claude process. Not SIGTERM: a graceful exit kills the agent's background jobs, while
     after SIGKILL they keep running (reparented) and its MCP servers exit on their own. The agent is between
     turns (its turn ended on the limit, or it is idle), so nothing is half written.
  2. Copy its transcript (and session folder, if any) into the other login's projects/, same project folder,
     same session id, so mail replies keep reaching it.
  3. Trust the working directory in that login and link its auto memory (agentmail/usage.link_memory).
  4. In the same tmux pane: `stty sane; export CLAUDE_CONFIG_DIR=...` (or unset it for the default login),
     then `claude --resume ID --name NAME <its flags> [prompt]`.
  5. Wait until the registry shows it live on the new login.

Switching back. An agent moved off its first login ("home") goes back once the limit that moved it has reset,
home has headroom again and comes first in usage.order, and the agent has been idle for `idle_seconds`. It is
resumed with no prompt, so it simply waits for its next input. An idle agent on a login that is used up and
billing extra usage moves to a login with headroom the same way. A busy agent is never interrupted.

A Claude Code background session (`claude --bg`, or one dispatched from `claude agents`; the claude daemon runs
it, with no terminal) moves the same way, with the daemon's own commands: `claude stop ID` under its login (this
also ends its background commands, and the note it resumes with says so), the copy, then `claude --resume ID
--bg <its model, effort and permission mode, read from the transcript> prompt` under the other login, in the
session's folder. memcap then moves the daemon out of the caller's cgroup, so a restart of agent-failover (or
the end of the agent that ran agent-switch) cannot take it down.

Only those, and agents in a tmux pane whose shell started claude (agent-spawn's layout), are moved; a session in
someone's terminal is left alone. Pure decisions here; bin/agent-failover reads, acts and loops.
"""
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import registry, usage

DEFAULTS = {
    "enabled": True,
    # Background commands the agent started: "keep" leaves them running and lists them in the note it resumes
    # with (pid, command, output file); "kill" stops them first and says so.
    "background": "keep",
    "interval_seconds": 20,
    "switch_back": True,          # move agents back to their first login after its limit resets
    "idle_seconds": 300,          # an agent must have been idle this long before a switch back
    "max_moves_per_hour": 4,      # per agent: a guard against ping-pong
    "launch_wait_seconds": 60,
    # Session names (shell globs) that stay on their login: never moved when a limit stops them (they wait for its
    # reset), never moved back or rebalanced, and resumed where their newest copy is. E.g. ["nightly-*"] for jobs
    # that must only use the login they were started on.
    "pinned": [],
    "tail_bytes": 256 << 10,
}
LIMIT_PROMPT = ("[agent-failover] Your Claude login '{src}' hit its usage limit ({why}), so this session was moved "
                "to login '{dst}' and resumed here. Carry on with your task from where you stopped.")
NO_PROMPT_JOBS = "[agent-failover] This session was moved to login '{dst}' ({why}). Nothing else changed."
REBALANCE_WHY = "its window is used up and it bills extra usage"
CREDITS_ERROR = "model_requires_usage_credits"   # Claude Code's apiError when a model's own bucket is used up
CREDITS_TEXT = "You're out of usage credits"
MODEL_WINDOW_SECONDS = 7 * 86400                 # a per-model window is weekly
BG = "bg"   # movable()'s handle for a background session: the claude daemon runs it, not a tmux pane


def settings(cfg):
    return dict(DEFAULTS, **(cfg.get("failover") or {}))


def pinned(sess, f):
    """Whether this session stays on its login (settings' "pinned", by session name)."""
    name = sess.get("name") or ""
    return bool(name) and any(fnmatch.fnmatchcase(name, p) for p in f.get("pinned") or [])


# ------------------------------------------------------------------ reading a session (pure where possible)
def home_dir(sess):
    return Path(sess["config_dir"]) if sess.get("config_dir") else registry.DEFAULT_CLAUDE_HOME


def transcript(sess):
    """The session's transcript under its own login, or None."""
    hits = sorted(home_dir(sess).glob("projects/*/%s.jsonl" % sess["id"]), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def read_tail(path, nbytes):
    with open(path, "rb") as fh:
        fh.seek(max(0, os.path.getsize(path) - nbytes))
        return fh.read().decode("utf-8", "replace").splitlines()


def entry_text(d):
    content = (d.get("message") or {}).get("content")
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return str(content or "")


def credits_model(text):
    """'Fable 5.1' from "... Run /usage-credits to keep using Fable 5.1 or /model to switch models.", or None."""
    m = re.search(r"keep using (.+?) or /model", text or "")
    return m.group(1).strip() if m else None


def limit_hit(lines, now=None):
    """The limit that stopped this session, if its last conversation entry is Claude Code's record of one and
    the reset is not known to be past: {"window": "five_hour" | "model_credits", "resets_at": epoch or None,
    "at": epoch of the record, "model": "Fable 5.1" (credits only), "text": ...}. None otherwise."""
    now = time.time() if now is None else now
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") not in ("assistant", "user") or d.get("isMeta"):
            continue
        if not (d.get("type") == "assistant" and d.get("isApiErrorMessage")):
            return None
        text = entry_text(d)
        at = usage.iso_epoch(d.get("timestamp"))
        q = d.get("quotaLimits") or {}
        if d.get("error") == "rate_limit" and q.get("status") == "rejected":
            resets = q.get("resetsAt")
            if resets and resets <= now:
                return None   # reset already: Claude Code's own auto-continue (or the next input) carries on
            return {"window": q.get("rateLimitType") or "limit", "resets_at": resets, "at": at, "text": text[:160]}
        if d.get("apiError") == CREDITS_ERROR or text.startswith(CREDITS_TEXT):
            return {"window": "model_credits", "resets_at": None, "at": at, "model": credits_model(text),
                    "text": text[:160]}
        return None
    return None


def session_model(lines):
    """The model of the last real reply in these transcript lines ("claude-fable-5-1"), or None. Error records
    say "<synthetic>"."""
    for line in reversed(lines):
        if '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        model = (d.get("message") or {}).get("model") if d.get("type") == "assistant" else None
        if model and not model.startswith("<"):
            return model
    return None


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


VALUE_FLAGS = ("--model", "--effort", "--permission-mode", "--agent")
BARE_FLAGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--chrome", "--no-chrome")


def relaunch_flags(argv):
    """The flags of a running claude worth keeping across a move (model, effort, permissions); never its prompt
    or a --resume/--continue it was started with."""
    out, i = [], 1
    while i < len(argv):
        a = argv[i]
        if a in BARE_FLAGS:
            out.append(a)
        elif a in VALUE_FLAGS and i + 1 < len(argv):
            out += [a, argv[i + 1]]
            i += 1
        elif "=" in a and a.split("=", 1)[0] in VALUE_FLAGS:
            out.append(a)
        i += 1
    return out


def background_jobs(pid, ppids=None):
    """The Bash-tool commands a claude process is running in the background: [{"pid", "command", "output"}].
    They are its children that eval a command (MCP servers and other helpers are not); stdout is the task's
    output file. An agent between turns has no foreground command, so every one of these is a background job."""
    if ppids is None:
        from . import memcap
        ppids = memcap.parents()
    jobs = []
    for child in sorted(p for p, pp in ppids.items() if pp == pid):
        m = re.search(r"eval '(.*)' < /dev/null", " ".join(cmdline(child)), re.S)
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


def jobs_note(jobs, killed):
    if not jobs:
        return ""
    lines = ["pid %d: %s%s" % (j["pid"], j["command"], (" (output: %s)" % j["output"]) if j["output"] else "")
             for j in jobs]
    if killed:
        return (" These background commands from before the move were stopped; start again any you still "
                "need: " + "; ".join(lines))
    return (" These background commands from before the move are still running, but this session no longer "
            "tracks them (Claude Code's notice that they \"didn't finish\" only means that), so check them with "
            "ps or their output files: " + "; ".join(lines))


def stop_jobs(jobs):
    from . import memcap
    ppids = memcap.parents()
    for j in jobs:
        for p in reversed(memcap.tree(j["pid"], ppids)):
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass


def override_flags(flags, overrides):
    """flags with --model/--effort (etc.) replaced or added from overrides {flag: value}; None values ignored."""
    out, i, done = [], 0, set()
    while i < len(flags):
        f = flags[i]
        name = f.split("=", 1)[0]
        if name in (overrides or {}) and overrides[name] is not None:
            out += [name, overrides[name]]
            done.add(name)
            i += 1 if "=" in f else 2
            continue
        out.append(f)
        i += 1
    for name, value in (overrides or {}).items():
        if value is not None and name not in done:
            out += [name, value]
    return out


def pane_id(sess):
    """'%17' from the registry's tmux field ('name:@7.%17'), or None."""
    m = re.search(r"(%\d+)$", sess.get("tmux") or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------ deciding (pure)
def recent_moves(entry, now, window=3600):
    return [m for m in (entry or {}).get("moves", []) if now - m[0] < window]


def stuck_key(hit):
    """What a 'waits for the reset' note is logged once per: the reset, or the record itself when no reset is known."""
    return hit.get("resets_at") or hit.get("at") or hit.get("text")


def best_other(snaps, cfg, current, now=None, model=None):
    """The first Claude login in usage.order, other than `current`, with headroom for `model` (default: the
    spawn model). None if there is none."""
    s = usage.settings(cfg)
    model = model or cfg.get("spawn_claude_model")
    for t in usage.ranked(s):
        if not t.startswith("claude:") or t == "claude:" + current:
            continue
        acct = t.split(":", 1)[1]
        if not usage.account_ready(usage.account(s, acct))[0]:
            continue
        if usage.assess(snaps.get(t) or {"windows": []}, s, model, now)["status"] == "ok":
            return acct
    return None


def rank(cfg, acct):
    order = usage.ranked(usage.settings(cfg))
    t = "claude:" + acct
    return order.index(t) if t in order else len(order)


def model_window(snap, model):
    """The login's per-model window for this model (its "weekly Fable"), or None."""
    for w in (snap or {}).get("windows") or []:
        if w.get("kind") == "model_weekly" and usage.counted(w, model):
            return w
    return None


def credits_hit(hit, snap, model, now):
    """A model-credits record (no reset time of its own) read against the login's usage: the hit with the
    window's reset filled in, or None when that window has reset since the record was written."""
    w = model_window(snap, model)
    if not w:
        return hit   # no per-model window known (or usage unread): the transcript is the only verdict
    resets = w.get("resets_at")
    if w["remaining_pct"] > 0 and resets and hit.get("at") and hit["at"] < resets - MODEL_WINDOW_SECONDS:
        return None  # written before the window's last reset, and it has headroom now: the next input carries on
    return dict(hit, resets_at=resets if w["remaining_pct"] <= 0 else None,
                window_name=w.get("name") or "model window")


def limit_why(hit, now):
    if hit["window"] == "model_credits":
        return "out of %s usage credits%s" % (
            hit.get("model") or "model", (", %s resets %s" % (hit.get("window_name") or "its window",
                                                             usage.fmt_reset(hit["resets_at"], now)))
            if hit.get("resets_at") else "")
    return "%s limit, resets %s" % (hit["window"].replace("_", "-"), usage.fmt_reset(hit["resets_at"], now))


def decide(sessions, limits, snaps, state, cfg, now):
    """-> (moves, notes). moves: [{"session", "to", "reason", "prompt", "resets_at"}]; notes: lines to log once.
    sessions: live Claude sessions movable here, each with the "model" it runs if known; limits: {session id:
    limit_hit(...)}."""
    f = settings(cfg)
    s = usage.settings(cfg)
    moves, notes = [], []
    for sess in sessions:
        sid, cur = sess["id"], sess.get("account")
        model = sess.get("model") or cfg.get("spawn_claude_model")
        entry = (state.get("sessions") or {}).get(sid) or {}
        if cur is None or len(recent_moves(entry, now)) >= f["max_moves_per_hour"]:
            continue
        hit = limits.get(sid)
        if hit and hit["window"] == "model_credits":
            hit = credits_hit(hit, snaps.get("claude:" + cur), model, now)
        if pinned(sess, f):
            if hit and entry.get("stuck_until") != stuck_key(hit):
                notes.append((sid, stuck_key(hit), "%s (%s) hit its limit on %s (%s); it is pinned there"
                              " (failover.pinned), so it waits for the reset" % (sess.get("name"), sid[:8], cur,
                                                                                 limit_why(hit, now))))
            continue
        if hit:
            dst = best_other(snaps, cfg, cur, now, model)
            why = limit_why(hit, now)
            if dst:
                moves.append({"session": sess, "to": dst, "reason": "limit: " + why, "resets_at": hit["resets_at"],
                              "prompt": LIMIT_PROMPT.format(src=cur, dst=dst, why=why)})
            elif entry.get("stuck_until") != stuck_key(hit):
                notes.append((sid, stuck_key(hit), "%s (%s) hit its limit on %s (%s) and no other Claude login has "
                              "headroom for %s; it waits for the reset" % (sess.get("name") or sid[:8], sid[:8], cur,
                                                                         why, model)))
            continue
        idle = sess.get("status") == "idle" and not sess.get("waiting_for") and \
            now - (sess.get("updated_at") or now) >= f["idle_seconds"]
        if not (f["switch_back"] and idle):
            continue
        home = entry.get("home")
        if home and home != cur and now >= (entry.get("resets_at") or 0) and rank(cfg, home) < rank(cfg, cur):
            if usage.assess(snaps.get("claude:" + home) or {"windows": []}, s, model, now)["status"] == "ok":
                moves.append({"session": sess, "to": home, "reason": "switch back: %s has reset" % home,
                              "prompt": None, "resets_at": None})
                continue
        snap = snaps.get("claude:" + cur) or {"windows": []}
        if usage.assess(snap, s, model, now)["status"] == "out" and (snap.get("extra_usage") or {}).get("enabled"):
            dst = best_other(snaps, cfg, cur, now, model)
            if dst:
                moves.append({"session": sess, "to": dst, "reason": "rebalance: %s %s" % (cur, REBALANCE_WHY),
                              "prompt": None, "resets_at": None})
    return moves, notes


def record(state, move, now):
    """The state after a successful move. `home` is the first login the agent was moved off."""
    st = json.loads(json.dumps(state or {}))
    sid = move["session"]["id"]
    entry = st.setdefault("sessions", {}).setdefault(sid, {})
    entry.setdefault("home", move["session"].get("account"))
    entry["current"] = move["to"]
    entry["name"] = move["session"].get("name") or ""
    if move.get("resets_at"):
        entry["resets_at"] = move["resets_at"]
    entry["moves"] = recent_moves(entry, now, 86400) + [[now, move["session"].get("account"), move["to"], move["reason"]]]
    if entry["home"] == move["to"]:
        entry.pop("resets_at", None)
    return st


# ------------------------------------------------------------------ acting
def tmux(*args, check=False, timeout=15):
    return subprocess.run(["tmux"] + list(args), capture_output=True, text=True, timeout=timeout, check=check)


def pane_pid(pane):
    r = tmux("display-message", "-p", "-t", pane, "#{pane_pid}")
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def parent_pid(pid):
    try:
        return int(Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def cmdline(pid):
    try:
        return [a.decode(errors="replace") for a in Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0") if a]
    except OSError:
        return []


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
    shell, parent = pane_pid(pane), parent_pid(int(sess["pid"]))
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


def launch_line(claude, sid, name, flags, config_dir, prompt):
    env = ("export CLAUDE_CONFIG_DIR=%s" % shlex.quote(config_dir)) if config_dir else "unset CLAUDE_CONFIG_DIR"
    cmd = [claude, "--resume", sid] + (["--name", name] if name else []) + list(flags)
    if prompt:
        cmd.append(prompt)
    return "stty sane; %s; %s" % (env, " ".join(shlex.quote(c) for c in cmd))


def fill_flags(flags, defaults):
    """flags plus each of defaults [flag, value, ...] it does not set already (a skip-permissions flag counts as
    setting the permission mode)."""
    have = {f.split("=", 1)[0] for f in flags}
    if have & set(BARE_FLAGS[:2]):
        have.add("--permission-mode")
    out = list(flags)
    for i in range(0, len(defaults) - 1, 2):
        if defaults[i] not in have:
            out += defaults[i:i + 2]
    return out


def claude_env(config_dir):
    """The environment for a claude command run for one login: that login's config dir (none: the default
    login), and nothing inherited from a session this may run inside (agent-switch from an agent)."""
    env = usage.claude_env({"config_dir": config_dir})
    for k in list(env):
        if k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_KIND") or k.startswith("CLAUDE_BG_"):
            env.pop(k)
    return env


def bg_command(claude, sid, name, flags, prompt):
    return [claude, "--resume", sid, "--bg"] + (["--name", name] if name else []) + list(flags) + \
        ([prompt] if prompt else [])


def bg_stop(claude, sess):
    """`claude stop` for a background session, under its own login. (ok, output). It takes the short id that
    `claude agents` lists; a full session id is "No job matching"."""
    try:
        r = subprocess.run([claude, "stop", sess["id"][:8]], capture_output=True, text=True, timeout=60,
                           env=claude_env(sess.get("config_dir")))
    except (OSError, subprocess.SubprocessError) as e:
        return False, "%s: %s" % (type(e).__name__, e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[:300]


def bg_saved_flags(config_dir, sid):
    """The options a login's daemon saved for a background session it has run there (its jobs/<short id>/
    state.json respawnFlags), or None if it has not run there. Given flags, `claude --resume ID --bg` on such a
    login starts a copy under a new id ("keeps its own saved options"); with none it continues the session."""
    home = Path(config_dir) if config_dir else registry.DEFAULT_CLAUDE_HOME
    try:
        d = json.loads((home / "jobs" / sid[:8] / "state.json").read_text())
    except (OSError, ValueError):
        return None
    return list(d.get("respawnFlags") or [])


def flag(flags, name):
    for i, f in enumerate(flags):
        if f == name and i + 1 < len(flags):
            return flags[i + 1]
        if f.startswith(name + "="):
            return f.split("=", 1)[1]
    return None


COPY_RE = re.compile(r"started a copy as ([0-9a-f]{8})")


def bg_resume(claude, sess, flags, config_dir, prompt):
    """`claude --resume ID --bg` under config_dir, in the session's folder. (ok, output). A login whose daemon has
    run the session before gets no flags (see bg_saved_flags); if claude starts a copy anyway, the copy is
    stopped and the session resumed once more with no flags."""
    cwd = sess.get("cwd") if sess.get("cwd") and os.path.isdir(sess["cwd"]) else None
    name = sess.get("name")
    if bg_saved_flags(config_dir, sess["id"]) is not None:
        flags, name = [], None   # --name is a flag too: the saved options keep its name
    out = ""
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
            for verb in ("stop", "rm"):
                subprocess.run([claude, verb, copy.group(1)], capture_output=True, text=True, timeout=60,
                               env=claude_env(config_dir))
            continue
        return False, "claude resumed it as a copy (%s), which is left running: %s" % (copy.group(1), out)
    return True, out


def bg_launch(claude, sess, flags, config_dir, prompt, cfg):
    """Resume a background session under config_dir (bg_resume), then move the daemon out of this process's
    cgroup (and the session into its own capped scope), as agent-send does for a resume. (ok, output)."""
    ok, out = bg_resume(claude, sess, flags, config_dir, prompt)
    if not ok:
        return False, out
    from . import memcap
    try:
        props = memcap.limits(cfg)
    except ValueError:
        props = {}
    want = usage.expand(config_dir) if config_dir else None

    def live_pid():
        return next((x["pid"] for x in registry.claude_sessions()
                     if x["id"] == sess["id"] and x["live"] and (x.get("config_dir") or None) == want), None)
    return True, out + "; memory: " + memcap.cap_bg_session(sess["id"], sess.get("name") or sess["id"][:8], props,
                                                             session_pid=live_pid)


def migrate(sess, to, prompt, cfg, trust=None, claude=None, dry_run=False, why="", overrides=None):
    """Move one live agent to login `to`. Returns (ok, detail). Never leaves it without a launch attempt once
    its process has been stopped. `why` names the reason in the note sent when only background jobs need one."""
    f = settings(cfg)
    s = usage.settings(cfg)
    acct = usage.account(s, to)
    ready, not_ready = usage.account_ready(acct)
    if not ready:
        return False, "login %s not ready: %s" % (to, not_ready)
    pane, not_movable = movable(sess)
    if not pane:
        return False, "not moved: %s" % not_movable
    bg = pane == BG
    src = transcript(sess)
    if not src:
        return False, "not moved: no transcript under %s" % home_dir(sess)
    pid = int(sess["pid"])
    claude = claude or registry.claude_bin() or "claude"
    dst_dir = usage.expand(acct.get("config_dir"))
    flags = relaunch_flags(cmdline(pid))   # now: its command line goes with it
    if bg:   # a daemon's spare names no flags: take what the transcript last recorded
        flags = fill_flags(flags, transcript_flags(read_tail(src, f["tail_bytes"])))
    base, flags = flags, override_flags(flags, overrides)
    saved = bg_saved_flags(dst_dir, sess["id"]) if bg else None
    if saved is not None:   # that daemon resumes it with these, whatever is asked
        asked = [(k, v) for k, v in (overrides or {}).items()
                 if v is not None and v != flag(base, k) and v != flag(saved, k)]
        if asked:
            return False, ("not moved: login %s's claude daemon has run this background session before and resumes "
                           "it there with its saved options (%s), so %s cannot be changed by moving it; use /model "
                           "inside the session" % (to, " ".join(saved) or "none",
                                                    " and ".join("%s %s" % (k[2:], v) for k, v in asked)))
        flags = []
    same_login = (usage.expand(sess.get("config_dir")) or None) == (dst_dir or None)
    jobs = background_jobs(pid)
    kill_jobs = bg or f["background"] == "kill"   # `claude stop` ends a background session's jobs
    if jobs:   # the agent must hear about them, even on a move that would otherwise send no prompt
        prompt = (prompt or NO_PROMPT_JOBS.format(dst=to, why=why or "moved")) + jobs_note(jobs, kill_jobs)
    if dry_run:
        if bg:
            how = "claude stop %s, copy %s to %s and run in %s: %sclaude %s" % (
                sess["id"][:8], src.name, dst_dir or str(registry.DEFAULT_CLAUDE_HOME), sess.get("cwd") or ".",
                ("CLAUDE_CONFIG_DIR=%s " % dst_dir) if dst_dir else "",
                " ".join(shlex.quote(c) for c in bg_command(claude, sess["id"], None if saved is not None else
                                                            sess.get("name"), flags, prompt)[1:]))
            if saved is not None:
                how += " (no flags: that login's daemon resumes it with its saved options, %s)" % (" ".join(saved) or "none")
            return True, "would %s%s" % (how, (" (ending %d background job(s))" % len(jobs)) if jobs else "")
        return True, "would stop pid %d%s, copy %s to %s and type: %s" % (
            pid, (" and %d background job(s)" % len(jobs)) if kill_jobs and jobs else "", src.name,
            dst_dir or str(registry.DEFAULT_CLAUDE_HOME),
            launch_line(claude, sess["id"], sess.get("name"), flags, dst_dir, prompt))
    if bg:
        stopped, out = bg_stop(claude, sess)
        if not stopped and not wait_dead(pid, 1.0):
            return False, "not moved: claude stop failed: %s" % out
        if not wait_dead(pid):
            return False, "pid %d still running after claude stop (%s)" % (pid, out)
        stop_jobs(jobs)   # any that outlived the stop
    else:
        if kill_jobs:
            stop_jobs(jobs)
        try:
            os.kill(pid, signal.SIGKILL)   # keeps the agent's background jobs alive; see the module docstring
        except ProcessLookupError:
            pass   # gone already: relaunching it is all that is left to do
        if not wait_dead(pid):
            return False, "pid %d did not exit after SIGKILL" % pid
    registry.remove_dead_entry(sess)   # claude removes its own on exit; left, it is a second entry for this id
    detail = ""
    try:
        if not same_login:   # a model or effort change on the same login keeps the transcript where it is
            copy_transcript(src, dst_dir or registry.DEFAULT_CLAUDE_HOME, sess["id"])
        cwd = sess.get("cwd") or ""
        if trust and cwd and os.path.isdir(cwd):
            trust(cwd, dst_dir)
        if dst_dir and cwd and os.path.isdir(cwd):
            usage.link_memory(repo_root(cwd), dst_dir)
    except Exception as e:  # noqa: BLE001 - relaunch where it was rather than leave the agent stopped
        to, dst_dir = sess.get("account"), sess.get("config_dir")
        detail = "copy failed (%s: %s); relaunched on %s instead" % (type(e).__name__, e, to)
    if bg:
        launched, said = bg_launch(claude, sess, flags, dst_dir, prompt, cfg)
        if not launched:
            return False, "%sclaude --resume --bg on %s failed: %s" % ((detail + "; ") if detail else "", to, said)
    else:
        session = (sess.get("tmux") or "").split(":", 1)[0]
        if session:
            if dst_dir:
                tmux("set-environment", "-t", session, "CLAUDE_CONFIG_DIR", dst_dir)
            else:
                tmux("set-environment", "-t", session, "-r", "CLAUDE_CONFIG_DIR")
        tmux("send-keys", "-t", pane, "-l", launch_line(claude, sess["id"], sess.get("name"), flags, dst_dir, prompt))
        time.sleep(0.3)
        tmux("send-keys", "-t", pane, "Enter")
    want = usage.expand(dst_dir) if dst_dir else None
    deadline = time.time() + f["launch_wait_seconds"]
    while time.time() < deadline:
        live = [x for x in registry.claude_sessions() if x["id"] == sess["id"] and x["live"]]
        if any((x.get("config_dir") or None) == want for x in live):
            return not detail, detail or "moved %s to %s%s" % (sess.get("name") or sess["id"][:8], to,
                                                               (" (%s)" % said) if bg else "")
        time.sleep(1)
    shows = ("claude said: " + said) if bg else \
        "pane shows: " + " | ".join(tmux("capture-pane", "-p", "-t", pane).stdout.strip().splitlines()[-3:])
    return False, "%srelaunched on %s but not live after %ds; %s" % (
        (detail + "; ") if detail else "", to, f["launch_wait_seconds"], shows[:300])


def repo_root(path):
    r = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=path, capture_output=True, text=True, timeout=15)
    root = r.stdout.strip()
    return str(Path(os.path.join(path, root)).resolve().parent) if r.returncode == 0 and root else str(Path(path).resolve())


# ------------------------------------------------------------------ resuming an offline session
def copies(sid):
    """The session's transcript under each login, newest first: [(mtime, account, config_dir, path)]. A move leaves
    a copy on every login the session ran on; only the newest holds the whole conversation."""
    found = [(p.stat().st_mtime, acct, cdir, p) for acct, home, cdir in registry.claude_homes()
             for p in home.glob("projects/*/%s.jsonl" % sid)]
    return sorted(found, key=lambda c: c[0], reverse=True)


def resume_login(newest, model, owner, snaps, cfg, now=None):
    """(account, why): the login an offline session resumes on. agent-spawn's rule for a new Claude agent: the first
    login in usage.order with headroom for its model and allowed for its repo's owner, or with every login low, the
    one with the most left. With no usage known, or every login used up, it stays on `newest`, the login holding
    its newest copy."""
    rec = usage.recommend(snaps, cfg, models={"claude": model}, owner=owner, only_cli="claude", now=now)
    pick = next((c for c in rec["candidates"] if c["target"] == rec["use"]), None)
    if pick and pick["status"] in ("ok", "low"):
        return pick["account"], rec["why"]
    return newest, "no login with known headroom, so where its newest copy is (%s)" % (rec["why"] or "nothing to pick")


def place_resume(sess, cfg, trust=None, snaps=None, now=None, dry_run=False):
    """Get an offline Claude session ready to resume on the best login (resume_login): (config_dir, note). Unless
    that login holds the newest copy of the conversation, the copy goes there (with its session folder), and the
    folder is trusted and its memory linked there, as a move does. A session with no transcript stays where it is;
    a failed copy resumes where the newest copy is."""
    found = copies(sess["id"])
    if not found:
        return sess.get("config_dir"), ""
    _, src_acct, src_dir, src = found[0]
    if pinned(sess, settings(cfg)):
        return src_dir, "on login %s: pinned (failover.pinned), so where its newest copy is" % src_acct
    cwd = sess.get("cwd") or ""
    has_cwd = bool(cwd) and os.path.isdir(cwd)
    model = session_model(read_tail(src, settings(cfg)["tail_bytes"])) or cfg.get("spawn_claude_model")
    try:
        snaps = usage.snapshots(cfg) if snaps is None else snaps
        acct, why = resume_login(src_acct, model, usage.repo_owner(cwd) if has_cwd else None, snaps, cfg, now)
    except Exception as e:  # noqa: BLE001 - a failed usage read must not block the resume
        acct, why = src_acct, "usage unread (%s: %s), so where its newest copy is" % (type(e).__name__, e)
    dst_dir = src_dir if acct == src_acct else usage.expand(usage.account(usage.settings(cfg), acct).get("config_dir"))
    note = "on login %s: %s" % (acct, why)
    if (dst_dir or None) == (src_dir or None):
        return dst_dir, note
    if dry_run:
        return dst_dir, note + "; would copy its conversation from %s" % src_acct
    try:
        copy_transcript(src, dst_dir or registry.DEFAULT_CLAUDE_HOME, sess["id"])
        if trust and has_cwd:
            trust(cwd, dst_dir)
        if dst_dir and has_cwd:
            usage.link_memory(repo_root(cwd), dst_dir)
    except Exception as e:  # noqa: BLE001 - resume where the newest copy is rather than not at all
        return src_dir, "on login %s, where its newest copy is: copying it to %s failed (%s: %s)" % (
            src_acct, acct, type(e).__name__, e)
    return dst_dir, note + "; conversation copied from %s" % src_acct
