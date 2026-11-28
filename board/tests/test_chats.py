"""Group chats on the board (agentboard/chats.py and its actions): python3 -m unittest discover -s tests

agent-group is the mailroom's command, another component with its own tests, and here it would read and deliver to
the owner's real groups and agents, so each test answers for it with a stand-in that records the argv and stdin it was
given.
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
REPLY = "X5O_N7WdXobdN9KHoDyKHw"
TOPIC = "8fLuiBEUyaigdFaTgeVN9A"
DASHED = "-FyE0Oabv7Ha4jfVa-xxGw"                    # a chat's id may start with "-" (16 bytes in base64url)
NOW = 1_800_000_000.0
ALICE, BOB = {"device": "desktop", "agent": "alice"}, {"device": "desktop", "agent": "bob"}
RELEASE = {"id": TOPIC, "title": "Release"}


def chat(**fields):
    """A chat as agent-group's list --json gives it: no posts, no topics, but for `fields`."""
    return dict({"id": GROUP, "title": "Garden", "members": [], "maker": None, "task": None, "hidden": False,
                 "created": 1, "updated": 2, "posts": 0, "last_post": None, "topics": []}, **fields)


def details(**fields):
    """A chat as agent-group's show, new and set --json give it to the owner: chat()'s fields and each member's alert
    settings, the default (named) for the chat and its own for each topic (none), but for `fields`."""
    c = chat(**fields)
    c.setdefault("member_alerts", [dict(m, alerts="named", topic_alerts={}) for m in c["members"]])
    return c


def post(post_id, created, text, owner=False, **fields):
    """A post as agent-group's posts --json gives it, in no topic: from the desktop, alpha's, or the owner's when
    `owner`; `fields` over all of that."""
    return dict({"id": post_id, "created": created, "from": "desktop", "from_agent": None if owner else "alpha",
                 "from_owner": owner, "sender_label": "Alex" if owner else "alpha@desktop", "reply_to": None,
                 "topic": None, "text": text}, **fields)


ROWS = [{"id": "11111111-2222-3333-4444-555555555555", "cli": "claude", "live": True, "display": "alpha", "name": "alpha"},
        {"id": "0190dddd-0000-7000-8000-00000000d001", "cli": "codex", "live": True, "display": "(untitled)", "name": ""},
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
                    {"action": "chat_post", "group": GROUP, "post": "-" + POST[1:], "title": "t", "text": "hi"},
                    {"action": "chat_post", "group": GROUP, "post": None, "title": "t", "text": "hi"},
                    {"action": "chat_post", "group": GROUP, "post": POST, "title": "t", "text": "   "},
                    {"action": "chat_post", "group": GROUP, "post": POST, "title": "two\nlines", "text": "hi"},
                    {"action": "chat_posts", "group": GROUP, "since": "x; rm"},
                    {"action": "chat_new", "title": "t", "members": [ROWS[1]["id"]]},          # an unnamed session
                    {"action": "chat_new", "title": "t", "members": ["99999999-2222-3333-4444-555555555555"]},
                    {"action": "chat_new", "title": "t", "members": [ROWS[0]["id"]] * 17},
                    {"action": "chat_new", "title": "", "members": [ROWS[0]["id"]]},
                    {"action": "chat_members", "group": GROUP, "op": "delete", "members": [ROWS[0]["id"]]},
                    {"action": "chat_hide", "group": GROUP, "hidden": "yes"},
                    {"action": "chat_new", "title": "tab\there", "members": [ROWS[0]["id"]]},
                    {"action": "chat_share", "group": GROUP},                  # no such action in the kit
                    {"action": "chat_accept", "group": GROUP, "agents": [ALICE]}):
            run = AgentGroup()
            res = self.act(req, run)
            self.assertFalse(res["ok"], req)
            self.assertEqual(run.calls, [], req)

    def test_new_and_members_name_board_agents_by_session_name(self):
        alpha = {"device": "desktop", "agent": "alpha"}
        beta = {"device": "desktop", "agent": "beta"}
        made = details(title="-Garden", members=[alpha, beta])
        run = AgentGroup((0, json.dumps(made), ""))
        res = self.act({"action": "chat_new", "title": "  -Garden  ", "members": [ROWS[0]["id"], ROWS[2]["id"]]}, run)
        # the chat as chat_show gives it, which the Mac reads as a Chat and opens by its id: each member's per-topic
        # settings a list, as chats.show makes them, not agent-group's dict
        self.assertEqual(res, {"ok": True, "chat": dict(made, member_alerts=[
            dict(alpha, alerts="named", topic_alerts=[]), dict(beta, alerts="named", topic_alerts=[])])})
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "new", "--json", "--as", "owner", "--with", "alpha,beta",
                                           "--", "-Garden"])
        run = AgentGroup()
        self.assertTrue(self.act({"action": "chat_members", "group": GROUP, "op": "remove",
                                  "members": [ROWS[2]["id"]]}, run)["ok"])
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "remove", "--as", "owner", "--", GROUP, "beta"])
        run = AgentGroup()
        self.assertTrue(self.act({"action": "chat_hide", "group": GROUP, "hidden": False}, run)["ok"])
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "unhide", "--as", "owner", "--", GROUP])

    def test_new_and_set_give_the_chat_as_show_does_and_a_chat_made_is_never_said_to_be_unmade(self):
        alerts = dict(ALICE, alerts="named", topic_alerts={TOPIC: "all", "a_b": "never"})
        shown = details(members=[ALICE], member_alerts=[alerts])
        for what, got in (("new", chats.new("Garden", ["alice"], AgentGroup((0, json.dumps(shown), "")))),
                          ("set", chats.set_chat(GROUP, "Garden", ["alice"], AgentGroup((0, json.dumps(shown), "")))),
                          ("show", chats.show(GROUP, AgentGroup((0, json.dumps(shown), ""))))):
            self.assertEqual(got["member_alerts"][0]["topic_alerts"],
                             [{"topic": TOPIC, "alerts": "all"}, {"topic": "a_b", "alerts": "never"}], what)
        # agent-group made the chat but its answer does not check out: the error says it was made, and which, so
        # the owner does not make it again
        bare = {k: v for k, v in shown.items() if k != "members"}
        run = AgentGroup((0, json.dumps(bare), ""))
        res = self.act({"action": "chat_new", "title": "Garden", "members": [ROWS[0]["id"]]}, run)
        self.assertFalse(res["ok"])
        self.assertIn("agent-group made chat %s, but its answer is not understood" % GROUP, res["error"])
        self.assertIn("not a chat with members", res["error"])
        self.assertEqual(res["made"], GROUP)                    # the Mac says it was made, not "Not made"
        res = self.act({"action": "chat_new", "title": "Garden", "members": [ROWS[0]["id"]]},
                       AgentGroup((0, json.dumps([]), "")))
        self.assertIn("agent-group made the chat, but its answer is not understood", res["error"])
        self.assertIn("made", res)
        self.assertIsNone(res["made"])                          # made, but which is not known
        audit = [json.loads(line) for line in actions.AUDIT.read_text().splitlines()]
        self.assertEqual([(a["action"], a["group"], a["members"]) for a in audit],
                         [("chat_new", GROUP, ["alpha"]), ("chat_new", None, ["alpha"])])   # each on the record

    def test_the_chat_list_has_the_snapshot_s_rows(self):
        # the Mac reads both as ChatSummary, whose members is a count
        raw = [chat(members=[{"device": "desktop", "agent": "alpha"}], posts=3, last_post=9)]
        listed = self.act({"action": "chat_list"}, AgentGroup((0, json.dumps(raw), "")))
        snap = subchats.summary(AgentGroup((0, json.dumps(raw), "")))
        self.assertEqual(listed["chats"], snap["chats"])
        self.assertEqual(listed["chats"][0]["members"], 1)

    def test_posts_show_the_owner_s_text_without_the_footer(self):
        mine = "Put the parser fixes live." + chats.footer("k3f9")
        theirs = "quoting it:" + chats.footer("k3f9")                      # an agent's post is shown as it is
        run = AgentGroup((0, json.dumps([post(POST, 1, mine, owner=True), post(GROUP, 2, theirs, reply_to=POST)]), ""))
        res = self.act({"action": "chat_posts", "group": GROUP, "since": POST}, run)
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "posts", "--json", "--as", "owner", "--since=" + POST,
                                           "--", GROUP])
        self.assertEqual([(p["text"], p.get("ref")) for p in res["posts"]],
                         [("Put the parser fixes live.", "k3f9"), (theirs, None)])
        self.assertEqual(chats.split_footer("no footer"), ("no footer", None))

    def test_posts_carry_their_topic_and_sender_and_the_owner_s_are_known_without_from_owner(self):
        mine = "Plan for the release." + chats.footer("k3f9")
        raw = [post(POST, 1, mine, owner=True, topic=RELEASE),
               post(REPLY, 2, "on it", reply_to=POST, topic=RELEASE,
                    **{"from": "mac", "from_agent": "mac-agent", "sender_label": "mac-agent@mac"})]
        res = self.act({"action": "chat_posts", "group": GROUP}, AgentGroup((0, json.dumps(raw), "")))
        self.assertEqual([(p["from_owner"], p["sender_label"], p["topic"], p["text"], p.get("ref"))
                          for p in res["posts"]],
                         [(True, "Alex", RELEASE, "Plan for the release.", "k3f9"),
                          (False, "mac-agent@mac", RELEASE, "on it", None)])
        # an agent-group that does not mark the owner's posts: a post with no agent is the owner's
        bare = [{k: v for k, v in p.items() if k != "from_owner"} for p in raw]
        res = self.act({"action": "chat_posts", "group": GROUP}, AgentGroup((0, json.dumps(bare), "")))
        self.assertEqual([p.get("ref") for p in res["posts"]], ["k3f9", None])
        res = self.act({"action": "chat_posts", "group": GROUP}, AgentGroup((0, json.dumps({"id": POST}), "")))
        self.assertIn("not a list of posts", res["error"])

    def test_a_chat_is_listed_with_its_topics(self):
        raw = [chat(members=[ALICE, BOB], posts=5, last_post=5, topics=[dict(RELEASE, posts=2, last_post=2)]),
               {k: v for k, v in chat(id=DASHED).items() if k != "topics"}]          # an agent-group that lists none
        res = self.act({"action": "chat_list"}, AgentGroup((0, json.dumps(raw), "")))
        self.assertEqual([(c["id"], c["members"], c["topics"]) for c in res["chats"]],
                         [(GROUP, 2, [{"id": TOPIC, "title": "Release", "posts": 2, "last_post": 2}]), (DASHED, 0, [])])
        res = self.act({"action": "chat_list"}, AgentGroup((0, json.dumps([chat(topics=["Release"])]), "")))
        self.assertIn("does not list its topics as topics", res["error"])

    def test_an_id_starting_with_a_dash_is_never_read_as_an_option(self):
        dashed = "-" + REPLY[1:]
        run = AgentGroup((0, "[]", ""))
        self.assertTrue(self.act({"action": "chat_posts", "group": DASHED, "since": dashed}, run)["ok"])
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "posts", "--json", "--as", "owner", "--since=" + dashed,
                                           "--", DASHED])
        run = AgentGroup((0, "posted", ""))
        res = self.act({"action": "chat_post", "group": DASHED, "post": POST, "title": "Garden", "text": "hi",
                        "topic": "-" + TOPIC[1:], "reply_to": dashed}, run)
        self.assertTrue(res["ok"], res)
        self.assertEqual(run.calls[0][0], ["/bin/agent-group", "post", "--as", "owner", "--id", POST,
                                           "--topic=-" + TOPIC[1:], "--reply-to=" + dashed, "--", DASHED])

    def test_a_post_goes_in_a_topic_opens_one_or_answers_a_post(self):
        base = {"action": "chat_post", "group": GROUP, "post": POST, "title": "Shared", "text": "hi"}
        for place, flags in (({"topic": TOPIC}, ["--topic=" + TOPIC]),
                             ({"new_topic": "  -Release plan  "}, ["--new-topic=-Release plan"]),
                             ({"reply_to": REPLY}, ["--reply-to=" + REPLY]),
                             ({"topic": TOPIC, "reply_to": REPLY}, ["--topic=" + TOPIC, "--reply-to=" + REPLY]),
                             ({"new_topic": "Aside", "reply_to": REPLY}, ["--new-topic=Aside", "--reply-to=" + REPLY]),
                             ({"topic": None, "new_topic": None}, [])):
            run = AgentGroup((0, "posted %s" % POST, ""))
            res = self.act(dict(base, **place), run)
            self.assertTrue(res["ok"], (place, res))
            (argv, stdin), = run.calls
            self.assertEqual(argv, ["/bin/agent-group", "post", "--as", "owner", "--id", POST] + flags + ["--", GROUP])
            self.assertTrue(stdin.startswith("hi\n\n(Alex posted this"))
        audit = [json.loads(line) for line in actions.AUDIT.read_text().splitlines()]
        self.assertEqual([(a["topic"], a["new_topic"], a["reply_to"]) for a in audit],
                         [(TOPIC, None, None), (None, "-Release plan", None), (None, None, REPLY),
                          (TOPIC, None, REPLY), (None, "Aside", REPLY), (None, None, None)])
        for place in ({"topic": "Release"}, {"reply_to": REPLY + "x"}, {"new_topic": "   "},
                      {"new_topic": "a\tb"}, {"new_topic": "line separator"}, {"new_topic": "nel\x85"},
                      {"new_topic": "é" * 101}, {"new_topic": 7}, {"new_topic": "Release", "topic": TOPIC}):
            run = AgentGroup()
            res = self.act(dict(base, **place), run)
            self.assertFalse(res["ok"], place)
            self.assertEqual(run.calls, [], place)
        self.assertIsNone(chats.title_error("é" * 100, "topic"))         # 200 bytes: agent-group takes it

    def layout(self, *subs):
        return [{"name": "Work", "members": [], "subgroups": list(subs)}]

    def test_a_sub_group_s_chat_follows_it_from_two_named_agents_until_it_is_deleted(self):
        made = details(title="Web")              # new and set --json answer as show --json does
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
        run = AgentGroup((0, json.dumps(details()), ""))
        self.assertEqual(subchats.sync(self.layout(em), ROWS, run), [])    # an exited agent is no problem
        self.assertEqual([argv for argv, _ in run.calls],
                         [["/bin/agent-group", "new", "--json", "--as", "owner", "--with", "alpha,gamma", "--", "Web"]])

    def test_a_sub_group_s_chat_made_but_not_understood_is_kept_and_never_made_again(self):
        # agent-group made the chat, but its answer does not check out (here, one that lists no members): the sync
        # keeps the chat's id, so the next one changes it, not makes another
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[3]["id"]], "id": SUB}
        older = {"id": GROUP, "title": "Web", "maker": None}
        problems = subchats.sync(self.layout(em), ROWS, AgentGroup((0, json.dumps(older), "")))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn('sub-group "Work / Web": agent-group made chat %s, but its answer is not understood' % GROUP,
                      problems[0])
        self.assertEqual(subchats.state()["chats"][SUB], {"chat": GROUP, "title": "Web", "members": ["alpha", "gamma"],
                                                          "unnamed": []})
        run = AgentGroup((0, json.dumps(details()), ""))
        self.assertEqual(subchats.sync(self.layout(dict(em, name="Web 2")), ROWS, run), [])
        self.assertEqual([argv[1] for argv, _ in run.calls], ["set"])          # the same chat, not a second one
        # an answer with no id it can keep: the problem says the chat was made, though not which
        problems = subchats.sync(self.layout(dict(em, id=SOLO)), ROWS, AgentGroup((0, json.dumps([]), "")))
        self.assertIn("agent-group made the chat, but its answer is not understood", problems[0])
        self.assertNotIn(SOLO, subchats.state()["chats"])

    def test_a_sub_group_sent_without_its_id_hides_no_chat(self):
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[3]["id"]], "id": SUB}
        self.act({"action": "groups", "groups": self.layout(em)}, AgentGroup((0, json.dumps(details()), "")))
        run = AgentGroup()                                     # an app older than sub-group chats sends no ids
        res = self.act({"action": "groups", "groups": self.layout({"name": "Web", "members": em["members"]})}, run)
        self.assertEqual(res["chat_problems"], ['sub-group "Work / Web" came without its id: relaunch the board app'])
        self.assertEqual(run.calls, [])
        self.assertEqual(subchats.state()["chats"][SUB]["chat"], GROUP)

    def test_a_failed_sync_is_a_snapshot_problem_and_keeps_the_chat(self):
        em = {"name": "Web", "members": [ROWS[0]["id"], ROWS[3]["id"]], "id": SUB}
        self.act({"action": "groups", "groups": self.layout(em)}, AgentGroup((0, json.dumps(details()), "")))
        run = AgentGroup((4, "", "agent-group: no group " + GROUP))
        res = self.act({"action": "groups", "groups": self.layout(dict(em, name="Web 2"))}, run)
        self.assertTrue(res["ok"])                             # the groups are stored all the same
        self.assertEqual(res["chat_problems"], ['sub-group "Work / Web 2": agent-group: no group ' + GROUP])
        self.assertEqual(subchats.state()["chats"][SUB]["title"], "Web")
        listed = [chat(title="Web")]
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
        run = AgentGroup((0, json.dumps(details()), ""))
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
            subchats.sync(self.layout(em), ROWS, AgentGroup((0, json.dumps(details()), "")))
        self.assertEqual(subchats.state()["chats"][SUB]["chat"], GROUP)
        run = AgentGroup((0, json.dumps(details()), ""))
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
        run = AgentGroup((0, json.dumps(details()), ""))
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
        self.act({"action": "groups", "groups": self.layout(em)}, AgentGroup((0, json.dumps(details()), "")))
        res = self.act({"action": "groups", "groups": self.layout()}, AgentGroup((1, "", "agent-group: store locked")))
        self.assertEqual(res["chat_problems"], ['hiding the chat of sub-group "Web", which left the layout: '
                                                'agent-group: store locked'])
        self.assertNotIn("gone", subchats.state()["chats"][SUB])
        run = AgentGroup()
        self.act({"action": "groups", "groups": self.layout()}, run)
        self.assertEqual([argv[1] for argv, _ in run.calls], ["hide"])
        self.assertTrue(subchats.state()["chats"][SUB]["gone"])

    def test_agent_group_s_failure_or_absence_is_the_error(self):
        res = self.act({"action": "chat_list"}, AgentGroup((2, "", "agent-group: only members and Alex read a group\n")))
        self.assertEqual(res, {"ok": False, "error": "agent-group: only members and Alex read a group"})
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
        listed = [chat(members=[{"device": "desktop", "agent": "alpha"}], posts=3, last_post=9)]
        run = AgentGroup((0, json.dumps(listed), ""), (1, "", "agent-group: groups store unreadable"))
        with tempfile.TemporaryDirectory() as d:
            col = fixtures.collector(d, chats=sources.Periodic(lambda: subchats.summary(run), chats.REFRESH, None,
                                                               True))
            snap = col.snapshot(NOW)
            self.assertEqual(snap["chats"], {"chats": [{"id": GROUP, "title": "Garden", "hidden": False,
                                                        "members": 1, "posts": 3, "last_post": 9, "topics": [],
                                                        "subgroup": None, "unnamed": []}]})
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
        os.environ["CODEX_THREAD_ID"] = "0190dddd-0000-7000-8000-00000000d001"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["done", "k3f9", "-m", "shipped"]), 0)
        self.assertEqual(sent.messages()[0]["done"]["by"], "0190dddd-0000-7000-8000-00000000d001")


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
