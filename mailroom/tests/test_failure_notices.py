"""Shutdown, transport receipts, and notice routing. No Gmail or real agent turns."""
import contextlib
import configparser
import hashlib
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

from test_remote import Base, CFG, ROOT, SID, script
from agentmail import deliver, recovery, registry, routing

INFO = dict(ok=True, cost=0, turns=1, duration_s=1, error='', result='done')


class NoticeRoutes(Base):
    def test_accepted_but_killed_sender_records_delivery_without_an_alert(self):
        sess = self.st.session_for('claude', SID, 'fixture')
        self.message(sess)
        def accept_then_die(cmd, **kwargs):
            receipt = Path(cmd[cmd.index('--receipt')+1])
            receipt.write_text(json.dumps(dict(target=SID,
                sha256=hashlib.sha256(kwargs['input'].strip().encode()).hexdigest())))
            return Mock(returncode=-signal.SIGTERM, stdout='', stderr='')
        with patch.object(deliver.subprocess, 'run', side_effect=accept_then_die):
            self.room.process(self.fake, self.st, CFG, 'in', False)
        row = self.st.list_inbound()[0]
        self.assertEqual(row['action'], 'delivered')
        self.assertIn('receipt verified', row['detail'])
        self.assertIsNone(row['recovery_json'])
        self.assertEqual(self.sent, [])

    def test_reply_and_forward_subject_variants(self):
        sess = self.st.session_for('claude', SID)
        for prefix in ('', 'Re: ', 'Fwd: ', 'FW: ', 'Re: Fwd: RE: ', '  re :  fWd: '):
            with self.subTest(prefix=prefix):
                self.assertEqual(routing.match(self.st, {
                    'subject': prefix+'[mailroom] failure [#%s]' % sess['tag'],
                    'to': routing.tagged_address(CFG['account'], sess['tag'])}, None, CFG['account']),
                    (None, 'mailroom-notice'))

    def test_notice_overrides_all_original_session_hints(self):
        sess = self.st.session_for('claude', SID, 'closed-session')
        self.st.record_sent(sess['tag'], 'old', 'thread', '<old@test>', 'alex', 'old subject')
        self.message(sess)
        headers = self.fake.get_full.return_value['headers']
        headers.update(subject='Re: Fwd: [MAILROOM] reply #319 needs attention [#%s]' % sess['tag'],
                       **{'in-reply-to': '<old@test>', 'references': '<old@test>'})
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO) as run, \
                patch.object(deliver, 'deliver_session') as send, patch.object(recovery, 'recover') as recover:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        send.assert_not_called()
        recover.assert_not_called()
        self.assertEqual(self.st.list_inbound()[0]['action'], 'delivered')
        self.assertEqual(self.sent, [])
        self.assertIn('replying to or forwarding a mailroom notice', run.call_args.args[0])
        self.assertIn('Alex is replying', run.call_args.args[0])   # config owner_name
        self.assertNotIn('this is an unowned reply', run.call_args.args[0])

    def test_recorded_notice_thread_with_changed_subject_overrides_session(self):
        sess = self.st.session_for('claude', SID)
        self.message(sess)
        self.st.set_meta('alert:reply-recovery:319:thread_id', 'thread')
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO), \
                patch.object(deliver, 'deliver_session') as send:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        send.assert_not_called()
        self.assertEqual(self.st.list_inbound()[0]['action'], 'delivered')

    def test_historical_notice_header_exempts_unowned_reply_recovery(self):
        self.message()
        self.st.record_sent('', 'old', 'thread', '<old@test>', 'alex', 'lost origin')
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
        sess = self.st.session_for('claude', SID)
        self.message(sess)
        self.fake.get_full.return_value['headers']['subject'] = 'Re: explain [mailroom] warnings [#%s]' % sess['tag']
        with patch.object(deliver, 'deliver_session', return_value=(0, 'sent')) as send, \
                patch.object(self.room.orchestrate, 'run_event') as run:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        send.assert_called_once()
        run.assert_not_called()

    def test_unknown_sender_notice_is_held(self):
        self.message(sender='stranger@example.com')
        self.fake.get_full.return_value['headers']['subject'] = 'Fwd: [mailroom] failure'
        with patch.object(self.room.orchestrate, 'run_event') as run:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        run.assert_not_called()
        self.assertEqual(self.st.list_inbound()[0]['action'], 'held')

    def test_notice_still_requires_dkim(self):
        self.message()
        self.fake.get_full.return_value['headers']['subject'] = 'Re: [mailroom] failure'
        self.fake.auth_results.return_value = dict(dkim=[], raw='dkim=fail')
        with patch.object(self.room.orchestrate, 'run_event') as run:
            self.room.process(self.fake, self.st, CFG, 'in', False)
        run.assert_not_called()
        self.assertIn('DKIM check failed', self.st.list_inbound()[0]['detail'])

    def test_collaborator_notice_keeps_guard(self):
        cfg = dict(CFG, collaborators={'collab@gmail.com': {'owner': 'Someone'}},
                   notify_owner_new_collab_thread=False)
        self.message(sender='collab@gmail.com')
        self.fake.get_full.return_value['headers']['subject'] = 'Re: [mailroom] failure'
        with patch.object(self.room.orchestrate, 'run_event', return_value=INFO) as run:
            self.room.process(self.fake, self.st, cfg, 'in', False)
        self.assertIn(routing.collab_guard('Alex'), run.call_args.args[0])
        self.assertNotIn('is replying to or forwarding', run.call_args.args[0])

    def test_failed_notice_orchestration_remains_saved_and_followup_routes_here(self):
        self.message()
        self.fake.get_full.return_value['headers']['subject'] = 'Re: [mailroom] failure'
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
        sess = self.st.session_for('claude', SID, 'closed-session')
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
        sess = self.st.session_for('claude', SID)
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


class Receipts(unittest.TestCase):
    def test_daemon_sigterm_leaves_real_inflight_child_to_finish(self):
        room = script('agent-mailroom')
        with tempfile.TemporaryDirectory() as tmp:
            started = Path(tmp) / 'started'
            sender = Path(tmp) / 'sender'
            sender.write_text('#!%s\n' % sys.executable +
                'import hashlib,json,sys,time\nfrom pathlib import Path\n' +
                'text=sys.stdin.read().strip()\n' +
                'Path(%r).touch()\ntime.sleep(0.25)\n' % str(started) +
                'Path(sys.argv[sys.argv.index("--receipt")+1]).write_text(json.dumps({"target":sys.argv[-1],"sha256":hashlib.sha256(text.encode()).hexdigest()}))\n')
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
                    rc, _ = deliver.deliver('claude', SID, 'reply')
                worker.join(4)
            finally:
                worker.join(4)
                signal.signal(signal.SIGTERM, previous)
            self.assertTrue(room.STOP)
            self.assertEqual(rc, 0)

    def test_service_preserves_children_for_a_bounded_drain(self):
        unit = configparser.RawConfigParser(strict=False)  # systemd allows repeated Environment entries
        unit.read(ROOT / 'systemd' / 'agent-mailroom.service.in')
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
            sender.write_text('#!%s\n' % sys.executable +
                'import hashlib,json,os,signal,sys,time\nfrom pathlib import Path\n' +
                'text=sys.stdin.read().strip()\n' +
                ('Path(sys.argv[sys.argv.index("--receipt")+1]).write_text(json.dumps({"target":sys.argv[-1],"sha256":hashlib.sha256(text.encode()).hexdigest()}))\n' if accepted else '') +
                exit_code+'\n')
            sender.chmod(0o700)
            with patch.object(deliver, 'agent_send_bin', return_value=str(sender)), \
                    patch.object(deliver, 'STATE_DIR', Path(tmp)):
                rc, detail = deliver.deliver('claude', SID, 'fixture reply\n', timeout=timeout)
            self.assertEqual(rc, 0 if accepted else -signal.SIGTERM)
            if accepted:
                self.assertIn('receipt verified', detail)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ['sender'])

    def test_mismatched_receipt_is_not_accepted(self):
        for target, text in ((SID, 'wrong body'), ('other-session', 'reply')):
            def run(cmd, **kwargs):
                receipt = Path(cmd[cmd.index('--receipt')+1])
                receipt.write_text(json.dumps(dict(target=target, sha256=hashlib.sha256(text.encode()).hexdigest())))
                return Mock(returncode=-15, stdout='', stderr='')
            with patch.object(deliver.subprocess, 'run', side_effect=run):
                self.assertEqual(deliver.deliver('claude', SID, 'reply')[0], -15)

    def test_malformed_receipt_keeps_original_failure(self):
        for data in ('invalid json', '[]', '{}'):
            def run(cmd, **kwargs):
                Path(cmd[cmd.index('--receipt')+1]).write_text(data)
                return Mock(returncode=1, stdout='', stderr='failed')
            with self.subTest(data=data), patch.object(deliver.subprocess, 'run', side_effect=run):
                self.assertEqual(deliver.deliver('claude', SID, 'reply'), (1, 'failed'))

    def test_successful_send_without_receipt_still_succeeds(self):
        with patch.object(deliver.subprocess, 'run', return_value=Mock(returncode=0, stdout='sent', stderr='')):
            self.assertEqual(deliver.deliver('claude', SID, 'reply'), (0, 'sent'))

    def test_timeout_without_receipt_still_fails(self):
        with patch.object(deliver.subprocess, 'run', side_effect=subprocess.TimeoutExpired('agent-send', 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                deliver.deliver('claude', SID, 'reply')

    def test_receipt_saved_before_memory_capping_both_resumes(self):
        sender = script('agent-send')
        sess = dict(id=SID, cwd='', name='fixture')
        for cli in ('claude', 'codex'):
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / 'accepted.json'
                def caps():
                    self.assertEqual(json.loads(receipt.read_text())['target'], SID)
                    self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
                    raise RuntimeError('fixture interruption after accepted transport')
                with patch.object(sender, 'all_sessions', return_value=[dict(sess, cli=cli, live=False)]), \
                        patch.object(sender, cli+'_bin', return_value='fixture'), \
                        patch.object(sender, 'LOG_DIR', Path(tmp)), \
                        patch.object(sender.subprocess, 'run', return_value=Mock(returncode=0, stdout='started', stderr='')), \
                        patch.object(sender.subprocess, 'Popen', return_value=Mock(pid=123)), \
                        patch.object(sender, 'agent_caps', side_effect=caps), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = sender.main(['--exact-id', '--raw', '--cli', cli, '--offline', 'resume',
                                      '--receipt', str(receipt), SID, 'reply'])
                self.assertEqual(rc, 1)
                self.assertEqual(json.loads(receipt.read_text())['sha256'], hashlib.sha256(b'reply').hexdigest())

    def test_live_socket_and_queue_save_receipts_only_after_acceptance(self):
        sender = script('agent-send')
        for cli in ('claude', 'codex'):
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / 'accepted.json'
                sess = dict(id=SID, cwd=tmp, name='fixture', cli=cli, live=True)
                with patch.object(sender, 'all_sessions', return_value=[sess]), \
                        patch.object(sender, 'send_claude_socket', return_value='ok'), \
                        patch.object(sender, 'queue_codex', return_value=(True, 'Queued message')), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(sender.main(['--raw', '--exact-id', '--receipt', str(receipt), SID, 'reply']), 0)
                self.assertTrue(receipt.exists())
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / 'accepted.json'
            with patch.object(sender, 'all_sessions', return_value=[dict(id=SID, cli='codex', live=True, name='fixture')]), \
                    patch.object(sender, 'queue_codex', return_value=(False, 'failed')), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(sender.main(['--raw', '--exact-id', '--receipt', str(receipt), SID, 'reply']), 1)
            self.assertFalse(receipt.exists())


class RemovedWorktrees(unittest.TestCase):
    def test_both_clis_refuse_missing_cwd_before_launch(self):
        sender = script('agent-send')
        with tempfile.TemporaryDirectory() as tmp:
            for cli in ('claude', 'codex'):
                with self.subTest(cli=cli), patch.object(sender.subprocess, 'run') as run, \
                        patch.object(sender.subprocess, 'Popen') as popen:
                    ok, detail = getattr(sender, 'resume_'+cli)(dict(id=SID, cwd=tmp+'/removed'), 'reply', True)
                self.assertFalse(ok)
                self.assertIn('original working folder no longer exists', detail)
                run.assert_not_called()
                popen.assert_not_called()

    def test_transcript_lookup_keeps_original_missing_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / 'projects' / 'fixture'
            project.mkdir(parents=True)
            (project / (SID+'.jsonl')).write_text(json.dumps(dict(cwd=tmp+'/removed'))+'\n')
            with patch.object(registry, 'claude_homes', return_value=[('default', home, None)]):
                session = registry.claude_transcript_session(SID)
            self.assertEqual(session['cwd'], tmp+'/removed')


if __name__ == '__main__':
    unittest.main()
