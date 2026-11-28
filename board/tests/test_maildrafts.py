"""The owner's drafts kept on the desktop (agentboard/maildrafts.py) and the draft actions over them (actions.py):
python3 -m unittest discover -s tests

Every test keeps its drafts in a temporary folder of real files, with its own clock. The words in them are made up.
"""
import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_mail import AgentMail, MailTest, answer  # noqa: E402
from agentboard import actions, maildrafts, mailbox  # noqa: E402

T = "18f2a9c0d1e2f3a4"
KEY = "Zt0pZt0pZt0pZt0pZt0pZt"
NOW = 1_800_000_000.0


def content(text="", picks=(), comments=(), files=(), key=None):
    return {"text": text, "picks": list(picks), "comments": list(comments), "files": list(files), "key": key}


PICK = {"widget": "s12-1", "picked": [0, 2], "question": "Which days?", "labels": ["Mon", "Wed"], "comment": None}
COMMENT = {"id": "c1", "message": "s12", "at": 40, "quote": "the cache is 512 MB", "comment": "make it 1 GB"}


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "mail-drafts"
        self.clock = Clock()
        self.store = maildrafts.Store(self.root, self.clock, dict(maildrafts.DEFAULTS))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root.parent)

    def put(self, text, base, device="mac", **more):
        return self.store.put(T, base, content(text, **more), device)

    def later(self, seconds):
        self.clock.t += seconds

    def file(self, data):
        got = self.store.add_file(T, data)
        return {"id": got["id"], "name": "notes.txt", "size": got["size"]}

    def test_a_draft_saved_is_read_back_with_its_rev_and_who_saved_it(self):
        self.assertEqual(self.store.view(T), {"thread": T, "rev": 0, "draft": None, "updated": None, "versions": []})
        got = self.put("Yes, merge it.", 0, picks=[PICK], comments=[COMMENT])
        self.assertEqual(got["rev"], 1)
        self.assertEqual(self.store.view(T)["draft"], content("Yes, merge it.", picks=[PICK], comments=[COMMENT]))
        self.assertEqual(self.store.view(T)["updated"], {"at": NOW, "device": "mac"})
        self.assertEqual(self.store.listing(), {T: {"rev": 1, "updated": {"at": NOW, "device": "mac"}}})
        self.assertEqual(self.put("Yes, merge it now.", 1, device="other")["rev"], 2)
        self.assertEqual(self.store.view(T)["updated"]["device"], "other")
        self.assertEqual(self.put("Yes, merge it now.", 2)["rev"], 2)            # the same again changes nothing

    def test_a_rev_is_never_given_twice_even_once_an_emptied_draft_s_record_has_gone(self):
        self.put("a draft", 0, key=KEY)
        self.store.sent(T, KEY)                                              # rev 2, the record kept, empty
        self.assertEqual(self.put("more", 2)["rev"], 3)                       # no conflict for a device that saw it
        self.store.discard(T, 3, "mac")
        self.later(8 * 86400)
        self.store.put("18f2a9c0d1e2f3a5", 0, content("another thread"), "mac")   # the sweep: the record goes
        self.assertEqual(self.store.view(T)["rev"], 0)
        got = self.put("a new draft", 0, device="other")
        self.assertEqual(got["rev"], 6)                                      # after 1 to 5, given before
        self.assertTrue(self.put("from a device that read rev 1", 1)["conflict"])

    def test_a_count_gone_missing_goes_on_past_every_record_and_a_damaged_one_is_an_error(self):
        self.put("one", 0)
        self.store.put("18f2a9c0d1e2f3a5", 0, content("two"), "mac")             # rev 2
        (self.root / "rev").unlink()
        self.assertEqual(self.put("one, more", 1)["rev"], 3)
        (self.root / "rev").write_text("not a count")
        with self.assertRaisesRegex(ValueError, "not the drafts' count of changes"):
            self.put("one, more again", 3)
        self.assertEqual(self.store.view(T)["draft"]["text"], "one, more")    # nothing changed

    def test_the_store_is_the_owner_s_alone(self):
        self.put("hi", 0)
        self.file(b"x")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "threads" / (T + ".json")).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "files").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "files" / T).stat().st_mode), 0o700)

    def test_a_blank_draft_is_no_draft_and_is_not_listed(self):
        self.put("hi", 0)
        self.put("  \n", 1)
        self.assertEqual(self.store.listing(), {})
        self.assertIsNone(self.store.view(T)["draft"])

    def test_a_save_from_a_device_that_has_not_seen_the_draft_keeps_its_content_as_a_version(self):
        self.put("from the Mac", 0)
        self.later(5)
        got = self.put("from the other window", 0, device="other")              # it last read it before the Mac's save
        self.assertTrue(got["conflict"])
        self.assertEqual((got["rev"], got["draft"]["text"]), (1, "from the Mac"))    # the current draft stays
        self.assertEqual(got["updated"]["device"], "mac")                  # which the device names in its note
        [v] = got["versions"]
        self.assertEqual((v["id"], v["why"], v["device"], v["at"], v["words"]), (got["kept"], "conflict", "other",
                                                                                 NOW + 5, "from the other window"))
        # the same content is no conflict, whatever its base
        self.assertNotIn("conflict", self.put("from the Mac", 0, device="other"))
        # an empty one keeps nothing
        got = self.put("", 0, device="other")
        self.assertEqual((got["conflict"], got["kept"], len(got["versions"])), (True, None, 1))

    def test_a_save_replacing_content_kept_more_than_two_minutes_ago_keeps_it_as_a_version(self):
        self.put("first words", 0)
        self.later(60)
        self.put("first words, more", 1)
        self.assertEqual(self.store.view(T)["versions"], [])                # within two minutes of starting: none
        self.later(61)
        got = self.put("first words, more and more", 2)
        [v] = got["versions"]
        self.assertEqual((v["why"], v["words"], v["at"], v["kept"]), ("saved", "first words, more", NOW + 60, NOW + 121))
        self.later(119)
        self.put("first words, more and more again", 3)
        self.assertEqual(len(self.store.view(T)["versions"]), 1)            # two minutes since the last one: not yet
        self.later(2)
        self.assertEqual(len(self.put("and again", 4)["versions"]), 2)

    def test_emptying_a_draft_keeps_what_it_held(self):
        self.put("all of this", 0)
        got = self.put("", 1)
        self.assertEqual([(v["why"], v["words"]) for v in got["versions"]], [("cleared", "all of this")])

    def test_versions_are_pruned_to_the_newest_twenty_and_none_older_than_a_week(self):
        self.store.cfg = dict(maildrafts.DEFAULTS, versions=3)
        rev = 0
        for i in range(5):
            rev = self.put("draft %d" % i, rev)["rev"]
            self.later(121)
        self.assertEqual([v["words"] for v in self.store.view(T)["versions"]], ["draft 3", "draft 2", "draft 1"])
        oldest = min(v["kept"] for v in self.store.view(T)["versions"])       # "draft 1"'s
        self.clock.t = oldest + 7 * 86400 + 1                                 # a week and a second after it was kept
        self.put("draft 5", rev)
        self.assertEqual([v["words"] for v in self.store.view(T)["versions"]], ["draft 4", "draft 3", "draft 2"])
        self.later(7 * 86400)
        got = self.store.discard(T, self.store.view(T)["rev"], "mac")
        self.assertEqual([v["words"] for v in got["versions"]], ["draft 5"])  # the week-old ones gone, the discard kept

    def test_the_same_content_kept_again_is_one_version_the_newest(self):
        self.put("same words", 0)
        v1 = self.store.discard(T, 1, "mac")["version"]
        self.store.restore(T, v1, 2, "mac")
        got = self.store.discard(T, 3, "mac")
        self.assertEqual([v["words"] for v in got["versions"]], ["same words"])
        self.assertEqual(got["versions"][0]["id"], got["version"])

    def test_every_thread_is_pruned_when_the_store_is_written(self):
        self.put("old one", 0)
        self.store.discard(T, 1, "mac")
        self.later(8 * 86400)
        self.store.put("18f2a9c0d1e2f3a5", 0, content("another thread"), "mac")
        self.assertFalse((self.root / "threads" / (T + ".json")).exists())  # nothing left there: its record goes

    def test_discard_keeps_the_draft_as_a_version_that_brings_it_back(self):
        self.put("keep me", 0, picks=[PICK], key=KEY)
        got = self.store.discard(T, 1, "other")
        self.assertEqual((got["rev"], got["draft"]), (2, None))
        [v] = got["versions"]
        self.assertEqual((v["id"], v["why"], v["device"], v["words"], v["picks"]), (got["version"], "discarded", "mac",
                                                                                     "keep me", 1))
        back = self.store.restore(T, got["version"], 2, "other")
        self.assertEqual((back["rev"], back["draft"]), (3, content("keep me", picks=[PICK])))   # its key not kept
        self.assertEqual(back["updated"]["device"], "other")

    def test_discard_or_restore_of_a_draft_changed_since_changes_nothing(self):
        self.put("keep me", 0)
        v = self.store.discard(T, 1, "mac")["version"]
        self.put("newer", 2, device="other")
        self.assertTrue(self.store.discard(T, 2, "mac")["conflict"])
        self.assertTrue(self.store.restore(T, v, 2, "mac")["conflict"])
        self.assertEqual(self.store.view(T)["draft"]["text"], "newer")
        with self.assertRaisesRegex(maildrafts.Refused, "no longer kept"):
            self.store.restore(T, "0" * 12, 3, "mac")

    def test_restoring_keeps_the_draft_it_replaces(self):
        self.put("one", 0)
        v = self.store.discard(T, 1, "mac")["version"]
        self.put("two", 2)
        got = self.store.restore(T, v, 3, "mac")
        self.assertEqual(got["draft"]["text"], "one")
        self.assertEqual([(x["why"], x["words"]) for x in got["versions"]], [("replaced", "two"), ("discarded", "one")])

    def test_a_reply_sent_with_the_draft_s_key_empties_it_and_keeps_no_version(self):
        self.put("Yes, merge it.", 0, key=KEY)
        self.assertIsNone(self.store.sent(T, "another-key-another-key"))      # another reply: the draft stays
        self.assertEqual(self.store.view(T)["draft"]["text"], "Yes, merge it.")
        self.assertEqual(self.store.sent(T, KEY), 2)
        self.assertEqual(self.store.view(T), {"thread": T, "rev": 2, "draft": None, "updated": None, "versions": []})
        self.put("next", 2)                                                  # the device that sent it starts another
        self.assertNotIn("conflict", self.store.view(T))
        self.assertIsNone(self.store.sent(T, KEY))                            # a draft with no key is not the reply's

    def test_a_draft_holding_a_key_takes_no_change_but_its_key_going(self):
        self.put("on its way", 0, key=KEY)
        got = self.put("changed meanwhile", 1, device="other")
        self.assertTrue(got["conflict"])
        self.assertEqual(got["draft"]["key"], KEY)
        self.assertEqual(got["versions"][0]["words"], "changed meanwhile")    # what was typed is kept
        self.assertTrue(self.store.put(T, 1, content("on its way", key="Another0Another0Another"), "other")["conflict"])
        self.assertTrue(self.put("on its way", 0, device="other")["conflict"])   # nor goes for one that has not seen it
        self.assertEqual(self.store.view(T)["draft"]["key"], KEY)
        got = self.put("on its way", 1)                                       # the reply refused: its key goes
        self.assertEqual((got["rev"], got["draft"]["key"]), (2, None))
        # a device that has not seen the draft may still put a key on the same content (to send it)
        self.assertEqual(self.put("on its way", 0, key=KEY)["draft"]["key"], KEY)

    def test_files_are_kept_once_by_their_digest_and_must_be_there_to_be_named(self):
        f = self.file(b"a,b\n1,2\n")
        self.assertEqual(f["id"], hashlib.sha256(b"a,b\n1,2\n").hexdigest())
        self.assertEqual(self.store.add_file(T, b"a,b\n1,2\n")["id"], f["id"])
        self.assertEqual(self.store.file(T, f["id"]), b"a,b\n1,2\n")
        self.put("", 0, files=[f])
        self.assertEqual(self.store.listing(), {T: {"rev": 1, "updated": {"at": NOW, "device": "mac"}}})  # files alone
        missing = dict(f, id="0" * 64)
        with self.assertRaisesRegex(maildrafts.Refused, "notes.txt is not on the desktop"):
            self.put("", 1, files=[missing])
        with self.assertRaisesRegex(maildrafts.Refused, "is 8 bytes on the desktop, not 9"):
            self.put("", 1, files=[dict(f, size=9)])
        with self.assertRaisesRegex(maildrafts.Refused, "attach it again"):
            self.store.file(T, "0" * 64)
        (self.root / "files" / T / f["id"]).write_bytes(b"other!!!")
        with self.assertRaisesRegex(maildrafts.Refused, "not the file its draft names"):
            self.store.file(T, f["id"])

    def test_a_draft_carries_no_more_files_than_a_reply(self):
        f = self.file(b"x")
        many = [dict(f, name="f%d" % i) for i in range(mailbox.MAX_FILES + 1)]
        with self.assertRaisesRegex(maildrafts.Refused, "at most 20"):
            self.put("", 0, files=many)
        with self.assertRaisesRegex(maildrafts.Refused, "more than"):
            self.put("", 0, files=[dict(f, size=mailbox.MAX_FILE_BYTES), dict(f, name="g", size=1)])
        with self.assertRaisesRegex(maildrafts.Refused, "at most"):
            self.store.add_file(T, b"x" * (mailbox.MAX_FILE_BYTES + 1))

    def test_a_file_no_draft_or_version_names_goes_a_day_after_its_upload(self):
        kept, dropped, named = self.file(b"in a version"), self.file(b"named by nothing"), self.file(b"in the draft")
        self.put("with a file", 0, files=[kept])
        self.store.discard(T, 1, "mac")                                      # a version names it
        self.later(23 * 3600)
        self.put("", 2, files=[named])
        folder = self.root / "files" / T
        self.assertEqual(len(os.listdir(folder)), 3)                          # not a day yet
        self.later(2 * 3600)
        self.put("now", 3, files=[named])
        self.assertEqual(sorted(os.listdir(folder)), sorted([kept["id"], named["id"]]))

    def test_content_that_is_not_a_draft_s_is_refused(self):
        self.assertEqual(maildrafts.check_content({"text": "x" * maildrafts.MAX_TEXT})["text"], "x" * maildrafts.MAX_TEXT)
        self.assertGreater(maildrafts.MAX_TEXT, mailbox.MAX_REPLY)          # a draft longer than a reply is still kept
        for bad in ({"text": 5}, {"text": "x" * (maildrafts.MAX_TEXT + 1)}, {"txt": "x"}, {"key": "-bad"},
                    {"picks": [dict(PICK, widget="x")]}, {"picks": [dict(PICK, labels=["one"])]},
                    {"picks": [PICK, PICK]}, {"comments": [dict(COMMENT, at=-1)]},
                    {"files": [{"id": "x", "name": "a", "size": 1}]},
                    {"files": [{"id": "0" * 64, "name": "", "size": 1}]}, None, []):
            with self.assertRaises(maildrafts.Refused, msg=bad):
                maildrafts.check_content(bad)

    def test_a_damaged_record_is_never_written_over(self):
        path = self.root / "threads" / (T + ".json")
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            self.put("hi", 0)
        self.assertEqual(path.read_text(), "not json")
        self.assertEqual(self.store.listing(), {})

    def test_the_numbers_come_from_the_settings(self):
        path = self.root.parent / "drafts.json"
        path.write_text(json.dumps({"versions": 5, "version_minutes": 0.5}))
        self.assertEqual(maildrafts.settings(path), dict(maildrafts.DEFAULTS, versions=5, version_minutes=0.5))
        for bad in ({"versions": 2.5}, {"version_days": 0}, {"upload_hours": "1"}, {"version_minutes": True}):
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError, msg=bad):
                maildrafts.settings(path)


class DraftActionTest(MailTest):
    """The draft actions over a store of real files, as the Mac's stream runs them (every window of the app saves as
    "mac")."""

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp()) / "mail-drafts"
        self.clock = Clock()
        self.store = maildrafts.Store(self.root, self.clock, dict(maildrafts.DEFAULTS))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root.parent)
        super().tearDown()

    def act(self, req, run=None):
        return actions.dispatch(req, lambda: [], lambda: [], drafts=self.store, **({"run": run} if run else {}))

    def test_each_action_answers_and_the_record_holds_no_words(self):
        got = self.act({"action": "draft_put", "thread": T, "base_rev": 0, "device": "mac",
                        "draft": content("A secret reply", picks=[PICK])})
        self.assertEqual((got["ok"], got["rev"]), (True, 1))
        self.assertEqual(self.act({"action": "draft_list"}), {"ok": True, "drafts": {T: {"rev": 1, "updated": {
            "at": NOW, "device": "mac"}}}})
        got = self.act({"action": "draft_get", "thread": T})
        self.assertEqual((got["ok"], got["draft"]["text"], got["rev"]), (True, "A secret reply", 1))
        data = base64.b64encode(b"col\n1\n").decode()
        f = self.act({"action": "draft_file_add", "thread": T, "data": data})["file"]
        self.assertEqual(f, {"id": hashlib.sha256(b"col\n1\n").hexdigest(), "size": 6})
        self.assertEqual(self.store.file(T, f["id"]), b"col\n1\n")
        got = self.act({"action": "draft_discard", "thread": T, "base_rev": 1, "device": "mac"})
        self.assertEqual((got["ok"], got["draft"]), (True, None))
        self.assertEqual(got["versions"][0]["id"], got["version"])
        got = self.act({"action": "draft_restore", "thread": T, "version": got["version"], "base_rev": 2,
                        "device": "mac"})
        self.assertEqual(got["draft"]["text"], "A secret reply")
        record = actions.AUDIT.read_text()
        self.assertNotIn("secret", record)
        self.assertNotIn("Which days", record)
        self.assertEqual([(r["action"], r.get("rev"), r.get("device")) for r in self.audited()], [
            ("draft_put", 1, "mac"), ("draft_file_add", None, None), ("draft_discard", 2, "mac"),
            ("draft_restore", 3, "mac")])
        self.assertEqual({k: self.audited()[0][k] for k in ("chars", "picks", "files", "conflict", "key")},
                         {"chars": 14, "picks": 1, "files": 0, "conflict": False, "key": False})

    def test_a_conflict_is_an_answer_not_an_error(self):
        self.act({"action": "draft_put", "thread": T, "base_rev": 0, "device": "mac", "draft": content("one")})
        got = self.act({"action": "draft_put", "thread": T, "base_rev": 0, "device": "mac", "draft": content("two")})
        self.assertEqual((got["ok"], got["conflict"], got["draft"]["text"]), (True, True, "one"))
        self.assertTrue(self.audited()[-1]["conflict"])

    def test_requests_that_do_not_check_out_are_refused(self):
        for req in ({"action": "draft_get", "thread": "--x"},
                    {"action": "draft_put", "thread": T, "base_rev": -1, "device": "mac", "draft": content()},
                    {"action": "draft_put", "thread": T, "base_rev": 0, "device": "tablet", "draft": content()},
                    {"action": "draft_put", "thread": T, "base_rev": 0, "device": "watch", "draft": content()},
                    {"action": "draft_put", "thread": T, "base_rev": 0, "device": "mac", "draft": {"text": 1}},
                    {"action": "draft_restore", "thread": T, "base_rev": 0, "device": "mac", "version": "../x"},
                    {"action": "draft_file_add", "thread": T, "data": "not base64!"},
                    {"action": "draft_file_add", "thread": "../x", "data": "eA=="}):
            got = self.act(req)
            self.assertFalse(got["ok"], req)
            self.assertTrue(got["error"], req)
        self.assertEqual(self.audited(), [])

    def test_a_reply_sends_files_from_its_draft_and_empties_the_draft_holding_its_key(self):
        f = self.act({"action": "draft_file_add", "thread": T, "data": base64.b64encode(b"a,b").decode()})["file"]
        self.act({"action": "draft_put", "thread": T, "base_rev": 0, "device": "mac",
                  "draft": content("see the file", files=[dict(f, name="r.csv")], key=KEY)})
        run = AgentMail(answer({"ok": True, "key": "i31", "status": "sending"}))
        got = self.act({"action": "mail_reply", "thread": T, "key": KEY, "text": "see the file",
                        "files": [{"name": "r.csv", "draft": f["id"]}]}, run)
        self.assertEqual((got["ok"], got["draft"]), (True, {"cleared": True, "rev": 2}))
        self.assertEqual(json.loads(run.calls[0][1]), {"text": "see the file", "files": [
            {"name": "r.csv", "data": base64.b64encode(b"a,b").decode()}]})
        self.assertEqual(self.store.view(T)["draft"], None)
        self.assertEqual(self.store.view(T)["versions"], [])                 # a draft sent is not kept
        self.assertEqual(self.audited()[-1]["files"], [{"name": "r.csv", "bytes": 3}])
        self.assertTrue(self.audited()[-1]["draft"])

    def test_a_reply_under_another_key_or_one_refused_leaves_the_draft(self):
        self.act({"action": "draft_put", "thread": T, "base_rev": 0, "device": "mac", "draft": content("mine", key=KEY)})
        run = AgentMail(answer({"ok": True, "key": "i31", "status": "sending"}))
        got = self.act({"action": "mail_reply", "thread": T, "key": "Other0Other0Other0Other", "text": "x"}, run)
        self.assertEqual(got["draft"], {"cleared": False, "rev": None})
        run = AgentMail((mailbox.ANSWER_REFUSED, "", "agent-mail: no agent mail in this thread"))
        got = self.act({"action": "mail_reply", "thread": T, "key": KEY, "text": "mine"}, run)
        self.assertEqual((got["ok"], got["refused"]), (False, True))
        self.assertEqual(self.store.view(T)["draft"]["text"], "mine")

    def test_a_reply_whose_draft_cannot_be_emptied_has_gone_and_says_so(self):
        path = self.root / "threads" / (T + ".json")
        path.parent.mkdir(parents=True)
        path.write_text("damaged")
        run = AgentMail(answer({"ok": True, "key": "i31", "status": "sending"}))
        got = self.act({"action": "mail_reply", "thread": T, "key": KEY, "text": "x"}, run)
        self.assertTrue(got["ok"])                                           # the reply went
        self.assertNotIn("draft", got)
        self.assertRegex(got["error"], "^the reply went, but its draft was not emptied: .*not valid JSON")
        self.assertRegex(self.audited()[-1]["draft_error"], "not valid JSON")
        self.assertEqual(path.read_text(), "damaged")

    def test_a_reply_naming_a_file_its_drafts_do_not_hold_is_refused_before_agent_mail(self):
        run = AgentMail()
        got = self.act({"action": "mail_reply", "thread": T, "key": KEY, "text": "x",
                        "files": [{"name": "r.csv", "draft": "0" * 64}]}, run)
        self.assertEqual((got["ok"], got["refused"]), (False, True))
        self.assertIn("attach it again", got["error"])
        got = self.act({"action": "mail_reply", "thread": T, "key": KEY, "text": "x",
                        "files": [{"name": "r.csv", "draft": "../x"}]}, run)
        self.assertEqual((got["ok"], got["refused"]), (False, True))
        self.assertEqual(run.calls, [])


if __name__ == "__main__":
    unittest.main()
