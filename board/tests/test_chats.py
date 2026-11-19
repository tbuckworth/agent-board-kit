"""Group chats on the board (agentboard/chats.py and its actions): python3 -m unittest discover -s tests

agent-group is the mailroom's command, another component with its own tests, and here it would read and deliver to
the owner's real groups and agents, so each test answers for it with a stand-in that records the argv and stdin it was given.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fixtures  # noqa: E402
import stand_ins  # noqa: E402
from agentboard import actions, chats, groups, sent, sources, subchats, timesources  # noqa: E402

GROUP = "q8F3Ab_-q8F3Ab_-q8F3Ab"
SUB, SOLO = "6F1C2E9A-0B7D-4C1E-9F3A-2D5B8C7E4A10", "0A9B8C7D-6E5F-4A3B-8C1D-0E9F8A7B6C5D"   # the Mac's sub-group ids
POST = "Zt0pZt0pZt0pZt0pZt0pZt"
NOW = 1_800_000_000.0
ROWS = [{"id": "11111111-2222-3333-4444-555555555555", "cli": "claude", "live": True, "display": "alpha", "name": "alpha"},
        {"id": "0190aaaa-0000-7000-8000-00000000a001", "cli": "codex", "live": True, "display": "(untitled)", "name": ""},
        {"id": "22222222-2222-3333-4444-555555555555", "cli": "claude", "live": False, "display": "beta", "name": "beta"},
        {"id": "33333333-2222-3333-4444-555555555555", "cli": "claude", "live": True, "display": "gamma", "name": "gamma"}]


AgentGroup = stand_ins.Command        # agent-group


class ChatTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.real = (sent.PATH, sent.MARKS, actions.AUDIT, sources.find_bin, groups.GROUPS)
        sent.PATH, sent.MARKS, actions.AUDIT = d / "sent.jsonl", d / "sent-marks.jsonl", d / "actions.jsonl"
        groups.GROUPS = d / "groups.json"                                  # subchats' files sit beside it
        sources.find_bin = stand_ins.find_bin

    def tearDown(self):
        sent.PATH, sent.MARKS, actions.AUDIT, sources.find_bin, groups.GROUPS = self.real

    def act(self, req, run):
        return actions.dispatch(req, lambda: list(ROWS), lambda: [], run=run)

    def test_a_post_goes_on_stdin_with_its_ref_and_is_on_the_record(self):
        run = AgentGroup((0, "posted %s" % POST, ""))
        text = "--help; rm -rf ~ $(x)\nsecond line"
        res = self.act({"action": "chat_post", "group": GROUP, "post": POST, "title": "Garden", "text": text}, run)
        self.assertTrue(res["ok"], res)
        (argv, stdin), = run.calls
        self.assertEqual(argv, ["/bin/agent-group", "post", "--as", "owner", "--id", POST, "--", GROUP])
        rec = sent.records()[-1]
        self.assertEqual((rec["kind"], rec["to"], rec["group"], rec["post"], rec["text"]),
                         ("chat", "group chat Garden", GROUP, POST, text))
        self.assertEqual(stdin, text + chats.footer(rec["ref"]))
        self.assertIn("%s %s -m" % (sent.DONE_CMD, rec["ref"]), stdin)        # one member marks it done

    def test_bad_requests_never_reach_agent_group(self):
        for req in ({"action": "chat_post", "group": "short", "post": POST, "title": "t", "text": "hi"},
                    {"action": "chat_post", "group": "-" + GROUP[1:], "post": POST, "title": "t", "text": "hi"},
                    {"action": "chat_post", "group": GROUP, "post": None, "title": "t", "text": "hi"},
                    {"action": "chat_post", "group": GROUP, "post": POST, "title": "t", "text": "   "},
                    {"action": "chat_post", "group": GROUP, "post": POST, "title": "two\nlines", "text": "hi"},
                    {"action": "chat_posts", "group": GROUP, "since": "x; rm"},
                    {"action": "chat_new", "title": "t", "members": [ROWS[1]["id"]]},          # an unnamed session
                    {"action": "chat_new", "title": "t", "members": ["99999999-2222-3333-4444-555555555555"]},
                    {"action": "chat_new", "title": "t", "members": [ROWS[0]["id"]] * 17},
                    {"action": "chat_new", "title": "", "members": [ROWS[0]["id"]]},
                    {"action": "chat_members", "group": GROUP, "op": "delete", "members": [ROWS[0]["id"]]},
                    {"action": "chat_hide", "group": GROUP, "hidden": "yes"}):
            run = AgentGroup()
            res = self.act(req, run)
            self.assertFalse(res["ok"], req)
            self.assertEqual(run.calls, [], req)

    def test_new_and_members_name_board_agents_by_session_name(self):
        made = {"id": GROUP, "title": "-Garden", "members": [{"device": "desktop", "agent": "alpha"}], "maker": None}
        run = AgentGroup((0, json.dumps(made), ""))
        res = self.act({"action": "chat_new", "title": "  -Garden  ", "members": [ROWS[0]["id"], ROWS[2]["id"]]}, run)
        self.assertEqual(res, {"ok": True, "chat": made})                  # the Mac opens it by its id
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "new", "--json", "--as", "owner", "--with", "alpha,beta",
                                           "--", "-Garden"])
        run = AgentGroup()
        self.assertTrue(self.act({"action": "chat_members", "group": GROUP, "op": "remove",
                                  "members": [ROWS[2]["id"]]}, run)["ok"])
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "remove", "--as", "owner", "--", GROUP, "beta"])
        run = AgentGroup()
        self.assertTrue(self.act({"action": "chat_hide", "group": GROUP, "hidden": False}, run)["ok"])
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "unhide", "--as", "owner", "--", GROUP])

    def test_the_chat_list_has_the_snapshot_s_rows(self):
        # the Mac reads both as ChatSummary, whose members is a count
        raw = [{"id": GROUP, "title": "Garden", "members": [{"device": "desktop", "agent": "alpha"}], "maker": None,
                "task": None, "hidden": False, "created": 1, "updated": 2, "posts": 3, "last_post": 9}]
        listed = self.act({"action": "chat_list"}, AgentGroup((0, json.dumps(raw), "")))
        snap = subchats.summary(AgentGroup((0, json.dumps(raw), "")))
        self.assertEqual(listed["chats"], snap["chats"])
        self.assertEqual(listed["chats"][0]["members"], 1)

    def test_posts_show_the_owner_s_text_without_the_footer(self):
        mine = "Put the parser fixes live." + chats.footer("k3f9")
        theirs = "quoting it:" + chats.footer("k3f9")                      # an agent's post is shown as it is
        run = AgentGroup((0, json.dumps([
            {"id": POST, "created": 1, "from": "desktop", "from_agent": None, "reply_to": None, "text": mine},
            {"id": GROUP, "created": 2, "from": "desktop", "from_agent": "alpha", "reply_to": POST, "text": theirs}]), ""))
        res = self.act({"action": "chat_posts", "group": GROUP, "since": POST}, run)
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "posts", "--json", "--as", "owner", "--since", POST,
                                           "--", GROUP])
        self.assertEqual([(p["text"], p.get("ref")) for p in res["posts"]],
                         [("Put the parser fixes live.", "k3f9"), (theirs, None)])
        self.assertEqual(chats.split_footer("no footer"), ("no footer", None))
        self.assertTrue(chats.footer("k3f9").startswith("\n\n(Alex posted this from the agent board as message k3f9."))
        self.assertEqual(chats.as_owner(), "owner")                # the mailroom's alias, whatever the owner's name

    def layout(self, *subs):
        return [{"name": "Work", "members": [], "subgroups": list(subs)}]

    def test_a_sub_group_s_chat_follows_it_from_two_named_agents_until_it_is_deleted(self):
        made = {"id": GROUP, "title": "Web", "members": [], "maker": None}
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[1]["id"], ROWS[3]["id"]], "id": SUB}
        solo = {"name": "Solo", "members": [ROWS[0]["id"], "99999999-2222-3333-4444-555555555555"], "id": SOLO}
        run = AgentGroup((0, json.dumps(made), ""))
        res = self.act({"action": "groups", "groups": self.layout(em, solo)}, run)
        self.assertTrue(res["ok"], res)
        self.assertEqual([argv for argv, _ in run.calls],      # Solo: one named agent (a tile off the board) is no chat
                         [["/bin/agent-group", "new", "--json", "--as", "owner", "--with", "alpha,gamma", "--", "Web"]])
        self.assertEqual(res["chat_problems"], [])             # an agent without a usable name is no problem:
        self.assertEqual(subchats.state()["chats"], {SUB: {"chat": GROUP, "title": "Web", "members": ["alpha", "gamma"],
                                                           "unnamed": ["(untitled)"]}})
        listed = self.act({"action": "chat_list"}, AgentGroup((0, json.dumps([made]), "")))
        self.assertEqual(listed["chats"][0]["unnamed"], ["(untitled)"])   # its chat says it is left out

        run = AgentGroup((0, json.dumps(made), ""))            # renamed to "-Web", and gamma moved out
        em2 = dict(em, name="-Web", members=[ROWS[0]["id"]])
        self.assertEqual(self.act({"action": "groups", "groups": self.layout(em2)}, run)["chat_problems"], [])
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "set", "--json", "--as", "owner", "--title=-Web",
                                           "--with=alpha", "--", GROUP])
        listed = self.act({"action": "chat_list"}, AgentGroup((0, json.dumps([made, dict(made, id=POST)]), "")))
        self.assertEqual([(c["subgroup"], c["unnamed"]) for c in listed["chats"]], [("-Web", []), (None, [])])
        run = AgentGroup()                                     # its members are the sub-group's to change
        res = self.act({"action": "chat_members", "group": GROUP, "op": "add", "members": [ROWS[3]["id"]]}, run)
        self.assertIn('follow the sub-group "-Web"', res["error"])
        self.assertEqual(run.calls, [])

        run = AgentGroup()                                     # the sub-group leaves the layout: its chat is hidden
        self.assertTrue(self.act({"action": "groups", "groups": self.layout()}, run)["ok"])
        self.assertEqual([argv for argv, _ in run.calls], [["/bin/agent-group", "hide", "--as", "owner", "--", GROUP]])
        self.assertTrue(subchats.state()["chats"][SUB]["gone"])
        self.assertEqual(self.act({"action": "chat_list"}, AgentGroup((0, json.dumps([made]), "")))["chats"][0]
                         ["subgroup"], None)                   # and its members are the owner's to change again
        run = AgentGroup()
        self.act({"action": "groups", "groups": self.layout()}, run)
        self.assertEqual(run.calls, [])                        # hidden once
        run = AgentGroup((0, "", ""), (0, json.dumps(made), ""))   # it comes back (a group made a sub-group again)
        self.act({"action": "groups", "groups": self.layout(em2)}, run)
        self.assertEqual([argv[1] for argv, _ in run.calls], ["unhide", "set"])
        self.assertNotIn("gone", subchats.state()["chats"][SUB])

    def test_an_agent_that_has_exited_is_left_out_of_its_sub_group_s_chat(self):
        # agent-group adds only a session that exists: an exited one (its tile still in the sub-group, shown offline)
        # would make it refuse the whole chat
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[2]["id"], ROWS[3]["id"]], "id": SUB}
        run = AgentGroup((0, json.dumps({"id": GROUP}), ""))
        self.assertEqual(subchats.sync(self.layout(em), ROWS, run), [])    # an exited agent is no problem
        self.assertEqual([argv for argv, _ in run.calls],
                         [["/bin/agent-group", "new", "--json", "--as", "owner", "--with", "alpha,gamma", "--", "Web"]])

    def test_a_sub_group_sent_without_its_id_hides_no_chat(self):
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[3]["id"]], "id": SUB}
        self.act({"action": "groups", "groups": self.layout(em)}, AgentGroup((0, json.dumps({"id": GROUP}), "")))
        run = AgentGroup()                                     # an app older than sub-group chats sends no ids
        res = self.act({"action": "groups", "groups": self.layout({"name": "Web", "members": em["members"]})}, run)
        self.assertEqual(res["chat_problems"], ['sub-group "Work / Web" came without its id: relaunch the board app'])
        self.assertEqual(run.calls, [])
        self.assertEqual(subchats.state()["chats"][SUB]["chat"], GROUP)

    def test_a_failed_sync_is_a_snapshot_problem_and_keeps_the_chat(self):
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[3]["id"]], "id": SUB}
        self.act({"action": "groups", "groups": self.layout(em)}, AgentGroup((0, json.dumps({"id": GROUP}), "")))
        run = AgentGroup((4, "", "agent-group: no group " + GROUP))
        res = self.act({"action": "groups", "groups": self.layout(dict(em, name="Web 2"))}, run)
        self.assertTrue(res["ok"])                             # the groups are stored all the same
        self.assertEqual(res["chat_problems"], ['sub-group "Work / Web 2": agent-group: no group ' + GROUP])
        self.assertEqual(subchats.state()["chats"][SUB]["title"], "Web")
        listed = [{"id": GROUP, "title": "Web", "members": [], "hidden": False, "posts": 0, "last_post": None}]
        with tempfile.TemporaryDirectory() as d:
            col = fixtures.collector(d, chats=sources.Periodic(lambda: subchats.summary(AgentGroup((0, json.dumps(
                listed), ""))), chats.REFRESH, None, True))
            snap = col.snapshot(NOW)
        self.assertEqual(snap["chats"]["chats"][0]["subgroup"], "Web")
        self.assertIn({"source": "sub-group chats", "error": res["chat_problems"][0]}, snap["problems"])

    def test_a_state_file_that_cannot_be_written_stops_the_sync_before_any_chat_changes(self):
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[3]["id"]], "id": SUB}
        self.act({"action": "groups", "groups": self.layout()}, AgentGroup())     # makes the state file and lock
        folder = groups.GROUPS.parent
        folder.chmod(0o500)                                                      # the file can no longer be replaced
        run = AgentGroup((0, json.dumps({"id": GROUP}), ""))
        try:
            with self.assertRaises(PermissionError):
                subchats.sync(self.layout(em), ROWS, run)
        finally:
            folder.chmod(0o700)
        self.assertEqual(run.calls, [])                        # no chat made that the file could not record

    def test_a_log_that_cannot_be_written_still_leaves_the_chat_recorded(self):
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[3]["id"]], "id": SUB}
        groups.beside("subgroup-chats.log").mkdir(parents=True)   # the log cannot be written
        with self.assertRaises(IsADirectoryError):             # loud: the groups action answers with the error
            subchats.sync(self.layout(em), ROWS, AgentGroup((0, json.dumps({"id": GROUP}), "")))
        self.assertEqual(subchats.state()["chats"][SUB]["chat"], GROUP)
        run = AgentGroup((0, json.dumps({"id": GROUP}), ""))
        with self.assertRaises(IsADirectoryError):
            subchats.sync(self.layout(dict(em, name="Web 2")), ROWS, run)
        self.assertEqual([argv[1] for argv, _ in run.calls], ["set"])   # the same chat, not a second one

    def test_the_groups_action_fails_when_the_sync_cannot_run_but_keeps_the_groups(self):
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[3]["id"]], "id": SUB}
        subchats.state_path().parent.mkdir(parents=True, exist_ok=True)
        subchats.state_path().write_text("{damaged")
        run = AgentGroup()
        res = self.act({"action": "groups", "groups": self.layout(em)}, run)
        self.assertFalse(res["ok"])                            # the Mac shows it and sends the groups again
        self.assertIn("the groups were stored, but the sub-group chats were not brought in line", res["error"])
        self.assertIn("subgroup-chats.json", res["error"])
        self.assertEqual(json.loads(groups.GROUPS.read_text())["groups"][0]["subgroups"][0]["id"], SUB)
        self.assertIn("subgroup-chats.json", self.act({"action": "chat_list"}, AgentGroup((0, "[]", "")))["error"])
        subchats.state_path().write_text("{}")
        lock = groups.beside("subgroup-chats.lock")
        lock.unlink()                                          # in the test's own folder
        lock.mkdir()                                           # a lock that cannot be taken
        res = self.act({"action": "groups", "groups": self.layout(em)}, run)
        self.assertIn("IsADirectoryError", res["error"])
        self.assertEqual(run.calls, [])

    def test_a_sub_group_s_chat_holds_at_most_sixteen_and_a_title_agent_group_would_refuse_is_a_problem(self):
        rows = [{"id": "%08d-2222-3333-4444-555555555555" % i, "name": "a%02d" % i, "display": "a%02d" % i,
                 "live": True}
                for i in range(chats.MAX_MEMBERS + 1)]
        big = {"name": "Big", "members": [r["id"] for r in rows], "id": SUB}
        run = AgentGroup((0, json.dumps({"id": GROUP}), ""))
        problems = subchats.sync(self.layout(big), rows, run)
        self.assertEqual(problems, ['sub-group "Work / Big" has 17 named agents; its chat holds the first 16'])
        self.assertEqual(run.calls[0][0][6], ",".join(r["name"] for r in rows[:chats.MAX_MEMBERS]))
        run = AgentGroup()
        problems = subchats.sync(self.layout(dict(big, name="   ")), rows, run)
        self.assertIn('sub-group "Work /    ": the chat needs a title', problems)
        self.assertEqual(run.calls, [])
        self.assertEqual(subchats.state()["chats"][SUB]["title"], "Big")

    def test_a_hide_that_fails_is_a_problem_and_is_tried_again(self):
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[3]["id"]], "id": SUB}
        self.act({"action": "groups", "groups": self.layout(em)}, AgentGroup((0, json.dumps({"id": GROUP}), "")))
        res = self.act({"action": "groups", "groups": self.layout()}, AgentGroup((1, "", "agent-group: store locked")))
        self.assertEqual(res["chat_problems"], ['hiding the chat of sub-group "Web", which left the layout: '
                                                'agent-group: store locked'])
        self.assertNotIn("gone", subchats.state()["chats"][SUB])
        run = AgentGroup()
        self.act({"action": "groups", "groups": self.layout()}, run)
        self.assertEqual([argv[1] for argv, _ in run.calls], ["hide"])
        self.assertTrue(subchats.state()["chats"][SUB]["gone"])

    def test_agent_group_s_failure_or_absence_is_the_error(self):
        res = self.act({"action": "chat_list"}, AgentGroup((2, "", "agent-group: only members and alex read a group\n")))
        self.assertEqual(res, {"ok": False, "error": "agent-group: only members and alex read a group"})
        res = self.act({"action": "chat_show", "group": GROUP}, AgentGroup((0, "not json", "")))
        self.assertIn("not JSON", res["error"])
        sources.find_bin = lambda name: None
        res = self.act({"action": "chat_list"}, AgentGroup())
        self.assertIn("agent-group is not installed", res["error"])


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.real = (sources.find_bin, groups.GROUPS)
        sources.find_bin = stand_ins.find_bin
        groups.GROUPS = Path(tempfile.mkdtemp()) / "groups.json"         # no sub-group chats yet

    def tearDown(self):
        sources.find_bin, groups.GROUPS = self.real

    def test_the_snapshot_carries_the_chats_and_says_when_they_fail(self):
        listed = [{"id": GROUP, "title": "Garden", "members": [{"device": "desktop", "agent": "alpha"}],
                   "maker": None, "task": None, "hidden": False, "created": 1, "updated": 2, "posts": 3, "last_post": 9}]
        run = AgentGroup((0, json.dumps(listed), ""), (1, "", "agent-group: groups store unreadable"))
        with tempfile.TemporaryDirectory() as d:
            col = fixtures.collector(d, chats=sources.Periodic(lambda: subchats.summary(run), chats.REFRESH, None,
                                                               True))
            snap = col.snapshot(NOW)
            self.assertEqual(snap["chats"], {"chats": [{"id": GROUP, "title": "Garden", "hidden": False,
                                                        "members": 1, "posts": 3, "last_post": 9, "subgroup": None,
                                                        "unnamed": []}]})
            self.assertEqual(snap["problems"], [])
            snap = col.snapshot(NOW + chats.REFRESH + 1)                    # the next read fails: the last value stays
            self.assertEqual(snap["chats"]["chats"][0]["posts"], 3)
            self.assertEqual([p["source"] for p in snap["problems"]], ["group chats"])
        with self.assertRaises(chats.ChatError):                           # a list that isn't of chats is an error
            chats.summary(AgentGroup((0, json.dumps([listed[0], "?"]), "")))
        sources.find_bin = lambda name: None
        self.assertIsNone(subchats.summary(run))                           # no agent-group: no Chats button
        sub = {"name": "Web", "members": [ROWS[0]["id"], ROWS[2]["id"]], "id": SUB}
        self.assertEqual(subchats.sync([{"name": "Work", "members": [], "subgroups": [sub]}], ROWS, run), [])
        self.assertFalse(subchats.state_path().exists())                  # and no sub-group chats


class DoneByTest(unittest.TestCase):
    """A chat post is marked done by whichever member dealt with it, so the mark says who: a Codex one too."""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.real = (sent.PATH, sent.MARKS, dict(os.environ))
        sent.PATH, sent.MARKS = d / "sent.jsonl", d / "sent-marks.jsonl"

    def tearDown(self):
        sent.PATH, sent.MARKS = self.real[:2]
        os.environ.clear()
        os.environ.update(self.real[2])

    def test_a_codex_member_s_mark_names_its_thread(self):
        path = str(Path(__file__).resolve().parent.parent / "bin" / "agent-board")
        loader = importlib.machinery.SourceFileLoader("agent_board_cli", path)
        cli = importlib.util.module_from_spec(importlib.util.spec_from_loader("agent_board_cli", loader))
        loader.exec_module(cli)
        sent.record("chat", "group chat Garden", "ship it", True, ref="k3f9", group=GROUP, post=POST)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ["CODEX_THREAD_ID"] = "0190aaaa-0000-7000-8000-00000000a001"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["done", "k3f9", "-m", "shipped"]), 0)
        self.assertEqual(sent.messages()[0]["done"]["by"], "0190aaaa-0000-7000-8000-00000000a001")


class TimeTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.real = (sent.PATH, sent.MARKS)
        sent.PATH, sent.MARKS = d / "sent.jsonl", d / "sent-marks.jsonl"

    def tearDown(self):
        sent.PATH, sent.MARKS = self.real

    def test_a_chat_post_is_an_item_of_its_own(self):
        sent.record("chat", "group chat Garden", "ship it", True, ts=NOW, ref="k3f9", group=GROUP, post=POST)
        acts = timesources.board_messages(NOW - 60, {}, {}, hand=[], window=600)
        self.assertEqual(acts, [{"ts": NOW, "kind": "board", "key": "chat:" + GROUP, "label": "group chat Garden"}])


if __name__ == "__main__":
    unittest.main()
