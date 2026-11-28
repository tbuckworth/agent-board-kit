"""Temporary homes for the files agentmail reads and writes: config.json, the status log, and the session registries
of Claude Code and Codex (agentmail/registry.py); and a repo whose worktree has a review gate its main checkout
lacks; and agent-group's commands run with their arguments filled in (run_group). A test writes and reads real files
there instead of stubbing the code that does, and nothing touches the live config, status log, ~/.claude or ~/.codex.
It imports isolate_state, then agentmail: a test module puts the repo on sys.path before importing it."""
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import isolate_state  # noqa: F401  (temporary state dirs and the test identity; before agentmail)
from agentmail import config, groupcmd, groups, registry, statuslog


def temp_dir(test):
    """A new temporary folder, removed with what is in it when the test ends."""
    d = Path(tempfile.mkdtemp()).resolve()
    test.addCleanup(shutil.rmtree, d)
    return d


def patch(test, obj, name, value):
    """obj.name is value until the test ends."""
    p = mock.patch.object(obj, name, value)
    p.start()
    test.addCleanup(p.stop)


def gated_worktree(test):
    """(main checkout, linked worktree) of a new git repo whose worktree has a .codex/hooks.json (the review gate set
    up on its branch) and whose main checkout has none, both in a temporary folder removed when the test ends."""
    repo = temp_dir(test) / "tool"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "first"], check=True)
    wt = repo.parent / "tool-gate"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "gate", str(wt)], check=True)
    (wt / ".codex").mkdir()
    (wt / ".codex" / "hooks.json").write_text('{"hooks": {}}\n')
    return repo, wt


def config_file(test, data):
    """config.json holding data over the test identity (isolate_state.IDENTITY), in a temporary folder;
    config.CONFIG_PATH points at it until the test ends. Returns its path."""
    path = temp_dir(test) / "config.json"
    path.write_text(json.dumps(dict(isolate_state.IDENTITY, **data)))
    patch(test, config, "CONFIG_PATH", path)
    return path


EXAMPLE_SETTINGS = Path(__file__).resolve().parent.parent / "config" / "claude-openrouter-settings.example.json"
NEON = "google/gemini-4-neon"   # the model of the example's login


def per_token_login(test, folder=None):
    """A Claude login billed per token, as config usage.claude_accounts names one ("manual", a settings file and a
    model of its own): its config dir, with no /login in it, and a copy of EXAMPLE_SETTINGS, in folder (default: a
    temporary one, removed when the test ends). Returns its account entry."""
    folder = Path(folder) if folder else temp_dir(test)
    (folder / ".claude-gemini").mkdir()
    settings = folder / "gemini-claude-settings.json"
    shutil.copy(str(EXAMPLE_SETTINGS), str(settings))
    return {"config_dir": str(folder / ".claude-gemini"), "label": "Gemini Neon (OpenRouter)",
            "settings": str(settings), "model": NEON, "manual": True, "weekly_budget_usd": 500}


def researcher_install(home, cli, version):
    """Install a minimal test plugin in the real CLI cache format without running its workflow."""
    marketplace = config.DEFAULTS["researcher_marketplaces"][cli]
    root = home / "plugins" / "cache" / marketplace / "researcher" / version
    files = {
        ".%s-plugin/plugin.json" % cli: json.dumps({"name": "researcher", "version": version}),
        "commands/researcher.md": "Read docs/WORKFLOW.md.",
        "docs/WORKFLOW.md": "Verified Researcher fixture.",
        "scripts/methods_review.py": ("import argparse\nparser = argparse.ArgumentParser()\n"
                                      "parser.add_argument('action', choices=('approve-human', 'verify'))\n"
                                      "parser.parse_args()\n"),
    }
    if cli == "codex":
        files.update({"skills/researcher/SKILL.md": "Read the command.", "templates/codex-adapter.md": "Codex adapter."})
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if cli == "claude":
        record = {"scope": "user", "installPath": str(root), "version": version}
        (home / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"researcher@" + marketplace: [record]}}))
    return root


def status_log(test):
    """A status log of the test's own: statuslog.LOG is a file in a temporary folder until the test ends, so
    statuslog.read sees only the events this test wrote. Returns its path."""
    path = temp_dir(test) / "events.jsonl"
    patch(test, statuslog, "LOG", path)
    return path


def dead_pid():
    """The pid of a process that has exited and been reaped: a registry entry with it is a claude that is gone."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class Homes:
    """A Claude home for each login and a Codex home, in a temporary folder; until the test ends the registry reads
    them and config.json names the logins. logins: the Claude logins' names, the first being the default login
    (~/.claude) and each other one a second login with its own config dir (.claude-<name>). cfg: more config.json
    keys."""

    def __init__(self, test, logins=("default",), cfg=None):
        self.test = test
        self.base = temp_dir(test)
        self.homes = {name: self.base / (".claude" if i == 0 else ".claude-" + name) for i, name in enumerate(logins)}
        accounts = {name: {"config_dir": None if i == 0 else str(self.homes[name])} for i, name in enumerate(logins)}
        self.config = config_file(test, dict(cfg or {}, usage=dict((cfg or {}).get("usage") or {},
                                                                    claude_accounts=accounts)))
        self.default = self.homes[logins[0]]
        self.codex_home = self.base / ".codex"
        for home in list(self.homes.values()) + [self.codex_home]:
            home.mkdir()
        self.socket = self.base / "inbox.sock"   # a live session's messaging socket: the registry checks it exists
        self.socket.write_text("")
        for name, value in (("DEFAULT_CLAUDE_HOME", self.default), ("CLAUDE_HOME", self.default),
                            ("CODEX_HOME", self.codex_home)):
            patch(test, registry, name, value)

    def claude(self, sid, name="", login=None, live=True, cwd="/w", updated_at=0, **fields):
        """A Claude session's registry entry on login (default: the default login): live is this process's pid with
        the socket there, else a pid that is gone. updated_at in seconds; fields: more of the entry's keys, as
        Claude Code writes them. Returns the entry's path."""
        sessions = self.homes[login] / "sessions" if login else self.default / "sessions"
        sessions.mkdir(exist_ok=True)
        pid = os.getpid() if live else dead_pid()
        entry = dict({"pid": pid, "sessionId": sid, "name": name, "cwd": cwd, "statusUpdatedAt": updated_at * 1000},
                     **fields)
        if live:
            entry["messagingSocketPath"] = str(self.socket)
        path = sessions / ("%d.json" % pid)
        n = 1
        while path.exists():   # a second live entry has this process's pid too
            path, n = sessions / ("%d-%d.json" % (pid, n)), n + 1
        path.write_text(json.dumps(entry))
        return path

    def codex(self, tid, name="", title="", live=True, cwd="/w", updated_at=0):
        """A Codex thread: its row in Codex's state database, its /rename name in the session index, and for a live
        one a lock file held open by a process named codex, as Codex holds it (stopped when the test ends)."""
        con = sqlite3.connect(str(self.codex_home / "state_5.sqlite"))
        with con:
            con.execute("create table if not exists threads "
                        "(id text, title text, cwd text, archived int, updated_at int, rollout_path text)")
            con.execute("insert into threads values (?, ?, ?, 0, ?, null)", (tid, title, cwd, updated_at))
        con.close()
        if name:
            with open(self.codex_home / "session_index.jsonl", "a") as fh:
                fh.write(json.dumps({"id": tid, "thread_name": name}) + "\n")
        if live:
            lock = self.codex_home / "thread-writer-locks" / (tid + ".lock")
            lock.parent.mkdir(exist_ok=True)
            lock.write_text("")
            with open(lock) as fh:
                holder = subprocess.Popen(["codex", "3600"], executable=shutil.which("sleep"), stdin=fh)
            self.test.addCleanup(holder.wait)
            self.test.addCleanup(holder.kill)


def run_group(cfg, who, action, **args):
    """agent-group's command action for who (groupcmd.run), every argument groupcmd.ACTIONS lists for it None but
    those given, and a new id for a post or a group when none is."""
    full = {k: None for k in groupcmd.ACTIONS[action]}
    full.update(args)
    if action == "post" and full["post_id"] is None:
        full["post_id"] = groups.new_id()
    if action == "new" and full["group_id"] is None:
        full["group_id"] = groups.new_id()
    return groupcmd.run(cfg, who, action, full)
