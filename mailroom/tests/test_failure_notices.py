"""Shutdown, transport receipts, and notice routing. No Gmail or real agent turns."""
import argparse
import contextlib
import configparser
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import isolate_state  # noqa: F401  (temporary state dirs; before agentmail)
from scripts import load_script  # noqa: E402
from test_remote import Base, CFG, ROOT, SID
import homes  # noqa: E402  (after test_remote, which puts the repo on sys.path)
import fakes  # noqa: E402
from agentmail import config, deliver, gmail, outbound, recovery, registry, remote, routing, send

INFO = dict(ok=True, cost=0, turns=1, duration_s=1, error='', result='done')
HOST = CFG['host_id']   # this machine, where the sessions these tests deliver to run


class NoticeRoutes(Base):
    def test_accepted_but_killed_sender_records_delivery_without_an_alert(self):
        sess = self.st.session_for('claude', SID, 'fixture', origin_host=HOST)
        self.message(sess)
        def accept_then_die(cmd, **kwargs):   # agent-send, killed after the session took the message
            deliver.write_receipt(cmd[cmd.index('--receipt')+1], SID, kwargs['input'])
            return Mock(returncode=-signal.SIGTERM, stdout='', stderr='')
        with patch.object(deliver.subprocess, 'run', side_effect=accept_then_die):
            self.room.process(self.fake, self.st, CFG, 'in', False)
        row = self.st.list_inbound()[0]
        self.assertEqual(row['action'], 'delivered')
        self.assertIn('receipt verified', row['detail'])
        self.assertIsNone(row['recovery_json'])
        self.assertEqual(self.sent, [])

    def test_mail_codex_holds_in_its_queue_is_recorded_queued_with_why_never_failed(self):
        """agent-send exits HELD: the message is in the agent's queue (accepted, its receipt written), so the mail is
        neither held nor alerted on, which would have it delivered twice, nor recorded as delivered: it is queued,
        and its detail says why it waits."""
        sess = self.st.session_for('codex', SID, 'coder', origin_host=HOST)
        self.message(sess)
        held = ("held in Codex's queue for codex coder [12345678] live (Queued message i for thread t.)\n"
                "its last turn was interrupted, so Codex holds it until another turn starts")
        def accept_and_hold(cmd, **kwargs):
            deliver.write_receipt(cmd[cmd.index('--receipt')+1], SID, kwargs['input'])
            return Mock(returncode=deliver.HELD, stdout=held, stderr='')
        with patch.object(deliver.subprocess, 'run', side_effect=accept_and_hold):
            self.room.process(self.fake, self.st, CFG, 'in', False)
        row = self.st.list_inbound()[0]
        self.assertEqual(row['action'], 'queued')
        self.assertIn('its last turn was interrupted, so Codex holds it', row['detail'])
        self.assertIsNone(row['recovery_json'])
        self.assertEqual(self.sent, [])

    def test_reply_and_forward_subject_variants(self):
        sess = self.st.session_for('claude', SID, origin_host=HOST)
        for prefix in ('', 'Re: ', 'Fwd: ', 'FW: ', 'Re: Fwd: RE: ', '  re :  fWd: '):
            with self.subTest(prefix=prefix):
                self.assertEqual(routing.match(self.st, {
                    'subject': prefix+'[mailroom] failure [#%s]' % sess['tag'],
                    'to': routing.tagged_address(CFG['account'], sess['tag'])}, None, CFG['account']),
                    (None, 'mailroom-notice'))

    def test_alert_header_tells_an_alert_from_a_routed_mail(self):
        recovery.alerts.send_alert(self.fake, self.st, CFG, 'quota', 'key', 'what happened', 'body')
        routed = outbound.compose(CFG, 'session', 'owner@example.com', None, 'results', 'text', reply_to_tag='ab34cd9k')
        alert, = [m for m, _thread in self.sent]
        self.assertEqual(alert['Subject'], '[mailroom] what happened')
        self.assertTrue(routing.is_alert({k.lower(): v for k, v in alert.items()}, CFG['account']))
        self.assertFalse(routing.is_alert({k.lower(): v for k, v in routed.items()}, CFG['account']))
        self.assertEqual(routed['X-Agent-Mailroom'], 'ab34cd9k')
        self.assertEqual(routing.match(self.st, {'subject': 'Re: ' + alert['Subject']}, None, CFG['account']),
                         (None, 'mailroom-notice'))

    def test_notice_overrides_all_original_session_hints(self):
        sess = self.st.session_for('claude', SID, 'closed-session', origin_host=HOST)
        self.st.record_sent(sess['tag'], 'old', 'thread', '<old@test>', 'alex', 'old subject',
                            cc_addr=None, body=None, eml=None, attachments=None)
        self.message(sess, subject='Re: Fwd: [MAILROOM] reply #319 needs attention [#%s]' % sess['tag'],
                     headers=[('In-Reply-To', '<old@test>'), ('References', '<old@test>')])
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO) as run, \
                patch.object(deliver, 'deliver_session') as send, patch.object(recovery, 'recover') as recover:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        send.assert_not_called()
        recover.assert_not_called()
        self.assertEqual(self.st.list_inbound()[0]['action'], 'delivered')
        self.assertEqual(self.sent, [])
        self.assertIn('replying to or forwarding a mailroom notice', run.call_args.args[0])
        self.assertNotIn('this is an unowned reply', run.call_args.args[0])

    def test_recorded_notice_thread_with_changed_subject_overrides_session(self):
        sess = self.st.session_for('claude', SID, origin_host=HOST)
        self.message(sess)
        self.st.set_meta('alert:reply-recovery:319:thread_id', 'thread')
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO), \
                patch.object(deliver, 'deliver_session') as send:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        send.assert_not_called()
        self.assertEqual(self.st.list_inbound()[0]['action'], 'delivered')

    def test_historical_notice_header_exempts_unowned_reply_recovery(self):
        self.message()
        self.st.record_sent('', 'old', 'thread', '<old@test>', 'alex', 'lost origin',
                            cc_addr=None, body=None, eml=None, attachments=None)
        self.fake.thread_messages.return_value = [
            ({'from': CFG['account'], 'x-agent-mailroom': 'alert'}, 'notice', 1, 'a'),
            ({'from': 'owner@example.com'}, 'answer', 2, 'b')]
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO), \
                patch.object(recovery, 'recover') as recover:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        recover.assert_not_called()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.st.list_inbound()[0]['action'], 'delivered')

    def test_normal_subject_with_mailroom_mentioned_still_routes_to_session(self):
        sess = self.st.session_for('claude', SID, origin_host=HOST)
        self.message(sess, subject='Re: explain [mailroom] warnings [#%s]' % sess['tag'])
        with patch.object(deliver, 'deliver_session', return_value=(0, 'sent')) as send, \
                patch.object(self.room.orchestrate, 'run_event') as run:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        send.assert_called_once()
        run.assert_not_called()

    def test_alexs_third_mail_in_a_thread_reaches_the_session_with_the_reminder(self):
        sess = self.st.session_for('claude', SID, origin_host=HOST)
        asked = 'Why did run 3 fail?'
        for gid, sender in (('one', CFG['allowlist'][0]), ('two', CFG['allowlist'][0]), ('other', 'x@example.com')):
            self.st.record_inbound(gmail_id=gid, thread_id='thread', from_addr=sender, action='delivered')
        self.message(sess, text=asked)
        with patch.object(deliver, 'deliver_session', return_value=(0, 'sent')) as send:   # agent-send to the session
            self.room.process(self.fake, self.st, CFG, 'in', False)
        head, rest = send.call_args.args[1].split('\n', 1)
        self.assertTrue(head.endswith('to reply: agent-mail reply 4  (reply body on stdin or --body-file)]'))
        self.assertEqual(rest, routing.reply_reminder(CFG['owner_name'], asked, 3) + asked)
        self.assertEqual(rest.count('\n'), 2)   # both reminder lines, then what Alex wrote

    def test_unknown_sender_notice_is_held(self):
        self.message(sender='stranger@example.com', subject='Fwd: [mailroom] failure')
        with patch.object(self.room.orchestrate, 'run_event') as run:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        run.assert_not_called()
        self.assertEqual(self.st.list_inbound()[0]['action'], 'held')

    def test_notice_still_requires_dkim(self):
        self.message(subject='Re: [mailroom] failure', dkim='fail')
        with patch.object(self.room.orchestrate, 'run_event') as run:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        run.assert_not_called()
        self.assertIn('DKIM check failed', self.st.list_inbound()[0]['detail'])

    def test_collaborator_notice_keeps_guard(self):
        cfg = dict(CFG, collaborators={'collab@example.org': {'owner': 'Someone'}},
                   notify_owner_new_collab_thread=False)
        self.message(sender='collab@example.org', subject='Re: [mailroom] failure')
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO) as run:
            self.room.process(self.fake, self.st, cfg, 'in', False)
        self.assertIn(routing.collab_guard({}, config.load()["owner_name"]), run.call_args.args[0])
        self.assertNotIn('Alex is replying', run.call_args.args[0])

    def test_failed_notice_orchestration_remains_saved_and_followup_routes_here(self):
        self.message(subject='Re: [mailroom] failure')
        with patch.object(self.room.orchestrate, 'run_event', return_value=dict(INFO, ok=False, error='limit')):
            self.room.process(self.fake, self.st, CFG, 'in', False)
        row = self.st.list_inbound()[0]
        self.assertEqual(row['action'], 'held')
        self.assertEqual(row['body'], 'reply')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(routing.match(self.st, {'subject': 'Re: '+self.sent[0][0]['Subject']}, 'thread', CFG['account']),
                         (None, 'mailroom-notice'))

    def test_uncertain_delivery_notice_explains_preservation_and_action(self):
        sess = self.st.session_for('codex', SID, 'example', origin_host='mac')
        inbound = self.inbound(sess)
        recovery.hold_delivery(self.st, self.st.inbound(inbound), sess, 'connection refused')
        self.assertTrue(recovery.notify(self.fake, self.st, CFG, self.st.inbound(inbound)))
        self.assertFalse(recovery.notify(self.fake, self.st, CFG, self.st.inbound(inbound)))
        body = self.sent[0][0].get_body(('plain',)).get_content()
        for phrase in ("couldn't confirm", 'example on mac', 'may have received', 'Nothing has been lost',
                       "don't need to resend", 'no action is needed', 'reply here', 'connection refused'):
            self.assertIn(phrase, body)
        self.assertNotIn('{', body)

    def test_missing_worktree_notice_explains_no_restart(self):
        sess = self.st.session_for('claude', SID, 'closed-session', origin_host=HOST)
        inbound = self.inbound(sess)
        recovery.hold_delivery(self.st, self.st.inbound(inbound), sess, 'original working folder no longer exists: /removed')
        recovery.notify(self.fake, self.st, CFG, self.st.inbound(inbound))
        body = self.sent[0][0].get_body(('plain',)).get_content()
        self.assertIn('did not restart it elsewhere', body)
        self.assertIn('how you want to continue', body)

    def test_unowned_notice_asks_for_intended_recipient(self):
        inbound = self.inbound()
        self.st.update_inbound(inbound, recovery_json=json.dumps(dict(checks=[], status='not_found')))
        recovery.notify(self.fake, self.st, CFG, self.st.inbound(inbound))
        body = self.sent[0][0].get_body(('plain',)).get_content()
        self.assertIn("couldn't identify which session", body)
        self.assertIn('intended session or document', body)
        self.assertIn('Nothing has been lost', body)

    def test_shutdown_finishes_delivery_leaves_remainder_and_does_not_notify(self):
        sess = self.st.session_for('claude', SID, origin_host=HOST)
        self.message(sess)
        self.st.set_meta('history_id', 'before')
        self.fake.history_since.return_value = (['first', 'second'], 'after')
        def finish(*args):
            self.room.on_signal(signal.SIGTERM, None)
            return 0, 'accepted'
        with patch.object(deliver, 'deliver_session', side_effect=finish) as send, \
                patch.object(self.room, 'watchdog') as watch:
            self.room.poll(self.fake, self.st, CFG, False)
        self.assertEqual(self.st.list_inbound()[0]['action'], 'delivered')
        self.assertFalse(self.st.seen('second'))
        self.assertEqual(self.st.get_meta('history_id'), 'before')
        self.assertEqual(self.sent, [])
        self.assertIn('UNREAD', self.mods[-1][1]['remove'])
        watch.assert_not_called()
        self.room.STOP = False
        with patch.object(deliver, 'deliver_session', return_value=(0, 'accepted')) as send:
            self.room.poll(self.fake, self.st, CFG, False)
        send.assert_called_once()
        self.assertTrue(self.st.seen('second'))
        self.assertEqual(self.st.get_meta('history_id'), 'after')


class OrphanThreads(Base):
    """Alex's mail to an agent that has closed or exited goes to the orchestrator, marked, with no notice to him."""
    GONE = ('agent-send: resume failed for claude closed-agent [12345678] offline: original working folder no '
            'longer exists: /removed; nothing started')

    def setUp(self):
        super().setUp()
        self.sess = self.st.session_for('claude', SID, 'closed-agent', origin_host=HOST)
        self.st.record_sent(self.sess['tag'], 'old', 'thread', '<old@test>', 'owner@example.com', 'results',
                            cc_addr=None, body=None, eml=None, attachments=None)
        self.fake.thread_messages.return_value = [({'from': CFG['account']}, 'the results you asked for', 1, 'old'),
                                                  ({'from': 'owner@example.com'}, 'reply', 2, 'in')]

    def agent_send_exits(self, code, out):
        """agent-send, the subprocess deliver runs, exiting with code and printing out."""
        return patch.object(deliver.subprocess, 'run', return_value=Mock(returncode=code, stdout='', stderr=out))

    def run_mail(self, cfg=CFG):
        """process() on the saved message, the orchestrator's run being the boundary. Returns the run mock."""
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO) as run, \
                patch.object(recovery, 'recover') as recover:
            self.room.process(self.fake, self.st, cfg, 'in', False)
        recover.assert_not_called()
        return run

    def assert_orphan_routed(self, run, where=HOST):
        text = run.call_args.args[0]
        self.assertTrue(text.startswith(
            '[email from owner@example.com (Alex) re "Re: exact example request" · the agent that owned this '
            'thread (closed-agent, claude session 12345678 on %s) has closed or exited, so the orphan fallback gave '
            'it to you as orchestrator · to reply: agent-mail reply 1]' % where), text)
        self.assertIn('the results you asked for', text)        # the earlier thread
        self.assertNotIn('unowned reply', text)
        row = self.st.list_inbound()[0]
        self.assertEqual((row['action'], row['notified_at'], row['recovery_json']), ('delivered', None, None))
        self.assertEqual(self.sent, [])                          # no "couldn't find the session" notice

    def test_reply_to_an_agent_whose_folder_is_gone_goes_to_the_orchestrator(self):
        self.message(self.sess, headers=[('In-Reply-To', '<old@test>')])
        with self.agent_send_exits(5, self.GONE):
            run = self.run_mail()
        self.assert_orphan_routed(run)

    def test_reply_to_an_exited_or_unknown_agent_goes_to_the_orchestrator(self):
        for code, out in ((2, 'agent-send: claude closed-agent [12345678] offline is not live; nothing sent'),
                          (4, "agent-send: no session matches '%s' (try --list)" % SID)):
            with self.subTest(code=code):
                self.st.con.execute('delete from inbound')
                self.message(self.sess, headers=[('In-Reply-To', '<old@test>')])
                with self.agent_send_exits(code, out):
                    run = self.run_mail()
                self.assert_orphan_routed(run)

    def test_forward_of_a_closed_agents_mail_carries_the_forwarded_text(self):
        body = ('now get an agent to do this\n\n---------- Forwarded message ---------\nFrom: Agent <%s>\n'
                'Date: Mon, 2 Nov 2026 at 14:24\nSubject: results\n\nstep 1: rotate the token' % CFG['account'])
        self.fake.get_full.return_value = fakes.gmail_message(
            [('From', 'owner@example.com'), ('Subject', 'Fwd: results [#%s]' % self.sess['tag']),
             ('Message-ID', '<in@test>'), ('In-Reply-To', '<old@test>'), ('References', '<old@test>'),
             ('Authentication-Results', fakes.auth_header('example.com', 'pass'))], [fakes.text_part(body)])
        with self.agent_send_exits(5, self.GONE):
            run = self.run_mail()
        text = run.call_args.args[0]
        self.assertIn('the agent that owned this thread (closed-agent, claude session 12345678 on %s)' % HOST, text)
        self.assertIn('--- latest message ---\nnow get an agent to do this\n\n---------- Forwarded message', text)
        self.assertIn('step 1: rotate the token', text)
        self.assertEqual(self.sent, [])

    def test_exited_mac_session_goes_to_the_orchestrator(self):
        mac = self.st.session_for('claude', SID, 'closed-agent', origin_host='mac')
        self.st.record_sent(mac['tag'], 'old2', 'thread', '<old2@test>', 'owner@example.com', 'results',
                            cc_addr=None, body=None, eml=None, attachments=None)
        self.message(mac, headers=[('In-Reply-To', '<old2@test>')])
        # the SSH call to the Mac is the boundary: the Mac answers that the session is not live
        with patch.object(remote, 'call', return_value={'code': 2, 'detail': 'not live; nothing sent'}):
            run = self.run_mail()
        self.assert_orphan_routed(run, where='mac')

    def test_a_failure_that_is_not_a_gone_session_is_still_held_with_a_notice(self):
        self.message(self.sess, headers=[('In-Reply-To', '<old@test>')])
        with self.agent_send_exits(1, 'agent-send: delivery to claude closed-agent failed: connection reset'):
            run = self.run_mail()
        run.assert_not_called()
        row = self.st.list_inbound()[0]
        self.assertEqual(row['action'], 'held')
        self.assertTrue(row['notified_at'])

    def test_a_collaborators_mail_to_a_gone_session_is_still_held(self):
        cfg = dict(CFG, collaborators={'collab@example.org': {'owner': 'Someone'}}, notify_owner_new_collab_thread=False)
        self.message(self.sess, sender='collab@example.org', headers=[('In-Reply-To', '<old@test>')])
        with self.agent_send_exits(5, self.GONE):
            run = self.run_mail(cfg)
        run.assert_not_called()
        self.assertEqual(self.st.list_inbound()[0]['action'], 'held')
        self.assertIn('original working folder no longer exists', self.sent[0][0].get_body(('plain',)).get_content())


class Labels(Base):
    """A mail's mailroom labels show its one outcome: putting one on takes the other mailroom labels off."""

    def last(self):
        _args, kw = self.mods[-1]
        return set(kw.get('add', [])), set(kw.get('remove', []))

    def test_delivery_to_a_session_takes_the_other_labels_off(self):
        self.message(self.st.session_for('claude', SID, origin_host=HOST))
        self.fake.thread_messages.return_value = [({'from': CFG['account']}, 'question', 1, 'a'),
                                                  ({'from': 'owner@example.com'}, 'answer', 2, 'in')]
        with patch.object(deliver, 'deliver_session', return_value=(0, 'sent')) as send:   # agent-send, the session boundary
            self.room.process(self.fake, self.st, CFG, 'in', False)
        self.fake.thread_messages.assert_not_called()         # Alex's mail to a session carries no thread
        self.assertTrue(send.call_args.args[1].endswith(']\nreply'))
        self.assertEqual(self.last(), ({'mailroom/delivered'}, {'mailroom/held', 'mailroom/skipped', 'UNREAD'}))

    def test_a_hold_takes_the_other_labels_off(self):
        self.message(sender='stranger@example.com')
        self.room.process(self.fake, self.st, CFG, 'in', False)
        self.assertEqual(self.last(), ({'mailroom/held'}, {'mailroom/delivered', 'mailroom/skipped'}))

    def test_orchestrated_mail_takes_the_other_labels_off(self):
        self.message()
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO):   # the claude CLI run
            self.room.process(self.fake, self.st, CFG, 'in', False)
        self.assertEqual(self.last(), ({'mailroom/delivered'}, {'mailroom/held', 'mailroom/skipped', 'UNREAD'}))

    def test_a_dry_run_changes_no_labels(self):
        self.message(sender='stranger@example.com')
        self.room.process(self.fake, self.st, CFG, 'in', True)
        self.assertEqual(self.st.list_inbound()[0]['action'], 'held')
        self.assertEqual(self.mods, [])

    def test_release_and_rerun_take_the_other_labels_off(self):
        i = self.inbound(self.st.session_for('claude', SID, origin_host=HOST))
        with patch.object(deliver, 'deliver_session', return_value=(0, 'sent')), contextlib.redirect_stdout(io.StringIO()):
            self.am.do_release(self.ctx, argparse.Namespace(inbound_id=i, target='origin', cli=None))
        self.assertEqual(self.last(), ({'mailroom/delivered'}, {'mailroom/held', 'mailroom/skipped', 'UNREAD'}))
        i = self.inbound()
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO), contextlib.redirect_stdout(io.StringIO()):
            self.am.do_rerun(self.ctx, argparse.Namespace(inbound_id=i))
        self.assertEqual(self.last(), ({'mailroom/delivered'}, {'mailroom/held', 'mailroom/skipped', 'UNREAD'}))

    def test_rerun_keeps_a_saved_recovery_and_sends_its_notice(self):
        i = self.inbound()
        self.st.update_inbound(i, recovery_json=json.dumps({'status': 'needs_handoff', 'checks': []}),
                               detail='ownership unresolved')
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.am.do_rerun(self.ctx, argparse.Namespace(inbound_id=i)), 0)
        row = self.st.inbound(i)
        self.assertEqual((row['action'], row['detail']), ('held', 'ownership unresolved'))
        self.assertTrue(row['notified_at'])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.mods, [])

    def test_a_rerun_that_fails_labels_the_mail_held(self):
        i = self.inbound()
        self.st.update_inbound(i, action='delivered')
        with patch.object(self.room.orchestrate, 'run_event', return_value=dict(INFO, ok=False, error='limit')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.am.do_rerun(self.ctx, argparse.Namespace(inbound_id=i)), 1)
        self.assertEqual((self.st.inbound(i)['action'], self.st.inbound(i)['detail']),
                         ('held', 'orchestrator failed: rerun: $0, 1 turns, 1s; ERROR limit'))
        self.assertEqual(self.last(), ({'mailroom/held'}, {'mailroom/delivered', 'mailroom/skipped'}))

    def test_a_relabel_failure_in_agent_mail_is_a_warning_after_the_state_is_saved(self):
        i = self.inbound(self.st.session_for('claude', SID, origin_host=HOST))
        self.fake.modify.side_effect = OSError('gmail unreachable')   # the Gmail API refuses the label change
        with patch.object(deliver, 'deliver_session', return_value=(0, 'sent')), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.am.do_release(self.ctx, argparse.Namespace(inbound_id=i, target='origin', cli=None)), 0)
        self.assertEqual(self.st.inbound(i)['action'], 'delivered')
        self.assertIn('warning: could not relabel: gmail unreachable', err.getvalue())


class ProcessErrors(Base):
    """process() raising after the mail's row is saved: the mail is held with the error and Alex is told."""

    def poll(self, *gids, cfg=CFG):
        self.st.set_meta('history_id', 'before')
        self.fake.history_since.return_value = (list(gids), 'after')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.room.poll(self.fake, self.st, cfg, False)
        return out.getvalue()

    def held_labels(self):
        return [l for a, kw in self.mods for l in kw.get('add', [])].count(CFG['labels']['held'])

    def test_orchestrator_that_raises_is_held_alerted_once_and_can_be_rerun(self):
        self.message()
        boom = RuntimeError('claude binary not found')    # what orchestrate raises when the claude CLI is missing
        # run_event stands in for the claude CLI run (the boundary; tests/test_orchestrate.py drives it)
        with patch.object(self.room.orchestrate, 'run_event', side_effect=[boom, boom, INFO]):
            log = self.poll('first', 'second', 'third')
        rows = {r['gmail_id']: r for r in self.st.list_inbound()}
        for gid in ('first', 'second'):
            self.assertEqual(rows[gid]['action'], 'held')
            self.assertEqual(rows[gid]['detail'], 'error: RuntimeError: claude binary not found')
        self.assertEqual(rows['third']['action'], 'delivered')           # the daemon kept serving
        self.assertEqual(self.st.get_meta('history_id'), 'after')
        self.assertEqual(self.held_labels(), 2)
        self.assertEqual(len(self.sent), 1)                               # one alert per kind of error
        body = self.sent[0][0].get_body(('plain',)).get_content()
        first = rows['first']['id']
        for phrase in ('claude binary not found', 'agent-mail rerun %d' % first, 'agent-mail inbox --held'):
            self.assertIn(phrase, body)
        self.assertIn('error  first (#%d held): RuntimeError' % first, log)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.am.do_inbox(self.ctx, argparse.Namespace(held=True, limit=50))
        self.assertIn('error: RuntimeError: claude binary not found', out.getvalue())
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.am.do_rerun(self.ctx, argparse.Namespace(inbound_id=first)), 0)
        self.assertEqual(self.st.inbound(first)['action'], 'delivered')

    def test_label_and_alert_failures_in_the_handler_are_logged(self):
        self.message()
        self.fake.modify.side_effect = OSError('gmail unreachable')   # fails before the run and again in the handler
        self.fake.send_raw.side_effect = OSError('send refused')        # the Gmail API fake refuses the alert
        with patch.object(self.room.orchestrate, 'run_event') as run:   # the claude CLI run; must not start
            log = self.poll('first')
        run.assert_not_called()
        row = self.st.list_inbound()[0]
        self.assertEqual((row['action'], row['detail']), ('held', 'error: OSError: gmail unreachable'))
        self.assertIn('ERROR  #%d: could not label it held: OSError: gmail unreachable' % row['id'], log)
        self.assertIn('ERROR  #%d: could not alert Alex: OSError: send refused' % row['id'], log)
        self.assertEqual(self.st.get_meta('history_id'), 'after')

    def test_a_collaborator_thread_of_agents_only_is_held_by_the_loop_guard(self):
        collab = 'collab@example.org'
        cfg = dict(CFG, collaborators={collab: {'name': 'Their agent', 'owner': 'Someone'}}, notify_owner_new_collab_thread=False)
        self.message(self.st.session_for('claude', SID, origin_host=HOST), sender=collab)
        self.fake.thread_messages.return_value = [({'from': (CFG['account'], collab)[n % 2]}, 'x', n, 'g%d' % n)
                                                  for n in range(CFG['agent_thread_cap'] + 1)] + [({'from': collab}, 'x', 99, 'in')]
        with patch.object(deliver, 'deliver_session') as send:            # agent-send; must not run
            self.poll('first', cfg=cfg)
        send.assert_not_called()
        row = self.st.list_inbound()[0]
        self.assertEqual((row['action'], row['detail']), ('held', 'agent-to-agent thread has 14 messages with no human; loop guard'))
        self.assertEqual(self.sent[0][0]['Subject'], '[mailroom] possible agent loop with Their agent; thread held')

    def test_a_thread_gmail_cannot_fetch_holds_the_mail_instead_of_routing_without_it(self):
        collab = 'collab@example.org'
        cfg = dict(CFG, collaborators={collab: {'owner': 'Someone'}}, notify_owner_new_collab_thread=False)
        self.fake.thread_messages.side_effect = OSError('thread fetch failed')   # the Gmail API call fails
        self.message()                                                    # Alex, for the orchestrator
        with patch.object(self.room.orchestrate, 'run_event') as run:    # the claude CLI run; must not start
            self.poll('first', cfg=cfg)
        run.assert_not_called()
        self.message(self.st.session_for('claude', SID, origin_host=HOST), sender=collab)  # a collaborator, for a session
        with patch.object(deliver, 'deliver_session') as send:            # agent-send; must not run: no loop guard
            self.poll('second', cfg=cfg)
        send.assert_not_called()
        for row in self.st.list_inbound():
            self.assertEqual((row['action'], row['detail']), ('held', 'error: OSError: thread fetch failed'))
        self.assertEqual(self.held_labels(), 2)
        self.assertIn('thread fetch failed', self.sent[0][0].get_body(('plain',)).get_content())

    def test_a_recovery_saved_during_the_run_is_logged_with_the_run(self):
        self.message()

        def run(text, source, cfg):   # the claude CLI run: it ran `agent-mail recover N`, then failed
            row = self.st.inbound_by_gmail_id('first')
            self.st.update_inbound(row['id'], action='held', recovery_json=json.dumps({'status': 'needs_handoff', 'checks': []}))
            return dict(INFO, ok=False, error='limit')
        with patch.object(self.room.orchestrate, 'run_event', side_effect=run):
            log = self.poll('first')
        row = self.st.list_inbound()[0]
        self.assertIn('held   #%d: recovery saved during the run; orchestrator ok=False: '
                      'orchestrator run: $0, 1 turns, 1s; ERROR limit' % row['id'], log)
        self.assertEqual(row['action'], 'held')
        self.assertEqual(len(self.sent), 1)                          # the recovery notice
        self.assertIn('needs attention', self.sent[0][0]['Subject'])

    def test_rerun_stops_when_the_thread_cannot_be_fetched(self):
        i = self.inbound()
        self.fake.thread_messages.side_effect = OSError('thread fetch failed')   # the Gmail API call fails
        with patch.object(self.room.orchestrate, 'run_event') as run:            # the claude CLI run
            with self.assertRaisesRegex(OSError, 'thread fetch failed'):
                self.am.do_rerun(self.ctx, argparse.Namespace(inbound_id=i))
        run.assert_not_called()
        self.assertEqual(self.st.inbound(i)['action'], 'held')

    def test_an_error_after_the_outcome_is_recorded_keeps_the_outcome(self):
        sess = self.st.session_for('claude', SID, origin_host=HOST)
        self.message(sess)
        self.fake.modify.side_effect = OSError('label failed')         # after delivery succeeded
        with patch.object(deliver, 'deliver_session', return_value=(0, 'sent')):   # agent-send, the session boundary
            log = self.poll('first')
        row = self.st.list_inbound()[0]
        self.assertEqual(row['action'], 'delivered')
        self.assertEqual(self.sent, [])
        self.assertIn('error  first (#%d, already delivered; kept): OSError: label failed' % row['id'], log)


class HistoryRescan(Base):
    """Gmail dropped the saved history cursor (it usually keeps a week): the inbox is searched from the last poll."""

    def setUp(self):
        super().setUp()
        self.st.set_meta('history_id', 'lost')
        self.st.set_meta('last_poll', 1700000000)
        self.fake.history_since.side_effect = gmail.HistoryGone()    # Gmail answered 404 for the cursor
        self.fake.profile.return_value = {'historyId': 'now'}
        self.message()

    def poll(self):
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.room.poll(self.fake, self.st, CFG, False)
        return out.getvalue()

    def test_the_inbox_since_the_last_poll_is_handled_oldest_first_and_alex_is_told(self):
        self.fake.search.return_value = ['newer', 'older']           # Gmail lists newest first
        before = time.time()
        log = self.poll()
        self.fake.search.assert_called_once_with(self.fake, 'in:inbox after:1700000000', self.room.RESCAN_LIMIT)
        self.assertEqual([r['gmail_id'] for r in sorted(self.st.list_inbound(), key=lambda r: r['id'])], ['older', 'newer'])
        self.assertEqual(self.st.get_meta('history_id'), 'now')
        self.assertGreaterEqual(int(self.st.get_meta('last_poll')), int(before))
        self.assertIn("searched 'in:inbox after:1700000000': 2 message(s)", log)
        self.assertEqual(len(self.sent), 1)
        alert = self.sent[0][0]
        self.assertIn('rescanned', alert['Subject'])
        body = alert.get_body(('plain',)).get_content()
        self.assertIn('in:inbox after:1700000000', body)
        self.assertIn('2 message(s)', body)
        self.assertNotIn('stopped at', body)

    def test_a_search_that_reaches_the_limit_says_older_mail_was_not_handled(self):
        self.fake.search.return_value = ['b', 'a']
        with patch.object(self.room, 'RESCAN_LIMIT', 2):
            self.poll()
        self.assertIn('stopped at 2 messages', self.sent[0][0].get_body(('plain',)).get_content())

    def test_an_alert_that_cannot_be_sent_stops_the_poll_before_the_cursor_moves(self):
        self.fake.search.return_value = ['older']
        self.fake.send_raw.side_effect = OSError('gmail send failed')   # the Gmail API is the boundary
        with self.assertRaisesRegex(OSError, 'gmail send failed'):
            self.poll()
        self.assertEqual(self.st.get_meta('history_id'), 'lost')    # the next poll rescans

    def test_with_no_poll_recorded_it_stops_and_keeps_the_cursor(self):
        self.st.con.execute("delete from meta where key='last_poll'")
        with self.assertRaisesRegex(RuntimeError, 'no successful poll'):
            self.poll()
        self.fake.search.assert_not_called()
        self.assertEqual(self.st.get_meta('history_id'), 'lost')


class FakeGmailService:
    """Stands in for the Gmail API client, the network boundary: each list call returns the next queued page."""

    def __init__(self, **pages):
        self.pages, self.calls = {k: list(v) for k, v in pages.items()}, []

    def users(self):
        return self

    def history(self):
        self.kind = 'history'
        return self

    def messages(self):
        self.kind = 'messages'
        return self

    def list(self, **kw):
        self.calls.append((self.kind, kw))
        self.page = self.pages[self.kind].pop(0)
        return self

    def execute(self):
        return self.page


def added(*ids, label='INBOX'):
    return {'messagesAdded': [{'message': {'id': i, 'labelIds': [label]}} for i in ids]}


class GmailListing(unittest.TestCase):
    def test_history_pages_keep_first_order_without_repeats_or_other_labels(self):
        svc = FakeGmailService(history=[{'history': [added('a', 'b')], 'historyId': '5', 'nextPageToken': 'p2'},
                                        {'history': [added('a'), added('s', label='SENT'), added('c')], 'historyId': '7'}])
        self.assertEqual(gmail.history_since(svc, '1'), (['a', 'b', 'c'], '7'))
        self.assertEqual([kw['pageToken'] for _kind, kw in svc.calls], [None, 'p2'])


    def test_search_follows_pages_up_to_the_limit(self):
        pages = [{'messages': [{'id': 'a'}, {'id': 'b'}], 'nextPageToken': 'p2'}, {'messages': [{'id': 'c'}]}]
        svc = FakeGmailService(messages=pages)
        self.assertEqual(gmail.search(svc, 'in:inbox after:1', 10), ['a', 'b', 'c'])
        self.assertEqual([(kw['q'], kw['maxResults'], kw['pageToken']) for _kind, kw in svc.calls],
                         [('in:inbox after:1', 10, None), ('in:inbox after:1', 8, 'p2')])
        svc = FakeGmailService(messages=pages)
        self.assertEqual(gmail.search(svc, 'q', 2), ['a', 'b'])
        self.assertEqual(len(svc.calls), 1)
        self.assertEqual(gmail.search(FakeGmailService(messages=[{'resultSizeEstimate': 0}]), 'q', 5), [])


# A stub agent-send (the real one would reach a live session) writes its receipt with the real writer.
RECEIPT_WRITER = 'import sys\nsys.path.insert(0, %r)\nfrom agentmail.deliver import write_receipt\n' % str(ROOT)


class Receipts(unittest.TestCase):
    def test_daemon_sigterm_leaves_real_inflight_child_to_finish(self):
        room = load_script('agent-mailroom')
        with tempfile.TemporaryDirectory() as tmp:
            started = Path(tmp) / 'started'
            sender = Path(tmp) / 'sender'
            sender.write_text('#!%s\n' % sys.executable + RECEIPT_WRITER +
                'import sys,time\nfrom pathlib import Path\n' +
                'text=sys.stdin.read().strip()\n' +
                'Path(%r).touch()\ntime.sleep(0.25)\n' % str(started) +
                'write_receipt(sys.argv[sys.argv.index("--receipt")+1], sys.argv[-1], text)\n')
            sender.chmod(0o700)
            def terminate_daemon():
                deadline = time.monotonic()+3
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if started.exists():
                    # KillMode=mixed sends SIGTERM to the daemon alone, not this child.
                    os.kill(os.getpid(), signal.SIGTERM)
            previous = signal.signal(signal.SIGTERM, room.on_signal)
            worker = threading.Thread(target=terminate_daemon)
            worker.start()
            try:
                with patch.object(deliver, 'agent_send_bin', return_value=str(sender)), \
                        patch.object(deliver, 'STATE_DIR', Path(tmp)):
                    rc, _ = deliver.deliver('claude', SID, 'reply', False, False)
                worker.join(4)
            finally:
                worker.join(4)
                signal.signal(signal.SIGTERM, previous)
            self.assertTrue(room.STOP)
            self.assertEqual(rc, 0)

    def test_service_preserves_children_for_a_bounded_drain(self):
        unit = configparser.RawConfigParser(strict=False)  # systemd allows repeated Environment entries
        unit.read(ROOT / 'systemd' / 'agent-mailroom.service.in')   # the kit ships unit templates (README, setup)
        self.assertEqual(unit['Service']['KillMode'], 'mixed')
        self.assertEqual(unit['Service']['TimeoutStopSec'], '1h')

    def test_real_sigterm_after_acceptance_is_success_without_stdout(self):
        self.check_sender('os.kill(os.getpid(), signal.SIGTERM)', accepted=True)

    def test_real_timeout_after_acceptance_is_success(self):
        self.check_sender('time.sleep(30)', accepted=True, timeout=0.3)

    def test_real_sigterm_before_acceptance_remains_failure(self):
        self.check_sender('os.kill(os.getpid(), signal.SIGTERM)', accepted=False)

    def check_sender(self, exit_code, accepted, timeout=3):
        with tempfile.TemporaryDirectory() as tmp:
            sender = Path(tmp) / 'sender'
            sender.write_text('#!%s\n' % sys.executable + RECEIPT_WRITER +
                'import os,signal,sys,time\n' +
                'text=sys.stdin.read().strip()\n' +
                ('write_receipt(sys.argv[sys.argv.index("--receipt")+1], sys.argv[-1], text)\n' if accepted else '') +
                exit_code+'\n')
            sender.chmod(0o700)
            with patch.object(deliver, 'agent_send_bin', return_value=str(sender)), \
                    patch.object(deliver, 'STATE_DIR', Path(tmp)):
                rc, detail = deliver.deliver('claude', SID, 'fixture reply\n', False, False, timeout=timeout)
            self.assertEqual(rc, 0 if accepted else -signal.SIGTERM)
            if accepted:
                self.assertIn('receipt verified', detail)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ['sender'])

    def test_mismatched_receipt_is_not_accepted(self):
        for target, text in ((SID, 'wrong body'), ('other-session', 'reply')):
            def run(cmd, **kwargs):
                deliver.write_receipt(cmd[cmd.index('--receipt')+1], target, text)
                return Mock(returncode=-15, stdout='', stderr='')
            with patch.object(deliver.subprocess, 'run', side_effect=run):
                self.assertEqual(deliver.deliver('claude', SID, 'reply', False, False)[0], -15)

    def test_malformed_receipt_keeps_original_failure(self):
        for data in ('invalid json', '[]', '{}'):
            def run(cmd, **kwargs):
                Path(cmd[cmd.index('--receipt')+1]).write_text(data)
                return Mock(returncode=1, stdout='', stderr='failed')
            with self.subTest(data=data), patch.object(deliver.subprocess, 'run', side_effect=run):
                self.assertEqual(deliver.deliver('claude', SID, 'reply', False, False), (1, 'failed'))

    def test_a_message_held_in_codex_s_queue_keeps_its_exit_code_beside_its_receipt(self):
        """A held message was accepted (its receipt is written), and its exit code must still say it is held: the
        receipt stands in for an answer only when agent-send was cut off or failed."""
        def run(cmd, **kwargs):
            deliver.write_receipt(cmd[cmd.index('--receipt')+1], SID, kwargs['input'])
            return Mock(returncode=deliver.HELD, stdout='held in Codex\'s queue\nwhy', stderr='')
        with patch.object(deliver.subprocess, 'run', side_effect=run):
            self.assertEqual(deliver.deliver('codex', SID, 'reply', False, False),
                             (deliver.HELD, "held in Codex's queue\nwhy"))

    def test_successful_send_without_receipt_still_succeeds(self):
        with patch.object(deliver.subprocess, 'run', return_value=Mock(returncode=0, stdout='sent', stderr='')):
            self.assertEqual(deliver.deliver('claude', SID, 'reply', False, False), (0, 'sent'))

    def test_timeout_without_receipt_still_fails(self):
        with patch.object(deliver.subprocess, 'run', side_effect=subprocess.TimeoutExpired('agent-send', 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                deliver.deliver('claude', SID, 'reply', False, False)

    def test_receipt_saved_before_memory_capping_both_resumes(self):
        sender = load_script('agent-send')
        for cli in ('claude', 'codex'):
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as tmp:
                getattr(homes.Homes(self), cli)(SID, 'fixture', live=False, cwd='')   # exited: resumed
                receipt = Path(tmp) / 'accepted.json'
                def caps():
                    self.assertTrue(deliver.receipt_written(receipt, SID, 'reply'))
                    self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
                    raise RuntimeError('fixture interruption after accepted transport')
                # the CLI's binary and its start (subprocess) are the boundary
                with patch.object(send, cli+'_bin', return_value='fixture'), \
                        patch.object(send, 'LOG_DIR', Path(tmp)), \
                        patch.object(send.subprocess, 'run', return_value=Mock(returncode=0, stdout='started', stderr='')), \
                        patch.object(send.subprocess, 'Popen', return_value=Mock(pid=123)), \
                        patch.object(send, 'agent_caps', side_effect=caps), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = sender.main(['--exact-id', '--raw', '--cli', cli, '--offline', 'resume',
                                      '--receipt', str(receipt), SID, 'reply'])
                self.assertEqual(rc, 1)
                self.assertTrue(deliver.receipt_written(receipt, SID, 'reply'))

    def test_live_socket_and_queue_save_receipts_only_after_acceptance(self):
        sender = load_script('agent-send')
        for cli in ('claude', 'codex'):
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / 'accepted.json'
                getattr(homes.Homes(self), cli)(SID, 'fixture', cwd=tmp)
                # the session's inbox socket and codex queue are the boundary
                with patch.object(sender, 'send_claude_socket', return_value='ok'), \
                        patch.object(sender, 'queue_codex', return_value=(True, 'Queued message')), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(sender.main(['--raw', '--exact-id', '--receipt', str(receipt), SID, 'reply']), 0)
                self.assertTrue(receipt.exists())
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / 'accepted.json'
            homes.Homes(self).codex(SID, 'fixture')
            with patch.object(sender, 'queue_codex', return_value=(False, 'failed')), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(sender.main(['--raw', '--exact-id', '--receipt', str(receipt), SID, 'reply']), 1)
            self.assertFalse(receipt.exists())


class RemovedWorktrees(unittest.TestCase):
    def test_both_clis_refuse_missing_cwd_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            for cli in ('claude', 'codex'):
                accepted = []
                with self.subTest(cli=cli), patch.object(send.subprocess, 'run') as run, \
                        patch.object(send.subprocess, 'Popen') as popen:
                    ok, detail = getattr(send, 'resume_'+cli)(dict(id=SID, cwd=tmp+'/removed'), 'reply', True,
                                                              lambda: accepted.append(cli))
                self.assertFalse(ok)
                self.assertEqual(accepted, [])
                self.assertIn('original working folder no longer exists', detail)
                run.assert_not_called()
                popen.assert_not_called()

    def test_agent_send_exits_5_when_the_folder_is_gone(self):
        sender = load_script('agent-send')
        for cli in ('claude', 'codex'):
            with self.subTest(cli=cli), patch.object(sender, 'resume_' + cli) as resume, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                getattr(homes.Homes(self), cli)(SID, 'closed', live=False, cwd='/removed-folder')
                rc = sender.main(['--exact-id', '--raw', '--cli', cli, '--offline', 'resume', SID, 'reply'])
            self.assertEqual(rc, 5)
            self.assertIn('original working folder no longer exists: /removed-folder', err.getvalue())
            resume.assert_not_called()

    def test_transcript_lookup_keeps_original_missing_cwd(self):
        home = homes.Homes(self).default
        project = home / 'projects' / 'fixture'
        project.mkdir(parents=True)
        (project / (SID+'.jsonl')).write_text(json.dumps(dict(cwd=str(home)+'/removed'))+'\n')
        self.assertEqual(registry.claude_transcript_session(SID)['cwd'], str(home)+'/removed')


if __name__ == '__main__':
    unittest.main()
