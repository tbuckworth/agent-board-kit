"""The Mail tab's mailbox: the message store, transmit keeping each mail before sending it, the sent columns, the
References of an answer, Alex's flags and the rules on them, the mailbox commands, replies written in the tab (with the
files he attaches) and the daemon routing them as his mail, the files put where the agent can open them, the backfill
and the backup. Gmail is the boundary (fakes.fake_gmail), and SSH to the Mac (subprocess.run, answered by the Mac's own
agent-mail rpc run here); the store, routing and composing are real."""
import argparse
import base64
import contextlib
import email
import email.policy
import gzip
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from test_remote import Base, CFG, CFG_FILE, MAC_FILE, SID  # noqa: E402
from test_groups import A, ALEX, Base as GroupBase  # noqa: E402
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import (alerts, backup, cli, config, deliver, groupmail, mailbox, mailroom, mailview,  # noqa: E402
                       messages, outbound, recovery, remote, replyfiles, routing, rpc, store)
from agentmail.store import Store  # noqa: E402

OWNER = CFG["allowlist"][0]
HOST = CFG["host_id"]
KEY = "k" * 20
INFO = dict(ok=True, cost=0, turns=1, duration_s=1, error="", result="done")
DAY = 86400
PNG = b"\x89PNG\r\n\x1a\n" + b"plot" * 300
MAC_SID = "87654321-4321-4321-8321-cba987654321"


def b64(data):
    return base64.b64encode(data).decode()


def no_agent_env():
    """The environment with none of an agent session's markers, as the board runs agent-mail."""
    return mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in cli.AGENT_ENV}, clear=True)


class MessageStore(unittest.TestCase):
    def setUp(self):
        homes.patch(self, config, "MESSAGES_DIR", homes.temp_dir(self) / "messages")

    def test_a_message_is_kept_once_under_its_sha256_owner_only_and_read_back(self):
        raw = b"Subject: hi\r\n\r\nbody\r\n"
        sha = messages.keep(raw)
        path = config.MESSAGES_DIR / sha[:2] / (sha + ".eml")
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for folder in (path.parent, config.MESSAGES_DIR):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        before = path.stat().st_mtime_ns
        self.assertEqual(messages.keep(raw), sha)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(messages.load(sha), raw)
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])   # no temp file left

    def test_a_damaged_message_or_a_bad_name_is_refused(self):
        sha = messages.keep(b"one")
        messages.path_in(config.MESSAGES_DIR, sha).write_bytes(b"two")
        with self.assertRaisesRegex(ValueError, "damaged"):
            messages.load(sha)
        for bad in ("../../etc/passwd", "AB" * 32, ""):
            with self.assertRaisesRegex(ValueError, "not a message sha256"):
                messages.load(bad)


class Mailbox(Base):
    """A desktop mailroom with one live session, mailtab, whose mail goes through transmit to a fake Gmail that puts
    each new mail in a thread of its own (tN) with Gmail id gN and Message-ID <gN@mail.gmail.com>."""

    def setUp(self):
        super().setUp()
        homes.patch(self, config, "MESSAGES_DIR", homes.temp_dir(self) / "messages")
        homes.patch(self, mailbox, "gmail", self.fake)
        self.raws = []
        self.fake.get_metadata.side_effect = lambda svc, gid, names: {
            "payload": {"headers": [{"name": "Message-ID", "value": "<%s@mail.gmail.com>" % gid}]}}
        self.sess = self.st.session_for("claude", SID, "mailtab", origin_host=HOST)

    def send(self, svc, raw, thread_id=None):
        self.raws.append(raw)
        self.sent.append((email.message_from_bytes(raw, policy=email.policy.default), thread_id))
        n = len(self.sent)
        return {"id": "g%d" % n, "threadId": thread_id or "t%d" % n}

    def agent_mail(self, subject="Run A results", text="The run finished.", to=OWNER, cc=None, sess="mailtab",
                   inb=None, files=(), at=None):
        """A mail from session sess ("mailtab", another sessions row, or None: unrouted) through transmit, in reply to
        inbound row inb if given, its sent time set to at if given. Returns its sent row."""
        row = self.sess if sess == "mailtab" else sess
        subject = routing.tag_subject(subject, row["tag"]) if row else subject
        outbound.transmit(self.ctx, row, to, cc, subject, outbound.Mail(text, None, [], list(files)), inb)
        sent_id = self.st.max_sent_id()
        if at is not None:
            self.st.con.execute("update sent set sent_at=? where id=?", (at, sent_id))
        return self.st.con.execute("select * from sent where id=?", (sent_id,)).fetchone()

    def his_mail(self, thread, text="Thanks.", at=None, gid="in1"):
        """An email of Alex's in thread, as the daemon records one it delivered. Returns its inbound row."""
        i = self.st.record_inbound(gmail_id=gid, thread_id=thread, message_id="<%s@mail.gmail.com>" % gid,
                                   from_addr=OWNER, from_name="Alex", subject="Re: Run A results", received_at=at,
                                   action="delivered", body=text)
        return self.st.inbound(i)

    def listed(self, box="all", query=None, now=2e9):
        return {t["id"]: t for t in mailbox.list_threads(self.st, CFG, box, query, 100, now)["threads"]}

    def mark(self, threads, op, upto=None, until=None, now=2e9):
        return mailbox.mark(self.st, CFG, threads, op, upto, until, now)["threads"]

    def answer(self, thread, text="Looks good.", key=KEY, files=()):
        with no_agent_env():
            return mailbox.answer(self.st, CFG, thread, key, text, list(files))

    def main(self, argv, stdin="", env=None):
        """(exit code, stdout, stderr) of agent-mail argv with stdin redirected from a file holding stdin, outside an
        agent session unless env sets one's markers."""
        path = homes.temp_dir(self) / "stdin"
        path.write_text(stdin)
        with no_agent_env(), mock.patch.dict(os.environ, env or {}), open(path) as fh, \
                mock.patch("sys.stdin", fh), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                code = self.am.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class Transmit(Mailbox):
    def test_each_mail_is_kept_before_it_is_sent_and_its_row_says_what_it_was(self):
        attach = homes.temp_dir(self) / "results.csv"
        attach.write_text("a,b\n1,2\n")
        sent = self.agent_mail(cc="sam@example.com", files=outbound.load_files([str(attach)]))
        self.assertEqual(messages.load(sent["eml"]), self.raws[0])   # exactly the bytes Gmail was given
        self.assertEqual((sent["to_addr"], sent["cc_addr"], sent["body"], sent["attachments"]),
                         (OWNER, "sam@example.com", "The run finished.", 1))
        body = homes.temp_dir(self) / "body.txt"
        body.write_text("Daily report.")
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, SSH_CONNECTION=""):
            self.am.do_send(self.ctx, argparse.Namespace(to="owner", cc=None, subject="report", unrouted=True,
                                                         body_file=str(body), html=None, image=None, attach=None))
        unrouted = self.st.con.execute("select * from sent order by id desc limit 1").fetchone()
        self.assertEqual((unrouted["tag"], messages.load(unrouted["eml"])), ("", self.raws[1]))

    def test_a_mail_that_cannot_be_kept_is_not_sent(self):
        blocker = homes.temp_dir(self) / "a-file"
        blocker.write_text("")
        homes.patch(self, config, "MESSAGES_DIR", blocker / "messages")   # no folder can be made under a file
        with self.assertRaises(OSError):
            self.agent_mail()
        self.fake.send_raw.assert_not_called()
        self.assertEqual(self.st.max_sent_id(), 0)

    def test_a_migration_error_other_than_a_column_already_there_is_raised(self):
        homes.patch(self, store, "ADDED_COLUMNS", store.ADDED_COLUMNS + (("no_such_table", "x", "text"),))
        with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
            Store(homes.temp_dir(self) / "s.sqlite")

    def test_an_older_store_gains_the_columns_and_keeps_its_rows(self):
        path = homes.temp_dir(self) / "old.sqlite"
        con = sqlite3.connect(str(path))
        con.execute("create table sent(id integer primary key, tag text not null, gmail_id text, thread_id text, "
                    "message_id text, to_addr text, subject text, sent_at integer not null, "
                    "in_reply_to_inbound integer)")
        con.execute("insert into sent(tag, gmail_id, sent_at) values('t', 'old', 1)")
        con.commit()
        con.close()
        st = Store(path)
        self.addCleanup(st.con.close)
        row = st.con.execute("select * from sent").fetchone()
        self.assertEqual((row["gmail_id"], row["cc_addr"], row["body"], row["eml"], row["attachments"], row["widgets"]),
                         ("old", None, None, None, None, None))


class References(Mailbox):
    def test_an_answer_names_the_thread_s_mail_and_ends_with_the_one_it_answers(self):
        first = self.agent_mail()
        self.agent_mail(inb=self.his_mail(first["thread_id"]))
        msg, thread = self.sent[-1]
        self.assertEqual(thread, first["thread_id"])
        self.assertEqual(msg["In-Reply-To"], "<in1@mail.gmail.com>")
        self.assertEqual(msg["References"].split(), ["<g1@mail.gmail.com>", "<in1@mail.gmail.com>"])

    def test_an_agents_answer_to_a_tab_reply_names_the_agent_mail_gmail_knows(self):
        first = self.agent_mail()
        inb = self.st.inbound(int(self.answer(first["thread_id"])["key"][1:]))
        outbound.reply_mail(self.ctx, self.sess, inb, outbound.Mail("Done.", None, [], []), "test", None)
        msg, thread = self.sent[-1]
        self.assertEqual(thread, first["thread_id"])
        self.assertEqual(msg["In-Reply-To"], routing.local_message_id(KEY))
        self.assertEqual(msg["References"].split(), ["<g1@mail.gmail.com>", routing.local_message_id(KEY)])
        self.assertEqual(msg["Subject"], "Re: " + first["subject"])

    def test_an_answer_under_a_new_subject_to_a_tab_reply_has_no_gmail_link(self):
        first = self.agent_mail()
        inb = self.st.inbound(int(self.answer(first["thread_id"])["key"][1:]))
        outbound.reply_mail(self.ctx, self.sess, inb, outbound.Mail("Done.", None, [], []), "test", "New")
        text = self.sent[-1][0].get_body(("plain",)).get_content()
        self.assertTrue(text.endswith('Continues the thread "Run A results".\n'), text)


class Flags(Mailbox):
    """Alex's flags by Gmail's rules (mailbox.state), through mark and list."""

    def test_new_agent_mail_is_unread_in_the_inbox_until_read_up_to_it(self):
        t = self.agent_mail(at=100)["thread_id"]
        self.assertEqual((self.listed()[t]["box"], self.listed()[t]["unread"]), ("inbox", True))
        newest = self.listed()[t]["newest"]
        self.assertEqual(self.mark([t], "read", upto=newest)[0]["unread"], False)
        self.agent_mail(inb=self.his_mail(t, at=150), at=200)
        self.assertTrue(self.listed()[t]["unread"])           # a newer agent mail
        self.mark([t], "read", upto=newest)                    # a stale read never moves the mark back
        self.assertTrue(self.listed()[t]["unread"])

    def test_marked_unread_after_reading_until_read_again(self):
        t = self.agent_mail()["thread_id"]
        self.mark([t], "read", upto=1, now=1000)
        self.assertTrue(self.mark([t], "unread", now=1001)[0]["unread"])
        self.assertFalse(self.mark([t], "read", upto=1, now=1002)[0]["unread"])

    def test_archived_until_a_newer_agent_mail_and_his_own_mail_moves_nothing(self):
        t = self.agent_mail(at=100)["thread_id"]
        self.mark([t], "read", upto=1)
        self.assertEqual(self.mark([t], "archive", upto=1)[0]["box"], "archived")
        inb = self.his_mail(t, at=150)
        self.assertEqual((self.listed()[t]["box"], self.listed()[t]["unread"]), ("archived", False))
        self.assertEqual(self.listed()[t]["count"], 2)
        self.agent_mail(inb=inb, at=200)
        self.assertEqual((self.listed()[t]["box"], self.listed()[t]["unread"]), ("inbox", True))

    def test_snoozed_until_its_time_then_back_unread_and_on_top(self):
        t = self.agent_mail(at=100)["thread_id"]
        other = self.agent_mail(subject="Other", at=500)["thread_id"]
        self.mark([t], "read", upto=1, now=1000)
        snoozed = self.mark([t], "snooze", upto=1, until=5000, now=1000)[0]
        self.assertEqual((snoozed["box"], snoozed["snooze_until"], snoozed["unread"]), ("snoozed", 5000, False))
        self.assertEqual(list(self.listed(box="inbox", now=4000)), [other])
        woke = self.listed(now=6000)[t]
        self.assertEqual((woke["box"], woke["unread"], woke["last"], woke["snooze_until"]), ("inbox", True, 5000, None))
        self.assertEqual(list(self.listed(box="inbox", now=6000)), [t, other])
        self.assertFalse(self.mark([t], "read", upto=1, now=7000)[0]["unread"])

    def test_a_newer_agent_mail_ends_a_snooze_for_good(self):
        t = self.agent_mail(at=100)["thread_id"]
        self.mark([t], "snooze", upto=1, until=5000, now=1000)
        self.agent_mail(inb=self.his_mail(t, at=150), at=2000)
        self.assertEqual(self.listed(now=3000)[t]["box"], "inbox")
        self.mark([t], "read", upto=2, now=3000)
        later = self.listed(now=6000)[t]
        self.assertEqual((later["unread"], later["last"]), (False, 2000))   # the snooze's time passing changes nothing

    def test_inbox_ends_an_archive_or_a_snooze_and_each_ends_the_other(self):
        t = self.agent_mail()["thread_id"]
        self.mark([t], "archive", upto=1)
        self.assertEqual(self.mark([t], "inbox")[0]["box"], "inbox")
        self.mark([t], "snooze", upto=1, until=5e9)
        self.assertEqual(self.mark([t], "archive", upto=1)[0]["box"], "archived")
        self.mark([t], "snooze", upto=1, until=5e9)
        self.assertEqual(self.listed()[t]["box"], "snoozed")
        self.assertEqual(self.mark([t], "inbox")[0]["box"], "inbox")

    def test_one_upto_marks_several_threads_and_the_counts_follow(self):
        a = self.agent_mail(subject="A")["thread_id"]
        b = self.agent_mail(subject="B")["thread_id"]
        c = self.agent_mail(subject="C")["thread_id"]
        self.mark([a, b], "read", upto=self.st.max_sent_id(), now=1000)
        self.mark([c], "snooze", upto=3, until=5e9, now=1000)
        self.assertEqual(mailbox.list_threads(self.st, CFG, "inbox", None, 100, 2000)["counts"],
                         {"inbox": 2, "unread": 0, "snoozed": 1})
        self.mark([a], "unread", now=1001)
        self.assertEqual(mailbox.summary(self.st, CFG, 2000)["unread"], 1)

    def test_summary_lists_the_agent_mail_to_alex_the_tab_counts_as_read(self):
        now = 2e9
        read = self.agent_mail(subject="A", at=now - DAY)
        unread = self.agent_mail(subject="B", at=now - DAY)
        old = self.agent_mail(subject="C", at=now - 9 * DAY)
        elsewhere = self.agent_mail(subject="D", to="sam@example.com", at=now - DAY)
        self.his_mail(elsewhere["thread_id"])   # his mail puts Sam's thread in the mailbox, but not as mail to him
        self.mark([read["thread_id"], old["thread_id"], elsewhere["thread_id"]], "read", upto=self.st.max_sent_id(),
                  now=now - 10)
        self.assertEqual(mailbox.summary(self.st, CFG, now), {"unread": 1, "inbox": 4, "read": [read["message_id"]]})
        self.mark([read["thread_id"]], "unread", now=now - 5)
        self.assertEqual(mailbox.summary(self.st, CFG, now)["read"], [])
        self.assertTrue(unread["message_id"])

    def test_marks_are_refused_when_they_would_mean_something_else(self):
        t = self.agent_mail()["thread_id"]
        cases = [(["nope"], "read", 1, None, "not in the mailbox"), ([t], "read", None, None, "--upto goes with"),
                 ([t], "unread", 1, None, "--upto goes with"), ([t], "snooze", 1, None, "--until goes with"),
                 ([t], "archive", 1, 3e9, "--until goes with"), ([t], "snooze", 1, 10, "in the past"),
                 ([t], "read", 99, None, "not the id of agent mail"), ([t], "nap", None, None, "--op is one of")]
        for threads, op, upto, until, said in cases:
            with self.subTest(said=said, op=op), self.assertRaisesRegex(outbound.MailError, said):
                mailbox.mark(self.st, CFG, threads, op, upto, until, 2e9)
        for var in cli.AGENT_ENV:
            code, out, err = self.main(["mailbox", "mark", t, "--op", "read", "--upto", "1"], env={var: "x"})
            self.assertEqual((code, out), (2, ""), var)
            self.assertIn("refusing mailbox mark inside an agent session", err)
        self.assertEqual(self.st.mailbox_flags(), {})


class Listing(Mailbox):
    def test_a_thread_s_list_object(self):
        long = "The run finished;   accuracy\n\nwas " + "very " * 60 + "good."
        t = self.agent_mail(text="first", at=100)["thread_id"]
        inb = self.his_mail(t, text="More?", at=150)
        orch = self.st.session_for("orchestrator", "orchestrator", "orchestrator", origin_host=HOST)
        attach = homes.temp_dir(self) / "r.csv"
        attach.write_text("x")
        self.agent_mail(sess=orch, inb=inb, text="routed", at=160, files=outbound.load_files([str(attach)]))
        self.agent_mail(sess=None, inb=inb, text=long, at=170)
        got = self.listed()[t]
        self.assertEqual(got, {
            "id": t, "subject": "Run A results", "agents": ["mailtab (claude)", "orchestrator", "mailroom"],
            "snippet": " ".join(long.split())[:mailbox.SNIPPET_CHARS - 1].rstrip() + "…", "last": 170, "count": 4,
            "unread": True, "box": "inbox", "snooze_until": None, "newest": 3, "attachments": True})

    def test_a_snippet_is_the_words_the_reader_sees_not_the_markdown(self):
        text = "**Merged into main:** [PR #70](https://github.com/o/r/pull/70)\n\n- `agent-mail` fixed\n- one more"
        t = self.agent_mail(text="first", at=100)["thread_id"]
        self.agent_mail(inb=self.his_mail(t, at=150), text=text, at=200)
        said = "Merged into main: PR #70 - agent-mail fixed - one more"
        self.assertEqual(self.listed()[t]["snippet"], said)
        self.mark([t], "read", upto=2)
        self.agent_mail(inb=self.his_mail(t, at=250, gid="in2"), text="last", at=300)
        page = mailbox.show(self.st, CFG, t, 2e9)["document"]
        self.assertIn('<span class="snip">%s</span>' % said, page)       # its closed line on the page

    def test_a_long_mail_s_snippet_comes_from_its_first_paragraphs(self):
        text = "**Done.** See [the PR](https://github.com/o/r/pull/9).\n\n" + "\n\n".join(
            "Paragraph %d: %s" % (i, "detail " * 30) for i in range(40))
        self.assertGreater(len(text), 4 * mailbox.SNIPPET_SOURCE_CHARS)
        got = mailbox.snippet(text)
        self.assertTrue(got.startswith("Done. See the PR. Paragraph 0: detail detail"), got)
        self.assertTrue(got.endswith("…") and len(got) <= mailbox.SNIPPET_CHARS, got)

    def test_only_threads_alex_is_in_and_only_his_and_agents_mail(self):
        mine = self.agent_mail(subject="Mine", cc=OWNER, to="sam@example.com")["thread_id"]
        theirs = self.agent_mail(subject="Theirs", to="sam@example.com")["thread_id"]
        self.st.record_inbound(gmail_id="j", thread_id=mine, from_addr="sam@example.com", subject="Re: Mine",
                               action="held", body="hello")
        self.assertEqual(list(self.listed()), [mine])
        self.assertEqual(self.listed()[mine]["count"], 1)
        self.assertNotIn(theirs, self.listed())

    def test_newest_first_within_the_limit_and_the_box(self):
        a = self.agent_mail(subject="A", at=100)["thread_id"]
        b = self.agent_mail(subject="B", at=300)["thread_id"]
        c = self.agent_mail(subject="C", at=200)["thread_id"]
        self.mark([c], "archive", upto=3)
        ids = lambda box, limit: [t["id"] for t in mailbox.list_threads(self.st, CFG, box, None, limit, 2e9)["threads"]]
        self.assertEqual(ids("all", 100), [b, c, a])
        self.assertEqual(ids("inbox", 1), [b])
        self.assertEqual(ids("archived", 100), [c])
        for box, limit in (("spam", 10), ("all", 0), ("all", 501)):
            with self.assertRaises(outbound.MailError):
                mailbox.list_threads(self.st, CFG, box, None, limit, 2e9)

    def test_search_wants_every_word_somewhere_in_the_thread_ignoring_case(self):
        a = self.agent_mail(subject="Run A results", text="Accuracy rose to 91%.")["thread_id"]
        b = self.agent_mail(subject="Disk space", text="The volume is full.")["thread_id"]
        self.his_mail(b, text="Delete the CHECKPOINTS", gid="in2")
        self.mark([b], "archive", upto=2)
        self.assertEqual(list(self.listed(query="accuracy run")), [a])
        self.assertEqual(list(self.listed(query="mailtab checkpoints")), [b])
        self.assertEqual(list(self.listed(box="inbox", query="checkpoints")), [])
        self.assertEqual(list(self.listed(query="accuracy volume")), [])

    def test_agent_mail_prints_one_json_object(self):
        t = self.agent_mail()["thread_id"]
        code, out, err = self.main(["mailbox", "list", "--box", "all"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([x["id"] for x in json.loads(out)["threads"]], [t])
        code, out, err = self.main(["mailbox", "summary"])
        self.assertEqual(json.loads(out)["unread"], 1)
        code, out, err = self.main(["mailbox", "list", "--limit", "0"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--limit is 1 to 500", err)


class StartUp(unittest.TestCase):
    def test_agent_mail_loads_google_s_client_only_to_talk_to_gmail(self):
        """The Mail tab runs `agent-mail mailbox show` for each thread it shows, so loading the command must not load
        Google's client, which is slow to load and which only gmail.service() needs. A fresh interpreter, as the tab's
        request has."""
        probe = ("import json, sys; import isolate_state; from scripts import load_script; load_script('agent-mail'); "
                 "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in ('google', 'googleapiclient'))))")
        done = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT / "tests"), capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [])


class Answer(Mailbox):
    def test_a_reply_is_recorded_once_for_the_daemon_to_route(self):
        first = self.agent_mail()
        code, out, err = self.main(["mailbox", "answer", first["thread_id"], "--key", KEY], stdin="  Looks good.\n")
        self.assertEqual((code, err), (0, ""))
        got = json.loads(out)
        row = self.st.inbound(int(got["key"][1:]))
        self.assertEqual(got, {"ok": True, "key": "i%d" % row["id"], "status": "sending"})
        self.assertEqual(
            {k: row[k] for k in ("gmail_id", "thread_id", "message_id", "from_addr", "from_name", "subject", "body",
                                 "matched_tag", "match_rule", "action")},
            {"gmail_id": "mailbox:" + KEY, "thread_id": first["thread_id"],
             "message_id": "<mailbox.%s@agent-mailroom.local>" % KEY, "from_addr": OWNER, "from_name": "Alex",
             "subject": "Re: " + first["subject"], "body": "Looks good.", "matched_tag": self.sess["tag"],
             "match_rule": "in-reply-to", "action": "local"})
        again = self.main(["mailbox", "answer", first["thread_id"], "--key", KEY], stdin="Looks good.\n")
        self.assertEqual(json.loads(again[1]), got)
        self.assertEqual(self.st.con.execute("select count(*) from inbound").fetchone()[0], 1)

    def test_answers_that_are_refused(self):
        first = self.agent_mail()
        his_only = self.his_mail("t-his")["thread_id"]
        self.answer(first["thread_id"], key="u" * 16)
        cases = [(first["thread_id"], "x", "short", "--key is 16 to 64"), ("nope", "x", KEY, "not in the mailbox"),
                 (his_only, "x", KEY, "holds no agent mail"), (first["thread_id"], " \n", KEY, "the reply is empty"),
                 (first["thread_id"], "x" * 20001, KEY, "the most is 20000"),
                 (his_only, "x", "u" * 16, "answered thread %s, not" % first["thread_id"])]
        for thread, text, key, said in cases:
            with self.subTest(said=said), self.assertRaisesRegex(outbound.MailError, said):
                self.answer(thread, text, key)
        for var in cli.AGENT_ENV:
            code, out, err = self.main(["mailbox", "answer", first["thread_id"], "--key", KEY], "hi", env={var: "1"})
            self.assertEqual((code, out), (2, ""), var)
            self.assertIn("refusing mailbox answer inside an agent session", err)
        self.assertIsNone(self.st.inbound_by_gmail_id("mailbox:" + KEY))


class AnswerFiles(Mailbox):
    """Files Alex attaches to a reply in the tab: `mailbox answer --json`, kept whole in the message store, shown
    with the thread, and served by part."""

    def answered(self, thread, payload, key=KEY):
        """(exit code, its JSON answer or None, stderr) of `mailbox answer --json` with payload piped in."""
        code, out, err = self.main(["mailbox", "answer", thread, "--key", key, "--json"], stdin=json.dumps(payload))
        return code, json.loads(out) if out else None, err

    def test_a_reply_with_files_is_kept_whole_and_shown_and_served_with_them(self):
        first = self.agent_mail()
        t = first["thread_id"]
        files = [{"name": "../figs/plot.png", "data": b64(PNG)}, {"name": "notes.txt", "data": b64(b"a,b\n")},
                 {"name": "PLOT.png", "data": b64(PNG[:100])}]
        code, got, err = self.answered(t, {"text": "  See the plot.\n", "files": files})
        self.assertEqual((code, err), (0, ""))
        row = self.st.inbound(int(got["key"][1:]))
        self.assertEqual((row["body"], row["action"], row["matched_tag"]), ("See the plot.", "local", self.sess["tag"]))
        kept = mailview.parse(messages.load(row["eml"]))
        self.assertEqual(kept.text.strip(), "See the plot.")
        self.assertEqual(kept.headers["Message-ID"], routing.local_message_id(KEY))
        self.assertEqual(kept.headers["In-Reply-To"], first["message_id"])
        self.assertEqual([(p.name, p.type, p.data) for p in kept.parts],
                         [("plot.png", "image/png", PNG), ("notes.txt", "text/plain", b"a,b\n"),
                          ("PLOT (2).png", "image/png", PNG[:100])])
        self.assertEqual(self.answered(t, {"text": "See the plot.", "files": files})[1], got)   # a retry: no second
        self.assertEqual(self.st.con.execute("select count(*) from inbound").fetchone()[0], 1)

        shown = mailbox.show(self.st, CFG, t, 2e9)
        mine = shown["messages"][1]
        self.assertEqual(mine["attachments"], [{"index": 1, "name": "plot.png", "type": "image/png", "size": len(PNG)},
                                               {"index": 2, "name": "notes.txt", "type": "text/plain", "size": 4},
                                               {"index": 3, "name": "PLOT (2).png", "type": "image/png", "size": 100}])
        self.assertIn('<a class="att" href="mailbox-part:%s/1">plot.png</a>' % got["key"], shown["document"])
        self.assertIn("See the plot.", shown["document"])
        self.assertTrue(shown["thread"]["attachments"])          # the agent's own mail has none
        code, out, err = self.main(["mailbox", "part", got["key"], "3"])
        self.assertEqual(json.loads(out), {"name": "PLOT (2).png", "type": "image/png", "data": b64(PNG[:100])})

    def test_files_alone_need_no_text_and_a_reply_without_files_keeps_no_message(self):
        t = self.agent_mail()["thread_id"]
        code, got, _ = self.answered(t, {"text": "", "files": [{"name": "plot.png", "data": b64(PNG)}]})
        self.assertEqual(code, 0)
        row = self.st.inbound(int(got["key"][1:]))
        self.assertEqual((row["body"], [p.name for p in replyfiles.of_row(row)]), ("", ["plot.png"]))
        code, got, _ = self.answered(t, {"text": "Just words."}, key="w" * 16)
        self.assertIsNone(self.st.inbound(int(got["key"][1:]))["eml"])
        code, out, _ = self.main(["mailbox", "answer", t, "--key", "p" * 16], stdin='{"text": "plain"}')
        self.assertEqual(self.st.inbound(int(json.loads(out)["key"][1:]))["body"], '{"text": "plain"}')   # no --json

    def test_reply_files_that_are_refused(self):
        t = self.agent_mail()["thread_id"]
        homes.patch(self, mailbox, "MAX_ANSWER_FILE_BYTES", 10)
        one = {"name": "a.png", "data": b64(b"12345")}
        cases = [([], "is not {\"text\""), ({"files": [one]}, "is not {\"text\""),
                 ({"text": "x", "files": one}, "is not {\"text\""), ({"text": "x", "files": [{"name": "a"}]}, "is not"),
                 ({"text": "x", "files": [{"name": "a.png", "data": "not base64!"}]}, "file 'a.png': its data is not base64"),
                 ({"text": "x", "files": [one] * 21}, "the reply has 21 files; the most is 20"),
                 ({"text": "x", "files": [one, one, one]}, "the reply's files are 15 B; the most is 10 B"),
                 ({"text": " ", "files": []}, "the reply is empty")]
        for payload, said in cases:
            with self.subTest(said=said):
                code, got, err = self.answered(t, payload)
                self.assertEqual((code, got), (1, None))
                self.assertIn(said, err)
        code, _, err = self.main(["mailbox", "answer", t, "--key", KEY, "--json"], stdin="{not json")
        self.assertEqual(code, 1)
        self.assertIn("the reply is not JSON", err)
        self.assertEqual(self.st.con.execute("select count(*) from inbound").fetchone()[0], 0)
        self.assertEqual({p.stem for p in config.MESSAGES_DIR.glob("*/*.eml")},
                         {r["eml"] for r in self.st.con.execute("select eml from sent")})   # only the agent's mail kept


class Widgets(Mailbox):
    """```choices and ```plot blocks on the way out (agent-mail send and a proxied request) and in show."""

    CHOICES = {"question": "Which do you want?", "options": [{"label": "Wait"}, {"label": "Ship it", "recommended": True}]}
    PLOT = {"title": "Accuracy", "kind": "line", "x": {}, "y": {}, "series": [{"name": "a", "x": [0, 1], "y": [0, 1]}],
            "image": "acc"}

    def body(self, text):
        path = homes.temp_dir(self) / "body.txt"
        path.write_text(text)
        return str(path)

    def send_mail(self, text, html=None, images=()):
        """agent-mail send --unrouted with this body (and --html, --image NAME=PATH): its sent row."""
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, SSH_CONNECTION=""):
            self.am.do_send(self.ctx, argparse.Namespace(to="owner", cc=None, subject="pick", unrouted=True,
                                                         body_file=self.body(text), html=html, image=list(images),
                                                         attach=None))
        return self.st.con.execute("select * from sent order by id desc limit 1").fetchone()

    def blocks(self):
        return "Pick one.\n\n```choices\n%s\n```\n\n```plot\n%s\n```\n" % (json.dumps(self.CHOICES),
                                                                             json.dumps(self.PLOT))

    def test_gmail_gets_the_fallbacks_the_row_keeps_the_widgets_and_show_marks_their_places(self):
        png = homes.temp_dir(self) / "acc.png"
        png.write_bytes(PNG)
        row = self.send_mail(self.blocks(), images=["acc=%s" % png])
        msg, _ = self.sent[-1]
        text = msg.get_body(("plain",)).get_content()
        page = msg.get_body(("html",)).get_content()
        self.assertIn("1. Wait\n2. Ship it (recommended)\n", text)
        self.assertNotIn("```", text)
        self.assertIn('<div data-agentmail-widget="0">', page)
        self.assertEqual(page.count("cid:acc"), 1)                                # in the plot's place, once
        self.assertEqual(json.loads(row["widgets"]), [{"kind": "choices", "spec": self.CHOICES},
                                                      {"kind": "plot", "spec": self.PLOT}])
        self.assertEqual(messages.load(row["eml"]), self.raws[-1])                # kept as Gmail got it
        self.assertIn("1. Wait", row["body"])
        shown = mailbox.show(self.st, CFG, row["thread_id"], 2e9)
        (m,) = shown["messages"]
        key = "s%d" % row["id"]
        self.assertEqual(m["widgets"], [{"id": key + "-0", "kind": "choices", "spec": self.CHOICES},
                                        {"id": key + "-1", "kind": "plot", "spec": self.PLOT}])
        doc = shown["document"]
        self.assertIn('<div class="widget" data-widget="%s-0">' % key, doc)
        place = doc[doc.index('data-widget="%s-1"' % key):]
        self.assertTrue(place[:400].count("data:image/png;base64,"), place[:400])   # the image inside its place
        self.assertNotIn("data-agentmail-widget", doc)

    def test_a_broken_block_stops_the_mail_before_anything_is_sent_or_kept(self):
        bad = self.blocks().replace('"Ship it"', '"' + "x" * 121 + '"')
        with self.assertRaisesRegex(outbound.MailError, r"widget block 1 \(choices\): options\[1\]\.label: at most 120"):
            self.send_mail(bad)
        with self.assertRaisesRegex(outbound.MailError, "cannot also have --html"):
            html = homes.temp_dir(self) / "mine.html"
            html.write_text("<p>mine</p>")
            self.send_mail("```choices\n%s\n```" % json.dumps(self.CHOICES), html=str(html))
        self.fake.send_raw.assert_not_called()
        self.assertEqual((self.st.max_sent_id(), self.sent), (0, []))
        self.assertFalse(config.MESSAGES_DIR.exists() and any(config.MESSAGES_DIR.iterdir()))

    def test_a_proxied_mail_is_prepared_or_checked_on_the_desktop(self):
        typed = {"text": "```choices\n%s\n```" % json.dumps(self.CHOICES), "html": None, "images": [], "files": []}
        got = rpc._proxied_mail(typed)                                      # from an agent-mail that did not prepare
        self.assertEqual(got.widgets, ({"kind": "choices", "spec": self.CHOICES},))
        self.assertIn('data-agentmail-widget="0"', got.html)
        again = rpc._proxied_mail(got.as_json())                             # from one that did
        self.assertEqual(again, got)
        tampered = dict(got.as_json(), widgets=[{"kind": "choices", "spec": dict(self.CHOICES, options=[])}])
        with self.assertRaisesRegex(outbound.MailError, "options: must be a list"):
            rpc._proxied_mail(tampered)

    def test_marks_in_an_agent_s_own_html_place_no_widget(self):
        html = homes.temp_dir(self) / "own.html"
        html.write_text('<div data-agentmail-widget="0" data-widget="s1-0">mine</div>')
        row = self.send_mail("text", html=str(html))
        self.assertIsNone(row["widgets"])
        shown = mailbox.show(self.st, CFG, row["thread_id"], 2e9)
        self.assertEqual(shown["messages"][0]["widgets"], [])
        self.assertNotIn("data-widget", shown["document"])
        self.assertNotIn("data-agentmail-widget", shown["document"])


class Show(Mailbox):
    PNG = b"\x89PNG\r\n\x1a\n" + b"plot" * 8

    def test_a_thread_s_messages_and_its_page_open_at_the_unread_and_the_last(self):
        old = self.agent_mail(subject="Run A results", text="Before the store.", at=100)
        self.st.con.execute("update sent set eml=null, attachments=null where id=?", (old["id"],))
        self.mark([old["thread_id"]], "read", upto=old["id"], now=150)
        reply = self.answer(old["thread_id"], "Plot it, please.")
        self.st.con.execute("update inbound set received_at=200")
        mail = outbound.Mail("Here it is.", None, [("acc", self.PNG)], [("results.csv", b"a,b\n1,2\n")])
        outbound.transmit(self.ctx, self.sess, OWNER, "sam@example.com", "Re: " + old["subject"], mail,
                          self.st.inbound(int(reply["key"][1:])))
        self.st.con.execute("update sent set sent_at=300 where id=2")
        code, out, err = self.main(["mailbox", "show", old["thread_id"]])
        self.assertEqual((code, err), (0, ""))
        got = json.loads(out)
        self.assertEqual(got["thread"], self.listed()[old["thread_id"]])
        first, mine, last = got["messages"]
        self.assertEqual(first, {"key": "s1", "from": "mailtab (claude)", "mine": False, "to": OWNER, "cc": "",
                                 "ts": 100, "subject": old["subject"], "unread": False, "stored": False,
                                 "attachments": [], "status": None, "widgets": []})   # mail from before widgets
        self.assertEqual(mine, {"key": reply["key"], "from": "Alex", "mine": True, "ts": 200, "unread": False,
                                "stored": True, "attachments": [], "status": "sending"})
        self.assertEqual((last["unread"], last["stored"], last["cc"]), (True, True, "sam@example.com"))
        (attachment,) = last["attachments"]
        self.assertEqual((attachment["name"], attachment["type"], attachment["size"]), ("results.csv", "text/csv", 8))
        page = got["document"]
        self.assertIn('<h1 class="subject">%s</h1>\n<p class="about">3 mails · %s</p>'
                      % (got["thread"]["subject"], ", ".join(got["thread"]["agents"])), page)
        self.assertIn('<details id="s1" class="msg">', page)                        # read, and not the last
        self.assertIn('<details id="s2" class="msg" open>', page)
        self.assertIn(gmail_link("all/g1"), page)                                   # sent before the store
        self.assertIn("Before the store.", page)
        self.assertIn("mailbox-part:s2/%d" % attachment["index"], page)
        self.assertIn("data:image/png;base64,", page)                               # the inline plot, from its part
        self.assertEqual(self.st.mailbox_flags()[old["thread_id"]]["seen_id"], 1)   # showing marks nothing read
        self.st.update_inbound(int(reply["key"][1:]), action="held", detail="delivery failed (1): refused")
        self.assertEqual(mailbox.show(self.st, CFG, old["thread_id"], 2e9)["messages"][1]["status"],
                         "held: delivery failed (1): refused")
        code, out, err = self.main(["mailbox", "part", "s2", str(attachment["index"])])
        self.assertEqual(json.loads(out), {"name": "results.csv", "type": "text/csv",
                                           "data": base64.b64encode(b"a,b\n1,2\n").decode()})

    def test_what_show_and_part_refuse(self):
        sent = self.agent_mail()
        self.st.con.execute("update sent set eml=null where id=?", (sent["id"],))
        stored = self.agent_mail(subject="Stored")
        for argv, said in ((["show", "nope"], "not in the mailbox"),
                           (["part", "s%d" % sent["id"], "1"], "is no stored message"),
                           (["part", "i1", "1"], "is no stored message"),
                           (["part", "x1", "1"], "is no stored message"),
                           (["part", "s%d" % stored["id"], "9"], "has no part 9")):
            code, out, err = self.main(["mailbox"] + argv)
            self.assertEqual((code, out), (4, ""), argv)
            self.assertIn(said, err)


def gmail_link(view):
    """The link to view in the agents account's Gmail, as the page writes it."""
    return outbound.gmail.web_url(CFG["account"], view).replace("&", "&amp;")


class TabReplies(Mailbox):
    """The daemon routes a reply written in the tab with the code that routes Alex's email. The boundaries the tests
    stand in for: deliver.deliver_session is agent-send into a live session (or SSH to the Mac's); orchestrate.run_event
    is the orchestrator's claude CLI run; recovery.recover searches transcripts here and on the Mac over SSH, and
    must not run at all in these tests."""

    def poll(self, cfg=CFG):
        self.fake.history_since.return_value = ([], "after")
        self.st.set_meta("history_id", "before")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.room.poll(self.fake, self.st, cfg, False)
        return out.getvalue()

    def emailed(self, thread, subject, text, gid):
        """Alex's email in thread, as Gmail gives it, put through the daemon."""
        hdrs = [("From", "Alex <%s>" % OWNER), ("Subject", subject), ("Message-ID", "<%s@mail.gmail.com>" % gid),
                ("In-Reply-To", "<g1@mail.gmail.com>"),
                ("Authentication-Results", fakes.auth_header(OWNER.rsplit("@", 1)[1]))]   # DKIM of the owner's domain
        self.fake.get_full.return_value = fakes.gmail_message(hdrs, [fakes.text_part(text)], gid=gid,
                                                              thread_id=thread)
        with contextlib.redirect_stdout(io.StringIO()):
            self.room.process(self.fake, self.st, CFG, gid, False)
        return self.st.inbound_by_gmail_id(gid)

    def test_a_tab_reply_reaches_the_session_as_his_email_does_with_no_labels(self):
        first = self.agent_mail()
        with mock.patch.object(deliver, "deliver_session", return_value=(0, "sent")) as send:
            by_mail = self.emailed(first["thread_id"], "Re: " + first["subject"], "Ship it.", "in1")
            self.fake.modify.reset_mock()
            self.fake.ensure_label.reset_mock()
            by_tab = self.st.inbound(int(self.answer(first["thread_id"], "Ship it.")["key"][1:]))
            log = self.poll()
        (mail_text,), (tab_text,) = [c.args[1:2] for c in send.call_args_list]
        self.assertEqual(tab_text, mail_text.replace("agent-mail reply %d" % by_mail["id"],
                                                     "agent-mail reply %d" % by_tab["id"]))
        self.assertTrue(tab_text.startswith('[email from Alex <%s> re "Re: %s"' % (OWNER, first["subject"])))
        self.assertEqual(self.st.inbound(by_tab["id"])["action"], "delivered")
        self.fake.modify.assert_not_called()
        self.fake.ensure_label.assert_not_called()
        self.assertIn("tab    #%d from %s" % (by_tab["id"], OWNER), log)
        self.assertIn("deliver #%d (owner)" % by_tab["id"], log)

    def test_a_tab_reply_to_a_gone_session_goes_to_the_orchestrator_with_the_thread(self):
        first = self.agent_mail(text="The results you asked for.")
        self.fake.thread_messages.return_value = [({"from": CFG["account"]}, "The results you asked for.", 1, "g1")]
        row = self.st.inbound(int(self.answer(first["thread_id"], "Rerun it.")["key"][1:]))
        with mock.patch.object(deliver, "deliver_session", return_value=(4, "no such session")), \
                mock.patch.object(self.room.orchestrate, "run_event", return_value=INFO) as run, \
                mock.patch.object(recovery, "recover") as recover:
            self.poll()
        recover.assert_not_called()
        text = run.call_args.args[0]
        self.assertTrue(text.startswith('[email from Alex <%s> (Alex) re "Re: %s" · the agent that owned this thread '
                                        '(mailtab, claude session 12345678 on desktop) has closed or exited'
                                        % (OWNER, first["subject"])), text)
        self.assertIn("agent (ours)]\nThe results you asked for.", text)
        self.assertTrue(text.endswith("--- latest message ---\nRerun it."), text)
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")
        self.fake.modify.assert_not_called()

    def test_a_tab_reply_to_unrouted_mail_goes_to_the_orchestrator_as_his_email_would(self):
        first = self.agent_mail(subject="[cloud-watchdog] Daily", sess=None)
        self.fake.thread_messages.return_value = [({"from": CFG["account"]}, "report", 1, "g1")]
        row = self.st.inbound(int(self.answer(first["thread_id"], "Close it.")["key"][1:]))
        self.assertEqual((row["matched_tag"], row["match_rule"]), (None, None))
        with mock.patch.object(self.room.orchestrate, "run_event", return_value=INFO) as run, \
                mock.patch.object(recovery, "recover") as recover:
            self.poll()
        recover.assert_not_called()
        run.assert_called_once()
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")
        self.assertEqual(len(self.sent), 1)   # no notice to Alex

    def test_a_tab_reply_that_fails_is_held_once_without_labels_and_waits_for_a_person(self):
        first = self.agent_mail()
        row = self.st.inbound(int(self.answer(first["thread_id"])["key"][1:]))
        with mock.patch.object(deliver, "deliver_session", side_effect=RuntimeError("agent-send broke")):
            log = self.poll()
        held = self.st.inbound(row["id"])
        self.assertEqual((held["action"], held["detail"]), ("held", "error: RuntimeError: agent-send broke"))
        self.assertIn("error  mailbox:%s (#%d held)" % (KEY, row["id"]), log)
        self.fake.modify.assert_not_called()
        alert = self.sent[-1][0].get_body(("plain",)).get_content()
        self.assertIn("Nothing has been lost: the email is saved in the mailroom.", alert)
        with mock.patch.object(deliver, "deliver_session") as send:
            self.poll()
        send.assert_not_called()
        with mock.patch.object(deliver, "deliver_session", return_value=(0, "sent")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.am.do_release(self.ctx, argparse.Namespace(inbound_id=row["id"], target="origin", cli=None))
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")
        self.fake.modify.assert_not_called()

    def test_a_stopping_daemon_leaves_tab_replies_waiting(self):
        first = self.agent_mail()
        row = self.st.inbound(int(self.answer(first["thread_id"])["key"][1:]))
        self.room.STOP = True
        self.addCleanup(setattr, self.room, "STOP", False)
        with mock.patch.object(deliver, "deliver_session") as send:
            self.poll()
        send.assert_not_called()
        self.assertEqual(self.st.inbound(row["id"])["action"], "local")


class TabReplyFiles(TabReplies):
    """The files of a tab reply go to the machine of whoever is handed the reply, before its text, and the text says
    where they are. SSH to the Mac is the boundary: subprocess.run stands in for it, and the Mac's own agent-mail rpc,
    run here with the Mac's config and files folder, answers it; agent-send on each machine is a stand-in that records
    what reached a session."""

    def setUp(self):
        super().setUp()
        self.here = homes.temp_dir(self) / "files"
        homes.patch(self, replyfiles, "FILES_DIR", self.here)
        self.mac_files = homes.temp_dir(self) / "mac-files"
        self.mac_config = homes.temp_dir(self) / "config.json"
        self.mac_config.write_text(json.dumps(MAC_FILE))
        self.asked = []          # the ops the Mac was asked for, in order
        self.on_mac = []         # the texts agent-send handed a session on the Mac
        self.mac = self.st.session_for("claude", MAC_SID, "macagent", origin_host="mac")

    def ssh(self, cmd, input, **_kw):
        """The Mac answering one request with its own agent-mail rpc."""
        self.asked.append(json.loads(input)["op"])
        with mock.patch.object(config, "CONFIG_PATH", self.mac_config), \
                mock.patch.object(replyfiles, "FILES_DIR", self.mac_files), \
                mock.patch.object(deliver, "deliver", side_effect=lambda cli, sid, text, **kw: (
                    self.on_mac.append(text), (0, "queued"))[1]), \
                mock.patch("sys.stdin", io.StringIO(input)), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.am.main(["rpc"]), 0)
        return mock.Mock(returncode=0, stdout=out.getvalue(), stderr="")

    def reply_with_files(self, sess="mailtab"):
        first = self.agent_mail(sess=sess)
        got = self.answer(first["thread_id"], "See the plot.", files=[("plot.png", PNG), ("notes.txt", b"a,b\n")])
        return self.st.inbound(int(got["key"][1:]))

    def said(self, folder, row):
        """The lines a reply's files add to its text when they are in folder."""
        d = folder / ("i%d" % row["id"])
        return ("See the plot.\n\nAlex attached 2 files, saved on this machine; open them with your file or image "
                "tools:\n- %s (image/png, 1 KB)\n- %s (text/plain, 4 B)" % (d / "plot.png", d / "notes.txt"))

    def test_on_this_machine_the_files_are_beside_the_session_and_named_in_its_text(self):
        row = self.reply_with_files()
        with mock.patch.object(deliver, "deliver_session", return_value=(0, "sent")) as send:
            log = self.poll()
        text = send.call_args.args[1]
        self.assertTrue(text.endswith("\n" + self.said(self.here, row)), text)
        d = self.here / ("i%d" % row["id"])
        self.assertEqual(((d / "plot.png").read_bytes(), (d / "notes.txt").read_bytes()), (PNG, b"a,b\n"))
        self.assertEqual(stat.S_IMODE((d / "plot.png").stat().st_mode), 0o600)
        self.assertIn("files  #%d: 2 put on desktop for claude@desktop mailtab" % row["id"], log)
        self.assertEqual(self.st.inbound(row["id"])["action"], "delivered")

    def test_on_the_mac_the_files_are_put_there_before_the_text_that_names_them(self):
        row = self.reply_with_files(sess=self.mac)
        with mock.patch.object(subprocess, "run", side_effect=self.ssh):
            log = self.poll()
        self.assertEqual(self.asked, ["files", "deliver"])
        (text,) = self.on_mac
        self.assertTrue(text.endswith("\n" + self.said(self.mac_files, row)), text)
        self.assertEqual((self.mac_files / ("i%d" % row["id"]) / "plot.png").read_bytes(), PNG)
        self.assertFalse((self.here / ("i%d" % row["id"])).exists())
        self.assertIn("files  #%d: 2 put on mac" % row["id"], log)
        self.assertEqual(self.st.inbound(row["id"])["action"], "queued")

    def test_files_the_mac_does_not_take_hold_the_reply_until_it_is_released(self):
        row = self.reply_with_files(sess=self.mac)
        older_mac = json.dumps({"protocol": 1, "ok": False, "error": "unsupported RPC operation"})
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0, stdout=older_mac,
                                                                         stderr="")) as run, \
                mock.patch.object(recovery, "recover"):
            self.poll()
        self.assertEqual(run.call_count, 1)                          # no text without its files
        held = self.st.inbound(row["id"])
        self.assertEqual((held["action"], held["detail"]),
                         ("held", "delivery failed (1): the files were not put on mac: mac: unsupported RPC "
                                  "operation"))
        release = argparse.Namespace(inbound_id=row["id"], target="origin", cli=None)
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0, stdout=older_mac, stderr="")), \
                contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            self.am.do_release(self.ctx, release)
        self.assertIn("delivery failed: the files were not put on mac: mac: unsupported RPC operation",
                      err.getvalue())
        self.assertEqual(self.st.inbound(row["id"])["action"], "held")
        with mock.patch.object(subprocess, "run", side_effect=self.ssh), contextlib.redirect_stdout(io.StringIO()):
            self.am.do_release(self.ctx, release)
        self.assertEqual(self.asked, ["files", "deliver"])
        self.assertTrue(self.on_mac[0].endswith("\n" + self.said(self.mac_files, row)))
        self.assertEqual(self.st.inbound(row["id"])["action"], "queued")

    def test_the_orchestrator_gets_the_files_here_and_so_does_a_rerun(self):
        row = self.reply_with_files(sess=None)
        self.fake.thread_messages.return_value = [({"from": CFG["account"]}, "report", 1, "g1")]
        with mock.patch.object(self.room.orchestrate, "run_event", return_value=INFO) as run, \
                mock.patch.object(recovery, "recover"):
            log = self.poll()
            self.assertTrue(run.call_args.args[0].endswith("\n" + self.said(self.here, row)))
            self.assertIn("files  #%d: 2 put here for the orchestrator" % row["id"], log)
            with contextlib.redirect_stdout(io.StringIO()):
                self.am.do_rerun(self.ctx, argparse.Namespace(inbound_id=row["id"]))
            self.assertTrue(run.call_args.args[0].endswith("\n" + self.said(self.here, row)))

    def test_only_the_central_mailroom_may_put_files_and_only_safe_ones(self):
        def rpc(cfg_file, **req):
            path = homes.temp_dir(self) / "config.json"
            path.write_text(json.dumps(cfg_file))
            wire = json.dumps(dict({"protocol": 1, "op": "files", "key": "i7",
                                    "files": [{"name": "a.png", "data": b64(PNG)}]}, **req))
            with mock.patch.object(config, "CONFIG_PATH", path), mock.patch("sys.stdin", io.StringIO(wire)), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.am.main(["rpc"])
            return json.loads(out.getvalue()).get("error")
        to_mac = dict(source_host="desktop", destination_host="mac")
        self.assertEqual(rpc(CFG_FILE, source_host="mac", destination_host="desktop"),
                         "only the configured central mailroom may write a reply's files")
        self.assertIn("is not a reply's key", rpc(MAC_FILE, key="../i7", **to_mac))
        self.assertIn("must be safe and distinct", rpc(MAC_FILE, files=[{"name": "../a.png", "data": b64(PNG)}],
                                                       **to_mac))
        self.assertIn("must be safe and distinct", rpc(MAC_FILE, files=[{"name": "a.png", "data": b64(PNG)}] * 2,
                                                       **to_mac))
        self.assertEqual(list(self.here.glob("**/*")), [])


class Sync(Mailbox):
    """The tab and Alex's Gmail brought into step: done in either is done in both."""

    def sync(self, inbox, unread, now=2e9):
        return mailbox.sync(self.st, CFG, {"inbox": inbox, "unread": unread}, now)

    def test_what_is_done_in_either_is_done_in_both(self):
        a = self.agent_mail(subject="A")   # in both inboxes, unread in both: left as it is
        b = self.agent_mail(subject="B")   # read in Gmail: read in the tab
        c = self.agent_mail(subject="C")   # read in the tab, unread in Gmail: Gmail marks it read
        d = self.agent_mail(subject="D")   # archived and read in the tab, in Gmail's inbox and unread: Gmail both
        e = self.agent_mail(subject="E")   # archived in Gmail but unread: archived in the tab, still unread
        f = self.agent_mail(subject="F")   # snoozed in the tab: left as it is, here and in Gmail
        mine = self.his_mail("t-his")["thread_id"]
        self.mark([c["thread_id"], d["thread_id"]], "read", upto=d["id"], now=1000)
        self.mark([d["thread_id"]], "archive", upto=d["id"], now=1000)
        self.mark([f["thread_id"]], "snooze", upto=f["id"], until=3e9, now=1000)
        before = self.st.mailbox_flags()
        c_given = " <%s> " % c["message_id"].upper()           # compared without <> and case, returned as given
        got = self.sync([a["message_id"], b["message_id"], c["message_id"], d["message_id"], f["message_id"]],
                        [a["message_id"], c_given, d["message_id"], e["message_id"], f["message_id"]])
        self.assertEqual(got, {"ok": True, "archived": 2, "read": 1,                          # e, his; b
                               "gmail": {"archive": [d["message_id"]], "read": sorted([c_given, d["message_id"]])}})
        listed = self.listed()
        self.assertEqual({k: (listed[t["thread_id"]]["box"], listed[t["thread_id"]]["unread"])
                          for k, t in (("a", a), ("b", b), ("c", c), ("d", d), ("e", e), ("f", f))},
                         {"a": ("inbox", True), "b": ("inbox", False), "c": ("inbox", False),
                          "d": ("archived", False), "e": ("archived", True), "f": ("snoozed", True)})
        self.assertEqual((listed[mine]["box"], listed[mine]["unread"]), ("archived", False))
        self.assertNotIn(a["thread_id"], self.st.mailbox_flags())         # already in step: nothing written
        self.assertEqual(self.st.mailbox_flags()[f["thread_id"]], before[f["thread_id"]])
        # once Gmail has made those changes, the two are in step
        self.assertEqual(self.sync([a["message_id"], b["message_id"], c["message_id"], f["message_id"]],
                                   [a["message_id"], e["message_id"], f["message_id"]]),
                         {"ok": True, "archived": 0, "read": 0, "gmail": {"archive": [], "read": []}})

    def test_nothing_comes_back_to_the_inbox_or_goes_unread(self):
        t = self.agent_mail()
        self.mark([t["thread_id"]], "read", upto=t["id"], now=1000)
        self.mark([t["thread_id"]], "archive", upto=t["id"], now=1000)
        self.assertEqual(self.sync([t["message_id"]], [t["message_id"]]),
                         {"ok": True, "archived": 0, "read": 0,
                          "gmail": {"archive": [t["message_id"]], "read": [t["message_id"]]}})
        self.assertEqual((self.listed()[t["thread_id"]]["box"], self.listed()[t["thread_id"]]["unread"]),
                         ("archived", False))

    def test_sync_from_the_command_line_and_what_it_refuses(self):
        t = self.agent_mail()
        code, out, err = self.main(["mailbox", "sync"], stdin=json.dumps({"inbox": [], "unread": []}))
        self.assertEqual((code, json.loads(out)),
                         (0, {"ok": True, "archived": 1, "read": 1, "gmail": {"archive": [], "read": []}}))
        self.assertEqual(err, "mailbox sync: 1 threads archived and 1 read in the tab; Gmail to archive 0 mails and "
                              "mark 0 read\n")
        for stdin, said in (("[]", "sync wants"), ('{"inbox": ["x"]}', "sync wants"),
                            ('{"inbox": [1], "unread": []}', "sync wants"), ("{", "is not JSON")):
            code, out, err = self.main(["mailbox", "sync"], stdin=stdin)
            self.assertEqual((code, out), (1, ""), stdin)
            self.assertIn(said, err)
        code, out, err = self.main(["mailbox", "sync"], stdin='{"inbox": [], "unread": []}', env={"CLAUDECODE": "1"})
        self.assertEqual((code, out), (2, ""))
        self.assertIn("refusing mailbox sync inside an agent session", err)
        self.assertEqual(self.listed()[t["thread_id"]]["box"], "archived")


class GroupTabReply(GroupBase):
    """A reply written in the tab to a group's mail becomes Alex's post there, as his emailed reply does."""

    def setUp(self):
        super().setUp()
        self.st = Store(homes.temp_dir(self) / "mailroom.sqlite")
        self.addCleanup(self.st.con.close)
        self.room = load_script("agent-mailroom")
        self.sent = fakes.sending_gmail(self, outbound, self.room, mailroom, alerts)
        homes.patch(self, outbound, "Store", lambda: self.st)
        homes.patch(self, config, "MESSAGES_DIR", homes.temp_dir(self) / "messages")

    def test_a_tab_reply_to_a_group_s_mail_is_his_post(self):
        gid = self.group(A, "Release", "bob")
        groupmail.announce(self.room.gmail, self.st, self.cfg)
        self.delivered.clear()
        with no_agent_env():
            reply = mailbox.answer(self.st, self.cfg, "t1", KEY, "Ship it.", [])
        self.room.gmail.history_since.return_value = ([], "after")
        self.st.set_meta("history_id", "before")
        with contextlib.redirect_stdout(io.StringIO()):
            self.room.poll(self.room.gmail, self.st, self.cfg, False)
        post, = json.loads(self.run_(ALEX, "posts", group=gid, json=True))
        self.assertEqual((post["id"], post["from_agent"], post["text"]),
                         (groupmail.post_id(routing.local_gmail_id(KEY)), None, "Ship it."))
        self.assertEqual(sorted(self.names_delivered()), ["alice", "bob"])
        self.assertEqual(self.st.inbound(int(reply["key"][1:]))["action"], "delivered")
        self.room.gmail.modify.assert_not_called()

    def test_a_group_takes_no_files(self):
        self.group(A, "Release", "bob")
        groupmail.announce(self.room.gmail, self.st, self.cfg)
        with no_agent_env(), self.assertRaisesRegex(outbound.MailError, "goes to group 'Release', which takes text only"):
            mailbox.answer(self.st, self.cfg, "t1", KEY, "Ship it.", [("plot.png", PNG)])
        self.assertIsNone(self.st.inbound_by_gmail_id(routing.local_gmail_id(KEY)))


class ReplyFileNames(unittest.TestCase):
    """A reply's file names, written on the Mac's file system as well as the desktop's, and what tells the agent."""

    def test_names_are_one_path_component_without_controls_or_leading_dots(self):
        long = "x" * 300
        self.assertEqual([replyfiles.safe_name(n) for n in ("../../etc/passwd", "C:\\a\\b.txt", ".bashrc", " . ",
                                                            "a\nb\x00c.png", long + ".png", long + "." + "e" * 20)],
                         ["passwd", "b.txt", "bashrc", replyfiles.DEFAULT_NAME, "abc.png",
                          "x" * (replyfiles.MAX_NAME_CHARS - 4) + ".png", (long + "." + "e" * 20)[:120]])

    def test_names_alike_but_for_case_are_numbered(self):
        self.assertEqual(replyfiles.unique(["a.png", "A.png", "a.png", "b", "a (2).png"]),
                         ["a.png", "A (2).png", "a (3).png", "b", "a (2) (2).png"])
        long = "y" * 200 + ".png"
        first, second = replyfiles.unique([long, long])
        self.assertEqual((len(second) <= replyfiles.MAX_NAME_CHARS, second.endswith(" (2).png"), first == second),
                         (True, True, False))

    def test_a_request_has_room_for_a_reply_s_files_and_an_agent_s_attachments(self):
        biggest = max(mailbox.MAX_ANSWER_FILE_BYTES, outbound.MAX_IMAGES_BYTES)
        self.assertGreaterEqual(remote.MAX_REQUEST, len(base64.b64encode(b"\0" * biggest)) + (1 << 20))


class Backup(Mailbox):
    def targets(self, *names):
        base = homes.temp_dir(self)
        return [str(base / n) for n in names]

    def test_backup_copies_what_a_target_lacks_and_never_deletes(self):
        self.agent_mail()
        target, = self.targets("bk")
        ok, line = backup.run(self.st, [target], 2e9)
        self.assertTrue(ok, line)
        root = Path(target)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        sha = self.st.con.execute("select eml from sent").fetchone()[0]
        self.assertEqual(messages.read_checked(messages.path_in(root / "messages", sha), sha), self.raws[0])
        day = "mailroom-%s.sqlite.gz" % time.strftime("%Y-%m-%d", time.localtime(2e9))
        self.assertEqual(line, "backup ok: %s: 1 new message, latest.sqlite, %s" % (target, day))
        copy = sqlite3.connect(str(root / "latest.sqlite"))
        self.assertEqual(copy.execute("select eml from sent").fetchone()[0], sha)
        copy.close()
        self.assertTrue(gzip.decompress((root / "daily" / day).read_bytes()).startswith(b"SQLite format 3"))
        self.assertEqual(float(self.st.get_meta(backup.META % target)), 2e9)
        (root / "messages" / "keep-me").write_text("x")
        (root / "daily" / "mailroom-2000-01-01.sqlite.gz").write_text("x")
        self.agent_mail(subject="second")
        ok, line = backup.run(self.st, [target], 2e9 + 60)
        self.assertEqual(line, "backup ok: %s: 1 new message, latest.sqlite" % target)
        self.assertTrue((root / "messages" / "keep-me").exists())
        self.assertEqual(sorted(p.name for p in (root / "daily").iterdir()),
                         ["mailroom-2000-01-01.sqlite.gz", day])

    def test_a_target_that_fails_is_reported_and_the_others_are_copied(self):
        self.agent_mail()
        good, = self.targets("bk")
        unmounted = str(homes.temp_dir(self) / "not-mounted" / "bk")
        homes.config_file(self, dict(CFG_FILE, backup={"targets": [unmounted, good]}))
        code, out, err = self.main(["backup"])
        self.assertEqual(code, 1)
        self.assertIn("backup FAILED: %s: FAILED: OSError: " % unmounted, out)
        self.assertIn("is its disk mounted?", out)
        self.assertIn("%s: 1 new message, latest.sqlite" % good, out)
        self.assertFalse(Path(unmounted).parent.exists())   # never a folder above a target

    def test_backup_refuses_without_targets(self):
        code, out, err = self.main(["backup"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("config backup.targets is empty", err)


class Backfill(Mailbox):
    def test_backfill_keeps_each_old_mail_once_and_goes_on_past_a_failure(self):
        old = [self.agent_mail(subject=s, cc=OWNER if s == "B" else None) for s in ("A", "B", "C")]
        self.st.con.execute("update sent set eml=null, body=null, cc_addr=null, attachments=null")
        raws = {"g%d" % (i + 1): raw for i, raw in enumerate(self.raws)}
        del raws["g2"]

        def get_raw(svc, gid):
            if gid not in raws:
                raise OSError("404 not found")
            return raws[gid]
        self.fake.get_raw.side_effect = get_raw
        out = []
        self.assertEqual(mailbox.backfill(self.st, "svc", None, out.append), 1)
        rows = {r["gmail_id"]: r for r in self.st.con.execute("select * from sent")}
        for gid in ("g1", "g3"):
            self.assertEqual(messages.load(rows[gid]["eml"]), raws[gid])
            self.assertEqual((rows[gid]["body"], rows[gid]["attachments"]), ("The run finished.\n", 0))   # its text part
        self.assertIsNone(rows["g2"]["eml"])
        self.assertIn("s%d FAILED: OSError: 404 not found" % old[1]["id"], out)
        raws["g2"] = self.raws[1]
        out.clear()
        self.assertEqual(mailbox.backfill(self.st, "svc", None, out.append), 0)
        self.assertEqual(self.st.con.execute("select cc_addr from sent where gmail_id='g2'").fetchone()[0], OWNER)
        self.assertEqual(self.fake.get_raw.call_count, 4)   # the kept ones are not fetched again
        self.assertEqual(out[-1], "backfill: 1 kept, 0 failed")


if __name__ == "__main__":
    unittest.main()
