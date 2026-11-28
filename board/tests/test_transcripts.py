"""The transcript archive: copying without losing anything, what each transcript says, the catalog's groups (placed,
guessed, frozen), the by-group tree and the list command. Every file goes to a temporary folder; the model's guess
and the Mac's ssh and rsync are stood in for at the process boundary."""
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fixtures import git, iso, parse_file, write_jsonl  # noqa: E402
from agentboard import classify, groups, transcripts  # noqa: E402

T0 = 1793000000.0
DAY = 86400
A, B, C, D, E = (str(uuid.UUID(int=n)) for n in range(1, 6))
REPO = Path(__file__).resolve().parent.parent
LAYOUT = [{"name": "Research", "subgroups": ["Evals"]}, {"name": "Home", "subgroups": []}]


class Sandbox:
    """The archive's own state (its lock, log, parse and guess caches) and the board's groups moved to a temporary
    folder, never this machine's."""
    NAMES = ("STATE", "LOCK", "LOG", "PARSED", "GUESSES")

    def __enter__(self):
        self.d = Path(tempfile.mkdtemp())
        self.saved = {k: getattr(transcripts, k) for k in self.NAMES}
        self.saved_groups = groups.GROUPS
        for k in self.NAMES:
            setattr(transcripts, k, self.d / ("state" if k == "STATE" else Path(str(self.saved[k])).name))
        groups.GROUPS = self.d / "groups.json"
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(transcripts, k, v)
        groups.GROUPS = self.saved_groups


def claude_user(sid, text, ts, cwd="/x/repo", **kw):
    return dict({"type": "user", "sessionId": sid, "timestamp": iso(ts), "cwd": cwd, "entrypoint": "cli",
                 "message": {"role": "user", "content": text}}, **kw)


def codex_rollout(path, tid, ts, text, cwd="/x/repo", parent=None):
    source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "cli"
    meta = {"id": tid, "cwd": cwd, "timestamp": iso(ts), "source": source}
    if parent:
        meta["parent_thread_id"] = parent
    rows = [{"type": "session_meta", "timestamp": iso(ts), "payload": meta},
            {"type": "response_item", "timestamp": iso(ts + 1), "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /x"}]}},
            {"type": "response_item", "timestamp": iso(ts + 2), "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}]
    return write_jsonl(path, rows)


def set_mtime(path, ts):
    os.utime(path, (ts, ts))


# ------------------------------------------------------------------ copying

class Copying(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.src, self.dst, self.old = self.d / "src", self.d / "dst", self.d / "versions"
        self.src.mkdir()

    def write(self, rel, data, ts):
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        set_mtime(p, ts)

    def every_file(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}

    def test_new_grown_rewritten_shrunk_touched_and_same(self):
        self.write("p/grows.jsonl", b"one\n", T0)
        self.write("p/rewritten.jsonl", b"abcd\n", T0)
        self.write("p/shrinks.jsonl", b"long line\n", T0)
        self.write("p/touched.jsonl", b"same\n", T0)
        self.write("p/still.jsonl", b"still\n", T0)
        first = transcripts.sync_tree(self.src, self.dst, self.old / "run1")
        self.assertEqual(first, {"new": 5})
        self.assertEqual(self.every_file(self.dst), self.every_file(self.src))
        self.assertEqual(os.stat(self.dst / "p/grows.jsonl").st_mtime, T0)          # the source's time is kept
        for folder in (self.dst, self.dst / "p"):                                    # every folder made is private
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)

        self.write("p/grows.jsonl", b"one\ntwo\n", T0 + 10)
        self.write("p/rewritten.jsonl", b"abXd\n", T0 + 10)      # same length, other bytes
        self.write("p/shrinks.jsonl", b"short\n", T0 + 10)
        self.write("p/touched.jsonl", b"same\n", T0 + 10)        # same bytes, a new time
        self.write("p/new.jsonl", b"new\n", T0 + 10)
        second = transcripts.sync_tree(self.src, self.dst, self.old / "run2")
        self.assertEqual(second, {"grown": 1, "rewritten": 2, "touched": 1, "same": 1, "new": 1})
        self.assertEqual(self.every_file(self.dst), self.every_file(self.src))
        # the versions written over are kept, as they were; a file that only grew keeps no old copy
        self.assertEqual(self.every_file(self.old), {"run2/p/rewritten.jsonl": b"abcd\n",
                                                     "run2/p/shrinks.jsonl": b"long line\n"})
        for f in self.every_file(self.src):
            self.assertEqual(os.stat(self.dst / f).st_mtime, os.stat(self.src / f).st_mtime)
        self.assertEqual(transcripts.sync_tree(self.src, self.dst, self.old / "run3"), {"same": 6})

    def test_a_grown_file_gets_only_its_new_end_appended(self):
        self.write("a.jsonl", b"x" * 3000000 + b"\n", T0)
        transcripts.sync_tree(self.src, self.dst, self.old)
        ino = os.stat(self.dst / "a.jsonl").st_ino
        with open(self.src / "a.jsonl", "ab") as fh:
            fh.write(b"y\n")
        self.assertEqual(transcripts.sync_tree(self.src, self.dst, self.old), {"grown": 1})
        self.assertEqual(os.stat(self.dst / "a.jsonl").st_ino, ino)          # the same file, appended to
        self.assertEqual((self.dst / "a.jsonl").read_bytes(), (self.src / "a.jsonl").read_bytes())

    def test_a_file_kept_twice_in_one_run_never_writes_over_the_first(self):
        dst = self.d / "copy"
        for n, data in enumerate((b"first\n", b"second!\n", b"third!!!\n")):
            self.write("f", data, T0 + n)
            transcripts.archive_file(self.src / "f", os.lstat(self.src / "f"), dst, self.old / "f")
        self.assertEqual(self.every_file(self.old), {"f": b"first\n", "f.again": b"second!\n"})
        self.assertEqual(dst.read_bytes(), b"third!!!\n")

    def test_links_and_other_files_that_are_not_regular_are_not_followed(self):
        self.write("real.jsonl", b"r\n", T0)
        outside = self.d / "outside"
        outside.mkdir()
        (outside / "secret").write_text("not a transcript")
        os.symlink(outside, self.src / "linked-dir")
        os.symlink(self.src / "real.jsonl", self.src / "linked.jsonl")
        self.assertEqual(transcripts.sync_tree(self.src, self.dst, self.old), {"new": 1})
        self.assertEqual(list(self.every_file(self.dst)), ["real.jsonl"])


# ------------------------------------------------------------------ reading transcripts

class Reading(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_claude_title_name_summary_and_start(self):
        p = write_jsonl(self.d / (A + ".jsonl"), [
            {"type": "permission-mode", "sessionId": A, "permissionMode": "default"},
            claude_user(A, "<command-name>/model</command-name>", T0),
            claude_user(A, "Caveat: the messages below", T0 + 1, isMeta=True),
            claude_user(A, "  Fix the   flaky test\nin sources.py  ", T0 + 2),
            claude_user(A, "and another thing", T0 + 3),
            {"type": "custom-title", "sessionId": A, "customTitle": "flaky-fix"},
            {"type": "ai-title", "sessionId": A, "aiTitle": "Fixing a flaky test"}])
        got = parse_file(transcripts.parse_claude, p)
        self.assertEqual((got["sid"], got["first"], got["name"], got["summary"], got["cwd"], got["entrypoint"]),
                         (A, "Fix the flaky test in sources.py", "flaky-fix", "Fixing a flaky test", "/x/repo", "cli"))
        self.assertEqual(got["start"], T0)

    def test_a_spawn_brief_gives_its_own_text_and_the_session_name(self):
        brief = (classify.SPAWN_MARK + " on the desktop, on the owner's behalf. Session name: docs-fix. Working "
                 "directory: /x. Branch: b.\n\nProtocol: ...\n\nBrief:\n\nWrite the setup guide")
        got = parse_file(transcripts.parse_claude, write_jsonl(self.d / "s.jsonl", [claude_user(A, brief, T0)]))
        self.assertEqual((got["first"], got["name"]), ("Write the setup guide", "docs-fix"))
        got = parse_file(transcripts.parse_codex, codex_rollout(self.d / "r.jsonl", C, T0, brief))
        self.assertEqual((got["first"], got["name"]), ("Write the setup guide", "docs-fix"))

    def test_codex_thread_its_parent_and_first_message(self):
        got = parse_file(transcripts.parse_codex, codex_rollout(self.d / "r.jsonl", D, T0, "Look into X", parent=C))
        self.assertEqual((got["sid"], got["parent"], got["entrypoint"], got["first"], got["start"]),
                         (D, C, "subagent", "Look into X", T0))

    def test_a_long_first_message_is_trimmed(self):
        got = parse_file(transcripts.parse_claude, write_jsonl(self.d / "s.jsonl", [claude_user(A, "word " * 200, T0)]))
        self.assertEqual(len(got["first"]), transcripts.TITLE_CHARS)


# ------------------------------------------------------------------ groups

class Grouping(unittest.TestCase):
    """decide(): the board's placement, then the Time page's grouping (its repo's tiles, then the model), and
    freezing once a session ends. The guesser stands in for the model, a paid call."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        for r in ("evals", "lone"):
            git("init", "-q", "-b", "main", str(self.root / r))
        self.asked = []

    def rec(self, sid, repo, last, title="t"):
        return {"name": "", "title": title, "repo": str(self.root / repo), "last_activity": last}

    def guesser(self, answer):
        def g(items, layout, history, labels):
            self.asked.append(list(items))
            return {k: answer.get(k[2:]) for k in items}, {"asked": len(items), "batches": 1}, None
        return g

    def test_placed_beats_guessed_then_repo_then_model_then_unknown(self):
        records = {A: self.rec(A, "evals", T0), B: self.rec(B, "evals", T0), C: self.rec(C, "lone", T0),
                   D: self.rec(D, "lone", T0)}
        history = {A: {"group": "Research", "subgroup": "Evals"}}
        got, stats = transcripts.decide(records, {}, {A, B, C, D}, history, LAYOUT,
                                        self.guesser({A: ["Home", None], C: ["Home", None]}), T0, 2 * DAY, 10)
        self.assertEqual(got[A], {"group_name": "Research", "subgroup": "Evals", "group_source": "placed",
                                  "group_basis": "board", "frozen_at": None})
        self.assertEqual(got[B]["group_name"], "Research")            # its repo's placed tile is in Research
        self.assertEqual((got[B]["group_source"], got[B]["group_basis"]), ("guessed", "repo"))
        self.assertEqual((got[C]["group_name"], got[C]["group_basis"]), ("Home", "model"))
        self.assertEqual((got[D]["group_name"], got[D]["group_source"]), (None, "unknown"))
        self.assertEqual(sorted(self.asked[0]), sorted(["s:" + C, "s:" + D]))       # never A or B
        self.assertEqual((stats["frozen"], stats["waiting"]), (0, 0))

    def test_an_ended_session_is_frozen_and_a_later_run_keeps_its_group(self):
        records = {A: self.rec(A, "evals", T0), B: self.rec(B, "lone", T0), C: self.rec(C, "lone", T0 + 3 * DAY)}
        now = T0 + 3 * DAY
        got, stats = transcripts.decide(records, {}, {C}, {A: {"group": "Research", "subgroup": None}}, LAYOUT,
                                        self.guesser({B: ["Home", None], C: ["Home", None]}), now, 2 * DAY, 10)
        self.assertEqual((got[A]["frozen_at"], got[B]["frozen_at"], got[C]["frozen_at"]), (now, now, None))
        self.assertEqual(stats["frozen"], 2)
        # the owner moves A, and the model would now say Research: the ended sessions keep the group they ended with,
        # and only the live one follows
        later = now + DAY
        self.asked.clear()
        again, _ = transcripts.decide(records, got, {C}, {A: {"group": "Home", "subgroup": None},
                                                          C: {"group": "Research", "subgroup": "Evals"}}, LAYOUT,
                                      self.guesser({B: ["Research", None]}), later, 2 * DAY, 10)
        self.assertEqual((again[A]["group_name"], again[A]["frozen_at"]), ("Research", now))
        self.assertEqual((again[B]["group_name"], again[B]["frozen_at"]), ("Home", now))
        self.assertEqual((again[C]["group_name"], again[C]["subgroup"], again[C]["group_source"]),
                         ("Research", "Evals", "placed"))
        self.assertEqual(self.asked, [])                           # nothing frozen is asked about again

    def test_a_frozen_session_counts_toward_its_repo_while_its_placement_is_on_the_board(self):
        now = T0 + 3 * DAY
        frozen = {"group_name": "Research", "subgroup": None, "group_source": "placed", "group_basis": "board",
                  "frozen_at": now}
        records = {A: self.rec(A, "evals", T0), B: self.rec(B, "evals", now), C: self.rec(C, "lone", T0)}
        history = {A: {"group": "Research", "subgroup": None}, C: {"group": "Gone", "subgroup": None}}
        got, _ = transcripts.decide(records, {A: frozen, C: dict(frozen, group_name="Gone")}, {B}, history, LAYOUT,
                                    self.guesser({}), now, 2 * DAY, 10)
        self.assertEqual((got[B]["group_name"], got[B]["group_basis"]), ("Research", "repo"))
        self.assertEqual(got[C]["group_name"], "Gone")              # frozen, and its group off the board: never asked
        self.assertEqual(self.asked, [])

    def test_a_frozen_session_that_runs_or_writes_again_is_grouped_afresh(self):
        now = T0 + 3 * DAY
        frozen = {"group_name": "Home", "subgroup": None, "group_source": "guessed", "group_basis": "model",
                  "frozen_at": now}
        records = {A: self.rec(A, "lone", T0), B: self.rec(B, "lone", now + DAY)}
        history = {A: {"group": "Research", "subgroup": None}, B: {"group": "Research", "subgroup": "Evals"}}
        got, _ = transcripts.decide(records, {A: frozen, B: frozen}, {A}, history, LAYOUT, self.guesser({}),
                                    now + 4 * DAY, 2 * DAY, 10)
        self.assertEqual((got[A]["group_name"], got[A]["frozen_at"]), ("Research", None))        # running again
        self.assertEqual((got[B]["group_name"], got[B]["subgroup"], got[B]["frozen_at"]),    # resumed, ended again
                         ("Research", "Evals", now + 4 * DAY))

    def test_an_unknown_session_is_not_frozen_and_the_model_is_asked_about_a_few_at_a_time(self):
        records = {s: self.rec(s, "lone", T0 + n) for n, s in enumerate((A, B, C))}
        got, stats = transcripts.decide(records, {}, set(), {}, LAYOUT, self.guesser({C: ["Home", None]}),
                                        T0 + 5 * DAY, 2 * DAY, 2)
        self.assertEqual(sorted(self.asked[0]), sorted(["s:" + C, "s:" + B]))   # the most recently active first
        self.assertEqual((got[C]["group_name"], got[C]["frozen_at"]), ("Home", T0 + 5 * DAY))
        self.assertEqual((got[A]["group_source"], got[A]["frozen_at"]), ("unknown", None))
        self.assertEqual((got[B]["group_source"], got[B]["frozen_at"]), ("unknown", None))
        self.assertEqual(stats["waiting"], 1)


# ------------------------------------------------------------------ the whole run

class Archive(unittest.TestCase):
    """archive() on a stand-in desktop (two Claude logins and Codex) and a stand-in Mac reached through a stood-in
    ssh and rsync."""

    def setUp(self):
        self.sb = Sandbox().__enter__()
        self.d = Path(tempfile.mkdtemp())
        self.home, self.mac, self.root = self.d / "home", self.d / "mac", self.d / "archive"
        self.root.mkdir()
        proj = self.home / ".claude/projects/-x-repo"
        write_jsonl(proj / (A + ".jsonl"), [claude_user(A, "First copy", T0)])
        write_jsonl(proj / A / "subagents/agent-1.jsonl",
                    [claude_user(A, "a subagent's task", T0 + 5, isSidechain=True)])
        (proj / A / "tool-results").mkdir(parents=True)
        (proj / A / "tool-results/out.txt").write_text("output")
        (proj.parent / "-x-repo/memory").mkdir()
        (proj.parent / "-x-repo/memory/MEMORY.md").write_text("a memory")
        write_jsonl(self.home / ".claude-work/projects/-x-repo" / (A + ".jsonl"),
                    [claude_user(A, "First copy", T0), claude_user(A, "carried on after failover", T0 + 60)])
        codex_rollout(self.home / (".codex/sessions/2026/10/26/rollout-1-%s.jsonl" % C), C, T0, "Codex job")
        codex_rollout(self.home / (".codex/sessions/2026/10/26/rollout-2-%s.jsonl" % D), D, T0, "sub job", parent=C)
        write_jsonl(self.mac / ".claude/projects/-Users-alex-repo" / (E + ".jsonl"),
                    [claude_user(E, "On the Mac", T0, cwd="/Users/alex/code/repo")])
        for f in list(self.home.rglob("*")) + list(self.mac.rglob("*")):     # every session last wrote at T0 ...
            set_mtime(f, T0)
        set_mtime(self.home / ".claude-work/projects/-x-repo" / (A + ".jsonl"), T0 + 60)   # ... but A's work-login copy
        self.cfg = dict(transcripts.DEFAULTS, archive=str(self.root), machines=[
            {"name": "desktop", "sources": [
                {"cli": "claude", "glob": str(self.home / ".claude*/projects")},
                {"cli": "codex", "name": "codex-sessions", "path": str(self.home / ".codex/sessions")},
                {"cli": "codex", "name": "codex-archived", "path": str(self.home / ".codex/archived_sessions")}]},
            {"name": "mac", "ssh": "mac", "sources": [
                {"cli": "claude", "name": "claude-projects", "path": "~/.claude/projects"}]}])
        self.ssh_answer = (0, "")          # ssh's exit and words; "hang" for no answer at all
        self.rsync_fails = False
        self.calls = []

    def tearDown(self):
        self.sb.__exit__()

    def run_cmd(self, cmd, **kw):
        """ssh and rsync to the Mac: the network boundary. rsync copies from the stand-in Mac's home."""
        self.calls.append(cmd)
        if cmd[0] == "ssh":
            if self.ssh_answer == "hang":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return subprocess.CompletedProcess(cmd, self.ssh_answer[0], "", self.ssh_answer[1])
        if self.rsync_fails:
            return subprocess.CompletedProcess(cmd, 23, "",
                                               "rsync: change_dir \".codex/sessions\" failed: No such file")
        remote, dest = cmd[-2], cmd[-1]
        shutil.copytree(self.mac / remote.split(":", 1)[1], dest, dirs_exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def archive(self, now=T0 + DAY, live=frozenset(), answer=None, names=None, read_names=False):
        guess = lambda items, *a: ({k: (answer or {}).get(k[2:]) for k in items},
                                   {"asked": len(items), "batches": 1}, None)
        return transcripts.archive(self.cfg, now=now, run=self.run_cmd, live=set(live), guesser=guess, logins={},
                                   names=None if read_names else (names or {}))

    def catalog(self):
        con = sqlite3.connect(str(self.root / transcripts.CATALOG))
        con.row_factory = sqlite3.Row
        return {r["id"]: r for r in transcripts.rows(con)}

    def test_every_source_is_mirrored_and_each_session_is_one_record(self):
        groups.save_groups([{"name": "Home", "members": [A], "subgroups": []}], set(), t=T0)
        report = self.archive(now=T0 + 3 * DAY, answer={C: ["Research", "Evals"]})
        self.assertEqual((report["problems"], report["warnings"]),
                         ([], ["desktop/codex-archived: %s is not there" % (self.home / ".codex/archived_sessions")]))
        for rel in ("desktop/claude-projects/-x-repo/%s.jsonl" % A, "desktop/claude-projects/-x-repo/memory/MEMORY.md",
                    "desktop/claude-work-projects/-x-repo/%s.jsonl" % A,
                    "desktop/codex-sessions/2026/10/26/rollout-2-%s.jsonl" % D,
                    "mac/claude-projects/-Users-alex-repo/%s.jsonl" % E):
            self.assertTrue((self.root / rel).is_file(), rel)
        cat = self.catalog()
        self.assertEqual(set(cat), {A, C, E})                    # D is C's sub-thread
        a = cat[A]
        self.assertEqual(a["files"], [
            "desktop/claude-work-projects/-x-repo/%s.jsonl" % A, "desktop/claude-projects/-x-repo/%s.jsonl" % A,
            "desktop/claude-projects/-x-repo/%s/subagents/agent-1.jsonl" % A])
        self.assertEqual((a["login"], a["logins"], a["title"], a["machine"], a["cli"]),
                         ("claude-work", ["claude", "claude-work"], "First copy", "desktop", "claude"))
        self.assertEqual((a["started"], a["last_activity"]), (T0, T0 + 60))
        self.assertEqual((a["group_name"], a["group_source"], a["frozen_at"]), ("Home", "placed", T0 + 3 * DAY))
        self.assertEqual(cat[C]["files"][1], "desktop/codex-sessions/2026/10/26/rollout-2-%s.jsonl" % D)
        self.assertEqual((cat[C]["group_name"], cat[C]["subgroup"], cat[C]["group_basis"]),
                         ("Research", "Evals", "model"))
        self.assertEqual((cat[E]["machine"], cat[E]["cwd"], cat[E]["group_source"]),
                         ("mac", "/Users/alex/code/repo", "unknown"))
        self.assertEqual(stat.S_IMODE((self.root / transcripts.CATALOG).stat().st_mode), 0o600)
        manifest = json.loads((self.root / transcripts.MANIFEST).read_text())
        self.assertEqual(manifest["mac/claude-projects"], {"machine": "mac", "cli": "claude", "login": "claude",
                                                               "origin": "~/.claude/projects"})
        self.assertIn(["rsync", "-a", "--timeout=120", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=15",
                       "mac:.claude/projects/", str(self.root / ".incoming/mac/claude-projects") + "/"], self.calls)

    def test_a_mac_that_cannot_be_reached_is_a_warning_and_is_copied_next_time(self):
        down = "ssh: connect to host mac.example.net port 22: "
        for answer, said in (((255, down + "Connection timed out"), "Connection timed out"),
                             ((255, down + "No route to host"), "No route to host"),
                             ((255, down + "Network is unreachable"), "Network is unreachable"),
                             ((255, down + "Host is down"), "Host is down"),
                             ("hang", "no answer in 45 seconds")):
            self.ssh_answer = answer
            report = self.archive()
            self.assertEqual(report["problems"], [])
            self.assertEqual([w for w in report["warnings"] if w.startswith("mac")], [
                "mac could not be reached (%s): it is asleep or away, and its transcripts are copied at the next run"
                % (answer[1] if answer != "hang" else said)])
        self.assertEqual([c for c in self.calls if c[0] == "rsync"], [])
        self.assertFalse((self.root / "mac").exists())
        self.ssh_answer = (0, "")
        self.archive(now=T0 + 2 * DAY)
        self.assertIn(E, self.catalog())

    def test_ssh_refused_and_rsync_failing_are_problems(self):
        self.ssh_answer = (255, "alex@mac: Permission denied (publickey).")
        report = self.archive()
        self.assertEqual(report["problems"],
                         ["mac: ssh mac failed (exit 255): alex@mac: Permission denied (publickey)."])
        self.assertEqual([c for c in self.calls if c[0] == "rsync"], [])
        # a refused connection: the Mac is up but its ssh does not answer (Remote Login off), not asleep
        self.ssh_answer = (255, "ssh: connect to host mac.example.net port 22: Connection refused")
        report = self.archive()
        self.assertEqual(report["problems"], ["mac: ssh mac failed (exit 255): ssh: connect to host mac.example.net "
                                              "port 22: Connection refused"])
        self.assertEqual([c for c in self.calls if c[0] == "rsync"], [])
        self.ssh_answer, self.rsync_fails = (0, ""), True
        report = self.archive(now=T0 + 2 * DAY)
        self.assertEqual(report["problems"], ['mac/claude-projects: rsync exited 23: rsync: change_dir '
                                              '".codex/sessions" failed: No such file'])

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_a_source_file_that_cannot_be_read_is_a_problem_and_the_rest_is_copied(self):
        bad = self.home / ".claude/projects/-x-repo" / (B + ".jsonl")
        write_jsonl(bad, [claude_user(B, "unreadable", T0)])
        os.chmod(bad, 0)
        report = self.archive()
        self.assertEqual(len(report["problems"]), 1)
        self.assertTrue(report["problems"][0].startswith("desktop/claude-projects: not copied: 1 (PermissionError: "),
                        report["problems"])
        self.assertEqual(set(self.catalog()), {A, C, E})

    def test_mailroom_names_that_cannot_be_read_are_a_problem_and_stored_names_stay(self):
        self.archive(names={C: "codex-agent"})
        self.assertEqual(self.catalog()[C]["name"], "codex-agent")
        broken = self.d / "mailroom.sqlite"
        broken.write_text("not a database")
        saved, transcripts.sources.MAIL_DB = transcripts.sources.MAIL_DB, broken
        try:
            report = self.archive(now=T0 + 2 * DAY, read_names=True)
        finally:
            transcripts.sources.MAIL_DB = saved
        self.assertEqual(len(report["problems"]), 1)
        self.assertTrue(report["problems"][0].startswith("session names could not be read from the mailroom, so each "
                                                         "session keeps the name the catalog has: DatabaseError"))
        self.assertEqual(self.catalog()[C]["name"], "codex-agent")

    def test_a_rewritten_transcript_keeps_its_old_version_and_the_record_is_read_afresh(self):
        self.archive()
        main = self.home / ".claude/projects/-x-repo" / (A + ".jsonl")
        write_jsonl(main, [claude_user(A, "Rewritten from the start", T0)])
        self.archive(now=T0 + 2 * DAY)
        kept = list((self.root / transcripts.VERSIONS).rglob(A + ".jsonl"))
        self.assertEqual(len(kept), 1)
        self.assertIn("First copy", kept[0].read_text())
        self.assertIn("/desktop/claude-projects/-x-repo/", str(kept[0]))

    def test_the_archive_folder_must_exist_and_is_never_made(self):
        cfg = dict(self.cfg, archive=str(self.d / "unmounted/archive"))
        with self.assertRaises(transcripts.ArchiveError):
            transcripts.archive(cfg, run=self.run_cmd, live=set(), logins={}, names={})
        self.assertFalse((self.d / "unmounted").exists())

    def test_by_group_links_follow_regrouping(self):
        groups.save_groups([{"name": "Home", "members": [], "subgroups": []}], set(), t=T0)
        self.archive(live={A}, answer={A: ["Home", None]})
        link = self.root / ("by-group/Home/(none)/%s First copy (claude).jsonl"
                            % time.strftime("%Y-%m-%d", time.localtime(T0)))
        self.assertTrue(link.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(link)))
        self.assertIn("carried on after failover", link.read_text())        # the newest copy
        # the owner places the running session in Research / Evals: its link moves there, and Home's empty folder goes
        groups.save_groups([{"name": "Research", "members": [], "subgroups": [{"name": "Evals", "members": [A]}]}],
                           {A}, t=T0)
        report = self.archive(now=T0 + 2 * DAY, live={A})
        self.assertFalse(link.is_symlink())
        self.assertTrue((self.root / "by-group/Research/Evals" / link.name).is_symlink())
        self.assertFalse((self.root / "by-group/Home").exists())
        self.assertGreaterEqual(report["links"]["removed"], 1)


class Command(unittest.TestCase):
    """`agent-board transcripts archive` as the timer runs it, in a home folder of its own: nothing of this machine's
    (its sessions, mailroom, board state or caches) is read or written, and with no board groups no model is asked."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cfg_path = self.home / "transcripts.json"

    def run_archive(self, cfg):
        self.cfg_path.write_text(json.dumps(cfg))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "CLAUDE", "CODEX"))}
        env.update(HOME=str(self.home), AGENT_BOARD_TRANSCRIPTS_CONFIG=str(self.cfg_path))
        return subprocess.run([sys.executable, str(REPO / "bin/agent-board"), "transcripts", "archive"],
                              capture_output=True, text=True, env=env, timeout=120)

    def config(self, archive):
        return {"archive": str(archive), "machines": [
            {"name": "desktop",
             "sources": [{"cli": "claude", "name": "claude-projects", "path": "~/.claude/projects"}]}]}

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_a_file_not_copied_exits_1_and_says_why(self):
        (self.home / "archive").mkdir()
        write_jsonl(self.home / ".claude/projects/-x" / (A + ".jsonl"), [claude_user(A, "fine", T0)])
        bad = write_jsonl(self.home / ".claude/projects/-x" / (B + ".jsonl"), [claude_user(B, "unreadable", T0)])
        os.chmod(bad, 0)
        r = self.run_archive(self.config(self.home / "archive"))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("problem: desktop/claude-projects: not copied: 1 (PermissionError", r.stderr)
        self.assertTrue((self.home / "archive/desktop/claude-projects/-x" / (A + ".jsonl")).is_file())
        self.assertIn("not copied: 1", (self.home / ".local/state/agent-board/transcripts.log").read_text())

    def test_a_run_that_cannot_start_exits_2(self):
        r = self.run_archive(self.config(self.home / "unmounted/archive"))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("does not exist: is its disk mounted?", r.stderr)
        self.assertFalse((self.home / "unmounted").exists())
        r = self.run_archive({"machines": []})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('must name the archive folder ("archive")', r.stderr)


class Tree(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "m/s").mkdir(parents=True)
        for n in "abc":
            (self.root / "m/s" / (n + ".jsonl")).write_text(n)

    def session(self, sid, f, group="Research", sub=None, name="", title="a title", started=T0):
        return {"id": sid, "cli": "claude", "files": ["m/s/%s.jsonl" % f], "group_name": group, "subgroup": sub,
                "name": name, "title": title, "started": started}

    def test_clashing_names_get_whole_ids_and_odd_names_are_made_safe(self):
        # Codex thread ids begin with the time: two threads started together share their first characters
        v7a, v7b = "019f8f40-2d4c-7572-88a3-d892a34ea7d0", "019f8f40-2e01-7c11-9b2e-0a1b2c3d4e5f"
        plan = transcripts.link_plan([self.session(v7a, "a", name="same"), self.session(v7b, "b", name="same"),
                                      self.session(C, "c", group="A/B", sub=".hidden", title="x\ny/z", started=T0)])
        day = time.strftime("%Y-%m-%d", time.localtime(T0))
        self.assertEqual(plan, {"Research/(none)/%s same %s (claude).jsonl" % (day, v7a): "m/s/a.jsonl",
                                "Research/(none)/%s same %s (claude).jsonl" % (day, v7b): "m/s/b.jsonl",
                                "A-B/_hidden/%s x y-z (claude).jsonl" % day: "m/s/c.jsonl"})

    def test_only_links_are_removed_and_anything_else_is_left(self):
        transcripts.link_tree(self.root, [self.session(A, "a"), self.session(B, "b", group=None)])
        stray = self.root / "by-group/Research/(none)/notes.txt"
        stray.write_text("the owner's own file")
        counts = transcripts.link_tree(self.root, [self.session(B, "b", group="Home")])
        self.assertEqual(counts, {"made": 1, "removed": 2, "kept": 0, "other": 1})
        self.assertEqual(stray.read_text(), "the owner's own file")
        self.assertEqual(sorted(p.name for p in (self.root / "by-group").iterdir()), ["Home", "Research"])
        self.assertEqual(sorted(p.name for p in (self.root / "m/s").iterdir()), ["a.jsonl", "b.jsonl", "c.jsonl"])


class Listing(unittest.TestCase):
    ROWS = [{"id": A, "group_name": "Research", "subgroup": "Evals", "name": "docs", "title": "Write a guide",
             "cwd": "/x/a", "project": "agent-board", "last_activity": T0},
            {"id": B, "group_name": None, "subgroup": None, "name": "", "title": "Fix the flaky test", "cwd": "/x/b",
             "project": "mail", "last_activity": T0 + DAY},
            {"id": C, "group_name": "Research", "subgroup": None, "name": "", "title": "Guide maths", "cwd": "/x/c",
             "project": "agent-board", "last_activity": T0 + 2 * DAY}]

    def ids(self, **kw):
        return [r["id"] for r in transcripts.matching(self.ROWS, **kw)]

    def test_filters(self):
        self.assertEqual(self.ids(group="research"), [A, C])
        self.assertEqual(self.ids(group="Unsorted"), [B])
        self.assertEqual(self.ids(group="Research", subgroup="(none)"), [C])
        self.assertEqual(self.ids(subgroup="evals"), [A])
        self.assertEqual(self.ids(since=T0 + DAY), [B, C])
        self.assertEqual(self.ids(query="GUIDE agent-board"), [A, C])
        self.assertEqual(self.ids(query="guide flaky"), [])
        self.assertEqual(self.ids(query=A), [A])

    def test_the_command_lists_matches_with_their_paths(self):
        with Sandbox() as sb:
            root = sb.d / "archive"
            root.mkdir()
            home = sb.d / "home"
            write_jsonl(home / "projects/-x" / (A + ".jsonl"), [claude_user(A, "Write a guide", T0)])
            cfg = dict(transcripts.DEFAULTS, archive=str(root), machines=[
                {"name": "desktop",
                 "sources": [{"cli": "claude", "name": "claude-projects", "path": str(home / "projects")}]}])
            transcripts.archive(cfg, now=T0, live=set(), logins={}, names={},
                                guesser=lambda items, *a: ({}, {"asked": 0, "batches": 0}, None))
            (sb.d / "cfg.json").write_text(json.dumps(cfg))
            env = dict(os.environ, AGENT_BOARD_TRANSCRIPTS_CONFIG=str(sb.d / "cfg.json"))
            r = subprocess.run([sys.executable, str(REPO / "bin/agent-board"), "transcripts", "list",
                                "--query", "guide", "--since", "2026-01-01"], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Unsorted / (none) [unknown]  claude desktop  Write a guide", r.stdout)
            self.assertIn(str(root / "desktop/claude-projects/-x" / (A + ".jsonl")), r.stdout)
            r = subprocess.run([sys.executable, str(REPO / "bin/agent-board"), "transcripts", "list",
                                "--group", "Research"], capture_output=True, text=True, env=env)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "no archived session matches"))

    def test_no_catalog_yet_is_said(self):
        with self.assertRaises(transcripts.ArchiveError):
            transcripts.listing(dict(transcripts.DEFAULTS, archive=tempfile.mkdtemp()))


class Settings(unittest.TestCase):
    def check(self, cfg):
        p = Path(tempfile.mkdtemp()) / "t.json"
        p.write_text(json.dumps(cfg))
        return transcripts.settings(p)

    def test_what_the_settings_must_say(self):
        ok = {"archive": "/a",
              "machines": [{"name": "desktop", "sources": [{"cli": "claude", "glob": "~/.claude*/projects"}]}]}
        self.assertEqual(self.check(ok)["end_after_hours"], 48)
        bad = [{"machines": ok["machines"]},
               dict(ok, machines=[]),
               dict(ok, machines=[{"name": "by-group", "sources": ok["machines"][0]["sources"]}]),
               dict(ok, machines=[{"name": "mac", "ssh": "mac", "sources": [{"cli": "claude", "glob": "~/x"}]}]),
               dict(ok, machines=[{"name": "mac", "sources": [{"cli": "claude", "path": "~/x"}]}]),
               dict(ok, machines=[{"name": "mac", "sources": [{"cli": "gemini", "path": "~/x", "name": "x"}]}]),
               dict(ok, machines=ok["machines"] * 2)]
        for cfg in bad:
            with self.assertRaises(transcripts.ArchiveError, msg=json.dumps(cfg)):
                self.check(cfg)

    def test_no_archive_folder_says_how_to_name_one(self):
        with self.assertRaises(transcripts.ArchiveError) as e:
            self.check({})
        self.assertIn('must name the archive folder ("archive")', str(e.exception))
        self.assertIn('{"archive": "~/transcript-archive"}', str(e.exception))
        self.assertIsNone(transcripts.DEFAULTS["archive"])                 # no default folder, on any disk

    def test_without_machines_this_desktop_and_a_configured_mac_are_archived(self):
        p = Path(tempfile.mkdtemp()) / "t.json"
        p.write_text(json.dumps({"archive": "/a"}))
        got = transcripts.settings(p, host_id="studio", mac="")
        self.assertEqual(got["machines"], [{"name": "studio", "sources": [
            {"cli": "claude", "glob": "~/.claude*/projects"},
            {"cli": "codex", "name": "codex-sessions", "path": "~/.codex/sessions"},
            {"cli": "codex", "name": "codex-archived", "path": "~/.codex/archived_sessions"}]}])
        # a Mac only when one is configured (the mailroom config's remote_hosts["mac"]), reached by its ssh alias
        got = transcripts.settings(p, host_id="desktop", mac="my-mac")
        self.assertEqual([(m["name"], m.get("ssh")) for m in got["machines"]], [("desktop", None), ("mac", "my-mac")])
        self.assertEqual([s["path"] for s in got["machines"][1]["sources"]],
                         ["~/.claude/projects", "~/.codex/sessions"])
        # a host id that cannot name a folder is "desktop"; a list of machines replaces the defaults
        self.assertEqual(transcripts.settings(p, host_id="by-group", mac="")["machines"][0]["name"], "desktop")
        mine = [{"name": "box", "sources": [{"cli": "codex", "name": "c", "path": "~/c"}]}]
        p.write_text(json.dumps({"archive": "/a", "machines": mine}))
        self.assertEqual(transcripts.settings(p, mac="my-mac")["machines"], mine)

    def test_the_mac_comes_from_the_mailroom_config(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.json").write_text(json.dumps({"host_id": "desktop", "remote_hosts": {"mac": "laptop"}}))
        (d / "t.json").write_text(json.dumps({"archive": "/a"}))
        saved = transcripts.config.MAIL_CONFIG, transcripts.sources.MAIL_CONFIG
        transcripts.config.MAIL_CONFIG = transcripts.sources.MAIL_CONFIG = d / "config.json"
        try:
            got = transcripts.settings(d / "t.json")
        finally:
            transcripts.config.MAIL_CONFIG, transcripts.sources.MAIL_CONFIG = saved
        self.assertEqual([(m["name"], m.get("ssh")) for m in got["machines"]], [("desktop", None), ("mac", "laptop")])

    def test_a_glob_names_each_login_folder_and_takes_the_mailroom_login_name(self):
        home = Path(tempfile.mkdtemp())
        for login in (".claude", ".claude-work"):
            (home / login / "projects").mkdir(parents=True)
        got = transcripts.machine_sources(
            {"name": "desktop", "sources": [{"cli": "claude", "glob": str(home / ".claude*/projects")}]},
            logins={(home / ".claude").resolve(): "default"})
        self.assertEqual([(s["name"], s["login"]) for s in got], [("claude-work-projects", "claude-work"),
                                                                 ("claude-projects", "default")])


class MacRepos(unittest.TestCase):
    """A Mac session's repo is found on the desktop: at the same place under the home folder, else under the repos
    root, by the longest ending of its folder's path that is there."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.home, self.repos = self.d / "home", self.d / "home/code"
        (self.repos / "site/docs").mkdir(parents=True)
        (self.home / "notes").mkdir(parents=True)
        self.saved = transcripts.sources.HOME
        transcripts.sources.HOME = self.home

    def tearDown(self):
        transcripts.sources.HOME = self.saved

    def path(self, cwd, remote=True):
        return transcripts._repo_path(cwd, remote, self.repos)

    def test_the_same_place_under_home_then_the_repos_root(self):
        self.assertEqual(self.path("/Users/alex/notes"), str(self.home / "notes"))
        self.assertEqual(self.path("/Users/alex/code/site/docs"), str(self.repos / "site/docs"))
        self.assertEqual(self.path("/Users/alex/src/site/docs"), str(self.repos / "site/docs"))
        self.assertEqual(self.path("/Users/alex/src/site"), str(self.repos / "site"))
        # nothing of it here: the same place under home, for agent-spawn's worktree naming to read
        self.assertEqual(self.path("/Users/alex/src/other-wt/x"), str(self.home / "src/other-wt/x"))
        # this machine's own sessions, and a folder outside any home, are left as they are
        self.assertEqual(self.path("/Users/alex/src/site", remote=False), "/Users/alex/src/site")
        self.assertEqual(self.path("/opt/x"), "/opt/x")


if __name__ == "__main__":
    unittest.main()
