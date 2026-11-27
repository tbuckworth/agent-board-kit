"""agent-mail fast-trial: fast mode's effort keys tried on one Claude Code or Codex version, so that each new release
gets fast mode without anyone trying its keys by hand (agentmail/paneeffort.py says why fast mode presses keys only on
a version they were tried on).

The trial lays the CLI out as agent-spawn lays out an agent, and runs fast mode's own code on it, as the daemon would:

1. The version's own binary (CLAUDE_VERSIONS, CODEX_RELEASES: each CLI's own install keeps the versions it has
   installed side by side), through a link named claude or codex so that tmux names the agent as it names a real
   one, in an isolated config (ROOT/CLI/home, as CLAUDE_CONFIG_DIR or CODEX_HOME) with a placeholder API key
   (PLACEHOLDER_KEY) and nothing else of this process's environment: the CLI can make no model call, and nothing of
   the owner's own logins' configs (~/.claude, ~/.codex) is read or written. The config keeps what the CLI writes there
   from run to run, and is seeded at each run with the least that brings the CLI up at its idle prompt with no dialog
   (seed).
2. A tmux session of its own, fasttrial-CLI-VERSION (session_name), on the server commands.tmux reaches, whose shell
   (SHELL) starts the CLI (launch_line) in ROOT/CLI/work at START.
3. paneeffort.place finds its pane, paneeffort.version reads the version on trial, paneeffort.ready lets keys go,
   paneeffort.current reads START, and paneeffort.set_level steps it down to LOW and back up to START, one level a
   key; a key each way leaves a half-typed draft in the prompt alone (DRAFT); ready refuses while the /model picker is
   open and lets keys go again once it is closed; and the keys saved nothing in the CLI's settings (SETTINGS). Every
   wait is bounded, so a trial ends within a few minutes.

A pass adds the version to fast mode's tested list (paneeffort.add_tested), which the daemon reads at each use. A
failure keeps the screens the trial read under ROOT/failed/CLI-VERSION-TIME/. Either way the trial's session ends
(kill-session of that session alone, in a finally); one left by a trial killed before its end is ended by the next
trial of that version. One trial runs at a time (lock_path)."""
import contextlib
import datetime
import os
import re
import shlex
import time
import uuid
from pathlib import Path

from . import commands, config, jsonfile, launch, paneeffort, procs, registry

PLACEHOLDER_KEY = "placeholder-not-a-key"   # the CLI's API key: nothing a model call could use
KEY_VAR = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}
HOME_VAR = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
SETTINGS = {"claude": "settings.json", "codex": "config.toml"}   # in the config: where /effort saves a level
VERSION = re.compile(r"\d+\.\d+\.\d+")   # a version the trial takes (fullmatch): it goes into paths and a session name
ROOT = config.STATE_DIR / "fast-trial"
# Each CLI's own install: Claude Code's keeps a binary per version, VERSIONS/<version>; Codex's a folder per release,
# RELEASES/<version>-<target>/bin/codex. The link on PATH leads to the one in use.
CLAUDE_VERSIONS = config.HOME / ".local" / "share" / "claude" / "versions"
CODEX_RELEASES = config.HOME / ".codex" / "packages" / "standalone" / "releases"
START, LOW = "xhigh", paneeffort.LOW   # the level the CLI starts at and goes back to, and the one fast mode lowers to
DRAFT = "half-typed draft"
WIDTH, HEIGHT = 160, 50   # the pane's size
# The pane's shell, which starts the CLI: a plain one, which reads no one's rc files and keeps no history of the line.
SHELL = "/bin/sh"
START_SECONDS = 60   # for the CLI's idle prompt and its session to show
READY_SECONDS = 20   # for ready to let keys go: once started, and once the picker is closed
SHOW_SECONDS = 5     # for what was typed, a picker opened or a draft cleared to show
KEYS_SECONDS = 60    # for another program typing in a pane to let go of the keys lock
STEP = 0.5           # how often a wait looks again


class Failed(Exception):
    """The version did not pass, or the trial could not run; the message says why."""


def lock_path():
    """The lock a trial holds throughout, so that only one runs at a time."""
    return ROOT / "trial.lock"


def session_name(cli, version):
    """The trial's tmux session: fasttrial-CLI-VERSION, the version's dots as dashes (tmux takes no dot in a session's
    name)."""
    return "fasttrial-%s-%s" % (cli, version.replace(".", "-"))


def binary(cli, version):
    """The binary of cli's version in its own install (CLAUDE_VERSIONS, CODEX_RELEASES). Raises Failed when there is
    none there, or several."""
    if cli == "claude":
        where = CLAUDE_VERSIONS / version
        found = [where]
    else:
        where = CODEX_RELEASES / ("%s-*" % version) / "bin" / "codex"
        found = sorted(CODEX_RELEASES.glob("%s-*/bin/codex" % version))
    found = [p for p in found if p.is_file() and os.access(p, os.X_OK)]
    if not found:
        raise Failed("%s %s is not installed here (no %s)" % (paneeffort.NAME[cli], version, where))
    if len(found) > 1:
        raise Failed("%d releases of %s %s are installed here: %s" % (len(found), paneeffort.NAME[cli], version,
                                                                       ", ".join(map(str, found))))
    return found[0]


def installed_version(cli):
    """The version of the cli this machine runs (registry.claude_bin, codex_bin), from the path its link leads to in
    the CLI's own install. Raises Failed when none is installed, or it is not in that install."""
    exe = registry.claude_bin() if cli == "claude" else registry.codex_bin()
    if not exe:
        raise Failed("no %s is installed (not on PATH, nor in ~/.local/bin)" % cli)
    real = Path(os.path.realpath(exe))
    if cli == "claude":
        ver = real.name if real.parent == CLAUDE_VERSIONS.resolve() and VERSION.fullmatch(real.name) else None
    else:
        m = paneeffort.VERSION_IN_PATH.search(str(real))
        ver = m.group(1) if m and real.parents[2] == CODEX_RELEASES.resolve() else None
    if ver is None:
        root = CLAUDE_VERSIONS if cli == "claude" else CODEX_RELEASES
        raise Failed("the installed %s, %s, is not in %s, where the trial finds each version's binary: pass --version "
                     "for one there" % (cli, real, root))
    return ver


def seed(cli, home, work):
    """Give cli's isolated config, home, the least that brings it up at its idle prompt in the folder work with no
    dialog, keeping what the CLI wrote there on earlier runs. Claude Code, in its .claude.json: onboarding done (the
    theme is one of its steps), the placeholder key approved (its last 20 characters, as Claude Code keeps an answer:
    no "Detected a custom API key" question), work trusted (no trust dialog), and external imports refused, as by one
    who was asked: a CLAUDE.md in a folder above work (such as ~/CLAUDE.md, above the state folder) that imports
    files from outside it would otherwise ask, and its imports are not read. Codex: auth.json signed in with the
    placeholder key (no sign-in screen); it asks no trust of a folder outside a git repo. Raises ValueError for a
    .claude.json that is not JSON (jsonfile.read), OSError when a file cannot be read or written."""
    home.mkdir(parents=True, exist_ok=True)
    if cli == "codex":
        jsonfile.write(home / "auth.json", {"auth_mode": "apikey", "OPENAI_API_KEY": PLACEHOLDER_KEY},
                       jsonfile.PRIVATE, 2)
        return
    path = home / ".claude.json"
    data = jsonfile.read(path)
    data["hasCompletedOnboarding"] = True
    answers = data.setdefault("customApiKeyResponses", {})
    approved = answers.setdefault("approved", [])
    if PLACEHOLDER_KEY[-20:] not in approved:
        approved.append(PLACEHOLDER_KEY[-20:])
    answers.setdefault("rejected", [])
    data.setdefault("projects", {}).setdefault(str(work), {}).update(
        hasTrustDialogAccepted=True,   # as trust.trust_claude
        hasClaudeMdExternalIncludesApproved=False, hasClaudeMdExternalIncludesWarningShown=True)
    jsonfile.write(path, data, jsonfile.PRIVATE, 2)


def launch_line(cli, link, home):
    """The line the trial's pane shell runs to start cli through link (named for it) at START with the model
    agent-spawn gives an agent (config spawn_*_model; none: the CLI's own), in the config home, with nothing of this
    process's environment (env -i) but HOME, a plain PATH and terminal, DISABLE_AUTOUPDATER=1 (Claude Code installs
    nothing), the pane's own $TMUX and $TMUX_PANE (Claude Code's registry entry records its pane from them:
    paneeffort.place), the placeholder key and the config. No exec: the pane's shell stays the agent's parent, which
    paneeffort.ready checks."""
    model = config.spawn_models(config.load())[cli]
    if cli == "claude":
        args = (["--model", model] if model else []) + ["--effort", START]
    else:
        args = registry.CODEX_NO_UPDATE_PROMPT + (["-m", model] if model else []) + launch.codex_effort(START)
    env = ["HOME=" + shlex.quote(str(config.HOME)), "PATH=/usr/bin:/bin", "TERM=xterm-256color", "LANG=C.UTF-8",
           "DISABLE_AUTOUPDATER=1", 'TMUX="$TMUX"', 'TMUX_PANE="$TMUX_PANE"', "%s=%s" % (KEY_VAR[cli], PLACEHOLDER_KEY),
           "%s=%s" % (HOME_VAR[cli], shlex.quote(str(home)))]
    return "env -i %s %s" % (" ".join(env), " ".join(shlex.quote(str(w)) for w in [link] + args))


def point(link, target):
    """Make link a symbolic link to target, in one step (a new link renamed over the old)."""
    tmp = link.with_name(".%s.%s.tmp" % (link.name, uuid.uuid4().hex))
    os.symlink(target, tmp)
    os.replace(tmp, link)


@contextlib.contextmanager
def registry_home(cli, home):
    """registry's home for cli (CLAUDE_HOME, CODEX_HOME) is home for the with-block, so that fast mode's code finds the
    trial's agent there as it finds a real one in the real home: paneeffort.ready reads the Claude registry again,
    registry.codex_pane the Codex locks. registry reads both when called; this process runs the trial alone."""
    attr = {"claude": "CLAUDE_HOME", "codex": "CODEX_HOME"}[cli]
    old = getattr(registry, attr)
    setattr(registry, attr, Path(home))
    try:
        yield
    finally:
        setattr(registry, attr, old)


def agent(cli, home, shell):
    """The trial's agent as fast mode has a session (a registry session; Codex's by its thread, as fastmode gives it),
    once the CLI the pane's shell (pid shell) started is running with its config in home, else None."""
    if cli == "claude":
        mine = [s for s in registry.claude_sessions()
                if s["live"] and s["config_dir"] == str(home) and s.get("pid") and procs.ppid(int(s["pid"])) == shell]
        return mine[0] if len(mine) == 1 else None
    holders = registry.codex_lock_holders(home / "thread-writer-locks") or {}
    mine = [tid for tid, pids in holders.items() if len(pids) == 1 and procs.ppid(pids[0]) == shell]
    return {"cli": "codex", "id": mine[0], "name": "", "live": True} if len(mine) == 1 else None


class Pane:
    """The trial's own tmux pane (id), the only one it reads or types in, and the screens it kept to show on a
    failure."""

    def __init__(self, cli, id_):
        self.cli, self.id, self.screens = cli, id_, []

    def screen(self, label=None):
        """What the pane shows now, kept under label if one is given."""
        text = commands.tmux("capture-pane", "-p", "-t", self.id, check=True).stdout
        if label:
            self.screens.append((label, text))
        return text

    def keys(self, *keys):
        commands.tmux("send-keys", "-t", self.id, *keys, check=True)

    def typed(self, text):
        """Whether the prompt's line shows text."""
        return any(line.startswith(paneeffort.PROMPT[self.cli]) and text in line for line in self.screen().split("\n"))

    def keep(self, cli, version):
        """Write the screens kept to a new folder under ROOT/failed/ and return it, or None when none was kept."""
        if not self.screens:
            return None
        folder = ROOT / "failed" / ("%s-%s-%s" % (cli, version, time.strftime("%Y%m%d-%H%M%S")))
        for i, (label, text) in enumerate(self.screens, 1):
            name = "%02d-%s.txt" % (i, re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"))
            jsonfile.write_text(folder / name, text, jsonfile.PLAIN)
        return folder


def ready(pane, sess, place, when):
    """Wait READY_SECONDS at most for paneeffort.ready to let keys go; raises Failed with its reason if it does not."""
    why = [""]

    def now():
        why[0] = paneeffort.ready(sess, place)
        return not why[0]
    if not launch.wait_for(now, READY_SECONDS, STEP):
        pane.screen("not ready " + when)
        raise Failed("paneeffort.ready did not let keys go %s within %d s: %s" % (when, READY_SECONDS, why[0]))


def step(pane, sess, place, target, start):
    """paneeffort.set_level from start to target, which must take one key a level. Returns the keys sent."""
    want = abs(paneeffort.ORDER.index(target) - paneeffort.ORDER.index(start))
    try:
        sent = paneeffort.set_level(sess, place, target, start, lambda: None)
    except (paneeffort.Wait, paneeffort.EffortError) as e:
        pane.screen("toward " + target)
        raise Failed("paneeffort.set_level from %s to %s: %s (after %d keys)" % (start, target, e, e.pressed)) from None
    pane.screen("at " + target)
    if sent != want:
        raise Failed("paneeffort.set_level from %s to %s took %d keys, not one a level (%d)" % (start, target, sent,
                                                                                               want))
    return sent


def draft(pane, sess, place):
    """A key down and one up with a half-typed draft (DRAFT) in the prompt leave it there; C-u then clears it."""
    pane.keys("-l", DRAFT)
    if not launch.wait_for(lambda: pane.typed(DRAFT), SHOW_SECONDS, STEP):
        pane.screen("draft typed")
        raise Failed("the draft typed did not show in the prompt within %d s" % SHOW_SECONDS)
    below = paneeffort.ORDER[paneeffort.ORDER.index(START) - 1]
    step(pane, sess, place, below, START)
    kept = pane.typed(DRAFT)
    step(pane, sess, place, START, below)
    if not (kept and pane.typed(DRAFT)):
        raise Failed("a key pressed with a half-typed draft in the prompt took the draft away")
    pane.keys("C-u")
    if not launch.wait_for(lambda: not pane.typed(DRAFT), SHOW_SECONDS, STEP):
        pane.screen("draft not cleared")
        raise Failed("C-u did not clear the draft within %d s" % SHOW_SECONDS)


def picker(pane, sess, place):
    """ready refuses while the /model picker is open, and lets keys go again once Escape has closed it. Returns its
    reason for refusing."""
    pane.keys("-l", "/model")
    if not launch.wait_for(lambda: pane.typed("/model"), SHOW_SECONDS, STEP):
        pane.screen("/model typed")
        raise Failed("/model typed did not show in the prompt within %d s" % SHOW_SECONDS)
    time.sleep(STEP)   # the command menu under the prompt settles on /model before Enter picks it
    pane.keys("Enter")
    why = launch.wait_for(lambda: paneeffort.ready(sess, place), SHOW_SECONDS, STEP)
    pane.screen("the /model picker")
    if not why:
        raise Failed("paneeffort.ready let keys go with the /model picker open")
    pane.keys("Escape")
    ready(pane, sess, place, "once the /model picker was closed")
    return why


def read_settings(path):
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def steps(pane, cli, version, home, link):
    """The trial in pane, its session's own (the module's docstring, step 3). Returns what it saw."""
    shell = int(commands.tmux("display-message", "-p", "-t", pane.id, "#{pane_pid}", check=True).stdout)
    if not launch.wait_for(lambda: pane.screen().strip(), SHOW_SECONDS, STEP):   # its prompt: it reads what is typed
        pane.screen("shell")
        raise Failed("the pane's shell showed no prompt within %g s" % SHOW_SECONDS)
    pane.keys("-l", launch_line(cli, link, home))
    pane.keys("Enter")
    sess = launch.wait_for(lambda: paneeffort.view(cli, pane.screen()) is not None and agent(cli, home, shell),
                           START_SECONDS, STEP)
    pane.screen("started")
    if not sess:
        raise Failed("%s's idle prompt and session did not show within %d s (a start-up dialog that seed does not "
                     "answer?)" % (paneeffort.NAME[cli], START_SECONDS))
    place, why = paneeffort.place(sess)
    if place is None or place.id != pane.id:
        raise Failed("paneeffort.place did not find the trial's pane %s: %s" % (pane.id,
                                                                                why or "it found %s" % (place,)))
    running = paneeffort.version(sess, place)
    if running != version:
        raise Failed("the agent in the pane runs %s %s, not %s" % (paneeffort.NAME[cli], running, version))
    ready(pane, sess, place, "once started")
    level = paneeffort.current(sess, place)
    if level != START:
        raise Failed("paneeffort.current reads %s, not %s, the level the agent was started at" % (level, START))
    settings = home / SETTINGS[cli]
    before = read_settings(settings)   # read once started: a CLI may write its settings as it starts
    with contextlib.ExitStack() as held:
        try:   # set_level takes the keys lock again without waiting, and finds it held
            held.enter_context(commands.keys_lock(KEYS_SECONDS))
        except commands.LockBusy as e:
            raise Failed("another program typing in a pane kept the keys lock: %s" % e) from None
        keys = step(pane, sess, place, LOW, START)
        step(pane, sess, place, START, LOW)
        draft(pane, sess, place)
    refused = picker(pane, sess, place)
    if read_settings(settings) != before:
        raise Failed("%s changed while the keys were pressed: they must save nothing (only /effort saves a level)"
                     % settings)
    return "effort %s -> %s -> %s, %d keys each way; a draft left alone; ready refused with the /model picker open " \
           "(%s); %s unchanged" % (START, LOW, START, keys, refused, settings.name)


def trial(cli, version):
    """Run the trial (the module's docstring) on cli's version in a tmux session of its own, which it always ends.
    Returns what it saw, for the line of outcome. Raises Failed when the version does not pass or the trial cannot
    run, naming the folder of the screens it read (Pane.keep); OSError or ValueError when its config cannot be
    seeded."""
    exe = binary(cli, version)
    base = ROOT / cli
    for d in ("home", "work", "bin"):
        (base / d).mkdir(parents=True, exist_ok=True)
    home, work, link = (base / "home").resolve(), (base / "work").resolve(), (base / "bin").resolve() / cli
    point(link, exe)
    seed(cli, home, work)
    name, pane = session_name(cli, version), None
    try:
        with registry_home(cli, home):
            if commands.tmux_has(name):   # left by a trial killed before its end: the lock says none runs now
                commands.tmux("kill-session", "-t", "=" + name, check=True)
            made = commands.tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name, "-x", str(WIDTH),
                                 "-y", str(HEIGHT), "-c", str(work), "exec " + SHELL, check=True)
            pane = Pane(cli, made.stdout.strip())
            return steps(pane, cli, version, home, link)
    except Exception as e:
        if pane is None:
            raise Failed(str(e)) from e
        with contextlib.suppress(commands.CommandError):
            pane.screen("at the failure")
        kept = pane.keep(cli, version)
        raise Failed("%s; %s" % (e, "screens in %s" % kept if kept else "no screen was read")) from e
    finally:
        if pane is not None:
            commands.tmux("kill-session", "-t", "=" + name, check=False)


def run(cli, version, if_untested):
    """agent-mail fast-trial: (exit code, its one line of outcome). 0: cli's version (None: the installed one) passed
    and is now on fast mode's tested list, or with if_untested was on it already; 1: it failed, or could not be
    tried."""
    label = "fast-trial %s" % cli
    try:
        version = version or installed_version(cli)
        label += " " + version
        if if_untested and version in paneeffort.tested(cli):
            return 0, "%s: on fast mode's tested list already; nothing to do" % label
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(commands.file_lock(lock_path(), False))
            except commands.LockBusy:
                return 1, "%s: FAILED: another fast-trial is running (it holds %s)" % (label, lock_path())
            seen = trial(cli, version)
            paneeffort.add_tested(cli, version,
                                  datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
    except (Failed, commands.CommandError, OSError, ValueError) as e:
        return 1, "%s: FAILED: %s" % (label, e)
    return 0, "%s: passed (%s); added to fast mode's tested list, %s" % (label, seen, paneeffort.TESTED_PATH)
