"""Host routing and recovery regressions. No real Gmail or agent turns."""
import argparse
import contextlib
import email
import email.policy
import importlib.machinery
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.TemporaryDirectory()
os.environ['AGENT_MAIL_STATE_DIR'] = TMP.name
os.environ['AGENT_MAIL_CONFIG_DIR'] = TMP.name
os.environ['AGENT_STATUS_DIR'] = TMP.name
from agentmail import config, deliver, recovery, remote, routing
from agentmail.store import Store

SID = '12345678-1234-4234-8234-123456789abc'
ORIGIN = dict(host='mac', cli='codex', id=SID, name='example', cwd='/Users/alex/code/example')
CFG = dict(config.DEFAULTS, account='agents@example.com', allowlist=['owner@example.com'], owner_name='Alex',
           remote_hosts={'mac': 'mac'}, recovery_hosts=['mac'])
MAC = dict(CFG, host_id='mac', mail_proxy='desktop', send_enabled=False, remote_hosts={'desktop': 'desktop'})


def script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace('-', '_'), str(ROOT / 'bin' / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.st = Store(Path(TMP.name) / (str(uuid.uuid4()) + '.sqlite'))
        self.am = script('agent-mail')
        self.room = script('agent-mailroom')
        self.patches = []
        for obj, key, value in [(self.am, 'Store', lambda: self.st), (config, 'load', lambda: CFG)]:
            p = patch.object(obj, key, value); p.start(); self.patches.append(p)
        self.sent = []
        self.mods = []
        self.fake = Mock()
        self.fake.service.return_value = 'svc'
        self.fake.send_raw.side_effect = self.send
        self.fake.get_metadata.side_effect = lambda *a: {'payload': {'headers': [{'name': 'Message-ID', 'value': '<sent@test>'}]}}
        self.fake.headers.side_effect = lambda m: m.get('headers') or {h['name'].lower():h['value'] for h in m['payload']['headers']}
        self.fake.body_text.side_effect = lambda m: m.get('body', '')
        self.fake.auth_results.return_value = {'dkim': ['example.com', 'gmail.com']}
        self.fake.thread_messages.return_value = []
        self.fake.ensure_label.side_effect = lambda svc, x: x
        self.fake.modify.side_effect = lambda *a, **kw: self.mods.append((a, kw))
        for obj in (self.am, self.room, recovery.alerts):
            p = patch.object(obj, 'gmail', self.fake); p.start(); self.patches.append(p)

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.st.con.close()

    def send(self, svc, raw, thread_id=None):
        self.sent.append((email.message_from_bytes(raw, policy=email.policy.default), thread_id))
        return {'id': 'out%d' % len(self.sent), 'threadId': thread_id or 'thread'}

    def request(self, **extra):
        req = dict(protocol=1, source_host='mac', destination_host='desktop', op='mail',
                   action='send', args={'to':'owner@example.com', 'subject':'example'},
                   origin=ORIGIN, text='body with $(not-a-command)', html='<p>html</p>', request_id=str(uuid.uuid4()))
        req.update(extra)
        return req

    def inbound(self, session=None):
        return self.st.record_inbound(gmail_id=str(uuid.uuid4()), thread_id='thread', message_id='<in@test>',
            from_addr='owner@example.com', subject='Re: exact example request',
            matched_tag=session['tag'] if session else None, action='held', body='reply')

    def message(self, session=None, sender='owner@example.com'):
        headers={'from':sender, 'subject':'Re: exact example request', 'message-id':'<in@test>'}
        if session: headers['to']=routing.tagged_address(CFG['account'],session['tag'])
        self.fake.get_full.return_value={'headers':headers,'threadId':'thread','body':'reply'}


class HostedStore(Base):
    def test_same_uuid_and_name_on_two_hosts_are_distinct(self):
        a=self.st.session_for('codex',SID,'same',origin_host='desktop')
        b=self.st.session_for('codex',SID,'same',origin_host='mac')
        self.assertNotEqual(a['tag'],b['tag'])
        self.st.record_sent(b['tag'],'g','t','<m@test>','x','y')
        matched,rule=routing.match(self.st,{'in-reply-to':'<m@test>'},None,CFG['account'])
        self.assertEqual(matched['origin_host'],'mac')
        self.assertEqual(self.st.session_for('codex',SID,origin_host='mac')['tag'],b['tag'])

    def test_legacy_migration_preserves_tags(self):
        p=Path(TMP.name)/('legacy-'+str(uuid.uuid4())+'.sqlite')
        c=sqlite3.connect(p)
        c.execute('create table sessions(tag text primary key,cli text,session_id text,session_name text,cwd text,created_at integer,unique(cli,session_id))')
        c.execute('insert into sessions values(?,?,?,?,?,?)',('oldtag','codex',SID,'old','/x',1)); c.commit(); c.close()
        migrated=Store(p)
        self.assertEqual(migrated.session_by_tag('oldtag')['origin_host'],'desktop')
        self.assertEqual(migrated.session_for('codex',SID)['tag'],'oldtag')
        self.assertNotEqual(migrated.session_for('codex',SID,origin_host='mac')['tag'],'oldtag')
        migrated.con.close()
        Store(p).con.close()


class ProxyMail(Base):
    def test_proxy_send_records_origin_and_html_before_reply(self):
        with patch.object(deliver,'registry',side_effect=AssertionError('must not resolve Mac UUID on desktop')):
            self.am.rpc_mail(self.request(),CFG)
        row=self.st.session_by_thread('thread')
        self.assertEqual((row['origin_host'],row['session_id'],row['cwd']),('mac',SID,ORIGIN['cwd']))
        msg=self.sent[0][0]
        self.assertEqual(msg.get_body(('html',)).get_content().strip(),'<p>html</p>')
        self.assertIn('$(not-a-command)',msg.get_body(('plain',)).get_content())

    def test_same_request_does_not_send_twice(self):
        req=self.request()
        first=self.am.rpc_mail(req,CFG)
        self.assertEqual(self.am.rpc_mail(req,CFG),first)
        self.assertEqual(len(self.sent),1)
        req['text']='changed'
        with self.assertRaises(ValueError): self.am.rpc_mail(req,CFG)

    def test_uncertain_send_is_not_automatically_retried(self):
        req=self.request()
        self.fake.send_raw.side_effect=TimeoutError('lost response')
        with self.assertRaises(TimeoutError): self.am.rpc_mail(req,CFG)
        with self.assertRaisesRegex(ValueError,'uncertain'): self.am.rpc_mail(req,CFG)
        self.assertEqual(self.fake.send_raw.call_count,1)

    def test_remote_reply_preserves_owner_and_thread(self):
        self.am.rpc_mail(self.request(),CFG)
        sess=self.st.session_by_thread('thread'); i=self.inbound(sess)
        self.am.rpc_mail(self.request(action='reply',args={'inbound_id':i}),CFG)
        self.assertEqual(self.sent[-1][1],'thread')
        self.assertEqual(self.sent[-1][0]['In-Reply-To'],'<in@test>')
        self.assertEqual(self.st.session_by_thread('thread')['session_id'],SID)

    def test_other_origin_cannot_take_over_reply(self):
        sess=self.st.session_for('codex',str(uuid.uuid4()),origin_host='mac')
        i=self.inbound(sess)
        with self.assertRaises(SystemExit): self.am.rpc_mail(self.request(action='reply',args={'inbound_id':i}),CFG)
        self.assertEqual(self.sent,[])

    def test_mac_send_proxies_despite_local_send_disabled(self):
        a=argparse.Namespace(cmd='send',to='x',subject='x',cc=None,body_file=None,html=None,
                             session=SID,cli='codex',unrouted=False,request_id=None)
        with patch.object(self.am,'resolve_session',return_value=dict(ORIGIN)), patch.object(remote,'call',return_value='sent') as call, patch('sys.stdin',io.StringIO('body')), contextlib.redirect_stdout(io.StringIO()):
            self.am.do_proxy(a,MAC)
        payload=call.call_args.args[2]
        self.assertEqual(payload['origin'],ORIGIN)
        self.assertEqual(payload['text'],'body')
        self.fake.service.assert_not_called()

    def test_proxy_unrouted_is_rejected(self):
        with self.assertRaises(SystemExit): self.am.do_proxy(argparse.Namespace(cmd='send',unrouted=True),MAC)

    def test_unrouted_ssh_bypass_is_rejected_before_gmail(self):
        with patch.dict(os.environ,SSH_CONNECTION='fixture'):
            with self.assertRaises(SystemExit): self.am.do_send(argparse.Namespace(unrouted=True))
        self.fake.service.assert_not_called()


class Transport(Base):
    def test_only_configured_peer_and_stdin_payload(self):
        payload={'op':'search','queries':['$(touch /tmp/forbidden)']}
        with patch.object(subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps({'protocol':1,'ok':True,'result':{}}))) as run:
            remote.call(CFG,'mac',payload)
        cmd=run.call_args.args[0]
        self.assertNotIn('$(touch /tmp/forbidden)',' '.join(cmd))
        self.assertIn('StrictHostKeyChecking=yes',cmd)
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['queries'],payload['queries'])
        with self.assertRaises(remote.RemoteError): remote.call(CFG,'unconfigured',payload)

    def test_timeout_is_bounded_and_not_retried(self):
        with patch.object(subprocess,'run',side_effect=subprocess.TimeoutExpired('ssh',30)) as run:
            with self.assertRaisesRegex(remote.RemoteError,'no automatic retry'): remote.call(CFG,'mac',{'op':'ping'})
        self.assertEqual(run.call_count,1)

    def test_remote_known_delivery_carries_exact_uuid_and_reply_command(self):
        s=self.st.session_for('codex',SID,origin_host='mac')
        with patch.object(remote,'call',return_value={'code':0,'detail':'queued'}) as call:
            self.assertEqual(deliver.deliver_session(s,'agent-mail reply 12',CFG),(0,'queued'))
        self.assertEqual(call.call_args.args[2]['session_id'],SID)
        self.assertIn('--session '+SID,call.call_args.args[2]['text'])

    def test_receiver_never_resumes(self):
        req=dict(protocol=1,source_host='desktop',destination_host='mac',op='deliver',session_id=SID,cli='codex',text='reply')
        with patch.object(config,'load',return_value=MAC), patch.object(deliver,'deliver',return_value=(2,'offline')) as send, patch('sys.stdin',io.StringIO(json.dumps(req))), contextlib.redirect_stdout(io.StringIO()) as out:
            self.am.do_rpc(None)
        self.assertFalse(send.call_args.kwargs['resume'])
        self.assertFalse(send.call_args.kwargs['dangerous'])
        self.assertEqual(json.loads(out.getvalue())['result']['code'],2)

    def test_delivery_helpers_preserve_explicit_permission_choice(self):
        for fn in (deliver.deliver, deliver.deliver_target):
            for dangerous in (False, True):
                with self.subTest(helper=fn.__name__, dangerous=dangerous):
                    with patch.object(subprocess, 'run', return_value=Mock(returncode=0, stdout='', stderr='')) as run:
                        args = ('codex', SID, 'reply') if fn is deliver.deliver else (SID, 'reply')
                        fn(*args, resume=True, dangerous=dangerous)
                    cmd = run.call_args.args[0]
                    self.assertIn('--dangerous' if dangerous else '--no-dangerous', cmd)
                    self.assertNotIn('--no-dangerous' if dangerous else '--dangerous', cmd)

    def test_manual_resume_default_and_opt_out_for_both_clis(self):
        sender = script('agent-send')
        for cli in ('claude', 'codex'):
            for flags, expected in (([], True), (['--no-dangerous'], False), (['--dangerous'], True)):
                with self.subTest(cli=cli, flags=flags):
                    sess = dict(id=SID, name='exited', cli=cli, live=False)
                    with patch.object(sender, 'all_sessions', return_value=[sess]), patch.object(sender, 'resume_' + cli, return_value=(True, 'resumed')) as resume, contextlib.redirect_stdout(io.StringIO()):
                        rc = sender.main(['--exact-id', '--raw', '--cli', cli, '--offline', 'resume'] + flags + [SID, 'reply'])
                    self.assertEqual(rc, 0)
                    self.assertEqual(resume.call_args.args[2], expected)

    def test_default_resume_permissions_do_not_enable_offline_delivery(self):
        sender = script('agent-send')
        for cli in ('claude', 'codex'):
            with self.subTest(cli=cli):
                sess = dict(id=SID, name='exited', cli=cli, live=False)
                with patch.object(sender, 'all_sessions', return_value=[sess]), patch.object(sender, 'resume_' + cli) as resume, contextlib.redirect_stderr(io.StringIO()):
                    rc = sender.main(['--exact-id', '--raw', '--cli', cli, SID, 'reply'])
                self.assertEqual(rc, 2)
                resume.assert_not_called()

    def test_exact_id_cannot_match_another_sessions_name(self):
        sender=script('agent-send')
        wrong=dict(id=str(uuid.uuid4()),name=SID,cli='claude',live=True)
        right=dict(id=SID,name='intended',cli='claude',live=True)
        with patch.object(sender,'all_sessions',return_value=[wrong,right]), patch.object(sender,'send_claude_socket',return_value='') as send, contextlib.redirect_stdout(io.StringIO()):
            rc=sender.main(['--exact-id','--raw','--cli','claude','--offline','fail',SID,'message'])
        self.assertEqual(rc,0); self.assertEqual(send.call_args.args[0]['id'],SID)

    def exact(self, sessions, *flags):
        sender=script('agent-send')
        with patch.object(sender,'all_sessions',return_value=sessions), patch.object(sender,'send_claude_socket',return_value='') as send, patch.object(sender,'resume_claude',return_value=(True,'resumed')) as resume, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            rc=sender.main(['--exact-id','--raw','--cli','claude']+list(flags)+[SID,'message'])
        return rc, send, resume, err.getvalue()

    def test_exact_id_takes_the_live_entry_beside_a_dead_one(self):
        # a SIGKILLed claude (a login move) leaves its entry; unhandled, every delivery would exit 3
        dead=dict(id=SID,name='x',cli='claude',live=False,account='work',updated_at=2)
        live=dict(id=SID,name='x',cli='claude',live=True,account='personal',updated_at=1)
        for order in ([dead,live],[live,dead]):
            rc,send,resume,err=self.exact(order,'--offline','resume')
            self.assertEqual(rc,0,err); self.assertEqual(send.call_args.args[0]['account'],'personal'); resume.assert_not_called()

    def test_exact_id_with_two_live_entries_is_ambiguous(self):
        live=[dict(id=SID,name='x',cli='claude',live=True,account=a,cwd='/w') for a in ('work','personal')]
        rc,send,resume,err=self.exact(live+[dict(live[0],live=False)],'--offline','resume')
        self.assertEqual(rc,3); self.assertEqual(err.count('\n  claude '),2); send.assert_not_called(); resume.assert_not_called()

    def test_exact_id_dead_duplicates_are_one_offline_session(self):
        dead=[dict(id=SID,name='x',cli='claude',live=False,account=a,updated_at=t) for a,t in (('work',5),('personal',9))]
        rc,send,resume,err=self.exact(dead,'--offline','fail')
        self.assertEqual(rc,2,err); self.assertIn('not live',err); resume.assert_not_called()
        rc,send,resume,err=self.exact(dead,'--offline','resume')
        self.assertEqual(rc,0,err); self.assertEqual(resume.call_args.args[0]['account'],'personal'); send.assert_not_called()

    def lookup(self, sessions):
        from agentmail import registry
        req=dict(protocol=1,source_host='desktop',destination_host='mac',op='lookup',session_id=SID,cli='claude')
        with patch.object(config,'load',return_value=MAC), patch.object(registry,'all_sessions',return_value=sessions), patch('sys.stdin',io.StringIO(json.dumps(req))), contextlib.redirect_stdout(io.StringIO()) as out:
            self.am.do_rpc(None)
        return json.loads(out.getvalue())

    def test_lookup_rpc_takes_the_live_entry_beside_a_dead_one(self):
        dead=dict(id=SID,name='old',cli='claude',live=False,cwd='/w')
        live=dict(dead,name='x',live=True)
        self.assertEqual(self.lookup([dead,live])['result'],dict(cli='claude',id=SID,name='x',cwd='/w',live=True))
        self.assertEqual(self.lookup([dead])['result']['name'],'old')   # offline, but present
        self.assertFalse(self.lookup([live,dict(live,name='y')])['ok'])   # two live: not unique

    def test_stale_codex_lock_does_not_resume(self):
        sender=script('agent-send')
        sess=dict(id=SID,name='stale',cli='codex',live=True)
        with patch.object(sender,'all_sessions',return_value=[sess]), patch.object(sender,'queue_codex',return_value=(False,'No active session')), patch.object(sender,'resume_codex') as resume:
            rc=sender.main(['--exact-id','--raw','--cli','codex','--offline','fail',SID,'message'])
        self.assertEqual(rc,2); resume.assert_not_called()

    def test_codex_queue_invocation_preserves_exact_text(self):
        sender=script('agent-send')
        with patch.object(sender,'codex_bin',return_value='/mock/codex'), patch.object(subprocess,'run',return_value=Mock(returncode=0,stdout='Queued message',stderr='')) as run:
            ok,_=sender.queue_codex({'id':SID},'reply\n$(literal)')
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args[0],['/mock/codex','queue','--thread',SID,'--message','reply\n$(literal)'])

    def test_claude_socket_receives_message(self):
        import socket
        import threading
        sender=script('agent-send')
        with tempfile.TemporaryDirectory(prefix='am-sock-',dir='/tmp') as d:
            path=str(Path(d)/'inbox')
            listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            listener.bind(path); listener.listen(1); listener.settimeout(3)
            received=[]
            def receive():
                conn,_=listener.accept()
                with conn:
                    received.append(conn.recv(65536))
                    conn.sendall(b'ok')
            worker=threading.Thread(target=receive); worker.start()
            try:
                sender.send_claude_socket({'id':SID,'socket':path},'fixture reply')
                worker.join(3)
                self.assertEqual(json.loads(received[0])['message']['content'],'fixture reply')
            finally:
                listener.close()


class RoutingFailures(Base):
    def test_unreachable_mac_retains_mail_and_notifies_once(self):
        sess=self.st.session_for('codex',SID,origin_host='mac'); self.message(sess)
        with patch.object(remote,'call',side_effect=remote.RemoteError('mac: connection refused')):
            self.room.process(self.fake,self.st,CFG,'in',False)
        row=self.st.list_inbound()[0]
        self.assertEqual(row['action'],'held'); self.assertTrue(row['notified_at'])
        body=self.sent[0][0].get_body(('plain',)).get_content()
        self.assertIn('connection refused',body); self.assertIn(SID,body)
        recovery.notify(self.fake,self.st,CFG,row)
        self.assertEqual(len(self.sent),1)

    def test_remote_success_means_queued_not_read(self):
        sess=self.st.session_for('codex',SID,origin_host='mac'); self.message(sess)
        with patch.object(remote,'call',return_value={'code':0,'detail':'queued'}):
            self.room.process(self.fake,self.st,CFG,'in',False)
        self.assertEqual(self.st.list_inbound()[0]['action'],'queued')

    def test_unknown_sender_does_not_trigger_remote(self):
        sess=self.st.session_for('codex',SID,origin_host='mac'); self.message(sess,'stranger@example.com')
        with patch.object(remote,'call') as call:
            self.room.process(self.fake,self.st,CFG,'in',False)
        call.assert_not_called()

    def orchestrate(self):
        info={'ok':True,'cost':0,'turns':1,'duration_s':1,'error':'','result':'done'}
        local={'host':'desktop','candidates':[]}
        with patch.object(self.room.orchestrate,'run_event',return_value=info) as run, patch.object(subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(local))), patch.object(remote,'call',side_effect=remote.RemoteError('Mac offline')):
            self.room.process(self.fake,self.st,CFG,'in',False)
        return self.st.list_inbound()[0], run.call_args[0][0]

    def assert_held_unowned(self, row, event):
        self.assertEqual(row['action'],'held'); self.assertTrue(row['recovery_json']); self.assertTrue(row['notified_at'])
        self.assertIn('unowned reply',event); self.assertIn(CFG['labels']['held'],[l for a,kw in self.mods for l in kw.get('add',[])])

    def test_unowned_reply_not_resolved_by_successful_orchestrator(self):
        # A session's tagged mail whose session can no longer be resolved.
        self.message(); self.st.record_sent('gonetag2','out','thread','<old@test>','x','specific subject text')
        self.assert_held_unowned(*self.orchestrate())

    def test_reply_to_unrouted_mail_goes_to_orchestrator_without_notice(self):
        # Mail 502: cloud-watchdog sent with --unrouted (tag ""), Alex replied, nobody else wrote.
        self.message(); self.st.record_sent('','w1','thread','<watchdog@test>',CFG['allowlist'][0],'[cloud-watchdog] Daily')
        self.fake.thread_messages.return_value=[({'from':CFG['account']},'report',1,'w1'),
                                               ({'from':'owner@example.com'},'close it',2,'in')]
        with patch.object(recovery,'recover') as recover, patch.object(recovery,'notify') as notify:
            row,event=self.orchestrate()
        recover.assert_not_called(); notify.assert_not_called()
        self.assertEqual(row['action'],'delivered'); self.assertFalse(row['recovery_json']); self.assertFalse(row['notified_at'])
        self.assertNotIn('unowned reply',event); self.assertEqual(self.sent,[])
        self.assertNotIn(CFG['labels']['held'],[l for a,kw in self.mods for l in kw.get('add',[])])

    def test_unrouted_thread_with_an_unrecorded_own_message_is_held(self):
        self.message(); self.st.record_sent('','w1','thread','<watchdog@test>','x','specific subject text')
        self.fake.thread_messages.return_value=[({'from':CFG['account']},'report',1,'w1'),
                                               ({'from':CFG['account']},'session request',2,'lost'),
                                               ({'from':'owner@example.com'},'answer',3,'in')]
        self.assert_held_unowned(*self.orchestrate())

    def test_own_message_with_no_sent_row_is_held(self):
        self.message()
        self.fake.thread_messages.return_value=[({'from':CFG['account']},'session request',1,'lost'),
                                               ({'from':'owner@example.com'},'answer',2,'in')]
        self.assert_held_unowned(*self.orchestrate())

    def test_reply_to_notification_goes_to_orchestrator_without_recovery(self):
        self.message()
        self.fake.get_full.return_value['headers']['in-reply-to']='<alert@test>'
        self.fake.thread_messages.return_value=[({'from':CFG['account'],'x-agent-mailroom':'alert'},'notice',1,'a'),
                                               ({'from':'owner@example.com'},'answer',2,'b')]
        info={'ok':True,'cost':0,'turns':1,'duration_s':1,'error':'','result':'done'}
        with patch.object(self.room.orchestrate,'run_event',return_value=info), patch.object(recovery,'recover') as recover:
            self.room.process(self.fake,self.st,CFG,'in',False)
        recover.assert_not_called()
        self.assertEqual(self.st.list_inbound()[0]['action'],'delivered')

    def test_collaborator_release_retains_guard(self):
        cfg=dict(CFG,collaborators={'collab@gmail.com':{'owner':'Someone'}})
        sess=self.st.session_for('codex',SID,origin_host='mac'); i=self.inbound(sess)
        self.st.update_inbound(i,from_addr='collab@gmail.com')
        with patch.object(config,'load',return_value=cfg), patch.object(deliver,'deliver_session',return_value=(0,'queued')) as send, contextlib.redirect_stdout(io.StringIO()):
            self.am.do_release(argparse.Namespace(inbound_id=i,target='origin',cli=None))
        self.assertIn(routing.collab_guard('Alex'),send.call_args.args[1])

    def test_explicit_mac_handoff_records_the_new_owner(self):
        i=self.inbound()
        with patch.object(remote,'call',side_effect=[dict(ORIGIN,live=True),{'code':0,'detail':'queued'}]) as call, contextlib.redirect_stdout(io.StringIO()):
            self.am.do_release(argparse.Namespace(inbound_id=i,target=SID,cli='codex',host='mac'))
        row=self.st.inbound(i);owner=self.st.session_by_tag(row['matched_tag'])
        self.assertEqual((owner['origin_host'],owner['session_id']),('mac',SID))
        self.assertEqual(row['action'],'queued');self.assertEqual(row['match_rule'],'manual')
        self.assertEqual([c.args[2]['op'] for c in call.call_args_list],['lookup','deliver'])


class Search(Base):
    def test_exited_transcript_found_without_content_disclosure(self):
        base=Path(TMP.name)/str(uuid.uuid4()); cx=base/'codex'; cl=base/'claude'
        path=cx/'sessions'/'old.jsonl'; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'payload':{'id':SID,'cwd':'/work'}})+'\n'+json.dumps({'text':'specific artifact phrase; private content not returned'}))
        with patch.object(recovery.registry,'CODEX_HOME',cx), patch.object(recovery.registry,'claude_homes',return_value=[('default',cl,None)]), patch.object(recovery.registry,'all_sessions',return_value=[]):
            result=recovery.search(['specific artifact phrase'],CFG)
        self.assertEqual(result['candidates'][0]['session_id'],SID)
        self.assertNotIn('private content',json.dumps(result))
        self.assertFalse(result['candidates'][0]['live_hint'])

    def test_saved_recovery_does_not_repeat_search(self):
        i=self.inbound(); self.st.update_inbound(i,recovery_json=json.dumps({'status':'needs_handoff'}))
        with patch.object(remote,'call') as call:
            result=recovery.recover(self.st,CFG,self.st.inbound(i))
        call.assert_not_called(); self.assertEqual(result['status'],'needs_handoff')


if __name__ == '__main__':
    unittest.main()
