"""Host routing and recovery regressions. No real Gmail or agent turns."""
import argparse
import base64
import contextlib
import email
import email.policy
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.TemporaryDirectory()
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import config, deliver, listing, mailroom, outbound, recovery, registry, remote, routing, rpc, send
from agentmail.store import Store

SID = '12345678-1234-4234-8234-123456789abc'
ORIGIN = dict(host='mac', cli='codex', id=SID, name='example', cwd='/Users/alex/code/example')
# config.json on the desktop and on the Mac, and what config.load makes of each
CFG_FILE = dict(remote_hosts={'mac': 'mac'}, recovery_hosts=['mac'])
MAC_FILE = dict(CFG_FILE, host_id='mac', mail_proxy='desktop', send_enabled=False, remote_hosts={'desktop': 'desktop'})
CFG = dict(config.load(), **CFG_FILE)
MAC = dict(config.load(), **MAC_FILE)


class Base(unittest.TestCase):
    def setUp(self):
        self.st = Store(Path(TMP.name) / (str(uuid.uuid4()) + '.sqlite'))
        self.homes = homes.Homes(self, cfg=CFG_FILE)
        self.am = load_script('agent-mail')
        self.room = load_script('agent-mailroom')
        self.patches = []
        p = patch.object(outbound, 'Store', lambda: self.st); p.start(); self.patches.append(p)
        self.sent = []
        self.mods = []
        self.fake = fakes.fake_gmail()
        self.fake.service.return_value = 'svc'
        self.fake.send_raw.side_effect = self.send
        self.fake.get_metadata.side_effect = lambda *a: {'payload': {'headers': [{'name': 'Message-ID', 'value': '<sent@test>'}]}}
        self.fake.thread_messages.return_value = []
        self.fake.ensure_label.side_effect = lambda svc, x: x
        self.fake.modify.side_effect = lambda *a, **kw: self.mods.append((a, kw))
        for obj in (self.am, outbound, self.room, recovery.alerts, mailroom):
            p = patch.object(obj, 'gmail', self.fake); p.start(); self.patches.append(p)
        self.ctx = outbound.Context(CFG)

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

    def message(self, session=None, sender='owner@example.com', subject='Re: exact example request', dkim='pass',
                headers=(), text='reply'):
        """Gmail's answer to get_full: a mail from sender with subject, to session's tagged address when given, with
        more headers ((name, value) pairs), Gmail's Authentication-Results giving DKIM dkim for the sender's domain,
        and the text given."""
        hdrs=[('From',sender),('Subject',subject),('Message-ID','<in@test>')]
        if session: hdrs.append(('To',routing.tagged_address(CFG['account'],session['tag'])))
        hdrs+=list(headers)+[('Authentication-Results',fakes.auth_header(sender.rsplit('@',1)[1],dkim))]
        self.fake.get_full.return_value=fakes.gmail_message(hdrs,[fakes.text_part(text)])


class HostedStore(Base):
    def test_same_uuid_and_name_on_two_hosts_are_distinct(self):
        a=self.st.session_for('codex',SID,'same',origin_host='desktop')
        b=self.st.session_for('codex',SID,'same',origin_host='mac')
        self.assertNotEqual(a['tag'],b['tag'])
        self.st.record_sent(b['tag'],'g','t','<m@test>','x','y',cc_addr=None,body=None,eml=None,attachments=None)
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
        self.assertEqual(migrated.session_for('codex',SID,origin_host='desktop')['tag'],'oldtag')
        self.assertNotEqual(migrated.session_for('codex',SID,origin_host='mac')['tag'],'oldtag')
        migrated.con.close()
        Store(p).con.close()

    def test_texts_name_another_host_than_the_configured_one(self):
        here=self.st.session_for('codex',str(uuid.uuid4()),'here',origin_host='linuxbox')
        there=self.st.session_for('codex',str(uuid.uuid4()),'there',origin_host='desktop')
        self.assertEqual([outbound.session_label(dict(s),'linuxbox') for s in (here,there)],
                         ['here (codex)','there (codex@desktop)'])
        for s in (here,there): self.st.record_sent(s['tag'],'g'+s['tag'],'t','<%s@test>'%s['tag'],'x','y',
                                                   cc_addr=None,body=None,eml=None,attachments=None)
        self.assertEqual(sorted(line.split()[2] for line in listing.sent(self.st,5,'linuxbox').splitlines()),
                         ['codex','codex@desktop'])
        self.assertEqual(listing.receipt(self.st,str(uuid.uuid4()),'linuxbox'),'request not recorded on linuxbox\n')


class ProxyMail(Base):
    def test_proxy_send_records_origin_and_html_before_reply(self):
        with patch.object(registry,'all_sessions',side_effect=AssertionError('must not resolve Mac UUID on desktop')), \
                patch.object(registry,'claude_sessions',side_effect=AssertionError('must not resolve Mac UUID on desktop')):
            rpc.mail(self.ctx,self.request())
        row=self.st.session_by_thread('thread')
        self.assertEqual((row['origin_host'],row['session_id'],row['cwd']),('mac',SID,ORIGIN['cwd']))
        msg=self.sent[0][0]
        self.assertEqual(msg.get_body(('html',)).get_content().strip(),'<p>html</p>')
        self.assertIn('$(not-a-command)',msg.get_body(('plain',)).get_content())

    def test_a_proxied_send_to_alex_by_name_goes_to_his_address(self):
        # the Mac passes the name as typed (WireFormat.test_mac_requests); the desktop reads it with its own config
        out=rpc.mail(self.ctx,self.request(args={'to':'alex','cc':None,'subject':'example'}))
        self.assertEqual(self.sent[0][0]['To'],'owner@example.com')
        self.assertTrue(out.startswith('sent to owner@example.com | '))

    def test_a_proxied_mail_over_the_size_cap_is_refused_before_sending(self):
        data = b'%PDF-1.4 ' + b'x' * 64
        files = [{'name': 'a.pdf', 'data': base64.b64encode(data).decode()},
                 {'name': 'b.pdf', 'data': base64.b64encode(data).decode()}]
        with patch.object(outbound, 'MAX_IMAGES_BYTES', len(data) + 1), \
                self.assertRaisesRegex(outbound.MailError, 'images and attachments total over'):
            rpc.mail(self.ctx, self.request(files=files))
        self.assertEqual(self.sent, [])

    def test_same_request_does_not_send_twice(self):
        req=self.request()
        first=rpc.mail(self.ctx,req)
        self.assertEqual(rpc.mail(self.ctx,req),first)
        self.assertEqual(len(self.sent),1)
        req['text']='changed'
        with self.assertRaises(ValueError): rpc.mail(self.ctx,req)

    def test_uncertain_send_is_not_automatically_retried(self):
        req=self.request()
        self.fake.send_raw.side_effect=TimeoutError('lost response')
        with self.assertRaises(TimeoutError): rpc.mail(self.ctx,req)
        with self.assertRaisesRegex(ValueError,'uncertain'): rpc.mail(self.ctx,req)
        self.assertEqual(self.fake.send_raw.call_count,1)

    def test_a_proxied_image_is_checked_as_agent_mails_own(self):
        png=base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x'*8).decode()
        for name,data,error in (('fig',base64.b64encode(b'not an image').decode(),'image fig is not a PNG'),
                                ('a b',png,"image name 'a b'")):
            with self.assertRaisesRegex(outbound.MailError,error):
                rpc.mail(self.ctx,self.request(images=[{'name':name,'data':data}]))
        self.assertEqual(self.fake.send_raw.call_count,0)

    def test_receipt_reports_the_recorded_outcome(self):
        req=self.request(); result=rpc.mail(self.ctx,req)
        for rid,expected in ((req['request_id'],json.dumps({'status':'complete','result':result})),
                             (str(uuid.uuid4()),'request not recorded on desktop')):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.am.do_receipt(self.ctx,argparse.Namespace(request_id=rid))
            self.assertEqual(out.getvalue().strip(),expected)

    def test_remote_reply_preserves_owner_and_thread(self):
        rpc.mail(self.ctx,self.request())
        sess=self.st.session_by_thread('thread'); i=self.inbound(sess)
        rpc.mail(self.ctx,self.request(action='reply',args={'inbound_id':i}))
        self.assertEqual(self.sent[-1][1],'thread')
        self.assertEqual(self.sent[-1][0]['In-Reply-To'],'<in@test>')
        self.assertEqual(self.st.session_by_thread('thread')['session_id'],SID)

    def test_other_origin_cannot_take_over_reply(self):
        sess=self.st.session_for('codex',str(uuid.uuid4()),origin_host='mac')
        i=self.inbound(sess)
        with self.assertRaisesRegex(ValueError,'not owned by this origin'): rpc.mail(self.ctx,self.request(action='reply',args={'inbound_id':i}))
        self.assertEqual(self.sent,[])

    def test_mac_send_proxies_despite_local_send_disabled(self):
        body=Path(tempfile.mkdtemp(dir=TMP.name))/'body.txt'; body.write_text('body')
        a=argparse.Namespace(cmd='send',to='x',subject='x',cc=None,body_file=str(body),html=None,image=None,attach=None,
                             no_review=None,session=SID,cli='codex',unrouted=False,request_id=None)
        with patch.object(self.am,'resolve_session',return_value=dict(ORIGIN)), patch.object(remote,'call',return_value='sent') as call, contextlib.redirect_stdout(io.StringIO()):
            self.am.do_proxy(outbound.Context(MAC),a)
        payload=call.call_args.args[2]
        self.assertEqual(payload['origin'],ORIGIN)
        self.assertEqual(payload['text'],'body')
        self.fake.service.assert_not_called()

    def test_proxy_unrouted_is_rejected(self):
        with self.assertRaises(SystemExit): self.am.do_proxy(outbound.Context(MAC),argparse.Namespace(cmd='send',unrouted=True))

    def test_unrouted_ssh_bypass_is_rejected_before_gmail(self):
        with patch.dict(os.environ,SSH_CONNECTION='fixture'):
            with self.assertRaises(SystemExit): self.am.do_send(self.ctx,argparse.Namespace(unrouted=True))
        self.fake.service.assert_not_called()


class WireFormat(Base):
    """What the Mac's agent-mail sends and the desktop's answers, byte for byte, through main() on both sides. The
    machines are updated one at a time, so an older agent-mail on either side must keep working: these literals are
    the format the copies deployed on both machines speak."""
    RID = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'
    ORIGIN_JSON = ('"origin": {"host": "mac", "cli": "codex", "id": "12345678-1234-4234-8234-123456789abc", '
                   '"name": "example", "cwd": "/Users/alex/code/example"}')
    TAIL = '"protocol": 1, "source_host": "mac", "destination_host": "desktop"}'
    PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='

    def client(self, argv, result):
        """agent-mail on the Mac: (exit code, the request it sent, what it printed)."""
        # the Mac's config, and its address book: one live Codex thread
        homes.Homes(self, cfg=MAC_FILE).codex(SID, 'example', cwd=ORIGIN['cwd'])
        response = json.dumps({'protocol': 1, 'ok': True, 'result': result})
        # subprocess.run stands in for ssh to the desktop
        with patch.object(outbound, 'Store', side_effect=AssertionError('a proxied command opens no store')), \
                patch.object(subprocess, 'run', return_value=Mock(returncode=0, stdout=response, stderr='')) as run, \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            rc = self.am.main(argv)
        return rc, run.call_args.kwargs['input'], out.getvalue()

    def rpc(self, req):
        """agent-mail rpc on the desktop: what it printed for one request."""
        with patch('sys.stdin', io.StringIO(json.dumps(req))), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.am.main(['rpc']), 0)
        return out.getvalue()

    def test_mac_requests(self):
        d = Path(tempfile.mkdtemp(dir=TMP.name))
        (d / 'b.txt').write_text('body\n')
        (d / 'b.html').write_text('<p>html</p>')
        (d / 'p.pdf').write_bytes(b'%PDF-1.4 x')
        (d / 'f.png').write_bytes(base64.b64decode(self.PNG))
        body, rid = ['--body-file', str(d / 'b.txt')], ['--request-id', self.RID]
        self.assertEqual(self.client(['send', '--to', 'owner@example.com', '--subject', 'example', '--session', SID,
                                      '--cli', 'codex', '--html', str(d / 'b.html'), '--image', 'fig=%s' % (d / 'f.png'),
                                      '--attach', str(d / 'p.pdf')] + body + rid, 'sent to x\n'),
                         (0, '{"op": "mail", "action": "send", "args": {"to": "owner@example.com", "cc": null, '
                             '"subject": "example", "no_review": null}, ' + self.ORIGIN_JSON + ', "html": "<p>html</p>", '
                             '"text": "body\\n", "images": [{"name": "fig", "data": "' + self.PNG + '"}], "files": '
                             '[{"name": "p.pdf", "data": "JVBERi0xLjQgeA=="}], "request_id": "' + self.RID + '", '
                             + self.TAIL, 'sent to x\n'))
        self.assertEqual(self.client(['send', '--to', 'a@b', '--subject', 's', '--session', SID] + body + rid,
                                     'agent-mail: NOT SENT: x\n'),
                         (3, '{"op": "mail", "action": "send", "args": {"to": "a@b", "cc": null, "subject": "s", '
                             '"no_review": null}, ' + self.ORIGIN_JSON + ', "html": null, "text": "body\\n", "images": [], '
                             '"files": [], "request_id": "' + self.RID + '", ' + self.TAIL, 'agent-mail: NOT SENT: x\n'))
        # the name as typed: the desktop reads it
        self.assertIn('"args": {"to": "alex", "cc": "Alex", "subject": "s", ',
                      self.client(['send', '--to', 'alex', '--cc', 'Alex', '--subject', 's', '--session', SID] + body
                                  + rid, 'x\n')[1])
        self.assertEqual(self.client(['reply', '12', '--session', SID, '--no-review', 'urgent'] + body + rid, 'replied\n')[1],
                         '{"op": "mail", "action": "reply", "args": {"inbound_id": 12, "no_review": "urgent"}, '
                         + self.ORIGIN_JSON + ', "html": null, "text": "body\\n", "images": [], "files": [], '
                         '"request_id": "' + self.RID + '", ' + self.TAIL)
        # --subject, a later flag (rpc.LATER_FLAGS), travels only when given: the request above has kept its format
        self.assertIn('"args": {"inbound_id": 12, "no_review": null, "subject": "the fix"}, ',
                      self.client(['reply', '12', '--session', SID, '--subject', 'the fix'] + body + rid, 'replied\n')[1])
        for argv, args, printed in ((['inbox', '--held'], '{"limit": 50, "held": true}', ''),
                                    (['sent', '--limit', '5'], '{"limit": 5}', 'x'),
                                    (['show', '7'], '{"inbound_id": 7}', 'x\n'),
                                    (['receipt', self.RID], '{"request_id": "%s"}' % self.RID, 'x\n')):
            self.assertEqual(self.client(argv, printed),
                             (0, '{"op": "mail", "action": "%s", "args": %s, ' % (argv[0], args) + self.TAIL,
                              printed if printed.endswith('\n') else printed + '\n'))
        self.assertEqual(self.client(['withdraw', '--session', SID], 'x\n')[1],
                         '{"op": "mail", "action": "withdraw", "args": {}, ' + self.ORIGIN_JSON + ', ' + self.TAIL)

    def test_desktop_results(self):
        def ok(result):
            return json.dumps({'protocol': 1, 'ok': True, 'result': result}) + '\n'
        sent = self.rpc(self.request(request_id=self.RID))
        tag = self.st.session_by_thread('thread')['tag']
        summary = ("sent to owner@example.com | subject 'example [#TAG]' | thread thread | tag TAG | "
                   "from example (codex@mac)\n").replace('TAG', tag)
        self.assertEqual(sent, '{"protocol": 1, "ok": true, "result": "%s\\n"}\n' % summary[:-1])
        self.assertEqual(self.rpc(self.request(text='key sk-ant-' + 'a' * 30, request_id=str(uuid.uuid4()))), ok(
            "agent-mail: NOT SENT:\n  1. Contains what looks like an Anthropic or OpenAI API key ('sk-ant...'). Never "
            "mail credentials: remove it. If it is not a secret, send with --no-review \"why\".\n\nNothing was kept: "
            "fix it and run your send/reply command again.\n"))
        i = self.st.record_inbound(gmail_id='g-in', thread_id='thread', message_id='<in@test>', from_addr=CFG['allowlist'][0],
                                   from_name='Alex', subject='Re: exact example request', received_at=1700000000,
                                   matched_tag=tag, match_rule='tag', action='held', detail='why it is held', body='reply body')
        self.assertEqual(self.rpc(self.request(action='reply', args={'inbound_id': i, 'no_review': None},
                                               request_id=str(uuid.uuid4()))),
                         ok("replied to owner@example.com | subject 'Re: exact example request [#%s]' | thread "
                            "thread | from example (codex@mac)\n" % tag))
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(1700000000))
        self.assertEqual(self.rpc(self.request(action='inbox', args={'limit': 50, 'held': True})),
                         ok('%-4d %s  held      tag            owner@example.com              Re: exact example request\n'
                            '     why it is held\n' % (i, when)))
        self.assertEqual(self.rpc(self.request(action='inbox', args={'limit': 0, 'held': False})),
                         ok('no inbound mail seen yet\n'))
        self.assertEqual(self.rpc(self.request(action='show', args={'inbound_id': i})), ok(
            'id             %d\ngmail_id       g-in\nthread_id      thread\nmessage_id     <in@test>\nfrom_addr      '
            'owner@example.com\nfrom_name      Alex\nsubject        Re: exact example request\nreceived_at    '
            '1700000000\nmatched_tag    %s\nmatch_rule     tag\naction         held\ndetail         why it is held\n'
            'released_to    None\nreleased_at    None\nrecovery_json  None\nnotified_at    None\neml            None\n'
            'body:\nreply body\n'
            % (i, tag)))
        self.st.con.execute('update sent set sent_at=1700000000')
        self.assertEqual(self.rpc(self.request(action='sent', args={'limit': 30})), ok(
            '%s  codex@mac example                      -> owner@example.com              example [#%s]\n'
            '%s  codex@mac example                      -> owner@example.com              Re: exact example '
            'request [#%s]\n' % (when, tag, when, tag)))
        self.assertEqual(self.rpc(self.request(action='receipt', args={'request_id': self.RID})),
                         ok(json.dumps({'status': 'complete', 'result': summary}) + '\n'))
        self.assertEqual(self.rpc(self.request(action='receipt', args={'request_id': str(uuid.uuid4())})),
                         ok('request not recorded on desktop\n'))
        self.assertEqual(self.rpc(self.request(action='withdraw', args={})),
                         ok('nothing to withdraw: no mail from this session is waiting on a revision\n'))
        self.assertEqual(self.rpc(self.request(action='status', args={})),
                         '{"protocol": 1, "ok": false, "error": "unsupported proxy mail action"}\n')
        self.assertEqual(self.rpc(self.request(origin=dict(ORIGIN, host='elsewhere'))),
                         '{"protocol": 1, "ok": false, "error": "origin host does not match source host"}\n')
        self.assertEqual(self.rpc(self.request(action='reply', args={'inbound_id': i, 'subject': 'the fix'},
                                               request_id=str(uuid.uuid4()))),
                         ok("replied to owner@example.com | subject 'the fix [#%s]' | new thread thread | from "
                            "example (codex@mac)\n" % tag))
        self.assertEqual((self.sent[-1][1], self.sent[-1][0]['In-Reply-To']), (None, None))   # not in the old thread

    def test_errors_say_what_went_wrong(self):
        # the error says what went wrong, never just the exit code agent-mail would stop with ("4", "1")
        def error(req):
            return json.loads(self.rpc(req))['error']
        self.assertEqual(error(self.request(action='show', args={'inbound_id': 99999})), 'no inbound mail with id 99999')
        other = self.st.session_for('codex', str(uuid.uuid4()), origin_host='mac')
        self.assertEqual(error(self.request(action='reply', args={'inbound_id': self.inbound(other)})),
                         'reply is not owned by this origin session; ask Alex for an explicit handoff')
        self.assertEqual(error(self.request(action='inbox', args={'limit': 5, 'held': False, 'to': 'x'})),
                         'invalid proxy arguments')     # a flag no agent-mail passes for this command
        self.assertEqual(self.sent, [])


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
        self.assertEqual(cmd[-2:],['mac','~/.local/bin/agent-mail rpc'])
        with patch.object(subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps({'protocol':1,'ok':True,'result':{}}))) as run:
            remote.call(dict(CFG,remote_command='/opt/mail/bin/agent-mail rpc'),'mac',payload)
        self.assertEqual(run.call_args.args[0][-1],'/opt/mail/bin/agent-mail rpc')

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
        homes.config_file(self,MAC_FILE)
        # deliver runs agent-send, which would reach a live session
        with patch.object(deliver,'deliver',return_value=(2,'offline')) as send, patch('sys.stdin',io.StringIO(json.dumps(req))), contextlib.redirect_stdout(io.StringIO()) as out:
            self.am.main(['rpc'])
        self.assertFalse(send.call_args.kwargs['resume'])
        self.assertFalse(send.call_args.kwargs['dangerous'])
        self.assertEqual(json.loads(out.getvalue())['result']['code'],2)

    def test_delivery_preserves_explicit_permission_choice(self):
        for dangerous in (False, True):
            with self.subTest(dangerous=dangerous):
                with patch.object(subprocess, 'run', return_value=Mock(returncode=0, stdout='', stderr='')) as run:
                    deliver.deliver('codex', SID, 'reply', resume=True, dangerous=dangerous)
                cmd = run.call_args.args[0]
                self.assertIn('--dangerous' if dangerous else '--no-dangerous', cmd)
                self.assertNotIn('--no-dangerous' if dangerous else '--dangerous', cmd)

    def exited(self, cli):
        """The registry holds one exited session, SID, of cli: a Claude entry whose claude is gone, or a Codex
        thread with no lock held, its folder still there."""
        getattr(homes.Homes(self), cli)(SID, 'exited', live=False, cwd=str(homes.temp_dir(self)))

    def test_manual_resume_default_and_opt_out_for_both_clis(self):
        sender = load_script('agent-send')
        for cli in ('claude', 'codex'):
            self.exited(cli)
            for flags, expected in (([], True), (['--no-dangerous'], False), (['--dangerous'], True)):
                with self.subTest(cli=cli, flags=flags):
                    # resume_<cli> starts the CLI (the boundary)
                    with patch.object(sender, 'resume_' + cli, return_value=(True, 'resumed')) as resume, contextlib.redirect_stdout(io.StringIO()):
                        rc = sender.main(['--exact-id', '--raw', '--cli', cli, '--offline', 'resume'] + flags + [SID, 'reply'])
                    self.assertEqual(rc, 0)
                    self.assertEqual(resume.call_args.args[2], expected)

    def test_manual_resume_without_a_flag_follows_config(self):
        sender = load_script('agent-send')
        for cli in ('claude', 'codex'):
            self.exited(cli)
            for configured in (False, True):
                with self.subTest(cli=cli, configured=configured):
                    homes.config_file(self, dict(CFG_FILE, resume_dangerous=configured))
                    with patch.object(sender, 'resume_' + cli, return_value=(True, 'resumed')) as resume, contextlib.redirect_stdout(io.StringIO()):
                        rc = sender.main(['--exact-id', '--raw', '--cli', cli, '--offline', 'resume', SID, 'reply'])
                    self.assertEqual(rc, 0)
                    sess_, text, dangerous, accepted = resume.call_args.args
                    self.assertEqual(dangerous, configured)
                    self.assertTrue(callable(accepted))

    def test_agent_send_is_this_checkouts(self):
        self.assertEqual(deliver.agent_send_bin(), str(ROOT / 'bin' / 'agent-send'))
        with patch.object(deliver, 'AGENT_SEND', ROOT / 'bin' / 'no-such-agent-send'):
            with self.assertRaisesRegex(RuntimeError, 'not found'):
                deliver.agent_send_bin()

    def test_default_resume_permissions_do_not_enable_offline_delivery(self):
        sender = load_script('agent-send')
        for cli in ('claude', 'codex'):
            with self.subTest(cli=cli):
                self.exited(cli)
                with patch.object(sender, 'resume_' + cli) as resume, contextlib.redirect_stderr(io.StringIO()):
                    rc = sender.main(['--exact-id', '--raw', '--cli', cli, SID, 'reply'])
                self.assertEqual(rc, 2)
                resume.assert_not_called()

    def test_exact_id_cannot_match_another_sessions_name(self):
        sender=load_script('agent-send')
        self.homes.claude(str(uuid.uuid4()),SID)                  # another session, named like SID's id
        self.homes.claude(SID,'intended')
        # the session's inbox socket is the boundary
        with patch.object(sender,'send_claude_socket',return_value='') as send, contextlib.redirect_stdout(io.StringIO()):
            rc=sender.main(['--exact-id','--raw','--cli','claude','--offline','fail',SID,'message'])
        self.assertEqual(rc,0); self.assertEqual(send.call_args.args[0]['id'],SID)

    def lookup(self, *entries):
        """The Mac's answer to the desktop's lookup of SID, its registry holding these Claude entries of SID:
        (name, live) each."""
        h=homes.Homes(self,cfg=MAC_FILE)
        for name,live in entries: h.claude(SID,name,live=live,cwd='/w')
        req=dict(protocol=1,source_host='desktop',destination_host='mac',op='lookup',session_id=SID,cli='claude')
        with patch('sys.stdin',io.StringIO(json.dumps(req))), contextlib.redirect_stdout(io.StringIO()) as out:
            self.am.main(['rpc'])
        return json.loads(out.getvalue())

    def test_lookup_rpc_takes_the_live_entry_beside_a_dead_one(self):
        self.assertEqual(self.lookup(('old',False),('x',True))['result'],dict(cli='claude',id=SID,name='x',cwd='/w',live=True))
        self.assertEqual(self.lookup(('old',False))['result']['name'],'old')   # offline, but present
        self.assertFalse(self.lookup(('x',True),('y',True))['ok'])   # two live: not unique

    def test_stale_codex_lock_does_not_resume(self):
        sender=load_script('agent-send')
        self.homes.codex(SID,'stale')            # its lock held, so the registry calls it live
        # codex queue, the boundary, finds no session behind the lock
        with patch.object(sender,'queue_codex',return_value=(False,'No active session')), patch.object(sender,'resume_codex') as resume:
            rc=sender.main(['--exact-id','--raw','--cli','codex','--offline','fail',SID,'message'])
        self.assertEqual(rc,2); resume.assert_not_called()

    def test_codex_queue_invocation_preserves_exact_text(self):
        with patch.object(send,'codex_bin',return_value='/mock/codex'), patch.object(subprocess,'run',return_value=Mock(returncode=0,stdout='Queued message',stderr='')) as run:
            ok,_=send.queue_codex({'id':SID},'reply\n$(literal)')
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args[0],['/mock/codex','queue','--thread',SID,'--message','reply\n$(literal)'])

    def inbox(self, answer):
        """send_claude_socket to a real Unix socket standing in for a session's inbox, whose end of each connection
        answer(conn) handles. Returns the acknowledgement, or raises what send_claude_socket raised."""
        import socket
        import threading
        with tempfile.TemporaryDirectory(prefix='am-sock-',dir='/tmp') as d:
            path=str(Path(d)/'inbox')
            listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            listener.bind(path); listener.listen(1); listener.settimeout(3)
            def serve():
                conn,_=listener.accept()
                with conn:
                    answer(conn)
            worker=threading.Thread(target=serve); worker.start()
            try:
                return send.send_claude_socket({'id':SID,'socket':path},'fixture reply')
            finally:
                worker.join(3)
                listener.close()

    def test_claude_socket_receives_message(self):
        received=[]
        def answer(conn):
            received.append(conn.recv(65536))
            conn.sendall(b'ok')
        self.assertEqual(self.inbox(answer),'ok')
        self.assertEqual(json.loads(received[0])['message']['content'],'fixture reply')

    def test_claude_socket_silent_after_the_message_is_sent(self):
        def answer(conn):
            conn.recv(65536)
            time.sleep(1)                   # past the half second send_claude_socket waits for an acknowledgement
        self.assertEqual(self.inbox(answer),'')

    def test_claude_socket_reset_is_a_failed_send(self):
        # the inbox closes with the message unread: the sender's read of the acknowledgement fails (ECONNRESET)
        with self.assertRaises(ConnectionResetError):
            self.inbox(lambda conn: conn.recv(1))


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

    def test_watchdog_sends_a_recovery_notice_that_failed_before(self):
        i=self.inbound(); self.st.update_inbound(i,recovery_json=json.dumps({'status':'needs_handoff','checks':[]}))
        delivered=self.inbound(); self.st.update_inbound(delivered,action='delivered',recovery_json='{}')
        self.room.watchdog(self.fake,self.st,CFG)
        self.assertTrue(self.st.inbound(i)['notified_at']); self.assertIsNone(self.st.inbound(delivered)['notified_at'])
        self.assertEqual(len(self.sent),1)
        self.room.watchdog(self.fake,self.st,CFG)
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
        self.message(); self.st.record_sent('gonetag2','out','thread','<old@test>','x','specific subject text',
                                            cc_addr=None,body=None,eml=None,attachments=None)
        self.assert_held_unowned(*self.orchestrate())

    def test_reply_to_unrouted_mail_goes_to_orchestrator_without_notice(self):
        # Mail 502: cloud-watchdog sent with --unrouted (tag ""), Alex replied, nobody else wrote.
        self.message(); self.st.record_sent('','w1','thread','<watchdog@test>',CFG['allowlist'][0],'[cloud-watchdog] Daily',
                                            cc_addr=None,body=None,eml=None,attachments=None)
        self.fake.thread_messages.return_value=[({'from':CFG['account']},'report',1,'w1'),
                                               ({'from':'owner@example.com'},'close it',2,'in')]
        with patch.object(recovery,'recover') as recover, patch.object(recovery,'notify') as notify:
            row,event=self.orchestrate()
        recover.assert_not_called(); notify.assert_not_called()
        self.assertEqual(row['action'],'delivered'); self.assertFalse(row['recovery_json']); self.assertFalse(row['notified_at'])
        self.assertNotIn('unowned reply',event); self.assertEqual(self.sent,[])
        self.assertNotIn(CFG['labels']['held'],[l for a,kw in self.mods for l in kw.get('add',[])])

    def test_unrouted_thread_with_an_unrecorded_own_message_is_held(self):
        self.message(); self.st.record_sent('','w1','thread','<watchdog@test>','x','specific subject text',
                                            cc_addr=None,body=None,eml=None,attachments=None)
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
        self.message(headers=[('In-Reply-To','<alert@test>')])
        self.fake.thread_messages.return_value=[({'from':CFG['account'],'x-agent-mailroom':'alert'},'notice',1,'a'),
                                               ({'from':'owner@example.com'},'answer',2,'b')]
        info={'ok':True,'cost':0,'turns':1,'duration_s':1,'error':'','result':'done'}
        with patch.object(self.room.orchestrate,'run_event',return_value=info), patch.object(recovery,'recover') as recover:
            self.room.process(self.fake,self.st,CFG,'in',False)
        recover.assert_not_called()
        self.assertEqual(self.st.list_inbound()[0]['action'],'delivered')

    def test_collaborator_release_retains_guard(self):
        cfg=dict(CFG,collaborators={'collab@example.org':{'name':"Someone's Claude",'owner':'Someone'}})
        sess=self.st.session_for('codex',SID,origin_host='mac'); i=self.inbound(sess)
        self.st.update_inbound(i,from_addr='collab@example.org',from_name='Display Name')
        with patch.object(deliver,'deliver_session',return_value=(0,'queued')) as send, contextlib.redirect_stdout(io.StringIO()):
            self.am.do_release(outbound.Context(cfg),argparse.Namespace(inbound_id=i,target='origin',cli=None))
        self.assertIn(routing.collab_guard({},CFG['owner_name']),send.call_args.args[1])
        # named as the daemon names it: the configured name, not the From header's
        self.assertTrue(send.call_args.args[1].startswith("[email from Someone's Claude <collab@example.org> (collaborator agent of Someone"))

    def test_explicit_mac_handoff_records_the_new_owner(self):
        i=self.inbound()
        with patch.object(remote,'call',side_effect=[dict(ORIGIN,live=True),{'code':0,'detail':'queued'}]) as call, contextlib.redirect_stdout(io.StringIO()):
            self.am.do_release(self.ctx,argparse.Namespace(inbound_id=i,target=SID,cli='codex',host='mac'))
        row=self.st.inbound(i);owner=self.st.session_by_tag(row['matched_tag'])
        self.assertEqual((owner['origin_host'],owner['session_id']),('mac',SID))
        self.assertEqual(row['action'],'queued');self.assertEqual(row['match_rule'],'manual')
        self.assertEqual([c.args[2]['op'] for c in call.call_args_list],['lookup','deliver'])


class Search(Base):
    def test_exited_transcript_found_without_content_disclosure(self):
        path=self.homes.codex_home/'sessions'/'old.jsonl'; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'payload':{'id':SID,'cwd':'/work'}})+'\n'+json.dumps({'text':'specific artifact phrase; private content not returned'}))
        result=recovery.search(['specific artifact phrase'],CFG)
        self.assertEqual(result['candidates'][0]['session_id'],SID)
        self.assertNotIn('private content',json.dumps(result))
        self.assertFalse(result['candidates'][0]['live_hint'])

    def test_default_queries_drop_reply_prefixes_and_the_tag(self):
        i=self.inbound(); self.st.record_sent('ab34cd9k','g','thread','<m@test>','x','Re: Fwd: weekly results [#ab34cd9k]',
                                              cc_addr=None,body=None,eml=None,attachments=None)
        self.assertEqual(recovery.default_queries(self.st,self.st.inbound(i)),['weekly results','<m@test>'])

    def test_saved_recovery_does_not_repeat_search(self):
        i=self.inbound(); self.st.update_inbound(i,recovery_json=json.dumps({'status':'needs_handoff'}))
        with patch.object(remote,'call') as call:
            result=recovery.recover(self.st,CFG,self.st.inbound(i))
        call.assert_not_called(); self.assertEqual(result['status'],'needs_handoff')


if __name__ == '__main__':
    unittest.main()
