import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import issue_body as ib  # noqa: E402
import todo_desk as td  # noqa: E402

# Never the real ~/.config: every Desk here gets CFG, and the config tests point the loader at temp dirs
CFG = {"repo": "example-user/tasks", "owner_name": "Alex", "owner_email": "owner@example.com",
       "repos_root": "~/code", "general_repo": "~/code/general"}
td.CONFIG_DIR = Path(tempfile.mkdtemp()) / "todo-desk"
td.MAIL_CONFIG_DIR = Path(tempfile.mkdtemp()) / "agent-mail"


class FakeGitHub:
    """Enough of GitHub's REST API for one issue, its comments and labels."""

    def __init__(self, issue):
        self.issue = issue
        self.comments = []
        self.labels = {"list:tasks", "source:app", "priority:high", "priority:medium"}
        self.calls = []

    def rest(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body))
        if path.endswith("/labels") and method == "POST" and "/issues/" in path:
            self.issue["labels"] = sorted(set(self.issue["labels"]) | set(body["labels"]))
            return []
        if "/labels/" in path and method == "DELETE":
            self.issue["labels"] = [l for l in self.issue["labels"] if l != td.ASSIGNED_LABEL]
            return None
        if path.startswith("/repos/example-user/tasks/labels"):
            if method == "POST":
                self.labels.add(body["name"])
                return {}
            return [{"name": n} for n in sorted(self.labels)]
        if path.endswith("/comments") or "/comments?" in path:
            if method == "POST":
                c = {"body": body["body"], "created_at": "2026-10-07T17:00:00Z", "html_url": "https://c/%d" % len(self.comments),
                     "user": {"login": "example-user"}}
                self.comments.append(c)
                return c
            return list(self.comments)
        if "/issues/" in path:
            if method == "PATCH":
                for k, v in body.items():
                    self.issue[k] = v
                self.issue["updated_at"] = "2026-10-07T17:00:01Z"
            return self.rest_issue()
        raise AssertionError("unexpected call %s %s" % (method, path))

    def rest_issue(self):
        i = self.issue
        return {"number": i["number"], "title": i["title"], "body": i["body"], "state": i["state"],
                "html_url": "https://github.com/example-user/tasks/issues/%d" % i["number"],
                "created_at": "2026-10-01T00:00:00Z", "updated_at": i.get("updated_at", "2026-10-02T00:00:00Z"),
                "labels": [{"name": n} for n in i["labels"]]}


def make_desk(issue, popen=None):
    tmp = tempfile.mkdtemp()
    gh = FakeGitHub(issue)
    td.STATE_DIR = Path(tmp) / "state"
    return td.Desk(gh=gh, cache_path=Path(tmp) / "cache.json", popen=popen, config=CFG), gh


BODY = "Notes here\n\n## Checklist\n- [ ] one\n- [ ] two\n\n## Claude Resolution Notes\nold notes"


class BodyMerge(unittest.TestCase):
    def test_sections_round_trip(self):
        self.assertEqual(ib.join_sections(ib.split_sections(BODY)), BODY)

    def test_mac_checklist_and_github_notes_merge(self):
        mine = BODY.replace("- [ ] one", "- [x] one")
        theirs = BODY.replace("old notes", "new notes from an agent")
        merged = td.merge_body(BODY, mine, theirs)
        self.assertIn("- [x] one", merged)
        self.assertIn("new notes from an agent", merged)

    def test_same_section_both_ways_conflicts(self):
        mine = BODY.replace("Notes here", "Mac notes")
        theirs = BODY.replace("Notes here", "Phone notes")
        self.assertIsNone(td.merge_body(BODY, mine, theirs))

    def test_section_added_on_github_survives(self):
        theirs = BODY + "\n\n## Extra\nkept"
        mine = BODY.replace("Notes here", "edited")
        merged = td.merge_body(BODY, mine, theirs)
        self.assertIn("## Extra\nkept", merged)
        self.assertTrue(merged.startswith("edited"))

    def test_checklist_parse(self):
        self.assertEqual(td.checklist_of(BODY), [(False, "one"), (False, "two")])
        self.assertEqual(td.checklist_of("- [x] loose\n- [ ] item"), [(True, "loose"), (False, "item")])


class Labels(unittest.TestCase):
    def test_single_valued_prefix_replaces(self):
        out = td.apply_labels(["list:books", "source:app", "priority:low"], ["list:tasks", "priority:high"], ["list:books"])
        self.assertEqual(sorted(out), ["list:tasks", "priority:high", "source:app"])

    def test_unmanaged_labels_kept(self):
        out = td.apply_labels(["status:waiting", "list:a", "type:follow-up"], ["list:b"], ["list:a"])
        self.assertIn("status:waiting", out)
        self.assertIn("type:follow-up", out)

    def test_case_insensitive_remove(self):
        self.assertEqual(td.apply_labels(["List:Books"], [], ["list:books"]), [])


class Config(unittest.TestCase):
    def test_ops(self):
        cfg = {"groups": [{"name": "To Do", "sortOrder": 4}], "listMetadata": [{"listName": "to-buy", "groupName": "To Do", "sortOrder": 0}]}
        out = td.apply_config_ops(cfg, [{"op": "add_list", "list": "New List"}, {"op": "set_group", "list": "new list", "group": "To Do"},
                                        {"op": "add_group", "group": "Fresh"}, {"op": "remove_list", "list": "TO-BUY"}])
        self.assertEqual([m["listName"] for m in out["listMetadata"]], ["new list"])
        self.assertEqual(out["listMetadata"][0]["groupName"], "To Do")
        self.assertEqual(out["groups"][-1], {"name": "Fresh", "sortOrder": 5})
        with self.assertRaises(ValueError):
            td.apply_config_ops(cfg, [{"op": "drop_table"}])


class Update(unittest.TestCase):
    def issue(self):
        return {"number": 7, "title": "Buy milk", "body": BODY, "state": "open", "labels": ["list:tasks", "source:app"]}

    def test_conflicting_title(self):
        desk, gh = make_desk(self.issue())
        gh.issue["title"] = "Buy oat milk"            # changed in the web app
        with self.assertRaises(td.Conflict) as c:
            desk.update(7, {"title": "Buy milk today"}, {"title": "Buy milk"})
        self.assertEqual(c.exception.fields, ["title"])
        self.assertFalse(any(m == "PATCH" for m, _, _ in gh.calls))

    def test_force_overwrites(self):
        desk, gh = make_desk(self.issue())
        gh.issue["title"] = "Buy oat milk"
        out = desk.update(7, {"title": "Buy milk today"}, {"title": "Buy milk"}, force=True)
        self.assertEqual(out["title"], "Buy milk today")

    def test_body_merges_and_state_and_labels(self):
        desk, gh = make_desk(self.issue())
        gh.issue["body"] = BODY.replace("old notes", "agent notes")
        out = desk.update(7, {"body": BODY.replace("- [ ] two", "- [x] two"), "state": "CLOSED"}, {"body": BODY},
                          add=["priority:high"], remove=[])
        self.assertIn("- [x] two", out["body"])
        self.assertIn("agent notes", out["body"])
        self.assertEqual(out["state"], "CLOSED")
        self.assertIn("priority:high", out["labels"])
        self.assertIn("source:app", out["labels"])

    def test_archive_closes_as_not_planned(self):
        desk, gh = make_desk(self.issue())
        out = desk.update(7, {"state": "CLOSED", "stateReason": "NOT_PLANNED"})
        self.assertEqual(out["state"], "CLOSED")
        patch = [b for m, _, b in gh.calls if m == "PATCH"][-1]
        self.assertEqual(patch["state_reason"], "not_planned")
        self.assertIn("list:tasks", out["labels"])           # archived tasks stay in their list

    def test_close_reason_only_with_a_close(self):
        desk, gh = make_desk(self.issue())
        desk.update(7, {"state": "CLOSED"})
        self.assertNotIn("state_reason", [b for m, _, b in gh.calls if m == "PATCH"][-1])
        gh.issue["state"] = "closed"
        desk.update(7, {"state": "OPEN", "stateReason": "NOT_PLANNED"})    # an undone archive: just reopened
        self.assertEqual([b for m, _, b in gh.calls if m == "PATCH"][-1], {"state": "open"})
        gh.issue["state"] = "open"
        desk.update(7, {"state": "CLOSED", "stateReason": "DROP TABLE"})
        self.assertNotIn("state_reason", [b for m, _, b in gh.calls if m == "PATCH"][-1])

    def test_new_label_created(self):
        desk, gh = make_desk(self.issue())
        desk.update(7, add=["list:brand-new"], remove=["list:tasks"])
        self.assertIn("list:brand-new", gh.labels)
        self.assertEqual(sorted(gh.issue["labels"]), ["list:brand-new", "source:app"])


class FakeProc:
    def __init__(self, code=None):
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None
        self.code = code

    def poll(self):
        return self.code

    def wait(self):
        return self.code


class Assign(unittest.TestCase):
    def setUp(self):
        self.orig_sleep, td.time.sleep = td.time.sleep, lambda s: None
        self.orig_find, td.find_orchestrate = td.find_orchestrate, lambda: "/bin/true"

    def tearDown(self):
        td.time.sleep = self.orig_sleep
        td.find_orchestrate = self.orig_find

    def issue(self, **kw):
        i = {"number": 9, "title": "Look into e-bikes", "body": "road legal ones", "state": "open", "labels": ["list:tasks"]}
        i.update(kw)
        return i

    def test_assign_labels_comments_and_sends(self):
        procs = []

        def popen(argv, **kw):
            procs.append((argv, FakeProc()))
            return procs[-1][1]
        desk, gh = make_desk(self.issue(), popen=popen)
        issue, url, log = desk.assign(9, "under $2k please", "mac")
        self.assertIn(td.ASSIGNED_LABEL, issue["labels"])
        self.assertEqual(issue["agent"]["status"], "assigned")
        self.assertIn("> under $2k please", gh.comments[0]["body"])
        argv, proc = procs[0]
        self.assertEqual(argv[1:], ["--source", "todo"])
        sent = proc.stdin.getvalue().decode()
        self.assertIn("Task #9: Look into e-bikes", sent)
        self.assertIn("https://github.com/example-user/tasks/issues/9", sent)
        self.assertIn("under $2k please", sent)
        # the orchestrator names the agent for what the task is, as it names its own
        self.assertIn("2 to 4 lowercase words joined by hyphens", sent)
        self.assertNotIn("todo-9-", sent)
        self.assertIn("Agent started: NAME (REPO@BRANCH, ACCOUNT)", sent)
        self.assertIn("the Mac app, over SSH", sent)
        # a second press is refused
        with self.assertRaises(ValueError):
            desk.assign(9, "", "mac")
        self.assertEqual(len(procs), 1)
        desk.assign(9, "", "mac", again=True)
        self.assertEqual(len(procs), 2)

    def test_from_the_desktop_says_so(self):
        procs = []
        desk, gh = make_desk(self.issue(), popen=lambda argv, **kw: procs.append(FakeProc()) or procs[-1])
        desk.assign(9, "", "desktop")
        self.assertIn("from the todo-desk command on the desktop", gh.comments[0]["body"])
        sent = procs[0].stdin.getvalue().decode()
        self.assertTrue(sent.startswith("[message from Alex via the to-do app (the todo-desk command on the desktop), "
                                        "not by email."))
        self.assertNotIn("Mac", sent.split("\n", 1)[0])

    def test_closed_refused(self):
        desk, gh = make_desk(self.issue(state="closed"), popen=lambda *a, **k: self.fail("launched"))
        with self.assertRaises(ValueError):
            desk.assign(9, "", "mac")

    def test_failed_launch_rolls_back(self):
        desk, gh = make_desk(self.issue(), popen=lambda argv, **kw: FakeProc(code=2))
        with self.assertRaises(ValueError):
            desk.assign(9, "", "mac")
        self.assertNotIn(td.ASSIGNED_LABEL, gh.issue["labels"])
        self.assertIn("todo-agent:failed", gh.comments[-1]["body"])

    def test_agent_info_from_comments(self):
        info = td.agent_info([
            {"body": "<!-- todo-agent:assigned -->\nAssigned", "created_at": "t1", "html_url": "u1"},
            {"body": "unrelated chat", "created_at": "t2"},
            {"body": "<!-- todo-agent:started -->\nAgent started: todo-9-look-into (general@todo-9, claude:personal)",
             "created_at": "t3", "html_url": "u3"},
        ])
        self.assertEqual(info["status"], "started")
        self.assertEqual(info["name"], "todo-9-look-into")
        self.assertEqual(len(info["events"]), 2)
        self.assertIsNone(td.agent_info([{"body": "hello"}]))


class Serve(unittest.TestCase):
    def test_unknown_and_bad_requests_answer(self):
        desk, gh = make_desk({"number": 1, "title": "t", "body": "", "state": "open", "labels": []})
        out = io.StringIO()
        s = td.Server(desk, out=out)
        s.q.put({"id": 1, "cmd": "nope"})
        s.q.put({"id": 2, "cmd": "update"})
        s.q.put({"id": 3, "cmd": "ping"})
        s.stop.set()
        s.stop.clear()
        import threading
        t = threading.Thread(target=s.worker, daemon=True)
        t.start()
        import time
        deadline = time.time() + 3
        while out.getvalue().count("\n") < 3 and time.time() < deadline:
            time.sleep(0.05)
        s.stop.set()
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual([l["id"] for l in lines], [1, 2, 3])
        self.assertFalse(lines[0]["ok"])
        self.assertFalse(lines[1]["ok"])
        self.assertTrue(lines[2]["ok"])


class MacGone(unittest.TestCase):
    """A Mac that sleeps never closes its connection: serve leaves once the kernel shows it acknowledging nothing."""
    CONN = "198.51.100.20 59853 192.0.2.10 22"
    LOCAL, REMOTE = "0A0200C0:0016", "146433C6:E9CD"         # as /proc/net/tcp writes the two ends above

    def gone(self, rows, conn=CONN, name="tcp"):
        with tempfile.TemporaryDirectory() as d:
            head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            (Path(d) / name).write_text(head + "".join(
                "%4d: %s %s %s %08X:00000000 01:00000020 %08X     0        0 1 1 0 20 4 30 10 -1\n" % ((i,) + r)
                for i, r in enumerate(rows)))
            return td.ssh_peer_gone(conn, tables=(str(Path(d) / "tcp"), str(Path(d) / "tcp6")))

    def test_only_with_data_in_flight_and_many_timeouts(self):
        mine = lambda st="01", tx=0x2A0, rt=8: (self.LOCAL, self.REMOTE, st, tx, rt)
        self.assertTrue(self.gone([mine()]))
        self.assertFalse(self.gone([mine(rt=7)]))
        self.assertFalse(self.gone([mine(tx=0, rt=9)]))
        self.assertFalse(self.gone([mine(st="08")]))
        self.assertFalse(self.gone([mine()], conn=""))
        mapped = lambda w: "0000000000000000FFFF0000" + w
        self.assertTrue(self.gone([(mapped("0A0200C0") + ":0016", mapped("146433C6") + ":E9CD", "01", 5, 9)], name="tcp6"))

    def test_ticker_leaves_only_when_idle(self):
        desk, gh = make_desk({"number": 1, "title": "t", "body": "", "state": "open", "labels": []})
        s = td.Server(desk, out=io.StringIO())

        class Left(Exception):
            pass

        class Clock:                                   # each tick is 6 s; the busy run ends after 30
            def __init__(self):
                self.t = real_time.time()
                self.end = self.t + 30

            def time(self):
                return self.t

            def sleep(self, n):
                self.t += 6
                if self.t > self.end:
                    s.stop.set()

        def exit_(code):
            raise Left(code)
        real_time, real_exit = td.time, td.os._exit
        td.time, td.os._exit = Clock(), exit_
        try:
            s.busy = True
            s.ticker(gone=lambda: True)                # mid-request: stays until the request is done
            s.stop.clear()
            s.busy = False
            with self.assertRaises(Left):
                s.ticker(gone=lambda: True)
        finally:
            td.time, td.os._exit = real_time, real_exit


class Idempotent(unittest.TestCase):
    def test_same_op_twice_is_done_once(self):
        desk, gh = make_desk({"number": 3, "title": "t", "body": "", "state": "open", "labels": ["list:tasks"]})
        s = td.Server(desk, out=io.StringIO())
        req = {"cmd": "update", "number": 3, "set": {"state": "CLOSED"}, "op": "abc-1"}
        first = s.handle(req)
        patches = sum(1 for m, _, _ in gh.calls if m == "PATCH")
        gh.issue["state"] = "open"              # even if GitHub changed since, the replay returns the old reply
        second = s.handle(req)
        self.assertEqual(first, second)
        self.assertEqual(sum(1 for m, _, _ in gh.calls if m == "PATCH"), patches)


class ReviewFixes(unittest.TestCase):
    def test_duplicate_headings_merge_whole_bodies(self):
        base = "n\n\n## Log\na\n\n## Log\nb"
        self.assertEqual(td.merge_body(base, base, base + "\nmore"), base + "\nmore")
        self.assertIsNone(td.merge_body(base, base.replace("a", "A"), base.replace("b", "B")))

    def test_whitespace_only_merge_does_not_patch(self):
        body = "Notes\n\n\n\n## Checklist\n- [ ] x"
        desk, gh = make_desk({"number": 4, "title": "t", "body": body, "state": "open", "labels": []})
        mine = ib.join_sections(ib.split_sections(body))        # what the Mac sends: normalised, same content
        desk.update(4, {"body": mine}, {"body": body.replace("Notes", "Old notes")})
        self.assertFalse(any(m == "PATCH" for m, _, _ in gh.calls))

    def test_missing_config_is_not_fatal(self):
        desk, gh = make_desk({"number": 1, "title": "t", "body": "", "state": "open", "labels": []})

        def rest(method, path, body=None, headers=None):
            if "contents/.todo/config.json" in path:
                e = td.GitHubError("not found")
                e.status = 404
                raise e
            return FakeGitHub.rest(gh, method, path, body, headers)
        gh.rest = rest
        self.assertFalse(desk.sync_config())
        self.assertIsNone(desk.data["config"])

    def test_refused_label_name_raises(self):
        desk, gh = make_desk({"number": 1, "title": "t", "body": "", "state": "open", "labels": []})
        orig = gh.rest

        def rest(method, path, body=None, headers=None):
            if method == "POST" and path.endswith("/labels") and "/issues/" not in path:
                e = td.GitHubError("Validation Failed")
                e.status = 422
                raise e
            return orig(method, path, body, headers)
        gh.rest = rest
        with self.assertRaises(ValueError):
            desk.ensure_labels(["bad,label"])
        self.assertNotIn("bad,label", desk.labels)

    def test_assign_comment_failure_removes_label(self):
        td.time.sleep, sleep = (lambda s: None), td.time.sleep
        td.find_orchestrate, find = (lambda: "/bin/true"), td.find_orchestrate
        try:
            desk, gh = make_desk({"number": 9, "title": "x", "body": "", "state": "open", "labels": ["list:tasks"]},
                                 popen=lambda *a, **k: self.fail("launched"))
            orig = gh.rest

            def rest(method, path, body=None, headers=None):
                if method == "POST" and path.endswith("/comments"):
                    raise td.GitHubError("boom")
                return orig(method, path, body, headers)
            gh.rest = rest
            with self.assertRaises(td.GitHubError):
                desk.assign(9, "", "mac")
            self.assertNotIn(td.ASSIGNED_LABEL, gh.issue["labels"])
        finally:
            td.time.sleep, td.find_orchestrate = sleep, find

    def test_background_failure_is_an_error_event(self):
        desk, gh = make_desk({"number": 1, "title": "t", "body": "", "state": "open", "labels": []})
        out = io.StringIO()
        s = td.Server(desk, out=out)
        s.handle = lambda req: (_ for _ in ()).throw(RuntimeError("x"))
        s.q.put({"cmd": "refresh"})
        import threading
        import time
        threading.Thread(target=s.worker, daemon=True).start()
        deadline = time.time() + 3
        while not out.getvalue() and time.time() < deadline:
            time.sleep(0.05)
        s.stop.set()
        msg = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(msg["type"], "error")


class SortOps(unittest.TestCase):
    def test_set_sort_for_lists_and_groups(self):
        cfg = {"groups": [{"name": "To Do", "sortOrder": 4}, {"name": "Digest", "sortOrder": 5}],
               "listMetadata": [{"listName": "Tasks", "sortOrder": 0}, {"listName": "tasks", "sortOrder": 0},
                                {"listName": "to-buy", "groupName": "To Do", "sortOrder": 0}]}
        out = td.apply_config_ops(cfg, [{"op": "set_sort", "group": "Digest", "order": 1},
                                        {"op": "set_sort", "list": "tasks", "order": 2},
                                        {"op": "set_sort", "list": "new-one", "order": 3}])
        self.assertEqual([g["sortOrder"] for g in out["groups"]], [4, 1])
        self.assertEqual([m["sortOrder"] for m in out["listMetadata"] if m["listName"].lower() == "tasks"], [2, 2])
        self.assertEqual(out["listMetadata"][-1], {"listName": "new-one", "sortOrder": 3})



class RequestGitHub(FakeGitHub):
    """FakeGitHub plus what dispatch reads: the requested-issue list, /user, and removing any label."""

    def rest(self, method, path, body=None, headers=None):
        if path == "/user":
            self.calls.append((method, path, body))
            return {"login": "example-user"}
        if path.startswith("/repos/example-user/tasks/issues?"):
            self.calls.append((method, path, body))
            want = td.urllib.parse.unquote(path.split("labels=", 1)[1])
            return [self.rest_issue()] if self.issue["state"] == "open" and want in self.issue["labels"] else []
        if "/labels/" in path and method == "DELETE":
            self.calls.append((method, path, body))
            name = td.urllib.parse.unquote(path.rsplit("/", 1)[1])
            self.issue["labels"] = [l for l in self.issue["labels"] if l != name]
            return None
        return super().rest(method, path, body, headers)


class Dispatch(unittest.TestCase):
    setUp, tearDown, issue = Assign.setUp, Assign.tearDown, Assign.issue

    def make(self, labels=("list:tasks", td.REQUESTED_LABEL), comments=(), popen=None):
        tmp = tempfile.mkdtemp()
        td.STATE_DIR = Path(tmp) / "state"
        gh = RequestGitHub(self.issue(labels=list(labels)))
        gh.comments = [{"body": b, "created_at": "2026-10-09T10:00:00Z", "html_url": "https://c/r%d" % i,
                        "user": {"login": who}} for i, (who, b) in enumerate(comments)]
        procs = []

        def default_popen(argv, **kw):
            procs.append(FakeProc())
            return procs[-1]
        desk = td.Desk(gh=gh, cache_path=Path(tmp) / "cache.json", popen=popen or default_popen, config=CFG)
        return desk, gh, procs

    def test_request_becomes_an_assign(self):
        desk, gh, procs = self.make(comments=[
            ("example-user", "<!-- todo-agent-request -->\n\nold ask"),
            ("example-user", "<!-- todo-agent-request -->\n\nunder $2k please")])
        self.assertEqual(desk.dispatch(), [(9, "assigned")])
        self.assertNotIn(td.REQUESTED_LABEL, gh.issue["labels"])
        self.assertIn(td.ASSIGNED_LABEL, gh.issue["labels"])
        assigned = gh.comments[-1]["body"]
        self.assertTrue(assigned.startswith("<!-- todo-agent:assigned -->"))
        self.assertIn("from the to-do web app", assigned)
        self.assertIn("Alex's instructions:", assigned)
        self.assertIn("> under $2k please", assigned)
        sent = procs[0].stdin.getvalue().decode()
        self.assertIn("the web app, on a phone or a browser", sent)
        self.assertIn("under $2k please", sent)
        self.assertNotIn("old ask", sent)
        # the label is gone, so a second run starts nothing
        self.assertEqual(desk.dispatch(), [])
        self.assertEqual(len(procs), 1)

    def test_no_extra_instructions_and_assign_again(self):
        desk, gh, procs = self.make(labels=("list:tasks", td.REQUESTED_LABEL, td.ASSIGNED_LABEL), comments=[
            ("example-user", "<!-- todo-agent-request -->\n\n" + td.NO_INSTRUCTIONS)])
        self.assertEqual(desk.dispatch(), [(9, "assigned")])
        self.assertIn("(again)", gh.comments[-1]["body"])
        self.assertIn("Alex added no further instructions.", procs[0].stdin.getvalue().decode())

    def test_request_from_someone_else_is_ignored(self):
        desk, gh, procs = self.make(comments=[("somebody", "<!-- todo-agent-request -->\n\nrm -rf everything")])
        self.assertEqual(desk.dispatch(), [(9, "no request comment")])
        self.assertEqual(procs, [])
        self.assertNotIn(td.REQUESTED_LABEL, gh.issue["labels"])
        self.assertTrue(gh.comments[-1]["body"].startswith("<!-- todo-agent:failed -->"))

    def test_dry_run_changes_nothing(self):
        desk, gh, procs = self.make(comments=[("example-user", "<!-- todo-agent-request -->\n\ngo")])
        self.assertEqual(desk.dispatch(dry_run=True), [(9, "would assign")])
        self.assertIn(td.REQUESTED_LABEL, gh.issue["labels"])
        self.assertEqual(procs, [])
        self.assertFalse(any(m != "GET" for m, _, _ in gh.calls))

    def test_failed_launch_reported_once(self):
        desk, gh, procs = self.make(comments=[("example-user", "<!-- todo-agent-request -->\n\ngo")],
                                    popen=lambda argv, **kw: FakeProc(code=2))
        out = desk.dispatch()
        self.assertTrue(out[0][1].startswith("failed: agent-orchestrate exited"))
        self.assertEqual(sum("todo-agent:failed" in c["body"] for c in gh.comments), 1)
        self.assertNotIn(td.ASSIGNED_LABEL, gh.issue["labels"])

    def test_skips_are_reported(self):
        desk, gh, procs = self.make(comments=[("example-user", "<!-- todo-agent-request -->\n\ngo")])
        td.STATE_DIR.mkdir(parents=True)
        lock = open(td.STATE_DIR / "dispatch.lock", "w")         # another run, holding the lock
        td.fcntl.flock(lock, td.fcntl.LOCK_EX)
        self.assertEqual(desk.dispatch(), [(None, "skipped: another dispatch run holds the lock")])
        lock.close()
        real = gh.rest

        def taken(method, path, body=None, headers=None):
            if method == "DELETE":
                err = td.GitHubError("404")
                err.status = 404
                raise err
            return real(method, path, body, headers)
        gh.rest = taken
        self.assertEqual(desk.dispatch(), [(9, "skipped: another run took the request")])
        self.assertEqual(procs, [])

    def test_request_marker_is_not_an_agent_status(self):
        self.assertIsNone(td.agent_info([{"body": "<!-- todo-agent-request -->\n\ngo"}]))


class RatingLabels(unittest.TestCase):
    def test_importance_urgency_due_are_single_valued(self):
        cur = ["list:tasks", "importance:low", "urgency:high", "due:2026-10-01"]
        got = td.apply_labels(cur, ["importance:high", "urgency:low", "due:2026-11-05"], [])
        self.assertEqual(sorted(got), ["due:2026-11-05", "importance:high", "list:tasks", "urgency:low"])


class TagsAndWaiting(unittest.TestCase):
    """Tags are many per task, one follow-up day, and new labels get their colours."""

    def test_follow_up_is_single_valued_and_tags_are_not(self):
        cur = ["list:tasks", "tag:home", "status:waiting", "follow-up:2026-10-20"]
        got = td.apply_labels(cur, ["tag:phone", "follow-up:2026-10-27"], [])
        self.assertEqual(sorted(got), ["follow-up:2026-10-27", "list:tasks", "status:waiting", "tag:home", "tag:phone"])

    def test_waiting_off_removes_both(self):
        got = td.apply_labels(["list:tasks", "status:waiting", "follow-up:2026-10-20"], [], ["status:waiting", "follow-up:2026-10-20"])
        self.assertEqual(got, ["list:tasks"])

    def test_new_tag_and_follow_up_labels_are_coloured(self):
        desk, gh = make_desk({"number": 3, "title": "t", "body": "", "state": "open", "labels": ["list:tasks"]})
        desk.update(3, add=["tag:home", "follow-up:2026-10-20", "status:waiting", "due:2026-10-30"])
        made = {b["name"]: b["color"] for m, p, b in gh.calls if m == "POST" and p.endswith("/labels") and "/issues/" not in p}
        self.assertEqual(made, {"tag:home": "5319E7", "follow-up:2026-10-20": "FBCA04", "status:waiting": "FBCA04",
                                "due:2026-10-30": "ededed"})
        self.assertIn("tag:home", gh.issue["labels"])

    def test_waiting_cleared_is_green_and_still_waiting_swaps_back(self):
        desk, gh = make_desk({"number": 5, "title": "t", "body": "Waiting on: Jo", "state": "open",
                              "labels": ["list:tasks", "follow-up:2026-10-20"]})
        desk.update(5, add=["status:waiting-cleared"], remove=["status:waiting"])
        made = {b["name"]: b["color"] for m, p, b in gh.calls if m == "POST" and p.endswith("/labels") and "/issues/" not in p}
        self.assertEqual(made, {"status:waiting-cleared": "0E8A16"})
        desk.update(5, add=["status:waiting"], remove=["status:waiting-cleared"])     # the Mac's "Still waiting"
        self.assertEqual(sorted(gh.issue["labels"]), ["follow-up:2026-10-20", "list:tasks", "status:waiting"])

    def test_waiting_line_is_part_of_the_notes_section(self):
        base = "Asked about the lease"
        mine = "Waiting on: Sam\n\nAsked about the lease"
        theirs = "Asked about the lease\n\n## Claude Resolution Notes\nfound the form"
        self.assertEqual(td.merge_body(base, mine, theirs), "Waiting on: Sam\n\nAsked about the lease\n\n## Claude Resolution Notes\nfound the form")

class PruneDeleted(unittest.TestCase):
    """An incremental sync never sees an issue deleted on GitHub; the periodic full sync drops it."""

    class Repo:
        def __init__(self, numbers):
            self.numbers = list(numbers)
            self.sinces = []

        def graphql(self, query, variables):
            self.sinces.append(variables.get("since"))
            nodes = [{"number": n, "title": "t%d" % n, "body": "", "state": "OPEN", "url": "u", "createdAt": "2026-10-01T00:00:00Z",
                      "updatedAt": "2026-10-01T00:00:00Z", "labels": {"nodes": []}} for n in self.numbers]
            return {"repository": {"issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}

        def rest(self, method, path, body=None, headers=None):
            if path.startswith("/repos/example-user/tasks/labels"):
                return []
            e = td.GitHubError("not found")
            e.status = 404
            raise e

    def test_full_sync_every_few_hours_drops_and_reports_deleted_issues(self):
        tmp = tempfile.mkdtemp()
        repo = self.Repo([1, 2, 3])
        desk = td.Desk(gh=repo, cache_path=Path(tmp) / "cache.json", config=CFG)
        desk.sync()
        self.assertIsNone(repo.sinces[-1])                   # the first sync is a full one
        repo.numbers = [1, 3]                                # #2 deleted on GitHub
        desk.sync()
        self.assertIsNotNone(repo.sinces[-1])                # incremental: #2 is still cached
        self.assertIn("2", desk.data["issues"])

        before = desk.data["synced_at"]
        desk.data["full_synced_at"] = td.iso_minus(td.now_iso(), td.FULL_SYNC_EVERY + 60)
        desk.save()                                          # sync() reloads the cache file
        _, removed, _ = desk.sync()
        self.assertIsNone(repo.sinces[-1])
        self.assertEqual(removed, [2])
        self.assertNotIn("2", desk.data["issues"])
        self.assertEqual(desk.pruned_since(before), [2])     # a client that was away still hears of it
        self.assertEqual(desk.pruned_since(td.iso_minus(td.now_iso(), -3600)), [])
        self.assertEqual(desk.pruned_since(None), [])


class QuietSync(unittest.TestCase):
    """A sync that changed nothing does not rewrite the 1.3 MB cache file (it did every minute: ~1.9 GB a day)."""

    def test_saves_only_on_change_or_after_a_while(self):
        tmp = tempfile.mkdtemp()
        repo = PruneDeleted.Repo([1, 2])
        desk = td.Desk(gh=repo, cache_path=Path(tmp) / "cache.json", config=CFG)
        desk.sync()
        ino = lambda: (Path(tmp) / "cache.json").stat().st_ino        # each save replaces the file
        first = ino()
        desk.sync()
        self.assertEqual(ino(), first)                                # nothing changed: not written
        self.assertIsNotNone(repo.sinces[-1])
        repo.numbers = [1, 2, 3]
        changed, _, _ = desk.sync()
        self.assertEqual([i["number"] for i in changed], [3])
        self.assertNotEqual(ino(), first)                             # a new issue: written
        self.assertIn("3", json.loads((Path(tmp) / "cache.json").read_text())["issues"])
        second = ino()
        desk.sync()
        self.assertEqual(ino(), second)
        desk.data["synced_at"] = td.iso_minus(td.now_iso(), td.QUIET_SAVE_SECONDS + 60)
        desk.save()                                                   # the file last written over half an hour ago
        third = ino()
        desk.sync()
        self.assertNotEqual(ino(), third)                             # so this quiet sync refreshes its synced_at
        self.assertEqual(json.loads((Path(tmp) / "cache.json").read_text())["synced_at"], desk.data["synced_at"])


class Groups(unittest.TestCase):
    """Groups are GitHub sub-issues plus the subtask label."""

    def setUp(self):
        self.desk, self.gh = make_desk({"number": 7, "title": "t", "body": "", "state": "open", "labels": ["list:tasks"]})
        self.parents = {}                      # child number -> parent number, as GitHub holds it
        self.mutations = []

        def graphql(query, variables):
            if "removeSubIssue" in query:
                self.mutations.append(("remove", variables["p"], variables["c"]))
                self.parents.pop(int(variables["c"][1:]), None)
                return {}
            if "addSubIssue" in query:
                self.mutations.append(("add", variables["p"], variables["c"]))
                self.parents[int(variables["c"][1:])] = int(variables["p"][1:])
                return {}
            n = variables["n"]
            p = self.parents.get(n)
            return {"repository": {"issue": {"id": "I%d" % n, "parent": {"number": p, "id": "I%d" % p} if p else None}}}
        self.gh.graphql = graphql

    def test_link_adds_label_and_is_idempotent(self):
        out = self.desk.set_parent(7, 3)
        self.assertEqual(out["parent"], 3)
        self.assertIn(td.SUBTASK_LABEL, out["labels"])
        self.assertEqual(self.parents, {7: 3})
        self.desk.set_parent(7, 3)                            # again: no second link
        self.assertEqual([m[0] for m in self.mutations], ["add"])
        self.desk.set_parent(7, 4)                            # another group: replaces the parent
        self.assertEqual(self.parents, {7: 4})
        self.assertIn(td.SUBTASK_LABEL, self.gh.issue["labels"])

    def test_regroup_marks_the_task_updated(self):
        self.desk.set_parent(7, 3)
        self.gh.calls.clear()
        self.desk.set_parent(7, 4)                            # label already on: off and on again, so updatedAt moves
        patches = [c[2]["labels"] for c in self.gh.calls if c[0] == "PATCH"]
        self.assertEqual([td.SUBTASK_LABEL in l for l in patches], [False, True])
        self.gh.calls.clear()
        self.desk.set_parent(7, 4)                            # same group again: nothing to do
        self.assertEqual([c for c in self.gh.calls if c[0] == "PATCH"], [])

    def test_unlink_removes_label_and_is_idempotent(self):
        self.desk.set_parent(7, 3)
        out = self.desk.set_parent(7, None)
        self.assertIsNone(out["parent"])
        self.assertNotIn(td.SUBTASK_LABEL, out["labels"])
        self.assertEqual(self.parents, {})
        self.desk.set_parent(7, None)
        self.assertEqual([m[0] for m in self.mutations], ["add", "remove"])

    def test_bad_parent_refused(self):
        for bad in (7, 0, -2):
            with self.assertRaises(ValueError):
                self.desk.set_parent(7, bad)

    def test_rest_answers_keep_the_cached_group(self):
        self.desk.put({"number": 7, "title": "t", "labels": [], "parent": 3})
        self.desk.put(td.from_rest(self.gh.rest_issue()))
        self.assertEqual(self.desk.data["issues"]["7"]["parent"], 3)
        self.assertEqual(td.from_graphql({"number": 9, "title": "x", "state": "OPEN", "url": "u", "createdAt": "c",
                                          "updatedAt": "u", "parent": {"number": 5}})["parent"], 5)

    def test_set_parent_is_a_queued_op_done_once(self):
        s = td.Server(self.desk, out=io.StringIO())
        req = {"cmd": "set_parent", "number": 7, "parent": 3, "op": "abc-0-0"}
        s.handle(req)
        self.parents.clear()                                  # had it run again, GitHub would get a second link
        s.handle(req)
        self.assertEqual(self.parents, {})

    def test_hello_lists_every_known_issue(self):
        tmp = tempfile.mkdtemp()
        repo = PruneDeleted.Repo([1, 2, 3])
        desk = td.Desk(gh=repo, cache_path=Path(tmp) / "cache.json", config=CFG)
        server = td.Server(desk, out=io.StringIO())
        out = server.dispatch({"cmd": "hello", "since": None})
        self.assertEqual(out["known"], [1, 2, 3])
        self.assertEqual(out["repo"], "example-user/tasks")         # the Mac shows it in the task's details
        repo.numbers = [1, 3]
        desk.data["full_synced_at"] = td.iso_minus(td.now_iso(), td.FULL_SYNC_EVERY + 60)
        desk.save()
        out = server.dispatch({"cmd": "hello", "since": td.now_iso()})
        self.assertEqual(out["known"], [1, 3])            # the Mac drops #2 even if it missed the removal


class Identity(unittest.TestCase):
    """Who the owner is, which repo holds the tasks and where repos live all come from config."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.saved = td.CONFIG_DIR, td.MAIL_CONFIG_DIR, os.environ.pop("TODO_REPO", None)
        td.CONFIG_DIR, td.MAIL_CONFIG_DIR = self.dir / "todo-desk", self.dir / "agent-mail"

    def tearDown(self):
        td.CONFIG_DIR, td.MAIL_CONFIG_DIR, env = self.saved
        os.environ.pop("TODO_REPO", None)
        if env is not None:
            os.environ["TODO_REPO"] = env

    def write(self, where, obj):
        where.mkdir(parents=True, exist_ok=True)
        (where / "config.json").write_text(json.dumps(obj))

    def test_missing_repo_is_a_clear_error(self):
        with self.assertRaises(td.ConfigError) as c:
            td.Desk(gh=object(), cache_path=self.dir / "cache.json")
        self.assertIn('"repo": "owner/name"', str(c.exception))
        for bad in ("tasks", "a/b/c", "/tasks", "owner/", "own er/tasks"):
            with self.assertRaises(td.ConfigError):
                td.repo_parts(bad)

    def test_repo_from_file_then_env(self):
        os.environ["TODO_REPO"] = "env-user/todo"
        self.assertEqual(td.load_config()["repo"], "env-user/todo")
        self.write(td.CONFIG_DIR, {"repo": "file-user/tasks"})
        self.assertEqual(td.load_config()["repo"], "file-user/tasks")
        self.assertEqual(td.repo_parts("file-user/tasks"), ("file-user", "tasks"))

    def test_owner_keys_fall_back_to_the_mailroom_config(self):
        self.write(td.MAIL_CONFIG_DIR, {"owner_name": "Sam", "allowlist": ["sam@example.com"], "repos_root": "~/src"})
        self.write(td.CONFIG_DIR, {"repo": "example-user/tasks", "owner_name": "Alex"})
        cfg = td.load_config()
        self.assertEqual(cfg["owner_name"], "Alex")                 # its own file wins
        self.assertEqual(cfg["owner_email"], "sam@example.com")     # the mailroom's allowlisted sender
        self.assertEqual(cfg["repos_root"], "~/src")
        self.assertIsNone(cfg["general_repo"])

    def test_message_uses_config_and_keeps_the_markers(self):
        issue = {"number": 5, "title": "Renew passport", "body": "before May", "url": "https://github.com/x/y/issues/5",
                 "labels": ["list:tasks"], "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-02T00:00:00Z"}
        cfg = {"repo": "example-user/tasks", "owner_name": "Alex", "owner_email": "owner@example.com",
               "repos_root": "~/code/", "general_repo": None}
        msg = td.orchestrator_message(issue, "", cfg)
        self.assertTrue(msg.startswith("[message from Alex via the to-do app (the Mac app, over SSH)"))
        self.assertIn("agent-mail send --to owner --subject", msg)      # the mailroom's alias: no address typed
        self.assertIn("existing repo under ~/code, start", msg)
        self.assertIn("start it in\n  ~/code/general on a new branch", msg)
        self.assertIn("gh issue comment 5 --repo example-user/tasks --body \"<!-- todo-agent:started -->", msg)
        self.assertIn("<!-- todo-agent:done --> (or <!-- todo-agent:blocked -->)", msg)
        self.assertIn("Alex added no further instructions.", msg)
        bare = td.orchestrator_message(issue, "x", {"repo": "example-user/tasks"})
        self.assertIn("the owner adds:", bare)
        self.assertIn("agent-mail send --to owner --subject", bare)

    def test_config_command_needs_no_github(self):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("sys.stdout", out), unittest.mock.patch("sys.stderr", err):
            self.assertEqual(td.main(["config"]), 2)                # no repo yet
        self.assertIn("no tasks repo configured", err.getvalue())
        self.write(td.CONFIG_DIR, {"repo": "example-user/tasks", "owner_name": "Alex"})
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            self.assertEqual(td.main(["config"]), 0)
        shown = json.loads(out.getvalue())
        self.assertEqual((shown["repo"], shown["owner_name"]), ("example-user/tasks", "Alex"))

    def test_cache_of_another_repo_is_ignored(self):
        cache = self.dir / "cache.json"
        cache.write_text(json.dumps({"version": td.VERSION, "repo": "someone/else", "synced_at": "2026-10-01T00:00:00Z",
                                     "issues": {"1": {"number": 1}}}))
        desk = td.Desk(gh=object(), cache_path=cache, config=CFG)
        self.assertEqual(desk.data["issues"], {})
        self.assertIsNone(desk.data["synced_at"])
        cache.write_text(json.dumps(dict(desk.data, issues={"2": {"number": 2}})))
        self.assertEqual(td.Desk(gh=object(), cache_path=cache, config=CFG).data["issues"], {"2": {"number": 2}})


A1 = "d21ff850-e1bb-46d1-8d0a-c5bc8842af30"
A2 = "01a11bf1-5b48-7651-b12c-140cb15c777e"
A3 = "5e550000-0000-4000-8000-000000000001"
GONE = "c7bfd808-0000-0000-0000-000000000000"
SNAPSHOT = {"agents": [
    {"id": A1, "cli": "claude", "display": "importance-urgency reply", "state": "busy", "live": True,
     "cwd": "/home/alex/code/to-do-x/", "last_activity": 1792160000.0,
     "last_note": {"ts": 1, "text": "note one\nwith a line break " + "x" * 200}},
    {"id": A2, "cli": "codex", "name": "docs-cleanup", "state": "idle", "live": True, "cwd": "/p/docs",
     "last_activity": 1792150000.0, "last_note": None},
    {"id": A3, "cli": "claude", "display": "older idle", "state": "idle", "live": True, "cwd": "/p/inv",
     "last_activity": 1792140000.0},
    {"id": GONE, "cli": "claude", "display": "gone", "state": "offline", "live": False, "cwd": "/p/gone"},
    {"id": "not-a-uuid", "cli": "claude", "display": "odd", "state": "idle", "live": True, "cwd": "/p/odd"},
]}


class RunningAgents(unittest.TestCase):
    """Assign a task to an agent already running on the board."""

    def setUp(self):
        self.orig_groups, td.BOARD_GROUPS = td.BOARD_GROUPS, Path(tempfile.mkdtemp()) / "none.json"

    def tearDown(self):
        td.BOARD_GROUPS = self.orig_groups

    def issue(self, **kw):
        return Assign.issue(self, **kw)

    def test_request_marker_with_and_without_an_agent(self):
        self.assertIsNone(td.REQUEST_RE.match("<!-- todo-agent-request -->\n\ngo").group("to"))
        m = td.REQUEST_RE.match("<!-- todo-agent-request to=%s -->\n\ngo" % A2)
        self.assertEqual(m.group("to"), A2)
        self.assertEqual(td.REQUEST_RE.sub("", "<!-- todo-agent-request to=%s -->\n\ngo" % A2).strip(), "go")
        self.assertIsNone(td.REQUEST_RE.match("<!-- todo-agent-request to=rm-rf -->"))

    def test_records_live_only_sorted_and_trimmed(self):
        recs = td.agent_records(SNAPSHOT, td.board_groups({"groups": [
            {"name": "Work", "members": [A3], "subgroups": [{"name": "Docs", "members": [A2]}]}]}))
        self.assertEqual([r["id"] for r in recs], [A2, A3, A1])     # idle (newest first), then busy
        one = recs[2]
        self.assertEqual(one["name"], "importance-urgency reply")
        self.assertEqual(one["folder"], "to-do-x")
        self.assertEqual(len(one["note"]), 160)
        self.assertNotIn("\n", one["note"])
        self.assertEqual(one["active"], "2026-10-16T14:13:20Z")
        self.assertEqual(recs[0]["group"], "Work / Docs")
        self.assertEqual(recs[1]["group"], "Work")
        self.assertIsNone(one["group"])
        self.assertEqual(recs[0]["name"], "docs-cleanup")
        self.assertEqual(recs[0]["note"], "")
        self.assertEqual(set(recs[0]), {"id", "cli", "name", "state", "folder", "note", "active", "group"})

    def test_publish_only_on_change_and_throttled(self):
        recs = td.agent_records(SNAPSHOT)
        self.assertTrue(td.should_publish(recs, None, 1000))
        last = {"at": 1000, "ids": sorted(r["id"] for r in recs),
                "key": [{k: v for k, v in r.items() if k != "active"} for r in recs]}
        moved = [dict(r, active="2030-01-01T00:00:00Z") for r in recs]
        self.assertFalse(td.should_publish(moved, last, 5000))      # only activity: never
        busy = [dict(r, state="busy") for r in recs]
        self.assertFalse(td.should_publish(busy, last, 1100))       # a state, within 5 minutes
        self.assertTrue(td.should_publish(busy, last, 1000 + td.PUBLISH_EVERY))
        self.assertTrue(td.should_publish(recs[:2], last, 1001))   # an agent went: at once

    def make(self, sent, code=0, issue=None):
        tmp = tempfile.mkdtemp()
        td.STATE_DIR = Path(tmp) / "state"
        gh = RequestGitHub(issue or self.issue())

        def send(cli, sid, text):
            sent.append((cli, sid, text))
            return code, "" if code == 0 else "no such session"
        desk = td.Desk(gh=gh, cache_path=Path(tmp) / "cache.json", popen=lambda *a, **k: self.fail("orchestrator"),
                       config=CFG, board=lambda: SNAPSHOT, send=send)
        return desk, gh

    def test_assign_to_a_running_agent(self):
        sent = []
        desk, gh = self.make(sent)
        issue, url, log = desk.assign(9, "after your current step", "mac", to=A2)
        self.assertIn(td.ASSIGNED_LABEL, issue["labels"])
        body = gh.comments[0]["body"]
        self.assertTrue(body.startswith("<!-- todo-agent:started -->\nAssigned to the running agent docs-cleanup (docs)"))
        self.assertIn("from the Mac to-do app", body)
        self.assertIn("Alex's instructions:\n> after your current step", body)
        self.assertEqual(issue["agent"]["status"], "started")
        self.assertEqual(issue["agent"]["name"], "docs-cleanup")
        cli, sid, text = sent[0]
        self.assertEqual((cli, sid), ("codex", A2))
        self.assertTrue(text.startswith("[relayed from Alex via the to-do app (the Mac app, over SSH)]"))
        self.assertIn("Task #9: Look into e-bikes", text)
        self.assertIn("gh issue view 9 --repo example-user/tasks --comments", text)
        self.assertIn("Answer Alex with agent-mail send --to owner", text)
        self.assertIn("after your current step", text)
        self.assertIn("finish or checkpoint your current", text)
        self.assertIn("<!-- todo-agent:done -->", text)
        self.assertNotIn("agent-spawn", text)
        self.assertEqual(log, "")
        with self.assertRaises(ValueError):                     # already assigned
            desk.assign(9, "", "mac", to=A2)

    def test_gone_agent_refused_and_failed_send_rolls_back(self):
        sent = []
        desk, gh = self.make(sent)
        with self.assertRaisesRegex(ValueError, "gone is no longer running"):
            desk.assign(9, "", "mac", to=GONE)
        with self.assertRaisesRegex(ValueError, "no longer running"):
            desk.assign(9, "", "mac", to="11111111-2222-3333-4444-555555555555")
        self.assertEqual(gh.comments, [])
        desk, gh = self.make(sent, code=1)
        with self.assertRaisesRegex(ValueError, "agent-send failed"):
            desk.assign(9, "", "mac", to=A1)
        self.assertNotIn(td.ASSIGNED_LABEL, gh.issue["labels"])
        self.assertTrue(gh.comments[-1]["body"].startswith("<!-- todo-agent:failed -->\nThe task was not sent to"))

    def test_dispatch_request_for_a_running_agent(self):
        sent = []
        desk, gh = self.make(sent, issue=self.issue(labels=["list:tasks", td.REQUESTED_LABEL]))
        gh.comments = [{"body": "<!-- todo-agent-request to=%s -->\n\nplease" % A3, "created_at": "t",
                        "html_url": "u", "user": {"login": "example-user"}}]
        self.assertEqual(desk.dispatch(), [(9, "sent to a running agent")])
        self.assertEqual(sent[0][1], A3)
        self.assertIn("please", sent[0][2])
        self.assertIn("from the to-do web app", gh.comments[-1]["body"])
        self.assertTrue(sent[0][2].startswith("[relayed from Alex via the to-do app (the web app"))

    def test_dispatch_request_for_a_gone_agent(self):
        sent = []
        desk, gh = self.make(sent, issue=self.issue(labels=["list:tasks", td.REQUESTED_LABEL]))
        gh.comments = [{"body": "<!-- todo-agent-request to=11111111-2222-3333-4444-555555555555 -->\n\ngo",
                        "created_at": "t", "html_url": "u", "user": {"login": "example-user"}}]
        out = desk.dispatch()
        self.assertTrue(out[0][1].startswith("failed:"))
        self.assertEqual(sent, [])
        self.assertTrue(gh.comments[-1]["body"].startswith("<!-- todo-agent:failed -->\nThe task was not sent: "))
        self.assertEqual(sum("todo-agent:failed" in c["body"] for c in gh.comments), 1)

    def test_agents_command_and_publish(self):
        desk, gh = self.make([])
        puts = []
        real = gh.rest

        def rest(method, path, body=None, headers=None):
            if path.endswith(td.AGENTS_PATH):
                self.assertEqual(path, "/repos/example-user/tasks/contents/.todo/agents.json")
                if method == "GET":
                    err = td.GitHubError("404")
                    err.status = 404
                    raise err
                puts.append(body)
                return {}
            return real(method, path, body, headers)
        gh.rest = rest
        res = td.Server(desk, out=io.StringIO()).dispatch({"cmd": "agents"})
        self.assertEqual(len(res["agents"]), 3)
        self.assertTrue(desk.publish_agents(now=1000))
        doc = json.loads(td.base64.b64decode(puts[0]["content"]))
        self.assertEqual((doc["v"], len(doc["agents"])), (1, 3))
        self.assertEqual(puts[0]["message"], "Agents list (todo-desk)")
        self.assertNotIn("sha", puts[0])
        self.assertFalse(desk.publish_agents(now=1010))            # unchanged
        self.assertTrue(desk.publish_agents(force=True, now=1020))
        other = td.Desk(gh=gh, cache_path=desk.path, config=dict(CFG, repo="example-user/other"),
                        board=lambda: SNAPSHOT, send=lambda *a: (0, ""))
        gh.rest = lambda method, path, body=None, headers=None: (puts.append(path) or {}) if "/other/" in path \
            else self.fail(path)
        self.assertTrue(other.publish_agents(now=1030))             # a new tasks repo gets the list at once

    def test_preview_for_a_running_agent(self):
        desk, gh = self.make([])
        msg = td.Server(desk, out=io.StringIO()).dispatch({"cmd": "assign_preview", "number": 9, "text": "x", "to": A2})
        self.assertTrue(msg["message"].startswith("[relayed from Alex via the to-do app"))
        self.assertNotIn("agent-spawn", msg["message"])
        msg = td.Server(desk, out=io.StringIO()).dispatch({"cmd": "assign_preview", "number": 9, "text": "x"})
        self.assertIn("Pass it to\n  agent-spawn --name", msg["message"])
        self.assertNotIn("todo-9-", msg["message"])

    def test_board_root_is_the_kits_board_or_the_commands_checkout(self):
        self.assertTrue((td.KIT_BOARD / "agentboard" / "__init__.py").exists())   # the kit layout: board/ beside todo/
        self.assertEqual(td.board_root(), str(td.KIT_BOARD))
        tmp = Path(tempfile.mkdtemp())
        (tmp / "repo/agentboard").mkdir(parents=True)
        (tmp / "repo/agentboard/__init__.py").write_text("")
        (tmp / "repo/bin").mkdir()
        (tmp / "repo/bin/agent-board").write_text("#!/usr/bin/env python3\n")
        (tmp / "bin").mkdir()
        (tmp / "bin/agent-board").symlink_to(tmp / "repo/bin/agent-board")
        (tmp / "bin/wrapper").write_text("#!/usr/bin/env bash\nexec python3 /somewhere/agent-board \"$@\"\n")
        saved, td.KIT_BOARD = td.KIT_BOARD, tmp / "no-board"
        try:
            self.assertEqual(td.board_root(str(tmp / "bin/agent-board")), str((tmp / "repo").resolve()))
            self.assertIsNone(td.board_root(str(tmp / "bin/wrapper")))     # then only `agent-board snapshot` is tried
            self.assertIsNone(td.board_root())
        finally:
            td.KIT_BOARD = saved


class AgentMail(unittest.TestCase):
    """Double-click the agent on a task to open its latest mail, as on the agent board."""

    def mailroom(self, sessions, sent):
        db = Path(tempfile.mkdtemp()) / "mailroom.sqlite"
        con = td.sqlite3.connect(str(db))
        con.execute("create table sessions(tag text primary key, cli text, session_id text, session_name text, "
                    "cwd text, created_at integer, origin_host text)")
        con.execute("create table sent(id integer primary key, tag text, gmail_id text, thread_id text, "
                    "message_id text, to_addr text, subject text, sent_at integer)")
        con.executemany("insert into sessions values (?, 'claude', ?, ?, ?, 0, 'desktop')", sessions)
        con.executemany("insert into sent (tag, message_id, to_addr, subject, sent_at) values (?, ?, ?, ?, ?)", sent)
        con.commit()
        con.close()
        return db

    def test_newest_mail_to_the_owner_by_name_or_folder(self):
        db = self.mailroom(
            [("t1", "s1", "todo-5-fix", "/home/alex/code/general-todo-5-fix"),
             ("t2", "s2", "todo-5-fix", "/home/alex/code/general-todo-5-fix"),          # the same agent, restarted
             ("t3", "s3", "renamed later", "/home/alex/code/to-do-todo-6-thing"),
             ("t4", "s4", "someone else", "/home/alex/code/other")],
            [("t1", "<a@x>", "owner@example.com", "First", 100),
             ("t2", "<b@x>", "Alex <owner@example.com>", "Second", 200),
             ("t2", "<c@x>", "someone@example.org", "Not to the owner", 300),
             ("t3", "<d@x>", "owner@example.com", "Renamed agent's", 150),
             ("t4", "<e@x>", "owner@example.com", "Other", 400)])
        owners = ["owner@example.com"]
        m = td.latest_mail("todo-5-fix", db=db, owners=owners)
        self.assertEqual((m["message_id"], m["subject"]), ("<b@x>", "Second"))
        self.assertEqual(m["sent_at"], "1970-01-01T00:03:20Z")
        self.assertEqual(td.latest_mail("todo-6-thing", "to-do-todo-6-thing", db=db, owners=owners)["subject"],
                         "Renamed agent's")
        self.assertIsNone(td.latest_mail("todo-7-none", "to-do-todo-7", db=db, owners=owners))
        self.assertIsNone(td.latest_mail("todo-5-fix", db=Path(tempfile.mkdtemp()) / "missing.sqlite", owners=owners))

    def test_folder_from_the_started_comment(self):
        info = {"events": [
            {"kind": "assigned", "text": "Assigned"},
            {"kind": "started", "text": "Agent started: todo-6-thing (to-do@todo-6-thing, claude:personal)"}]}
        self.assertEqual(td.agent_folder(info), "to-do-todo-6-thing")
        self.assertEqual(td.agent_folder({"events": [{"kind": "started", "text":
                         "Assigned to the running agent x (to-do-y) from the Mac to-do app at now."}]}), None)
        self.assertIsNone(td.agent_folder(None))

    def test_command(self):
        db = self.mailroom([("t3", "s3", "renamed later", "/home/alex/code/to-do-todo-6-thing")],
                           [("t3", "<d@x>", "owner@example.com", "S", 1793181600)])
        desk, _ = make_desk({"number": 6, "title": "T", "body": "", "state": "OPEN", "labels": []})
        desk.find_mail = lambda name, folder: td.latest_mail(name, folder, db=db, owners=["owner@example.com"])
        # Only the Gmail lookup is replaced: it asks Gmail over the network through the agent board's login
        desk.find_thread = lambda mid: "1a2b" if mid == "<d@x>" else None
        desk.data["agents"]["6"] = {"status": "started", "name": "todo-6-thing", "events": [
            {"kind": "started", "text": "Agent started: todo-6-thing (to-do@todo-6-thing, claude:personal)"}]}
        res = td.Server(desk, out=io.StringIO()).dispatch({"cmd": "agent_mail", "number": 6})
        self.assertEqual(res["mail"], {"message_id": "<d@x>", "subject": "S", "sent_at": "2026-10-28T10:00:00Z",
                                       "thread": "1a2b"})
        self.assertEqual(res["name"], "todo-6-thing")
        self.assertTrue(res["owner"])
        none = td.Server(desk, out=io.StringIO()).dispatch({"cmd": "agent_mail", "number": 7})
        self.assertEqual((none["name"], none["mail"]), (None, None))

    def test_failed_thread_lookup_still_opens_by_search(self):
        db = self.mailroom([("t3", "s3", "x", "/home/alex/code/to-do-todo-6-thing")],
                           [("t3", "<d@x>", "owner@example.com", "S", 1793181600)])
        desk, _ = make_desk({"number": 6, "title": "T", "body": "", "state": "OPEN", "labels": []})
        desk.find_mail = lambda name, folder: td.latest_mail(name, folder, db=db, owners=["owner@example.com"])

        def broken(mid):
            raise ValueError("could not look up the Gmail thread: boom")
        desk.find_thread = broken
        desk.data["agents"]["6"] = {"status": "started", "name": "todo-6-thing", "events": [
            {"kind": "started", "text": "Agent started: todo-6-thing (to-do@todo-6-thing, claude:personal)"}]}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            res = td.Server(desk, out=io.StringIO()).dispatch({"cmd": "agent_mail", "number": 6})
        self.assertIsNone(res["mail"]["thread"])
        self.assertIn("boom", res["mail"]["thread_error"])
        self.assertIn("opening by search", err.getvalue())


def fake_agent_board(rows_ok, snapshot_ok=True, thread="null"):
    """A stand-in agent-board install (another repository, run as a child process): bin/agent-board and the
    agentboard package that BOARD_ROWS and BOARD_THREAD import."""
    root = Path(tempfile.mkdtemp())
    (root / "bin").mkdir()
    exe = root / "bin" / "agent-board"
    exe.write_text("#!%s\nimport sys\nprint('{\"agents\": []}' if %r else 'boom', file=sys.stdout if %r else sys.stderr)"
                   "\nsys.exit(0 if %r else 1)\n" % (sys.executable, snapshot_ok, snapshot_ok, snapshot_ok))
    exe.chmod(0o755)
    pkg = root / "agentboard"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("" if rows_ok else "raise ImportError('rows broke')\n")
    (pkg / "gmailstate.py").write_text(
        "import sys\nclass GmailState:\n    def is_read(self, m): pass\n"
        "    def maybe_refresh(self, now=None, force=False): pass\n"
        "    def thread_of(self, m):\n        sys.stdout.write(%r)\n        sys.exit(0 if %r != 'exit' else 3)\n"
        "def for_owner(sync, sender=None, owners=None):\n    return GmailState()\n" % (thread, thread))
    (pkg / "mailbox.py").write_text("REFRESH = 30\ndef summary():\n    return None\n")
    (pkg / "sources.py").write_text("class Periodic:\n    def __init__(self, load, interval, initial, sync):\n"
                                    "        self.value = initial\n"
                                    "    def maybe_refresh(self, now=None, force=False, args=()):\n"
                                    "        return self.value\n")
    (pkg / "collect.py").write_text("class Collector:\n    def __init__(self, gmail=None, mailbox=None): pass\n"
                                    "    def agents(self, with_git=False):\n        return [[{'id': 'rows'}]]\n")
    return str(exe)


class KitBoardAPI(unittest.TestCase):
    """BOARD_ROWS and BOARD_THREAD run against the kit's own board/ (beside todo/), so a rename there fails here,
    not silently on the desktop. With no Gmail login configured the read-check is off: rows come back, no thread."""

    def run_snippet(self, code, *args):
        home = Path(tempfile.mkdtemp())
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BOARD_")}
        env.update(HOME=str(home), AGENT_BOARD_CONFIG=str(home / "no-board-config.json"), PATH="/usr/bin:/bin",
                   AGENT_MAIL_CONFIG_DIR=str(home / "cfg"), AGENT_MAIL_STATE_DIR=str(home / "state"),
                   AGENT_STATUS_DIR=str(home / "status"))
        return subprocess.run([sys.executable, "-c", code, str(td.KIT_BOARD)] + list(args), env=env,
                              capture_output=True, text=True, timeout=120)

    def test_thread_lookup_runs_on_the_kits_board(self):
        # BOARD_THREAD treats a missing gmailstate as "no read-check": check the kit's board still has what it calls
        out = self.run_snippet("import sys; sys.path.insert(0, sys.argv[1]); from agentboard import gmailstate; "
                               "g = gmailstate.GmailState; gmailstate.for_owner; g.is_read; g.maybe_refresh; g.thread_of")
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.run_snippet(td.BOARD_THREAD, "<m@example.com>")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIsNone(json.loads(out.stdout))

    def test_rows_run_on_the_kits_board(self):
        out = self.run_snippet(td.BOARD_ROWS)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), {"agents": []})


class Failures(unittest.TestCase):
    """What should have worked and didn't is an error (or, for the board's fallback, a warning), never silence."""

    def board(self, *a, **kw):
        exe = fake_agent_board(*a, **kw)
        orig, td.find_agent_board = td.find_agent_board, lambda: exe
        self.addCleanup(setattr, td, "find_agent_board", orig)
        kit, td.KIT_BOARD = td.KIT_BOARD, Path(tempfile.mkdtemp()) / "no-board"     # not the kit's own board/
        self.addCleanup(setattr, td, "KIT_BOARD", kit)

    def stderr(self, fn, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = fn(*args)
        return out, err.getvalue()

    def test_board_rows(self):
        self.board(rows_ok=True)
        self.assertEqual(self.stderr(td.board_snapshot), ({"agents": [{"id": "rows"}]}, ""))

    def test_board_fallback_warns_with_the_first_error(self):
        self.board(rows_ok=False)
        snap, err = self.stderr(td.board_snapshot)
        self.assertEqual(snap, {"agents": []})
        self.assertIn("rows broke", err)
        self.assertIn("agent-board snapshot", err)

    def test_board_both_fail_names_both(self):
        self.board(rows_ok=False, snapshot_ok=False)
        with self.assertRaises(ValueError) as e:
            td.board_snapshot()
        self.assertIn("rows broke", str(e.exception))
        self.assertIn("boom", str(e.exception))

    def test_gmail_thread(self):
        self.board(rows_ok=True, thread='"t1"')
        self.assertEqual(td.gmail_thread("<m@x>"), "t1")
        self.board(rows_ok=True, thread="null")
        self.assertIsNone(td.gmail_thread("<m@x>"))
        self.board(rows_ok=True, thread="not json")
        with self.assertRaises(ValueError):
            td.gmail_thread("<m@x>")
        self.board(rows_ok=True, thread="exit")
        with self.assertRaises(ValueError):
            td.gmail_thread("<m@x>")

    def test_mail_owners(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(td.mail_owners(CFG, tmp / "missing.json"), ["owner@example.com"])
        self.assertEqual(td.mail_owners({}, tmp / "missing.json"), [])
        (tmp / "c.json").write_text('{"allowlist": ["A@x", "Owner@example.com"]}')
        self.assertEqual(td.mail_owners(CFG, tmp / "c.json"), ["owner@example.com", "a@x"])
        self.assertEqual(td.mail_owners({}, tmp / "c.json"), ["a@x", "owner@example.com"])
        (tmp / "bad.json").write_text("{nope")
        with self.assertRaises(ValueError):
            td.mail_owners(CFG, tmp / "bad.json")
        for shape in ('["a@x"]', '{"allowlist": "a@x"}', '{"allowlist": [1]}'):
            (tmp / "shape.json").write_text(shape)
            with self.assertRaisesRegex(ValueError, "allowlist is a list"):
                td.mail_owners(CFG, tmp / "shape.json")

    def test_done_ops(self):
        desk, _ = make_desk({"number": 3, "title": "t", "body": "", "state": "open", "labels": []})
        self.assertIsNone(desk.done_op("a"))
        desk.record_op("a", {"ok": 1})
        self.assertEqual(desk.done_op("a"), {"ok": 1})
        desk._ops_path().write_text("{half a fi")
        with self.assertRaises(ValueError):
            desk.done_op("a")
        with self.assertRaises(ValueError):
            desk.record_op("b", {})

    def test_audit_failure_is_reported(self):
        blocker = Path(tempfile.mkdtemp()) / "a-file"
        blocker.write_text("")
        orig, td.STATE_DIR = td.STATE_DIR, blocker / "state"          # can't be made: its parent is a file
        self.addCleanup(setattr, td, "STATE_DIR", orig)
        _, err = self.stderr(td.audit, {"action": "assign"})
        self.assertIn("audit", err)


if __name__ == "__main__":
    unittest.main()
