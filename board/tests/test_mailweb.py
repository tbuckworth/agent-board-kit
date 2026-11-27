"""The Mail tab's page for the owner's phone (agentboard/mailweb.py, mailwebstate.py, webauthn.py):
python3 -m unittest discover -s tests

Each server test runs the real server on a free local port and talks HTTP to it. Requests carry the headers `tailscale
serve` adds (Tailscale-User-Login, X-Forwarded-For with the client's tailnet address, and Tailscale-Funnel-Request for
public traffic), since the tests cannot go through Tailscale, and the Host the phone asked for. The server takes a
connection only from a socket owned by a uid in `proxy_uids` (tailscaled's, root, when deployed); the tests list their
own uid there, so the real check of the kernel's socket table runs on their own connections. tailscale itself (serve
status, whois) and agent-mail, the mailroom's command behind the mail_* actions, are stood in for as in test_mail.py;
the security alert to the owner about an enrolment goes to a stand-in agent-mail script. The phone's passkey is a
test authenticator:
a real ES256 key, made with `cryptography`, that signs as a phone's passkey does, so the server's WebAuthn checks run on
real signatures. The browser tests need google-chrome, and the scripts' own tests node; each skips without it. The
whole module skips without the `cryptography` package (board/requirements.txt).
"""
import base64
import datetime
import errno
import hashlib
import html
import http.client
import http.server
import io
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:
    raise unittest.SkipTest("the cryptography package is not installed (board/requirements.txt)") from None

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fixtures  # noqa: E402  (first: a scratch config with an example owner)
import chrome  # noqa: E402
import stand_ins  # noqa: E402
from test_mail import LISTED, THREAD, AgentMail, MailTest, answer  # noqa: E402
from agentboard import (actions, colours, groups, keyring, mailbox, maildrafts, mailweb, mailwebstate,  # noqa: E402
                        sent, sources, webauthn)

OWNER = fixtures.OWNER                # the owner's address: their tailnet login in these tests
DESK = "desk.tail1.ts.net"            # the desktop's tailnet name in these tests; `tailscale serve` passes it as Host
HOST = DESK + ":8466"                 # the Host the phone asks for: the name and the page's port
ORIGIN = "https://" + HOST            # the page's origin: the passkeys' site
PHONE, PHONE_IP = "nPhone1CNTRL", "100.64.0.7"        # the phone's stable node id and tailnet address
TABLET, TABLET_IP = "nTablet2CNTRL", "100.64.0.8"     # another allowed device of the owner's
MAC, MAC_IP = "nMac3CNTRL", "100.64.0.9"              # a device of the owner's the page does not answer
# the cases both copies of the reply's text are checked on (the Mac's ReplyText and the phone's replytext.js)
CASES = Path(mailweb.ROOT, "mac", "Sources", "AgentBoard", "reply-text-cases.json")
DOC = '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><a href="mailbox-part:s7/2">r.csv</a></body></html>'
# What the owner types to unlock the desktop in these tests (no real one): UTF-8, and characters JSON and the journal
# escape; its first 16 characters are escaped by neither
TYPED = 'EXAMPLE pässwörd ☃ 🙂 "q" \\b'
NONCE = "n" * 22                      # an unlock's nonce, of the form app.js's newKey() makes
# A client sending an unlock, in a process of its own: its arguments are the page's socket, the session cookie, the
# request's JSON line and the password's two halves, which only it puts together. It prints the answer as it comes.
SEND_UNLOCK = ("OWNER, HOST, PHONE_IP, COOKIE = %r, %r, %r, %r\n" % (OWNER, HOST, PHONE_IP, mailweb.COOKIE)) + r"""
import socket, sys
path, cookie, line, first, second = sys.argv[1:]
body = line.encode() + b"\n" + (first + second).encode()
head = ("POST /api HTTP/1.1\r\nHost: localhost\r\nTailscale-User-Login: %s\r\nX-Forwarded-Host: %s\r\n"
        "X-Forwarded-Proto: https\r\nX-Forwarded-For: %s\r\nX-Agent-Board: 1\r\nCookie: %s=%s\r\n"
        "Content-Length: %d\r\n\r\n" % (OWNER, HOST, PHONE_IP, COOKIE, cookie, len(body)))
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(30)
s.connect(path)
s.sendall(head.encode() + body)
got = b""
while chunk := s.recv(65536):
    got += chunk
sys.stdout.write(got.decode())
"""


def copies_in_memory(first, second):
    """How many places in this process's writable memory hold `first` running straight into `second` (bytes), read
    from /proc/self/mem region by region (/proc/self/maps)."""
    found = 0
    with open("/proc/self/maps") as maps, open("/proc/self/mem", "rb", 0) as mem:
        for region in maps.read().splitlines():
            span, perms = region.split()[:2]
            if not perms.startswith("rw"):
                continue
            low, high = (int(x, 16) for x in span.split("-"))
            try:
                mem.seek(low)
                data = mem.read(high - low)
            except (OSError, OverflowError, ValueError):
                continue                       # a region the kernel will not let be read ([vvar] and the like)
            at = data.find(first)
            while at >= 0:
                found += data.startswith(second, at + len(first))
                at = data.find(first, at + 1)
    return found


SECURITY_HEADERS = ("Cache-Control", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy",
                    "Content-Security-Policy")


def b64(data):
    return webauthn.b64url(data)


def cose_p256(public_key):
    """The COSE_Key (CBOR) of a P-256 public key, as an authenticator writes it into its attested credential data:
    {1: 2 (EC2), 3: -7 (ES256), -1: 1 (P-256), -2: x, -3: y}."""
    n = public_key.public_numbers()
    return (b"\xa5\x01\x02\x03\x26\x20\x01\x21\x58\x20" + n.x.to_bytes(32, "big")
            + b"\x22\x58\x20" + n.y.to_bytes(32, "big"))


class Authenticator:
    """A passkey as a phone holds one for the site `rp_id` on `origin`: an ES256 key and a credential id. Its answers
    are what the page's script sends from navigator.credentials (app.js); `flags` and `count` set the authenticator
    data's flags and sign count, `origin`, `kind` and `cross` the client data's."""

    def __init__(self, rp_id=DESK, origin=ORIGIN, curve=ec.SECP256R1()):
        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(curve)
        self.id = os.urandom(16)

    def _client_data(self, kind, challenge, origin, cross):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": cross}).encode()

    def _auth_data(self, flags, count, attested=False, cred_id=None, cose=None):
        ad = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", count)
        if attested:
            cid = self.id if cred_id is None else cred_id
            ad += bytes(16) + struct.pack(">H", len(cid)) + cid + (cose or cose_p256(self.key.public_key()))
        return ad

    def public_key(self):
        return self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)

    def create(self, options, flags=webauthn.UP | webauthn.UV, origin=None, kind="webauthn.create", alg=webauthn.ES256,
               cred_id=None, cose=None):
        """enrol_finish's credential for creation `options`; `cose` puts another key in the authenticator data."""
        return {"id": b64(self.id), "publicKeyAlgorithm": alg, "publicKey": b64(self.public_key()),
                "clientDataJSON": b64(self._client_data(kind, options["challenge"], origin, False)),
                "authenticatorData": b64(self._auth_data(flags | webauthn.AT, 0, attested=True, cred_id=cred_id,
                                                         cose=cose))}

    def get(self, options, flags=webauthn.UP | webauthn.UV, count=0, origin=None, kind="webauthn.get", cross=False,
            key=None):
        """signin_finish's (or mail_reply's) credential for request `options`, signed with `key` (default its own)."""
        cdj = self._client_data(kind, options["challenge"], origin, cross)
        ad = self._auth_data(flags, count)
        sig = (key or self.key).sign(ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.id), "clientDataJSON": b64(cdj), "authenticatorData": b64(ad), "signature": b64(sig)}


class WebAuthnTest(unittest.TestCase):
    """webauthn.registration and webauthn.assertion on a test authenticator's answers."""

    CHALLENGE = b"c" * 32

    def setUp(self):
        self.a = Authenticator()

    def register(self, cred, challenge=CHALLENGE):
        u = webauthn.unb64url
        return webauthn.registration(DESK, ORIGIN, challenge, u(cred["id"]), u(cred["clientDataJSON"]),
                                     u(cred["authenticatorData"]), u(cred["publicKey"]), cred["publicKeyAlgorithm"])

    def check(self, got, credential=None, challenge=CHALLENGE):
        u = webauthn.unb64url
        return webauthn.assertion(DESK, ORIGIN, challenge, credential or self.cred, u(got["clientDataJSON"]),
                                  u(got["authenticatorData"]), u(got["signature"]))

    def test_a_new_passkey_is_taken_only_for_this_site_with_the_user_verified(self):
        opts = {"challenge": b64(self.CHALLENGE)}
        cred = self.register(self.a.create(opts))
        self.assertEqual((cred.id, cred.public_key, cred.sign_count), (self.a.id, self.a.public_key(), 0))
        other = ec.generate_private_key(ec.SECP256R1()).public_key()
        for why, bad in (("type is not webauthn.create", self.a.create(opts, kind="webauthn.get")),
                         ("origin", self.a.create(opts, origin="https://evil.example")),
                         ("not verified", self.a.create(opts, flags=webauthn.UP)),
                         ("not present", self.a.create(opts, flags=webauthn.UV)),
                         ("does not hold the credential id", self.a.create(opts, cred_id=b"other")),
                         ("only ES256", self.a.create(opts, alg=-257)),
                         ("another site", Authenticator(rp_id="evil.example").create(opts)),
                         ("not an ES256 \\(P-256\\) key",
                          Authenticator(curve=ec.SECP384R1()).create(opts, cose=cose_p256(other))),
                         ("not the key in the authenticator data", self.a.create(opts, cose=cose_p256(other))),
                         ("not a P-256 COSE key", self.a.create(opts, cose=b"\xa1\x01\x01")),
                         ("not a P-256 COSE key", self.a.create(opts, cose=cose_p256(self.a.key.public_key())
                                                                .replace(b"\x03\x26", b"\x03\x39\x01\x00", 1))),
                         ("nests deeper", self.a.create(opts, cose=b"\xa1\x01\xa1\x01\x01")),
                         ("map key that is not a number", self.a.create(opts, cose=b"\xa1\x41\x00\x01")),
                         ("CBOR", self.a.create(opts, cose=b"\xa5\x01"))):
            with self.assertRaisesRegex(webauthn.Refused, why):
                self.register(bad)
        with self.assertRaisesRegex(webauthn.Refused, "challenge is not the one issued"):
            self.register(self.a.create(opts), challenge=b"d" * 32)

    def test_a_signature_is_taken_only_from_the_passkey_with_the_user_verified(self):
        self.cred = webauthn.Credential(self.a.id, self.a.public_key(), 0)
        opts = {"challenge": b64(self.CHALLENGE)}
        self.assertEqual(self.check(self.a.get(opts)), 0)                         # a synced passkey counts nothing
        other = ec.generate_private_key(ec.SECP256R1())
        for why, bad in (("not the passkey's", self.a.get(opts, key=other)),
                         ("not verified", self.a.get(opts, flags=webauthn.UP)),
                         ("not present", self.a.get(opts, flags=webauthn.UV)),
                         ("origin", self.a.get(opts, origin="https://evil.example")),
                         ("another site's frame", self.a.get(opts, cross=True)),
                         ("type is not webauthn.get", self.a.get(opts, kind="webauthn.create"))):
            with self.assertRaisesRegex(webauthn.Refused, why):
                self.check(bad)
        with self.assertRaisesRegex(webauthn.Refused, "challenge is not the one issued"):
            self.check(self.a.get(opts), challenge=b"d" * 32)
        with self.assertRaisesRegex(webauthn.Refused, "another site"):
            self.check(Authenticator(rp_id="evil.example").get(opts))
        tampered = self.a.get(opts)
        tampered["authenticatorData"] = b64(webauthn.unb64url(tampered["authenticatorData"])[:-1] + b"\x09")
        with self.assertRaisesRegex(webauthn.Refused, "not the passkey's"):
            self.check(tampered)
        garbled = dict(self.a.get(opts), signature=b64(b"not a signature"))
        with self.assertRaisesRegex(webauthn.Refused, "not the passkey's"):
            self.check(garbled)

    def test_a_sign_count_that_does_not_go_up_is_refused(self):
        counted = webauthn.Credential(self.a.id, self.a.public_key(), 5)
        opts = {"challenge": b64(self.CHALLENGE)}
        self.assertEqual(self.check(self.a.get(opts, count=6), counted), 6)
        for count in (5, 0):
            with self.assertRaisesRegex(webauthn.Refused, "may have been copied"):
                self.check(self.a.get(opts, count=count), counted)

    def test_garbled_input_is_refused_not_crashed_on(self):
        with self.assertRaisesRegex(webauthn.Refused, "under the 37"):
            webauthn.auth_data(b"short")
        with self.assertRaisesRegex(webauthn.Refused, "not base64url"):
            webauthn.unb64url("a+b/")


class StoreTest(unittest.TestCase):
    """mailwebstate.Store: the enrolment code, passkeys and sessions on disk."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp()) / "mail-web"
        self.store = mailwebstate.Store(self.d)

    def tearDown(self):
        shutil.rmtree(self.d.parent)

    def test_a_code_works_once_and_lapses_and_a_wrong_one_leaves_it_waiting(self):
        code, lapses = self.store.mint_code(1000, 10)
        self.assertEqual(lapses, 1600)
        self.assertFalse(self.store.take_code("wrong", 1001))
        self.assertTrue(self.store.take_code(code, 1002))                     # the wrong one did not end it
        self.assertFalse(self.store.take_code(code, 1003))                    # used
        code, _ = self.store.mint_code(1000, 10)
        self.assertFalse(self.store.take_code(code, 1600))                    # lapsed
        self.assertFalse(self.store.take_code(code, 1000))                    # and ended
        self.assertNotIn(code, (self.d / "enrol.json").read_text())

    def test_sessions_are_kept_by_digest_and_end(self):
        token = self.store.new_session("cid", PHONE, 1000, 12)
        self.assertEqual(self.store.session(token, 1001)["device"], PHONE)
        self.assertNotIn(token, (self.d / "sessions.json").read_text())
        self.assertIsNone(self.store.session(token, 1000 + 12 * 3600))
        self.assertIsNone(self.store.session("other", 1001))
        self.store.add_passkey("cid", "pk", 0, PHONE, 1000)
        self.assertEqual(self.store.sign_out(forget=False), (1, 0))
        self.assertEqual(self.store.passkeys().keys(), {"cid"})
        self.assertEqual(self.store.sign_out(forget=True), (0, 1))
        self.assertEqual(self.store.passkeys(), {})

    def test_the_state_is_private(self):
        self.store.new_session("cid", PHONE, 1000, 12)
        self.assertEqual(stat.S_IMODE(self.d.stat().st_mode), 0o700)
        for name in ("sessions.json", "lock"):
            self.assertEqual(stat.S_IMODE((self.d / name).stat().st_mode), 0o600, name)


class PeerTest(unittest.TestCase):
    """sources.peer_uid, which the page's server checks: who is at the other end of a Unix-socket connection
    (SO_PEERCRED)."""

    def test_the_peer_s_uid_is_read_from_the_connection(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertEqual(sources.peer_uid(a), os.getuid())


class LineReaderTest(unittest.TestCase):
    """mailweb.LineReader: the request's lines taken exactly, its body left in the socket for _post."""

    def test_each_line_is_taken_exactly_and_what_follows_the_headers_is_left_unread(self):
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        long = b"X-Long: " + b"y" * (mailweb.LineReader.PEEK * 2) + b"\r\n"     # past one look
        ours.sendall(b"POST /api HTTP/1.1\r\n" + long + b"X-Short: abcdefgh\r\n\r\n{}\nthe body's own")
        ours.shutdown(socket.SHUT_WR)
        reader = mailweb.LineReader(theirs)
        self.assertEqual(reader.readline(65537), b"POST /api HTTP/1.1\r\n")
        self.assertEqual(reader.readline(), long)
        self.assertEqual(reader.readline(5), b"X-Sho")                           # at most its limit
        self.assertEqual(reader.readline(), b"rt: abcdefgh\r\n")
        self.assertEqual(reader.readline(65537), b"\r\n")
        self.assertEqual(reader.scratch, bytearray(mailweb.LineReader.PEEK))     # zeroed after each look
        body = bytearray(64)
        n = reader.readinto(body)
        self.assertEqual(bytes(body[:n]), b"{}\nthe body's own")                 # all of it still in the socket
        self.assertEqual(reader.readline(), b"")                                  # the input's end


class FunnelTest(unittest.TestCase):
    """mailweb.funnel_exposure on `tailscale serve status --json` (ipn.ServeConfig, as tailscale 1.104 prints it), for
    a page whose socket is a real one in a folder of the test's own."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)
        self.sock = str(self.d / "page.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.sock)
        self.addCleanup(s.close)

    def exposure(self, status, port=8466):
        return mailweb.funnel_exposure(port, self.sock, AgentMail(answer(status)))

    def proxied(self, target):
        return {"Web": {DESK + ":443": {"Handlers": {"/m": {"Proxy": target}}}}, "AllowFunnel": {DESK + ":443": True}}

    def forwarded(self, target):
        return {"TCP": {"10000": {"TCPForward": target}}, "AllowFunnel": {DESK + ":10000": True}}

    def test_any_funnel_that_reaches_the_page_is_found(self):
        link, hard = self.d / "link.sock", self.d / "hard.sock"
        link.symlink_to(self.sock)
        os.link(self.sock, hard)                             # the same file under another path: only its inode tells
        relative = self.sock.lstrip("/")                     # tailscaled resolves it against /, its working folder
        cases = {
            "the page's own port funnelled": {"AllowFunnel": {DESK + ":8466": True}},
            "a path on public 443 proxying to its port": self.proxied("http://127.0.0.1:8466"),
            "an IPv4-mapped address": self.proxied("http://[::ffff:127.0.0.1]:8466"),
            "the tailnet address (serve's own listener)": self.proxied("https+insecure://100.64.0.1:8466"),
            "an unspecified address": self.forwarded("0.0.0.0:8466"),
            "raw TCP": self.forwarded("127.0.0.1:8466"),
            "a path proxying to its socket": self.proxied("unix:" + self.sock),
            "raw TCP to its socket": self.forwarded("unix:%s/../%s/page.sock" % (self.d, self.d.name)),
            "a link to its socket": self.proxied("unix:%s" % link),
            "a hard link to its socket": self.forwarded("unix:%s" % hard),
            "a path proxying to its socket, relative": self.proxied("unix:" + relative),
            "raw TCP to its socket, relative": self.forwarded("unix:" + relative),
            "a foreground funnel": {"Foreground": {"sess123": self.proxied("unix:" + self.sock)}},
        }
        for name, status in cases.items():
            self.assertIsNotNone(self.exposure(status), name)

    def test_a_relative_target_is_found_before_the_page_has_made_its_socket(self):
        later = str(self.d / "not-yet.sock")
        self.assertIsNotNone(mailweb.funnel_exposure(8466, later, AgentMail(answer(self.proxied("unix:"
                                                                                               + later.lstrip("/"))))))

    def test_funnels_elsewhere_and_serving_on_the_tailnet_are_not(self):
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        other.bind(str(self.d / "other.sock"))
        self.addCleanup(other.close)
        status = {"TCP": {"443": {"HTTPS": True}, "8466": {"HTTPS": True}, "10000": {"TCPForward": "127.0.0.1:22"}},
                  "Web": {DESK + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"},
                                                       "/other": {"Proxy": "unix:%s/other.sock" % self.d},
                                                       "/gone": {"Proxy": "unix:/run/gone.sock"},
                                                       "/rel": {"Proxy": "unix:page.sock"}}},
                          DESK + ":8466": {"Handlers": {"/": {"Proxy": "unix:" + self.sock}}}},
                  "AllowFunnel": {DESK + ":443": True, DESK + ":10000": True, DESK + ":8466": False}}
        self.assertIsNone(self.exposure(status))
        self.assertIsNone(self.exposure({}))

    def test_a_status_it_cannot_read_raises(self):
        for garbled in ({"AllowFunnel": [DESK + ":8466"]},
                        {"Web": {DESK + ":443": {"Handlers": {"/": "unix:x"}}}, "AllowFunnel": {DESK + ":443": True}},
                        self.forwarded("127.0.0.1:port")):
            with self.assertRaises(Exception):
                self.exposure(garbled)


class MailWebTest(MailTest):
    def setUp(self):
        super().setUp()
        self.real_stderr, sys.stderr = sys.stderr, io.StringIO()          # the server's request log
        self.agent_mail = AgentMail(answer(LISTED))
        self.serve_status = {}                                             # what `tailscale serve status` says
        self.nodes = {PHONE_IP: (PHONE, "phone." + "tail1.ts.net.", OWNER),
                      TABLET_IP: (TABLET, "tablet." + "tail1.ts.net.", OWNER),
                      MAC_IP: (MAC, "laptop." + "tail1.ts.net.", OWNER)}
        self.now = 1_800_000_000.0
        self.d = Path(tempfile.mkdtemp())
        self.sock = self.d / "page.sock"
        self.store = mailwebstate.Store(self.d / "mail-web")
        self.saved_config = sources.MAIL_CONFIG
        sources.MAIL_CONFIG = self.d / "config.json"
        sources.MAIL_CONFIG.write_text(json.dumps({"owner_name": "Alex", "owner_email": OWNER, "allowlist": [OWNER]}))
        self.mail_said(0)
        self.popen = stand_ins.Popen()        # agent-orchestrate, which would run a paid `claude -p` event
        self.keyring = stand_ins.Keyring(self.d / "keyring")     # the desktop keyring, which is the owner's own
        self.answers = []                     # the answers to post(), which assert_nowhere scans
        self.saved_logs, actions.ORCH_LOGS = actions.ORCH_LOGS, self.d / "orchestrate-logs"
        self.srv = None
        self.start(proxy_uids=[os.getuid()])
        self.phone = Authenticator()
        self.cookie = None

    def start(self, **cfg):
        """The server under test, with the settings' defaults, the phone and tablet allowed, and `cfg`."""
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        cfg = dict(mailwebstate.DEFAULTS, devices=[PHONE, TABLET], **cfg)
        self.guard = mailweb.Guard(cfg, [OWNER], DESK, str(self.sock), self.store, self.commands, self.popen,
                                   clock=lambda: self.now, read_keyring=self.keyring.read,
                                   unlock_command=self.keyring.command)
        self.srv = mailweb.PageServer(self.sock, self.guard)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        sys.stderr = self.real_stderr
        sources.MAIL_CONFIG, actions.ORCH_LOGS = self.saved_config, self.saved_logs
        shutil.rmtree(self.d)
        super().tearDown()

    def commands(self, cmd, timeout=60, env=None, input=None, cwd=None):
        """sources.run for the server: tailscale (serve status, whois) here, agent-mail to its stand-in."""
        if Path(cmd[0]).name != "tailscale":
            return self.agent_mail(cmd, timeout, env, input, cwd)
        if cmd[1:] == ["serve", "status", "--json"]:
            return (1, "", "failed to connect to local tailscaled") if self.serve_status is None \
                else answer(self.serve_status)
        if cmd[1:3] == ["whois", "--json"] and cmd[3] in self.nodes:
            node, name, login = self.nodes[cmd[3]]
            return answer({"Node": {"StableID": node, "Name": name}, "UserProfile": {"LoginName": login}})
        return 1, "", "no such peer"

    def mail_said(self, code):
        """The agent-mail that mail_owner runs: a script that keeps its arguments and stdin, and exits `code`."""
        exe = self.root / "bin" / "agent-mail"
        exe.write_text('#!/bin/sh\nprintf "%%s\\n" "$@" > "%s/mail-argv"; cat > "%s/mail-stdin"; exit %d\n'
                       % (self.d, self.d, code))
        exe.chmod(0o755)

    def mailroom_before_alerts(self):
        """The agent-mail of a mailroom older than `send --alert`, as it answers one (its argparse refusing the option,
        before anything is sent), and as it sends any other mail: kept in mail-argv and mail-stdin."""
        exe = self.root / "bin" / "agent-mail"
        exe.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = --alert ] && { echo "usage: agent-mail [-h] ..." >&2;'
                       ' echo "agent-mail: error: unrecognized arguments: --alert phone-enrolment" >&2; exit 2; }; done\n'
                       'printf "%%s\\n" "$@" > "%s/mail-argv"; cat > "%s/mail-stdin"\n' % (self.d, self.d))
        exe.chmod(0o755)

    def ask(self, method, path, body=None, login=OWNER, host=HOST, proto="https", address=PHONE_IP, cookie=True,
            end_input=False, **headers):
        """(status, headers, body) of one request over the server's socket, as tailscaled passes one on for `login`
        at tailnet `address` asking for `host` over `proto` (None: no such header), with the session cookie when
        `cookie`. The request goes in one write, so a server that answers from the headers alone and closes does not
        break the sending of its body; with `end_input`, nothing more is sent after it (the socket is shut for
        writing)."""
        headers = dict({"Host": "localhost"}, **headers)
        for name, value in (("Tailscale-User-Login", login), ("X-Forwarded-Host", host),
                            ("X-Forwarded-Proto", proto), ("X-Forwarded-For", address)):
            if value:
                headers[name] = value
        if cookie and self.cookie:
            headers["Cookie"] = "%s=%s" % (mailweb.COOKIE, self.cookie)
        if body is not None:
            headers.setdefault("Content-Length", str(len(body)))
        head = "%s %s HTTP/1.1\r\n%s\r\n" % (method, path, "".join("%s: %s\r\n" % kv for kv in headers.items()))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        try:
            s.connect(str(self.sock))
            s.sendall(head.encode() + (body or b""))
            if end_input:
                s.shutdown(socket.SHUT_WR)
            r = http.client.HTTPResponse(s)
            r.begin()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            s.close()

    def api(self, req, keep_cookie=True, **headers):
        """(status, answer) of one API call; a session cookie it sets is kept for the next calls."""
        headers.setdefault("X-Agent-Board", "1")
        status, got, body = self.ask("POST", "/api", json.dumps(req).encode(), **headers)
        cookie = got.get("Set-Cookie", "")
        if keep_cookie and cookie.startswith(mailweb.COOKIE + "="):
            self.cookie = cookie.split(";")[0].split("=", 1)[1] or None
        return status, json.loads(body) if body.startswith(b"{") else body

    def part(self, path, **headers):
        headers.setdefault("X-Agent-Board", "1")
        return self.ask("GET", path, **headers)

    def enrol(self, phone=None):
        code, _ = self.store.mint_code(self.now, 10)
        status, got = self.api({"action": "enrol_begin", "code": code})
        self.assertEqual(status, 200, got)
        status, got = self.api({"action": "enrol_finish", "credential": (phone or self.phone).create(got["options"])})
        self.assertEqual((status, got), (200, {"ok": True}))

    def sign_in(self, **how):
        status, got = self.api({"action": "signin_begin"})
        self.assertEqual(status, 200, got)
        return self.api({"action": "signin_finish", "credential": self.phone.get(got["options"], **how)})

    def signed_in(self):
        self.enrol()
        self.agent_mail.calls.clear()

    def log(self):
        return sys.stderr.getvalue()

    def stalled(self, data):
        """A raw connection to the server that has sent `data` and then waits."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.connect(str(self.sock))
        s.sendall(data)
        return s

    def closed(self, s):
        """Whether the server closes connection `s` within 5 seconds, unanswered: a close with what it sent still
        unread reaches this end as a reset."""
        s.settimeout(5)
        try:
            return s.recv(1024) == b""
        except ConnectionResetError:
            return True

    # who may ask

    def test_only_the_owner_on_their_phone_on_the_tailnet_is_answered(self):
        for why, how in (("no tailnet login", dict(login=None)),
                         ("the tailnet login 'someone@else.org' is not one the page answers",
                          dict(login="someone@else.org")),
                         ("came through Tailscale Funnel", dict(**{"Tailscale-Funnel-Request": "?1"})),
                         ("no client address from tailscale serve", dict(address=None)),
                         ("device laptop.tail1.ts.net (nMac3CNTRL) is not one the page answers", dict(address=MAC_IP)),
                         ("tailscale knows no device at 100.64.0.99", dict(address="100.64.0.99"))):
            for method, path in (("GET", "/"), ("GET", "/part/s7/2"), ("POST", "/api")):
                status, _, body = self.ask(method, path, b"{}" if method == "POST" else None,
                                           **dict(how, **{"X-Agent-Board": "1"}))
                self.assertEqual(status, 403, (how, path))
            self.assertIn(why, self.log())
        self.nodes[PHONE_IP] = (PHONE, "phone." + "tail1.ts.net.", "someone@else.org")
        self.now += mailweb.WHOIS_SECONDS                                   # the phone's whois is asked again
        self.assertEqual(self.ask("GET", "/")[0], 403)
        self.assertIn("is logged in as 'someone@else.org', not '%s'" % OWNER, self.log())
        self.assertEqual(self.agent_mail.calls, [])

    def test_only_tailscale_s_own_connections_are_taken(self):
        # A program running as another uid that reaches the socket is cut off at accept, before a byte is read.
        self.start(proxy_uids=[os.getuid() + 1])
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)):
            self.ask("GET", "/")
        self.assertIn("refused a connection from uid %d, not tailscale serve's" % os.getuid(), self.log())
        self.assertEqual(self.agent_mail.calls, [])

    def test_the_socket_is_the_owner_s_alone_and_replaces_an_old_one(self):
        self.assertEqual(stat.S_IMODE(self.sock.stat().st_mode), 0o600)
        self.srv.shutdown()
        self.srv.server_close()
        self.assertFalse(self.sock.exists())
        old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old.bind(str(self.sock))                                             # a socket the last run left
        old.close()
        self.srv = None
        self.start(proxy_uids=[os.getuid()])
        self.assertEqual(self.ask("GET", "/")[0], 200)
        self.srv.shutdown()
        self.srv.server_close()
        self.srv = None
        self.sock.write_text("not a socket")
        with self.assertRaisesRegex(RuntimeError, "is not a socket"):
            self.start(proxy_uids=[os.getuid()])
        self.sock.unlink()
        self.start(proxy_uids=[os.getuid()])

    def test_only_https_for_the_desktop_s_tailnet_name_at_the_page_s_port_is_answered(self):
        for host, proto in ((None, "https"), ("evil.example:8466", "https"), (DESK + ".evil.example:8466", "https"),
                            ("127.0.0.1:8466", "https"), (DESK, "https"), (DESK + ":443", "https"), (HOST, "http"),
                            (HOST, None)):
            for method, path in (("GET", "/"), ("GET", "/part/s7/2"), ("POST", "/api")):
                status, _, body = self.ask(method, path, b"{}" if method == "POST" else None, host=host, proto=proto,
                                           **{"X-Agent-Board": "1"})
                self.assertEqual(status, 403, (host, proto, path))
        self.assertIn("asked for 'evil.example:8466' over https, not https://%s" % HOST, self.log())
        self.assertEqual(self.ask("GET", "/", host=HOST.upper())[0], 200)          # in any case

    def test_the_address_asked_for_comes_from_tailscale_never_from_host(self):
        # tailscaled sends a Unix backend Host: localhost and the phone's address in X-Forwarded-Host; a request without
        # the latter is refused, whatever its Host says
        self.assertEqual(self.ask("GET", "/", host=None, Host=HOST)[0], 403)
        self.assertIn("asked for '' over https", self.log())

    def test_nothing_is_answered_while_the_page_is_open_to_the_public(self):
        self.assertEqual(self.ask("GET", "/")[0], 200)
        self.serve_status = {"AllowFunnel": {DESK + ":443": True},
                             "Web": {DESK + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}
        self.now += mailweb.FUNNEL_CHECK
        self.assertEqual(self.ask("GET", "/")[0], 200)                     # another service's Funnel is not ours
        self.serve_status = {"AllowFunnel": {DESK + ":443": True},
                             "Web": {DESK + ":443": {"Handlers": {"/mail": {"Proxy": "unix:%s" % self.sock}}}}}
        self.now += mailweb.FUNNEL_CHECK
        for method, path in (("GET", "/"), ("POST", "/api")):
            self.assertEqual(self.ask(method, path, b"{}" if method == "POST" else None, **{"X-Agent-Board": "1"})[0],
                             503)
        self.assertIn("refused /: public port 443's /mail proxies to the page", self.log())

    def test_a_funnel_check_that_fails_answers_nothing_and_says_why(self):
        for status, said in ((None, "CommandError: failed to connect to local tailscaled"),
                             ({"AllowFunnel": [DESK + ":8466"]}, "AttributeError")):
            self.serve_status = status
            for method, path in (("GET", "/"), ("POST", "/api")):
                self.assertEqual(self.ask(method, path, b"{}" if method == "POST" else None,
                                          **{"X-Agent-Board": "1"})[0], 503)
            self.assertIn("could not check whether the page is open to the public (%s" % said, self.log())
        self.serve_status = {}
        self.assertEqual(self.ask("GET", "/")[0], 200)                     # a failure is not remembered

    def test_a_request_whose_headers_trickle_in_is_dropped(self):
        saved = mailweb.HEADER_SECONDS
        mailweb.HEADER_SECONDS = 0.5
        self.addCleanup(setattr, mailweb, "HEADER_SECONDS", saved)
        s = self.stalled(b"POST /api HTTP/1.1\r\n")
        s.settimeout(5)
        started = time.time()
        for _ in range(3):                                   # each byte well inside the per-read timeout
            time.sleep(0.2)
            try:
                s.sendall(b"X")
            except OSError:
                break
        self.assertEqual(s.recv(1024), b"")                  # closed by the server
        self.assertLess(time.time() - started, 4)
        self.assertIn("dropped: its request line and headers took over", self.log())

    def test_a_request_whose_body_stalls_is_dropped(self):
        saved = mailweb.Handler.timeout
        mailweb.Handler.timeout = 0.5
        self.addCleanup(setattr, mailweb.Handler, "timeout", saved)
        self.assertIsNotNone(saved)
        headers = "\r\n".join(["POST /api HTTP/1.1", "Host: localhost", "Tailscale-User-Login: " + OWNER,
                                "X-Forwarded-Host: " + HOST, "X-Forwarded-Proto: https", "X-Forwarded-For: " + PHONE_IP,
                                "X-Agent-Board: 1", "Content-Length: 100", "", ""])
        s = self.stalled(headers.encode())                   # and no body
        s.settimeout(5)
        started = time.time()
        while s.recv(1024):
            pass                                             # until the server closes it
        self.assertLess(time.time() - started, 4)

    def test_a_body_that_trickles_in_is_dropped_at_its_deadline(self):
        self.assertEqual([mailweb.body_seconds(n) for n in (0, mailweb.BODY_RATE, 10 ** 9)],
                         [mailweb.BODY_SECONDS, mailweb.BODY_SECONDS + 1, mailweb.BODY_MAX_SECONDS])
        saved = mailweb.BODY_SECONDS
        mailweb.BODY_SECONDS = 0.5
        self.addCleanup(setattr, mailweb, "BODY_SECONDS", saved)
        headers = "\r\n".join(["POST /api HTTP/1.1", "Host: localhost", "Tailscale-User-Login: " + OWNER,
                                "X-Forwarded-Host: " + HOST, "X-Forwarded-Proto: https", "X-Forwarded-For: " + PHONE_IP,
                                "X-Agent-Board: 1", "Content-Length: 100", "", ""])
        s = self.stalled(headers.encode())
        s.settimeout(5)
        started = time.time()
        for _ in range(8):                                   # a byte at a time, each well inside the per-read timeout
            time.sleep(0.2)
            try:
                s.sendall(b"{")
            except OSError:
                break
        while s.recv(1024):
            pass
        self.assertLess(time.time() - started, 4)
        self.assertIn("dropped: its body took over", self.log())

    def test_waiting_for_a_slot_does_not_hold_up_the_accept_loop(self):
        saved = mailweb.MAX_CONNECTIONS
        mailweb.MAX_CONNECTIONS = 2
        self.addCleanup(setattr, mailweb, "MAX_CONNECTIONS", saved)
        self.start(proxy_uids=[os.getuid()])
        held = [self.stalled(b"GET / HTTP/1.1\r\n") for _ in range(2)]
        time.sleep(0.3)
        started = time.time()
        extra = [self.stalled(b"GET / HTTP/1.1\r\n\r\n") for _ in range(4)]
        for s in extra:
            self.assertTrue(self.closed(s))                  # after waiting for a slot
        self.assertLess(time.time() - started, 2.5)          # together, not one after another
        for s in held:
            s.close()

    def test_one_device_has_only_so_many_requests_going_at_once(self):
        saved = mailweb.PER_DEVICE
        mailweb.PER_DEVICE = 1
        self.addCleanup(setattr, mailweb, "PER_DEVICE", saved)
        headers = "\r\n".join(["POST /api HTTP/1.1", "Host: localhost", "Tailscale-User-Login: " + OWNER,
                                "X-Forwarded-Host: " + HOST, "X-Forwarded-Proto: https", "X-Forwarded-For: " + PHONE_IP,
                                "X-Agent-Board: 1", "Content-Length: 100", "", ""])
        going = self.stalled(headers.encode())               # its headers read, its body still to come
        time.sleep(0.3)
        status, got = self.api({"action": "status"})
        self.assertEqual(status, 503)
        self.assertIn("1 requests from this device are going already", got["error"])
        self.assertEqual(self.api({"action": "status"}, address=TABLET_IP)[0], 200)     # another device's are its own
        going.close()
        time.sleep(0.3)
        self.assertEqual(self.api({"action": "status"})[0], 200)

    def test_connections_beyond_the_limit_are_closed(self):
        saved = mailweb.MAX_CONNECTIONS
        mailweb.MAX_CONNECTIONS = 2
        self.addCleanup(setattr, mailweb, "MAX_CONNECTIONS", saved)
        self.start(proxy_uids=[os.getuid()])
        held = [self.stalled(b"GET / HTTP/1.1\r\n") for _ in range(2)]
        time.sleep(0.3)
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)):
            self.ask("GET", "/")
        self.assertIn("refused a connection: 2 are being served already", self.log())
        for s in held:
            s.close()

    # signing in

    def test_mail_needs_a_signed_in_session(self):
        self.assertEqual(self.api({"action": "status"}),
                         (200, {"ok": True, "signed_in": False, "enrolled": False,
                                "limits": {"files": mailbox.MAX_FILES, "bytes": mailbox.MAX_FILE_BYTES,
                                           "message": actions.MAX_BRIEF}, "keyring": None}))
        for req in ({"action": "mail_list"}, {"action": "mail_show", "thread": THREAD},
                    {"action": "mail_fast", "thread": THREAD, "on": True},
                    {"action": "stepup_begin", "approves": "mail_reply", "digest": "0" * 64},
                    {"action": "orchestrate", "request": '{"text": "hi"}', "credential": {}},
                    {"action": "keyring_unlock", "request": json.dumps({"nonce": NONCE}), "credential": {}}):
            self.assertEqual(self.api(req), (401, {"ok": False, "error": "not signed in"}), req)
        self.assertEqual((self.keyring.reads, self.keyring.runs()), (0, []))
        self.assertEqual(self.part("/part/s7/2")[0], 401)
        self.assertEqual(self.api({"action": "signin_begin"})[0], 403)        # nothing enrolled yet
        self.assertEqual(self.agent_mail.calls, [])
        for path in ("/", "/enrol", "/app.js", "/snooze.js", "/app.css"):      # the page itself holds no mail
            self.assertEqual(self.ask("GET", path)[0], 200, path)

    def test_a_phone_is_enrolled_only_with_the_mac_s_code_once_and_in_time(self):
        code, _ = self.store.mint_code(self.now, 10)
        self.now += 600
        self.assertEqual(self.api({"action": "enrol_begin", "code": code})[0], 403)   # lapsed
        code, _ = self.store.mint_code(self.now, 10)
        self.assertEqual(self.api({"action": "enrol_begin", "code": "guess"})[0], 403)
        status, got = self.api({"action": "enrol_begin", "code": code})              # a guess does not end it
        self.assertEqual(status, 200)
        o = got["options"]
        self.assertEqual((o["rp"]["id"], o["authenticatorSelection"]["userVerification"], o["attestation"],
                          [p["alg"] for p in o["pubKeyCredParams"]]), (DESK, "required", "none", [-7]))
        self.assertEqual(self.api({"action": "enrol_begin", "code": code})[0], 403)   # used
        status, got = self.api({"action": "enrol_finish", "credential": self.phone.create(o)})
        self.assertEqual((status, got), (200, {"ok": True}))
        self.assertEqual(list(self.store.passkeys()), [b64(self.phone.id)])
        self.assertEqual(self.store.passkeys()[b64(self.phone.id)]["device"], PHONE)
        argv = (self.d / "mail-argv").read_text().split("\n")
        # a security alert (README, "Security alerts on the Mac"): never an ordinary notice
        self.assertEqual(argv[:7], ["send", "--unrouted", "--alert", "phone-enrolment", "--to", OWNER, "--subject"])
        self.assertIn("on device phone.tail1.ts.net", (self.d / "mail-stdin").read_text())
        self.assertEqual(self.api({"action": "status"})[1]["signed_in"], True)        # enrolling signs in
        self.assertIn("passkey enrolled", self.log())
        status, got = self.api({"action": "enrol_finish", "credential": self.phone.create(o)})   # again: used
        self.assertEqual(status, 403)

    def test_each_enrolment_has_a_user_handle_of_its_own(self):
        # A page on another port of the same host shares the passkeys' site; with a known user handle it could make
        # a passkey that replaces the owner's.
        handles = set()
        for _ in range(2):
            code, _ = self.store.mint_code(self.now, 10)
            handles.add(self.api({"action": "enrol_begin", "code": code})[1]["options"]["user"]["id"])
        self.assertEqual(len(handles), 2)
        self.assertTrue(all(len(webauthn.unb64url(h)) == 16 for h in handles))

    def test_enrolment_tries_are_limited(self):
        code, _ = self.store.mint_code(self.now, 10)
        for _ in range(mailweb.ENROL_TRIES):
            self.assertEqual(self.api({"action": "enrol_begin", "code": "guess"})[0], 403)
        status, got = self.api({"action": "enrol_begin", "code": code})
        self.assertEqual(status, 429)
        self.assertIn("too many enrolment tries", got["error"])
        self.assertEqual(self.api({"action": "signin_begin"})[0], 403)          # sign-in is counted apart
        self.now += mailweb.FAILURE_WINDOW
        code, _ = self.store.mint_code(self.now, 10)
        self.assertEqual(self.api({"action": "enrol_begin", "code": code})[0], 200)

    def test_a_passkey_that_fails_at_enrolment_counts_toward_the_enrolment_limit(self):
        for _ in range(mailweb.ENROL_TRIES):
            code, _ = self.store.mint_code(self.now, 10)
            status, got = self.api({"action": "enrol_begin", "code": code})
            self.assertEqual(self.api({"action": "enrol_finish", "credential": self.phone.create(
                got["options"], flags=webauthn.UP)})[0], 403)
        code, _ = self.store.mint_code(self.now, 10)
        self.assertEqual(self.api({"action": "enrol_begin", "code": code})[0], 429)

    def test_an_enrolment_the_owner_cannot_be_told_of_does_not_happen(self):
        self.mail_said(3)
        code, _ = self.store.mint_code(self.now, 10)
        status, got = self.api({"action": "enrol_begin", "code": code})
        status, got = self.api({"action": "enrol_finish", "credential": self.phone.create(got["options"])})
        self.assertEqual(status, 503)
        self.assertIn("the owner could not be told of it", got["error"])
        self.assertEqual(self.store.passkeys(), {})
        self.assertIsNone(self.cookie)

    def test_a_mailroom_that_cannot_send_the_warning_as_an_alert_refuses_the_enrolment(self):
        # Kept for the Mail tab alone, as an ordinary notice would be, the warning could be read and archived by
        # whoever enrolled before the owner saw it. A mailroom older than --alert refuses it, so the enrolment fails.
        self.mailroom_before_alerts()
        code, _ = self.store.mint_code(self.now, 10)
        status, got = self.api({"action": "enrol_begin", "code": code})
        status, got = self.api({"action": "enrol_finish", "credential": self.phone.create(got["options"])})
        self.assertEqual(status, 503)
        self.assertIn("the owner could not be told of it (agent-mail exit 2: ", got["error"])
        self.assertIn("unrecognized arguments: --alert phone-enrolment", got["error"])
        self.assertFalse((self.d / "mail-argv").exists())                     # sent neither way
        self.assertEqual(self.store.passkeys(), {})
        self.assertIsNone(self.cookie)
        self.assertNotIn("passkey enrolled", self.log())

    def test_a_passkey_that_does_not_check_out_is_not_enrolled(self):
        code, _ = self.store.mint_code(self.now, 10)
        status, got = self.api({"action": "enrol_begin", "code": code})
        status, got = self.api({"action": "enrol_finish", "credential": self.phone.create(got["options"],
                                                                                           flags=webauthn.UP)})
        self.assertEqual((status, got["error"]), (403, "passkey not enrolled: the user was not verified (no "
                                                       "fingerprint or screen lock)"))
        self.assertEqual(self.store.passkeys(), {})

    def test_signing_in_names_no_credential(self):
        # Anything on the phone's node can ask; a credential id it learnt would let it send bad signatures for it and
        # lock sign-in. The passkey is discoverable, so the browser finds it without one.
        self.enrol()
        self.cookie = None
        status, headers, body = self.ask("POST", "/api", b'{"action": "signin_begin"}', **{"X-Agent-Board": "1"})
        self.assertEqual(json.loads(body)["options"]["allowCredentials"], [])
        self.assertNotIn(b64(self.phone.id).encode(), body)
        code, _ = self.store.mint_code(self.now, 10)
        sel = self.api({"action": "enrol_begin", "code": code})[1]["options"]["authenticatorSelection"]
        self.assertEqual((sel["residentKey"], sel["requireResidentKey"]), ("required", True))

    def test_signing_in_takes_the_enrolled_passkey_with_the_user_verified(self):
        self.enrol()
        self.cookie = None
        self.assertEqual(self.sign_in(), (200, {"ok": True}))
        self.assertEqual(self.api({"action": "mail_list"})[0], 200)
        self.cookie = None
        for how in (dict(flags=webauthn.UP), dict(origin="https://evil.example"),
                    dict(key=ec.generate_private_key(ec.SECP256R1()))):
            self.assertEqual(self.sign_in(**how)[0], 403, how)
            self.assertIsNone(self.cookie)
        status, got = self.api({"action": "signin_begin"})
        signed = self.phone.get(got["options"])
        self.assertEqual(self.api({"action": "signin_finish", "credential": signed})[0], 200)
        self.assertEqual(self.api({"action": "signin_finish", "credential": signed})[0], 403)   # each challenge once
        stranger = Authenticator()
        status, got = self.api({"action": "signin_begin"})
        self.assertEqual(self.api({"action": "signin_finish", "credential": stranger.get(got["options"])}),
                         (403, {"ok": False, "error": "that passkey is not enrolled here"}))
        status, got = self.api({"action": "signin_begin"})
        self.now += mailweb.CHALLENGE_SECONDS
        self.assertEqual(self.api({"action": "signin_finish", "credential": self.phone.get(got["options"])})[0], 403)

    def test_a_challenge_is_for_the_device_and_the_purpose_it_was_issued_for(self):
        self.enrol()
        status, got = self.api({"action": "signin_begin"})
        status, got = self.api({"action": "signin_finish", "credential": self.phone.get(got["options"])},
                               address=TABLET_IP)
        self.assertEqual((status, got["error"]),
                         (403, "the signin challenge is unknown, used, lapsed or another device's"))
        reply = json.dumps({"thread": THREAD, "key": "k" * 22, "text": "Ship it."})
        status, got = self.api({"action": "signin_begin"})                       # a sign-in's, for a reply
        self.assertEqual(self.api({"action": "mail_reply", "request": reply,
                                   "credential": self.phone.get(got["options"])})[0], 403)
        self.assertEqual(self.agent_mail.calls, [])

    def test_failed_sign_ins_are_limited_and_only_signed_ones_count(self):
        self.enrol()
        for _ in range(10):                                         # junk without a signature counts for nothing
            self.api({"action": "signin_finish", "credential": {"id": "AA", "clientDataJSON": "AA",
                                                                "authenticatorData": "AA", "signature": "AA"}})
        status, pending = self.api({"action": "signin_begin"})
        self.assertEqual(status, 200)
        for _ in range(mailweb.FAILURES):
            self.assertEqual(self.sign_in(flags=webauthn.UP)[0], 403)
        status, got = self.api({"action": "signin_begin"})
        self.assertEqual(status, 429)
        self.assertIn("too many failed sign-ins", got["error"])
        self.assertEqual(self.api({"action": "signin_finish",                    # even a good one waits
                                   "credential": self.phone.get(pending["options"])})[0], 429)
        self.now += mailweb.FAILURE_WINDOW
        self.assertEqual(self.sign_in(), (200, {"ok": True}))

    def test_an_unknown_passkey_counts_for_nothing_toward_the_lockout(self):
        self.enrol()
        stranger = Authenticator()
        for _ in range(mailweb.FAILURES * 2):
            status, got = self.api({"action": "signin_begin"})
            self.assertEqual(self.api({"action": "signin_finish", "credential": stranger.get(got["options"])})[0], 403)
        self.assertEqual(self.sign_in(), (200, {"ok": True}))

    def test_each_purpose_keeps_only_its_newest_challenges(self):
        self.enrol()
        firsts = [self.api({"action": "signin_begin"})[1]["options"]
                  for _ in range(mailweb.CHALLENGES["signin"] + 1)]
        self.assertEqual(self.api({"action": "signin_finish", "credential": self.phone.get(firsts[0])})[0], 403)
        self.assertEqual(self.api({"action": "signin_finish", "credential": self.phone.get(firsts[-1])})[0], 200)
        replies = [json.dumps({"thread": THREAD, "key": "k" * 22, "text": "Reply %d" % i})
                   for i in range(mailweb.CHALLENGES["stepup"] + 1)]
        approvals = []
        for r in replies:
            status, got = self.api({"action": "stepup_begin", "approves": "mail_reply",
                                    "digest": hashlib.sha256(r.encode()).hexdigest()})
            approvals.append({"action": "mail_reply", "request": r, "credential": self.phone.get(got["options"])})
        self.assertEqual(self.api(approvals[0])[0], 403)
        self.assertEqual(self.agent_mail.calls, [])

    def test_a_session_ends_lapses_and_stays_on_its_device(self):
        self.signed_in()
        self.assertEqual(self.api({"action": "mail_list"})[0], 200)
        self.assertEqual(self.api({"action": "mail_list"}, address=TABLET_IP)[0], 401)   # its cookie on another device
        self.now += 12 * 3600
        self.assertEqual(self.api({"action": "mail_list"})[0], 401)
        self.sign_in()
        status, headers, _ = self.ask("POST", "/api", b'{"action": "signout"}', **{"X-Agent-Board": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Set-Cookie"], "%s=; Max-Age=0; %s" % (mailweb.COOKIE, mailweb.COOKIE_ATTRIBUTES))
        self.assertEqual(self.api({"action": "mail_list"})[0], 401)

    def test_the_mac_signs_the_phone_out_and_forgets_it(self):
        self.signed_in()
        cookie = self.cookie
        self.assertEqual(actions.dispatch({"action": "mail_web_sign_out", "forget": False}, None, None,
                                          store=self.store), {"ok": True, "sessions": 1, "passkeys": 0})
        self.assertEqual(self.api({"action": "mail_list"})[0], 401)
        self.assertEqual(self.sign_in(), (200, {"ok": True}))
        self.assertEqual(actions.dispatch({"action": "mail_web_sign_out", "forget": True}, None, None,
                                          store=self.store), {"ok": True, "sessions": 1, "passkeys": 1})
        self.assertEqual(self.api({"action": "status"})[1]["enrolled"], False)
        self.cookie = cookie
        self.assertEqual(self.api({"action": "mail_list"})[0], 401)
        self.assertEqual(actions.dispatch({"action": "mail_web_sign_out"}, None, None, store=self.store),
                         {"ok": False, "error": "forget must be true or false"})
        audited = [json.loads(x) for x in actions.AUDIT.read_text().splitlines()]
        self.assertEqual([(a["action"], a["forget"]) for a in audited], [("mail_web_sign_out", False),
                                                                         ("mail_web_sign_out", True)])

    # replying

    def approve(self, request, phone=None, action="mail_reply", approves=None):
        """The API request for step-up `action` carrying `request` (its JSON object, as app.js sends it), approved by
        the phone's passkey (or `phone`) for action `approves` (by default `action` itself)."""
        text = json.dumps(request)
        status, got = self.api({"action": "stepup_begin", "approves": approves or action,
                                "digest": hashlib.sha256(text.encode()).hexdigest()})
        self.assertEqual(status, 200, got)
        return {"action": action, "request": text, "credential": (phone or self.phone).get(got["options"])}

    def test_a_reply_needs_the_passkey_again_for_exactly_that_reply(self):
        self.signed_in()
        self.agent_mail.answers = [answer({"ok": True, "key": "i31", "status": "sending"})]
        reply = {"thread": THREAD, "key": "k" * 22, "text": "Ship it.",
                 "files": [{"name": "plot.png", "data": base64.b64encode(b"\x89PNG").decode()}]}
        self.assertEqual(self.api({"action": "mail_reply", "request": json.dumps(reply)})[0], 400)   # no approval
        req = self.approve(reply)
        changed = dict(req, request=json.dumps(dict(reply, text="Delete everything.")))
        self.assertEqual(self.api(changed), (403, {"ok": False, "error": "the request is not the one the passkey "
                                                                         "approved"}))
        self.assertEqual(self.agent_mail.calls, [])
        req = self.approve(reply)
        self.assertEqual(self.api(req), (200, {"ok": True, "key": "i31", "status": "sending",
                                               "draft": {"cleared": False, "rev": None}}))
        (cmd, stdin), = self.agent_mail.calls
        self.assertEqual(cmd[1:], ["mailbox", "answer", "--key", "k" * 22, "--json", "--", THREAD])
        self.assertEqual(json.loads(stdin), {"text": "Ship it.", "files": reply["files"]})
        self.assertEqual(self.api(req)[0], 403)                                  # an approval is used once
        self.assertEqual(len(self.agent_mail.calls), 1)
        self.assertIn("mail_reply sent: sending", self.log())

    def test_a_reply_is_approved_only_by_the_session_s_own_passkey(self):
        self.signed_in()
        tablet_key = Authenticator()
        self.enrol(phone=tablet_key)                       # a second passkey; its enrolment signs in again
        self.cookie = None
        self.sign_in()                                     # this session is the phone's own passkey's
        self.assertEqual(self.api(self.approve({"thread": THREAD, "key": "k" * 22, "text": "Ship it."},
                                               phone=tablet_key))[0], 403)
        self.assertEqual(self.agent_mail.calls, [])

    def test_a_reply_needs_a_session_even_when_approved(self):
        self.signed_in()
        req = self.approve({"thread": THREAD, "key": "k" * 22, "text": "Ship it."})
        self.api({"action": "signout"})
        self.assertEqual(self.api(req)[0], 401)
        self.assertEqual(self.agent_mail.calls, [])

    def test_a_reply_approval_is_refused_without_the_user_verified(self):
        self.signed_in()
        reply = json.dumps({"thread": THREAD, "key": "k" * 22, "text": "Ship it."})
        status, got = self.api({"action": "stepup_begin", "approves": "mail_reply",
                                "digest": hashlib.sha256(reply.encode()).hexdigest()})
        status, got = self.api({"action": "mail_reply", "request": reply,
                                "credential": self.phone.get(got["options"], flags=webauthn.UP)})
        self.assertEqual(status, 403)
        self.assertEqual(self.agent_mail.calls, [])
        self.assertEqual(self.api({"action": "stepup_begin", "approves": "mail_reply", "digest": "not hex"})[0], 400)

    def test_sign_in_calls_cannot_push_out_a_reply_waiting_for_approval(self):
        self.signed_in()
        self.agent_mail.answers = [answer({"ok": True, "key": "i31", "status": "sending"})]
        req = self.approve({"thread": THREAD, "key": "k" * 22, "text": "Ship it."})
        for _ in range(64):
            self.api({"action": "signin_begin"})
        self.assertEqual(self.api(req)[0], 200)

    def test_fast_mode_needs_the_session_alone_not_the_passkey_again(self):
        """It carries no words to an agent: switched as a mark is, with the session, and on the record."""
        self.signed_in()
        item = dict(LISTED["threads"][0], fast={"since": self.now, "until": self.now + 300, "minutes": 5,
                                                "agent": "fab", "effort": "low", "was": "high",
                                                "note": "Effort low until fast mode ends, then high again."})
        self.agent_mail.answers = [answer({"ok": True, "thread": item})]
        self.assertEqual(self.api({"action": "mail_fast", "thread": THREAD, "on": True}),
                         (200, {"ok": True, "thread": item}))
        (cmd, stdin), = self.agent_mail.calls
        self.assertEqual((cmd[1:], stdin), (["mailbox", "fast", "--", THREAD, "on"], None))
        self.assertEqual([(r["action"], r["thread"], r["on"]) for r in self.audited()], [("mail_fast", THREAD, True)])
        status, got = self.api({"action": "mail_fast", "thread": THREAD, "on": "yes"})
        self.assertEqual((status, got["ok"]), (200, False))                        # the action's own check
        self.assertEqual(len(self.agent_mail.calls), 1)
        self.assertIn("mail_fast failed: thread must be a thread id and on true or false", self.log())

    def test_researcher_needs_the_session_and_passkey_approval_of_the_exact_switch(self):
        body = {"thread": THREAD, "on": True, "expected_tag": "fab00001"}
        direct = dict(body, action="mail_researcher")
        self.assertEqual(self.api(direct)[0], 401)
        self.signed_in()
        self.assertEqual(self.api(direct)[0], 400)
        wrong_action = self.approve(body, action="mail_researcher", approves="mail_reply")
        self.assertEqual(self.api(wrong_action)[0], 403)
        for tampered in (dict(body, on=False), dict(body, expected_tag="fab00002")):
            changed = dict(self.approve(body, action="mail_researcher"), request=json.dumps(tampered))
            self.assertEqual(self.api(changed)[0], 403)
        self.assertEqual(self.agent_mail.calls, [])
        item = dict(LISTED["threads"][0], researcher={"on": True, "agent": "fab", "tag": "fab00001"})
        self.agent_mail.answers = [answer({"ok": True, "thread": item})]
        approved = self.approve(body, action="mail_researcher")
        self.assertEqual(self.api(approved), (200, {"ok": True, "thread": item}))
        self.assertEqual(self.api(approved)[0], 403)
        (cmd, stdin), = self.agent_mail.calls
        self.assertEqual((cmd[1:], stdin), (["mailbox", "researcher", "--expect-tag", "fab00001", "--", THREAD,
                                            "on"], None))

    # messaging the orchestrator

    def test_a_message_to_the_orchestrator_needs_the_passkey_again_for_exactly_that_text(self):
        """The orchestrator can start agents and act as the owner: a message needs an approval of exactly its text,
        starts one event whose header says it came from the phone (whatever the request says), and goes on the Sent
        list."""
        self.signed_in()
        message = {"key": "k" * 22, "text": "Spin up an agent on proj to add --help."}
        self.assertEqual(self.api({"action": "orchestrate", "request": json.dumps(message)}),
                         (400, {"ok": False, "error": "no credential"}))
        changed = dict(self.approve(message, action="orchestrate"),
                       request=json.dumps(dict(message, text="Close every agent.")))
        self.assertEqual(self.api(changed), (403, {"ok": False, "error": "the request is not the one the passkey "
                                                                         "approved"}))
        self.assertEqual(self.popen.calls, [])
        req = self.approve(dict(message, source="mac"), action="orchestrate")    # a source of the page's own: not taken
        status, got = self.api(req)
        self.assertEqual((status, got["ok"], got["output"]), (200, True, "The orchestrator is running; it will email "
                                                                         "you."))
        (call,) = self.popen.calls
        self.assertEqual(call["cmd"], [str(self.root / "bin" / "systemd-run"), "--user", "--scope", "--collect",
                                       "--unit", got["unit"], "--", str(self.root / "bin" / "agent-orchestrate"),
                                       "--source", "board"])
        body = call["stdin"].decode()
        (kept,) = sent.messages()
        self.assertEqual((kept["kind"], kept["to"], kept["ok"], kept["text"]),
                         ("orchestrate", "the orchestrator", True, message["text"]))
        header = actions.ORCH_HEADER_FMT.format(source=actions.ORCH_SOURCES["phone"], owner="Alex")
        self.assertTrue(body.startswith(header + "\n\n" + message["text"] + "\n\nThis is message %s on the board."
                                        % kept["ref"]))
        self.assertEqual([(a["action"], a["source"], a["key"]) for a in self.audited()],
                         [("orchestrate", "phone", message["key"])])
        self.assertIn("orchestrate started: %s" % got["log"], self.log())
        self.assertEqual(self.api(req)[0], 403)                                  # an approval is used once
        req = self.approve(dict(message, key="j" * 22), action="orchestrate")
        self.api({"action": "signout"})
        self.assertEqual(self.api(req)[0], 401)                                  # and needs the session still
        self.assertEqual((len(self.popen.calls), self.agent_mail.calls), (1, []))

    def test_an_approval_is_for_the_one_action_it_names(self):
        """A reply's approval sends no message to the orchestrator, nor the reverse, even of the same text."""
        self.signed_in()
        reply = {"thread": THREAD, "key": "k" * 22, "text": "Ship it."}
        for approves, action in (("mail_reply", "orchestrate"), ("orchestrate", "mail_reply")):
            req = self.approve(reply, action=action, approves=approves)
            self.assertEqual(self.api(req), (403, {"ok": False, "error": "the passkey approved %s, not %s"
                                                                         % (approves, action)}))
            self.assertEqual(self.api(dict(req, action=approves))[0], 403)       # and the approval is used up
        self.assertEqual((self.popen.calls, self.agent_mail.calls), ([], []))
        for approves in ("mail_list", "signin", None, ["orchestrate"]):
            status, got = self.api({"action": "stepup_begin", "approves": approves, "digest": "0" * 64})
            self.assertEqual((status, got["error"]),
                             (400, "approves must be one of mail_reply, mail_researcher, orchestrate, keyring_unlock"),
                             approves)

    def test_a_message_sent_again_after_its_answer_was_lost_starts_no_second_run(self):
        """The phone sends it again with its key and a fresh approval: the desktop answers from the first run."""
        self.signed_in()
        message = {"key": "k" * 22, "text": "Start an agent on proj."}
        first = self.api(self.approve(message, action="orchestrate"))[1]     # its answer lost on the way to the phone
        status, again = self.api(self.approve(message, action="orchestrate"))
        self.assertEqual((status, again), (200, {"ok": True, "already": True, "log": first["log"], "output":
                                                 "This message started the orchestrator already; it will email you."}))
        self.assertEqual((len(self.popen.calls), len(sent.messages())), (1, 1))

    def test_a_request_that_is_not_text_is_refused_and_one_the_page_did_not_foresee_still_answered(self):
        """A lone surrogate (half of a UTF-16 pair) in the request: 400, its challenge kept. An unexpected failure
        (agent-mail's stand-in raising): 500, with the security headers, and the traceback in the journal."""
        self.signed_in()
        status, got = self.api({"action": "stepup_begin", "approves": "orchestrate",
                                "digest": hashlib.sha256(b'{"text": "a\xef\xbf\xbdb"}').hexdigest()})
        lone = {"action": "orchestrate", "request": '{"text": "a\ud800b"}', "credential": self.phone.get(got["options"])}
        self.assertEqual(self.api(lone), (400, {"ok": False, "error": "the request holds a lone surrogate (half of a "
                                                "UTF-16 pair), which is not text a passkey can approve"}))
        self.assertEqual(self.popen.calls, [])

        def broken(*args, **kw):
            raise RuntimeError("agent-mail's stand-in broke")
        self.agent_mail = broken
        status, headers, body = self.ask("POST", "/api", b'{"action": "mail_list"}', **{"X-Agent-Board": "1"})
        self.assertEqual((status, json.loads(body)), (500, {"ok": False, "error": "the desktop failed on this request "
                                                                                  "(RuntimeError); its journal says why"}))
        for name, value in mailweb.SECURITY_HEADERS:
            self.assertEqual(headers[name], value, name)
        self.assertIn("RuntimeError: agent-mail's stand-in broke", self.log())
        self.assertEqual(self.api({"action": "status"})[0], 200)                 # and the server answers on

    def test_the_largest_reply_fits_in_a_request(self):
        size = mailbox.MAX_FILE_BYTES // mailbox.MAX_FILES
        files = [{"name": "f%d" % i, "data": base64.b64encode(b"x" * size).decode()} for i in range(mailbox.MAX_FILES)]
        reply = json.dumps({"thread": THREAD, "key": "k" * 22, "text": "\x01" * mailbox.MAX_REPLY, "files": files})
        cred = self.phone.get({"challenge": b64(b"c" * 32)})
        self.assertLessEqual(len(json.dumps({"action": "mail_reply", "request": reply, "credential": cred}).encode()),
                             mailweb.MAX_BODY)

    # unlocking the desktop

    def post(self, req, after=None):
        """(status, answer) of an API call as app.js sends it: `req` as one line of JSON, then `after` (bytes) on the
        next, as an unlock carries the password. Each answer is kept, for assert_nowhere."""
        body = json.dumps(req).encode() + (b"" if after is None else b"\n" + after)
        status, _, got = self.ask("POST", "/api", body, **{"X-Agent-Board": "1"})
        self.answers.append(got)
        return status, json.loads(got)

    def unlock(self, typed=TYPED.encode(), asked=None):
        """(status, answer) of an unlock carrying `typed`, approved by the phone's passkey for a request `asked` (by
        default a new nonce, as app.js makes one)."""
        return self.post(self.approve(asked or {"nonce": webauthn.b64url(os.urandom(16)).replace("-", "_")},
                                      action=mailweb.UNLOCK), typed)

    def page_keyring(self):
        """What the page is told of the stand-in keyrings' state now (Guard.keyring)."""
        return {k: self.keyring.state[k] for k in ("state", "error", "collections")}

    def assert_nowhere(self, typed=TYPED):
        """`typed` is in no answer the server gave, nor its journal, nor any file the server or the board's records
        hold (but the stand-in's own copy of its stdin): neither whole nor its first 16 characters, which the journal
        does not escape (it doubles a backslash), spelt as UTF-8 or as JSON escapes them."""
        spellings = {spelt for text in (typed, typed[:16]) for spelt in (
            text.encode(), json.dumps(text)[1:-1].encode(), json.dumps(text, ensure_ascii=False)[1:-1].encode())}
        places = {"an answer": b"\n".join(self.answers), "the journal": self.log().encode()}
        for folder in (self.d, self.root.parent):                 # the page's state; actions.jsonl, sent.jsonl
            for f in folder.rglob("*"):
                if f.is_file() and not f.is_relative_to(self.keyring.folder):
                    places[str(f)] = f.read_bytes()
        for where, data in places.items():
            for spelt in spellings:
                self.assertNotIn(spelt, data, where)

    def test_an_unlock_s_password_goes_to_keyring_unlock_s_stdin_alone_once_the_passkey_approved_its_nonce(self):
        """Exactly as typed, and only there: never its arguments or environment, an answer, the journal, the record or
        a file. The command gets only the environment the core needs, whatever the server's holds (LD_PRELOAD,
        PYTHONPATH). The outcome alone is on the record, and an approval unlocks once."""
        self.signed_in()
        self.keyring.lock()
        self.keyring.answer(0, {"result": "unlocked", "locked": False})
        req = self.approve({"nonce": NONCE}, action=mailweb.UNLOCK)
        self.keyring.lock("unlocked")                                            # as the unlock leaves it
        with mock.patch.dict(os.environ, {"LD_PRELOAD": "", "PYTHONPATH": str(self.d)}):
            self.assertEqual(self.post(req, TYPED.encode()), (200, {"ok": True, "unlock": {"result": "unlocked",
                                                                                           "locked": False},
                                                                    "keyring": self.page_keyring()}))
        (run,) = self.keyring.runs()
        self.assertEqual((run["stdin"], run["argv"], run["in_env"]), (TYPED.encode(), [], False))
        # LC_CTYPE: the stand-in's own Python sets it as it starts, given no locale (PEP 538)
        self.assertLessEqual(set(run["env"]) - {"LC_CTYPE"}, set(keyring.ENVIRONMENT))
        self.assertIn("HOME", run["env"])
        self.assertEqual([{k: v for k, v in a.items() if k != "ts"} for a in self.audited()],
                         [{"action": mailweb.UNLOCK, "result": "unlocked"}])
        self.assertIn("keyring_unlock: unlocked", self.log())
        self.assertEqual(self.post(req, TYPED.encode())[0], 403)                # an approval is used once
        self.assertEqual(len(self.keyring.runs()), 1)
        self.assert_nowhere()

    def test_an_unlock_needs_a_session_and_an_approval_of_this_unlock_s_own_nonce(self):
        """Not without one, nor with one of another nonce or of another action (nor the reverse), nor for a request
        that is not a nonce, nor with no password after it; nothing runs, and the password is nowhere."""
        self.signed_in()
        text = json.dumps({"nonce": NONCE})
        self.assertEqual(self.post({"action": mailweb.UNLOCK, "request": text}, TYPED.encode()),
                         (400, {"ok": False, "error": "no credential"}))
        other = dict(self.approve({"nonce": "m" * 22}, action=mailweb.UNLOCK), request=text)
        self.assertEqual(self.post(other, TYPED.encode()),
                         (403, {"ok": False, "error": "the request is not the one the passkey approved"}))
        reply = self.approve({"nonce": NONCE}, action=mailweb.UNLOCK, approves="mail_reply")
        self.assertEqual(self.post(reply, TYPED.encode()),
                         (403, {"ok": False, "error": "the passkey approved mail_reply, not keyring_unlock"}))
        unlock = self.approve({"thread": THREAD, "key": "k" * 22, "text": "Ship it."}, approves=mailweb.UNLOCK)
        self.assertEqual(self.api(unlock), (403, {"ok": False, "error": "the passkey approved keyring_unlock, not "
                                                                        "mail_reply"}))
        for asked in ({"nonce": "short"}, {"nonce": NONCE, "also": "this"}, {"nonce": 7}, {"key": NONCE}):
            self.assertEqual(self.unlock(asked=asked), (400, {"ok": False, "error": 'an unlock\'s request is {"nonce": '
                                                                                    'a new random key}'}), asked)
        self.assertEqual(self.post(self.approve({"nonce": NONCE}, action=mailweb.UNLOCK)),
                         (400, {"ok": False, "error": "the password must follow the request's line"}))
        req = self.approve({"nonce": NONCE}, action=mailweb.UNLOCK)
        self.api({"action": "signout"})
        self.assertEqual(self.post(req, TYPED.encode())[0], 401)
        self.assertEqual((self.keyring.runs(), self.agent_mail.calls, self.audited()), ([], [], []))
        self.assert_nowhere()

    def test_a_password_keyring_unlock_would_refuse_is_refused_before_it_runs(self):
        """By the core's own rule (keyring.password_end): 1 to 1024 bytes of UTF-8 on one line with no NUL, which may
        end in one newline, taken off, and does not end in a carriage return. The refusal never repeats the input or
        its length."""
        self.signed_in()
        self.keyring.answer(1, {"result": "wrong", "locked": True})
        longer, one_line, none = ("the password is longer than 1024 bytes", "the password must be one line with no "
                                  "NUL in it", "no password was given")
        return_ = "the password must not end in a carriage return"           # a client ending its lines CRLF
        for typed, said in ((b"x" * 1025, longer), (b"x" * 1025 + b"\n", longer), ("é".encode() * 513, longer),
                            (b"sec\0ret", one_line), (b"sec\nret", one_line), (b"secret\n\n", one_line),
                            (b"", none), (b"\n", none), (b"secret\r\n", return_), (b"secret\r", return_)):
            self.assertEqual(self.unlock(typed), (400, {"ok": False, "error": said}), typed[:12])
        self.assertEqual((self.keyring.runs(), self.audited()), ([], []))
        for typed in (b"y" * 1024, b"y" * 1024 + b"\n", "é".encode() * 512, TYPED.encode() + b"\n"):
            self.assertEqual(self.unlock(typed)[0], 200)
        self.assertEqual([r["stdin"] for r in self.keyring.runs()],
                         [b"y" * 1024, b"y" * 1024, "é".encode() * 512, TYPED.encode()])
        self.assert_nowhere()

    def test_keyring_unlock_s_answer_is_passed_on_as_it_gave_it(self):
        """Each exit code's answer (4: partial, one keyring opened and not the other), as its one JSON line has it, its
        outcome on the record, and the keyrings' state read straight after."""
        self.signed_in()
        self.keyring.lock()
        for code, said in ((0, {"result": "unlocked", "locked": False}), (1, {"result": "wrong", "locked": True}),
                           (2, {"result": "rate_limited", "locked": True, "retry_after": 125}),
                           (3, {"result": "error", "locked": None, "error": "no session bus: neither "
                                "DBUS_SESSION_BUS_ADDRESS nor XDG_RUNTIME_DIR is set"}),
                           (4, {"result": "partial", "locked": True, "tried": {"default": "wrong", "login": "unlocked"},
                                "collections": stand_ins.collections(True, False)})):
            if code == 4:
                self.keyring.lock("locked", login=False)                        # as a partial unlock leaves them
            reads = self.keyring.reads
            self.keyring.answer(code, said)
            status, got = self.unlock()
            self.assertEqual((status, got["ok"], got["unlock"], got["keyring"]),
                             (200, True, said, self.page_keyring()), code)
            self.assertEqual(self.keyring.reads, reads + 1, code)                # the state read straight after
        self.assertEqual([a["result"] for a in self.audited()],
                         ["unlocked", "wrong", "rate_limited", "error", "partial"])
        self.assert_nowhere()

    def test_a_keyring_unlock_that_cannot_run_or_does_not_answer_as_it_does_is_an_error_saying_why(self):
        """Not its one JSON line, an exit code that is not its result's, no answer in time (it is stopped), or no
        command to run: the page is told why, never as a result the core did not give, and the record says error."""
        self.signed_in()
        self.addCleanup(setattr, mailweb, "UNLOCK_SECONDS", mailweb.UNLOCK_SECONDS)
        mailweb.UNLOCK_SECONDS = 1
        unlocked = json.dumps({"result": "unlocked", "locked": False})
        for code, said, why in ((3, "Traceback (most recent call last): ...", "exited 3 without its answer: Traceback "
                                 "(most recent call last): ..."),
                                (1, unlocked, "answered unlocked but exited 1"),
                                (0, unlocked + "\n" + unlocked, "exited 0 without its answer: " + unlocked + "\n"
                                 + unlocked),
                                (0, '{"result": "fine"}', 'exited 0 without its answer: {"result": "fine"}')):
            self.keyring.answer(code, said)
            self.assertEqual(self.unlock(), (502, {"ok": False, "error": "keyring-unlock " + why}), why)
        self.keyring.answer(0, {}, hangs=True)
        started = time.monotonic()
        self.assertEqual(self.unlock(), (502, {"ok": False, "error": "keyring-unlock gave no answer within 1 s, so it "
                                                                     "was stopped; the desktop may count it as a "
                                                                     "wrong password"}))
        self.assertLess(time.monotonic() - started, 30)                         # stopped, not waited for its 60 s
        missing = self.d / "no-such-agent-board"
        self.guard.unlock_command = [str(missing), "keyring-unlock"]
        self.assertEqual(self.unlock(), (502, {"ok": False, "error": "keyring-unlock could not start: [Errno 2] No "
                                                                     "such file or directory: '%s'" % missing}))
        self.assertEqual([a["result"] for a in self.audited()], ["error"] * 6)
        self.assert_nowhere()

    def test_while_the_keyring_unlock_is_off_nothing_is_read_and_an_unlock_is_refused(self):
        """The board config's keyring_unlock off (the default): serve() gives the guard no keyring reader and no unlock
        command, so the status carries no keyring state and an unlock is refused before its approval is looked at."""
        self.guard.read_keyring = self.guard.unlock_command = None
        self.signed_in()
        self.assertIsNone(self.api({"action": "status"})[1]["keyring"])
        status, got = self.unlock()
        self.assertEqual((status, got), (403, {"ok": False, "error": keyring.OFF}))
        self.assertEqual((self.keyring.reads, self.keyring.runs()), (0, []))
        self.assert_nowhere()

    def test_only_an_unlock_carries_anything_after_its_line(self):
        self.signed_in()
        for req in ({"action": "mail_list"}, {"action": "status"}, {"action": "signout"}):
            self.assertEqual(self.post(req, TYPED.encode()), (400, {"ok": False, "error": "only keyring_unlock carries "
                                                                                         "anything after its request's "
                                                                                         "line"}), req)
        self.assertEqual(self.api({"action": "status"})[1]["signed_in"], True)  # the sign-out did not run
        self.assertEqual((self.agent_mail.calls, self.keyring.runs()), ([], []))
        self.assert_nowhere()

    def test_the_keyring_s_state_comes_with_the_session_s_status_read_at_most_once_a_minute(self):
        """keyring.REFRESH seconds, as the board reads it; again straight after an unlock; a read that fails is the
        state unknown, with why, and is not kept. Nothing is read for a phone that is not signed in."""
        status, got = self.api({"action": "status"})
        self.assertEqual((got["signed_in"], got["keyring"], self.keyring.reads), (False, None, 0))
        self.signed_in()

        def keyring_said():
            return self.api({"action": "status"})[1]["keyring"]
        self.keyring.lock()
        self.assertEqual(keyring_said(), self.page_keyring())
        self.assertEqual(keyring_said()["collections"]["default"]["label"], "Default keyring")
        self.keyring.lock("unlocked")
        self.now += keyring.REFRESH - 1
        self.assertEqual((keyring_said()["state"], self.keyring.reads), ("locked", 1))          # kept
        self.now += 1
        self.assertEqual((keyring_said()["state"], self.keyring.reads), ("unlocked", 2))        # read again
        self.keyring.lock("unknown", "Chrome's keyring (the \"default\" alias) could not be read: busctl timed out")
        self.now += keyring.REFRESH
        self.assertEqual(keyring_said(), self.page_keyring())

        def no_bus():
            raise keyring.KeyringError("no session bus: neither DBUS_SESSION_BUS_ADDRESS nor XDG_RUNTIME_DIR is set")
        self.guard.read_keyring = no_bus
        self.now += keyring.REFRESH
        self.assertEqual(keyring_said(), {"state": "unknown", "collections": None,
                                          "error": "its state could not be read: KeyringError: no session bus: "
                                                   "neither DBUS_SESSION_BUS_ADDRESS nor XDG_RUNTIME_DIR is set"})
        self.guard.read_keyring = self.keyring.read
        self.keyring.lock()
        reads = self.keyring.reads
        self.assertEqual(keyring_said()["state"], "locked")                     # a failed read was not kept
        self.keyring.answer(0, {"result": "unlocked", "locked": False})
        self.keyring.lock("unlocked")
        self.assertEqual(self.unlock()[1]["keyring"], self.page_keyring())   # read straight after
        self.assertEqual((keyring_said()["state"], self.keyring.reads), ("unlocked", reads + 2))

    def test_the_unlock_command_is_the_core_s_on_the_system_python_in_isolated_mode_as_the_mac_runs_it(self):
        """The page's command, the Mac's (Remote.swift: the system's Python in isolated mode on the script the kit's
        wrapper runs) and bin/agent-keyring-unlock (the same from a shell, through its own kit wrapper, which execs it
        as it is) are one: the system's Python in isolated mode on this checkout's agent-board."""
        self.assertEqual(mailweb.UNLOCK_COMMAND, ["/usr/bin/python3", "-I", str(mailweb.ROOT / "bin" / "agent-board"),
                                                  "keyring-unlock"])
        script = (mailweb.ROOT / "bin" / "agent-keyring-unlock").read_text()
        self.assertTrue(script.startswith("#!/bin/sh\n"))          # not a Python tool: the kit wraps it as it is
        self.assertIn('\nexec %s "$here/agent-board" keyring-unlock\n' % " ".join(mailweb.UNLOCK_COMMAND[:2]), script)

    def unlock_held(self, while_held):
        """(status, answer) of an unlock whose keyring-unlock is held as it starts, before it hardens (the stand-in's
        `hold`): `while_held(pid)` runs then, and the command is let go. The unlock is sent from a thread of its own."""
        answered = {}
        sender = threading.Thread(target=lambda: answered.update(got=self.unlock()))
        sender.start()
        run = len(self.keyring.runs()) + 1
        self.until_true(lambda: self.keyring.pid(run), "the command to start")
        try:
            while_held(self.keyring.pid(run))
        finally:
            self.keyring.release(run)
            sender.join(60)
        return answered["got"]

    def test_the_password_goes_only_once_keyring_unlock_has_made_itself_undumpable(self):
        """Before that, another process of the owner's could read its memory. A command slow to harden (held, here, until
        the server has had every chance to write) is waited for; one that never does gets nothing and is stopped; one
        that ends first gets nothing, and its answer is passed on without waiting."""
        self.signed_in()
        self.addCleanup(setattr, mailweb, "HARDEN_SECONDS", mailweb.HARDEN_SECONDS)
        wrong = {"result": "wrong", "locked": True}
        self.keyring.answer(1, wrong, hold=True)
        self.assertEqual(self.unlock_held(lambda pid: None)[1]["unlock"], wrong)
        (slow,) = self.keyring.runs()
        self.assertEqual((slow["early"], slow["stdin"]), (False, TYPED.encode()))   # nothing there before it hardened
        mailweb.HARDEN_SECONDS = 2
        self.keyring.answer(1, wrong, hardens=False)
        self.assertEqual(self.unlock(), (502, {"ok": False, "error": "keyring-unlock did not make itself undumpable "
                                                                     "within 2 s, so the password was not sent"}))
        never = self.keyring.runs()[1]
        self.assertEqual((never["early"], never["stdin"]), (False, None))
        mailweb.HARDEN_SECONDS = 60
        refused = {"result": "error", "locked": None, "error": "run it as `/usr/bin/python3 -I ...`"}
        self.guard.unlock_command = [sys.executable, "-I", "-c", "import json; print(json.dumps(%r)); "
                                     "raise SystemExit(3)" % refused]
        started = time.monotonic()
        self.assertEqual(self.unlock()[1]["unlock"], refused)
        self.assertLess(time.monotonic() - started, 30)                         # not waited for its 60 s
        self.assert_nowhere()

    def test_keyring_unlock_s_stdin_is_a_socket_no_other_process_can_open_through_proc(self):
        """While the command starts, before it is undumpable, another process of the owner's could open a pipe through
        /proc/<pid>/fd/0 and keep it to read the password sent later; a socket cannot be opened there (ENXIO). The
        password still arrives, after the command has hardened."""
        self.signed_in()
        wrong = {"result": "wrong", "locked": True}
        self.keyring.answer(1, wrong, hold=True)
        opened = []

        def open_its_stdin(pid):
            try:
                open("/proc/%d/fd/0" % pid, "rb").close()
                opened.append(None)
            except OSError as e:
                opened.append(e.errno)
        self.assertEqual(self.unlock_held(open_its_stdin)[1]["unlock"], wrong)
        self.assertEqual(opened, [errno.ENXIO])
        (run,) = self.keyring.runs()
        self.assertEqual((run["stdin_socket"], run["early"], run["stdin"]), (True, False, TYPED.encode()))
        self.assert_nowhere()

    def test_a_body_shorter_than_its_length_is_refused_before_anything_runs(self):
        """A hand-made client that sends less than it said: refused, its approval kept, nothing run or recorded."""
        self.signed_in()
        self.keyring.answer(1, {"result": "wrong", "locked": True})
        req = self.approve({"nonce": NONCE}, action=mailweb.UNLOCK)
        body = json.dumps(req).encode() + b"\n" + TYPED.encode()
        status, _, got = self.ask("POST", "/api", body, end_input=True,
                                  **{"X-Agent-Board": "1", "Content-Length": str(len(body) + 5)})
        self.answers.append(got)
        self.assertEqual((status, json.loads(got)), (400, {"ok": False, "error": "the request ended before its "
                                                                                 "Content-Length"}))
        self.assertEqual((self.keyring.runs(), self.audited()), ([], []))
        self.assertEqual(self.post(req, TYPED.encode())[0], 200)              # the approval was not used up
        self.assert_nowhere()

    def test_no_copy_of_the_password_is_left_in_the_server_s_memory(self):
        """Read from this process's own memory, which the server runs in, once the unlock is answered and its thread
        has ended: the request is read unbuffered straight into the one buffer that is zeroed (a buffered reader kept a
        copy). The password is sent by a process of its own, and this one holds it only as two halves apart, so any
        place where the first half runs straight into the second is a copy the server left."""
        self.signed_in()
        self.keyring.answer(1, {"result": "wrong", "locked": True})
        first, second = secrets.token_hex(8), secrets.token_hex(8)
        line = json.dumps(self.approve({"nonce": NONCE}, action=mailweb.UNLOCK))
        threads = threading.active_count()
        sent = subprocess.run([sys.executable, "-I", "-c", SEND_UNLOCK, str(self.sock), self.cookie, line, first,
                               second], capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(sent.stdout.split("\n\n", 1)[1])["unlock"]["result"], "wrong", sent.stderr)
        self.until_true(lambda: threading.active_count() <= threads, "the request's thread to end")
        self.assertEqual(len(self.keyring.runs()), 1)
        self.assertEqual(copies_in_memory(first.encode(), second.encode()), 0)

    def until_true(self, condition, what, seconds=5):
        ends = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > ends:
                self.fail("waited %s seconds for %s" % (seconds, what))
            time.sleep(0.02)

    # how it runs

    def test_the_page_s_unit_runs_it_in_isolated_mode(self):
        """So nothing from a user site, PYTHONPATH or the current folder loads in the process that holds the page's
        sessions and mail, and the password when the keyring unlock is on; the installer puts the kit venv's Python in
        for {{PYTHON}}."""
        unit = (mailweb.ROOT / "systemd" / "agent-board-mail-web.service.in").read_text()
        (start,) = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
        self.assertEqual(start.split("=", 1)[1].split(), ["{{PYTHON}}", "-I", "{{KIT_DIR}}/board/bin/agent-board",
                                                         "mail-web"])

    def test_the_server_makes_itself_undumpable_before_it_reads_anything(self):
        """serve() in a process of its own, stopped as it reads its settings: by then it has no core files and its
        memory and descriptors are out of other processes' reach."""
        probe = ("import ctypes, json, resource, sys; sys.path.insert(0, %r)\n"
                 "from agentboard import mailweb, mailwebstate\n"
                 "def settings():\n"
                 "    print(json.dumps([ctypes.CDLL(None).prctl(3, 0, 0, 0, 0), "
                 "resource.getrlimit(resource.RLIMIT_CORE)]))\n"
                 "    raise SystemExit(0)\n"
                 "mailwebstate.settings = settings\n"
                 "mailweb.serve()\n" % str(mailweb.ROOT))
        r = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(r.stdout), [0, [0, 0]], r.stderr)          # PR_GET_DUMPABLE 0, RLIMIT_CORE 0

    def test_an_unlock_whose_outcome_cannot_be_recorded_says_so_in_its_answer(self):
        """As the board's other actions do (actions.audit): the outcome stands, and the answer's error says the record
        failed; so does a refusal's."""
        self.signed_in()
        actions.AUDIT = self.d / "config.json" / "actions.jsonl"              # under a file: it cannot be written
        self.keyring.answer(1, {"result": "wrong", "locked": True})
        status, got = self.unlock()
        unrecorded = "the board could not record this action: [Errno 17] File exists: '%s'" % (self.d / "config.json")
        self.assertEqual((status, got["unlock"], got["error"]), (200, {"result": "wrong", "locked": True}, unrecorded))
        self.keyring.answer(1, {"result": "unlocked", "locked": False})
        self.assertEqual(self.unlock(), (502, {"ok": False, "error": "keyring-unlock answered unlocked but exited 1; "
                                                                     + unrecorded}))
        self.assertIn("keyring_unlock: " + unrecorded, self.log())
        self.assert_nowhere()

    def test_serve_refuses_settings_that_name_no_device_proxy_or_login(self):
        """With a clear reason, before it touches tailscale (the unit then tries again every 10 seconds)."""
        saved = mailwebstate.CONFIG
        self.addCleanup(setattr, mailwebstate, "CONFIG", saved)
        mailwebstate.CONFIG = self.d / "mailweb.json"
        for cfg, said in (({}, "must list the phone's Tailscale node id"),
                          ({"devices": [PHONE]}, "must list the phone's Tailscale node id"),
                          ({"devices": [PHONE], "proxy_uids": [0], "port": 443}, "not one Funnel can open")):
            mailwebstate.CONFIG.write_text(json.dumps(cfg))
            with mock.patch.object(mailweb.lockdown, "harden"), self.assertRaisesRegex(ValueError, said):
                mailweb.serve()
        mailwebstate.CONFIG.write_text(json.dumps({"devices": [PHONE], "proxy_uids": [0]}))
        sources.MAIL_CONFIG.write_text(json.dumps({}))                           # no owner address, no logins
        with mock.patch.object(mailweb.lockdown, "harden"), self.assertRaisesRegex(ValueError, "answer no one"):
            mailweb.serve()

    # what is served

    def test_the_widget_renderer_is_served_from_its_one_copy(self):
        status, headers, body = self.ask("GET", "/widgets.js")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/javascript; charset=utf-8"))
        self.assertEqual(body, (mailweb.ROOT / "mac" / "Sources" / "AgentBoard" / "widgets.js").read_bytes())
        for name, value in mailweb.SECURITY_HEADERS:
            self.assertEqual(headers[name], value, name)
        self.assertIn(b'<script src="widgets.js">', self.ask("GET", "/")[2])

    def test_the_app_s_manifest_and_icons_are_served_with_the_security_headers(self):
        status, headers, body = self.ask("GET", "/manifest.webmanifest")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/manifest+json"))
        for name, value in mailweb.SECURITY_HEADERS:
            self.assertEqual(headers[name], value, name)
        m = json.loads(body)
        self.assertEqual((m["id"], m["name"], m["short_name"], m["display"], m["start_url"], m["scope"]),
                         ("/", "Agent Mail", "Agent Mail", "standalone", "/", "/"))           # opens on the list
        self.assertNotIn("prefer_related_applications", m)
        for key in ("theme_color", "background_color"):
            self.assertRegex(m[key], "^#[0-9a-f]{6}$")
        sizes = {}
        for icon in m["icons"]:
            status, headers, png = self.ask("GET", icon["src"])
            self.assertEqual((status, headers["Content-Type"]), (200, "image/png"), icon["src"])
            for name, value in mailweb.SECURITY_HEADERS:
                self.assertEqual(headers[name], value, (icon["src"], name))
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            width, height = struct.unpack(">II", png[16:24])                           # the IHDR chunk
            self.assertEqual("%dx%d" % (width, height), icon["sizes"])
            self.assertEqual(icon["type"], "image/png")
            sizes.setdefault(icon["purpose"], set()).add(width)
        self.assertEqual(sizes, {"any": {192, 512}, "maskable": {512}})
        for path in ("/icons/other.png", "/icons/../app.js", "/icons/", "/manifest.json"):
            self.assertEqual(self.ask("GET", path)[0], 404, path)
        for how in (dict(address=MAC_IP), dict(login="someone@else.org"), {"Tailscale-Funnel-Request": "?1"}):
            for path in ("/manifest.webmanifest", "/icons/icon-192.png"):                # only the phone, as the page
                self.assertEqual(self.ask("GET", path, **how)[0], 403, (path, how))
        self.assertEqual(self.agent_mail.calls, [])

    def test_only_icons_in_the_page_s_icons_folder_are_taken_from_the_manifest(self):
        self.assertEqual(sorted(mailweb.manifest_icons()), ["/icons/icon-192.png", "/icons/icon-512.png",
                                                            "/icons/maskable-512.png"])
        bad = self.d / "manifest.webmanifest"
        for src in ("/icons/../app.js", "/icons/a/b.png", "/../README.md", "/icons/icon.svg", "icons/icon-192.png"):
            bad.write_text(json.dumps({"icons": [{"src": src}]}))
            with self.assertRaisesRegex(ValueError, "is not a PNG in web/mail/icons/"):
                mailweb.manifest_icons(bad)

    def test_the_page_links_its_manifest_under_a_policy_that_allows_only_its_own(self):
        status, headers, body = self.ask("GET", "/")
        self.assertIn(b'<link rel="manifest" href="manifest.webmanifest">', body)
        csp = dict(d.strip().split(" ", 1) for d in headers["Content-Security-Policy"].split(";"))
        self.assertEqual(csp, {"default-src": "'none'", "script-src": "'self'", "style-src": "'self' 'unsafe-inline'",
                               "img-src": "'self' data:", "connect-src": "'self'", "manifest-src": "'self'",
                               "base-uri": "'none'", "form-action": "'none'", "frame-ancestors": "'none'"})
        self.assertEqual(self.ask("GET", "/enrol")[2], body)                    # the enrolment link's page is the app

    def test_the_page_s_scripts_keep_nothing_on_the_phone(self):
        # No service worker, no storage: the lists, threads and drafts live in the page's memory alone (README, "Mail
        # on the phone"), and the history holds which view shows, never a thread.
        scripts = [f for f, ctype in mailweb.PAGES.values() if ctype.startswith("text/javascript")]
        self.assertEqual({f.name for f in scripts},
                         {"app.js", "snooze.js", "replytext.js", "widgets.js", "passkeys.js"})
        for f in scripts:
            text = f.read_text()
            for word in ("serviceWorker", "localStorage", "sessionStorage", "indexedDB", "caches.", "document.cookie",
                         "importScripts", "Worker("):
                self.assertNotIn(word, text, f.name)
        app = (mailweb.WEB / "app.js").read_text()
        self.assertEqual(re.findall(r"history\.(?:push|replace)State\(([^,]*),", app),
                         ['null', 'null', '{ view: "thread" }', 'null'])

    def test_every_answer_carries_the_security_headers(self):
        self.signed_in()
        self.agent_mail.answers = [answer({"name": "r.csv", "type": "text/csv", "data": "YSxi"})]
        answers = [self.ask("GET", "/"), self.part("/part/s7/2"),
                   self.ask("POST", "/api", b'{"action": "status"}', **{"X-Agent-Board": "1"}),
                   self.ask("GET", "/", login="someone@else.org"),               # refused
                   self.ask("OPTIONS", "/api"), self.ask("PUT", "/api", b"{}"),     # methods it does not take
                   self.ask("POST", "/api", b'{"action": "status"}'),             # no X-Agent-Board
                   self.ask("GET", "/part/s7/2")]                                 # an attachment without it
        for status, headers, _ in answers:
            self.assertEqual((headers["Cache-Control"], headers["X-Frame-Options"], headers["X-Content-Type-Options"],
                              headers["Referrer-Policy"]), ("no-store", "DENY", "nosniff", "no-referrer"), status)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers["Server"], "agent-board-mail")
        self.assertEqual([a[0] for a in answers[4:]], [501, 501, 404, 404])
        status, headers, body = self.ask("GET", "/")
        self.assertIn(b'<script src="app.js">', body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.ask("GET", "/../README.md")[0], 404)
        self.assertEqual(self.ask("GET", "/index.html.bak")[0], 404)

    def test_the_session_cookie_is_kept_from_scripts_and_other_sites(self):
        code, _ = self.store.mint_code(self.now, 10)
        status, got = self.api({"action": "enrol_begin", "code": code})
        status, headers, _ = self.ask("POST", "/api", json.dumps({"action": "enrol_finish", "credential":
                                      self.phone.create(got["options"])}).encode(), **{"X-Agent-Board": "1"})
        parts = [p.strip() for p in headers["Set-Cookie"].split(";")]
        self.assertTrue(parts[0].startswith("__Host-mailweb="))
        self.assertEqual(set(parts[1:]), {"Path=/", "Max-Age=43200", "Secure", "HttpOnly", "SameSite=Strict"})

    def test_the_api_runs_the_board_s_mail_actions_and_nothing_else(self):
        self.signed_in()
        status, got = self.api({"action": "mail_list", "box": "inbox"})
        self.assertEqual((status, got), (200, dict(LISTED, ok=True)))
        self.assertEqual(self.agent_mail.calls[0][0][1:5], ["mailbox", "list", "--box", "inbox"])
        for req in ({"action": "send", "id": "x", "text": "hi"}, {"action": "mail_part", "key": "s7", "index": 1},
                    {"action": "mail_web_sign_out", "forget": True}, {"action": "wt_spawn", "path": "/", "text": "hi"},
                    ["mail_list"], {}):
            status, got = self.api(req)
            self.assertEqual(status, 400, req)
        status, got = self.api({"action": "mail_show", "thread": "--x"})
        self.assertEqual((status, got["ok"]), (200, False))                        # the action's own check
        self.assertEqual(len(self.agent_mail.calls), 1)
        self.assertIn("%s phone.tail1.ts.net mail_show failed: %s" % (OWNER, got["error"]), self.log())

    def test_the_drafts_are_the_board_s_draft_actions_with_the_session_alone_and_each_change_is_the_phone_s(self):
        status, got = self.api({"action": "draft_list"})
        self.assertEqual(status, 401)                                          # a session first
        self.signed_in()
        draft = {"text": "From the phone", "picks": [], "comments": [], "files": [], "key": None}
        status, got = self.api({"action": "draft_put", "thread": THREAD, "base_rev": 0, "device": "mac",
                                "draft": draft})
        self.assertEqual((status, got["ok"], got["rev"]), (200, True, 1))
        self.assertEqual(got["updated"]["device"], "phone")                    # whatever the request says
        data = base64.b64encode(b"a,b\n").decode()
        status, got = self.api({"action": "draft_file_add", "thread": THREAD, "data": data})
        self.assertEqual(got["file"]["size"], 4)
        status, got = self.api({"action": "draft_list"})
        self.assertEqual(list(got["drafts"]), [THREAD])
        status, got = self.api({"action": "draft_discard", "thread": THREAD, "base_rev": 1})
        self.assertEqual((got["ok"], got["draft"], got["versions"][0]["device"]), (True, None, "phone"))
        status, got = self.api({"action": "draft_restore", "thread": THREAD, "base_rev": got["rev"],
                                "version": got["version"]})
        self.assertEqual((got["draft"]["text"], got["updated"]["device"]), ("From the phone", "phone"))
        self.assertEqual(self.agent_mail.calls, [])                            # no agent-mail for a draft
        self.assertEqual([r["device"] for r in self.audited() if r["action"] in ("draft_put", "draft_restore")],
                         ["phone", "phone"])

    def test_an_api_call_needs_its_header_and_a_bounded_well_formed_body(self):
        status, _ = self.api({"action": "status"}, **{"X-Agent-Board": ""})     # another site's form can't add it
        self.assertEqual(status, 404)
        for length, said in ((str(mailweb.MAX_BODY + 1), 413), ("0", 413), ("lots", 400)):
            status, _, body = self.ask("POST", "/api", b"x", **{"X-Agent-Board": "1", "Content-Length": length})
            self.assertEqual(status, said, length)                                 # refused unread
        self.assertEqual(self.ask("POST", "/api", b"[" * 100000, **{"X-Agent-Board": "1"})[0], 400)   # too deep
        self.assertEqual(self.ask("POST", "/elsewhere", b"{}", **{"X-Agent-Board": "1"})[0], 404)
        self.assertEqual(self.agent_mail.calls, [])

    def test_a_thread_s_links_open_outside_its_frame_and_its_attachments_are_inert_there(self):
        self.signed_in()
        self.agent_mail.answers = [answer({"thread": LISTED["threads"][0], "messages": [], "document": DOC})]
        status, got = self.api({"action": "mail_show", "thread": THREAD})
        self.assertEqual(status, 200)
        self.assertIn('<head><base target="_blank"><meta charset="utf-8">', got["document"])
        self.assertIn('<a data-part="s7/2">r.csv</a>', got["document"])           # listed by the page instead
        self.assertNotIn("mailbox-part:", got["document"])

    def test_a_document_without_a_head_is_refused_not_shown_with_links_in_its_frame(self):
        self.signed_in()
        self.agent_mail.answers = [answer({"thread": LISTED["threads"][0], "messages": [],
                                           "document": "<p><a href=\"https://x.org\">x</a></p>"})]
        status, got = self.api({"action": "mail_show", "thread": THREAD})
        self.assertEqual((status, got["ok"]), (500, False))
        self.assertIn("no <head>", got["error"])

    def test_an_attachment_downloads_as_a_file_for_the_page_s_own_script(self):
        self.signed_in()
        data = b"a,b\n1,2\n"
        self.agent_mail.answers = [answer({"name": "résults 1.csv", "type": "text/html",
                                           "data": base64.b64encode(data).decode()})]
        self.assertEqual(self.ask("GET", "/part/s7/2")[0], 404)                  # not without X-Agent-Board
        status, headers, body = self.part("/part/s7/2")
        self.assertEqual((status, body), (200, data))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")   # never rendered, whatever it claims
        self.assertEqual(headers["Content-Disposition"], "attachment; filename*=UTF-8''r%C3%A9sults%201.csv")
        self.assertEqual(self.agent_mail.calls[0][0][1:], ["mailbox", "part", "--", "s7", "2"])
        self.agent_mail.answers = [answer({"name": "plot.png", "type": "image/png", "data": "iVBO"})]
        self.assertEqual(self.part("/part/i7/1")[0], 200)                       # the files of the owner's own replies
        self.agent_mail.answers = [(4, "", "agent-mail: no part 9 in s7")]
        self.assertEqual(self.part("/part/s7/9")[:3:2], (404, b"agent-mail: no part 9 in s7\n"))
        for path in ("/part/x7/1", "/part/s7/0", "/part/s7/99999"):          # the action's own checks
            status, _, body = self.part(path)
            self.assertEqual(status, 404, path)
            self.assertIn(b"key must be a message's key", body, path)
        self.assertEqual(len(self.agent_mail.calls), 3)

    def test_an_attachment_s_path_is_matched_whole(self):
        self.signed_in()
        for path in ("/part/s7/2x", "/part/s7/2/x", "/x/part/s7/2"):
            self.assertEqual(self.part(path)[0], 404, path)
        self.assertEqual(self.agent_mail.calls, [])

    def test_a_thread_id_is_matched_whole(self):
        self.signed_in()
        self.assertEqual(self.api({"action": "mail_show", "thread": THREAD + "\n"})[1]["ok"], False)
        self.assertEqual(self.agent_mail.calls, [])

    def test_an_attachment_that_is_not_base64_is_refused(self):
        self.signed_in()
        self.agent_mail.answers = [answer({"name": "r.csv", "type": "text/csv", "data": "YSxi!!Cg=="})]
        status, _, body = self.part("/part/s7/2")
        self.assertEqual((status, body), (500, b"the part's data from agent-mail is not base64\n"))


CHOICES = {"question": "Which do you want?", "default": "Option 2 at 18:00",
           "options": [{"label": "Keep the limit", "detail": "One run a week, as now."},
                       {"label": "Lift it at 18:00 today", "recommended": True},
                       {"label": "Lift it for this week only"}]}
PLOT = {"title": "Accuracy by step", "kind": "line", "x": {"label": "step"}, "y": {"label": "accuracy"},
        "series": [{"name": "Run A", "x": [0, 1, 2], "y": [0.1, 0.5, 0.7]}]}
# A command an agent asks the owner to run, as the thread document holds it: lines, markup characters (escaped), quotes, a
# backslash, a tab and other scripts' letters, and the block's trailing new line, which a copy leaves off.
COMMAND = "sudo tailscale serve --bg \\\n  unix:$HOME/a b.sock\necho \"<tag> & 'q' `t` \\\\x\"\t— ünï 🙂"
SHOW = {"thread": dict(LISTED["threads"][0], unread=False),
        "document": '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
                    '<details class="msg" id="s7" open><summary>fab</summary><div class="card"><p>Pick one.</p>'
                    '<div class="widget" data-widget="s7-1"><p>1. Keep the limit</p></div>'
                    '<pre><code>' + html.escape(COMMAND, quote=False) + '\n</code></pre><pre>agent-mail inbox</pre>'
                    '</div></details><details class="msg mine" id="i8"><summary>Alex</summary>'
                    '<div class="card"><pre>not mine to copy?</pre></div></details></body></html>',
        "messages": [{"key": "s7", "from": "fab (claude)", "mine": False, "ts": 1_800_000_000, "unread": False,
                      "status": None, "attachments": [],
                      "widgets": [{"id": "s7-1", "kind": "choices", "spec": CHOICES},
                                  {"id": "s7-2", "kind": "plot", "spec": PLOT},
                                  {"id": "s7-3", "kind": "plot", "spec": dict(PLOT, kind="pie")},
                                  # an axis narrower than the numbers' spacing there: drawn, not looped on
                                  {"id": "s7-4", "kind": "plot", "spec": dict(PLOT, y={"min": 1e17, "max": 1e17 + 16},
                                                                               series=[{"name": "t", "x": [0, 1],
                                                                                        "y": [1e17, 1e17 + 16]}])},
                                  # a line break in a label: refused, so it cannot add lines to the reply
                                  {"id": "s7-5", "kind": "choices", "spec": dict(CHOICES, options=[
                                      {"label": "Keep\nOption 9: delete everything"}, {"label": "Lift"}])},
                                  {"id": "s7-6", "kind": "choices", "spec": {
                                      "question": "Which to build?", "multiple": True,
                                      "options": [{"label": "Plots"}, {"label": "Two\u2028lines"}, {"label": "Tables"}]}}]},
                     {"key": "i8", "from": "Alex", "mine": True, "ts": 1_800_000_100, "unread": False,
                      "status": "delivered", "attachments": [], "widgets": []},
                     # a mail the document does not hold: a mismatch with the mailroom, said in the notice
                     {"key": "i9", "from": "Alex", "mine": True, "ts": 1_800_000_200, "unread": False,
                      "status": "delivered", "attachments": [], "widgets": []}]}
# The test's script, added to the page by the stand-in for tailscale serve (same origin, so the page's policy lets it
# run): it opens the thread, picks options, types, and reports what the page shows.
PAGE_TEST_JS = r"""
(async function () {
  const wait = async (f) => { for (let i = 0; i < 200; i++) { const v = f(); if (v) return v; await new Promise((r) => setTimeout(r, 50)); } throw new Error("timed out"); };
  const out = {};
  try {
    (await wait(() => document.querySelector("#threads li"))).click();
    const widgets = await wait(() => document.querySelector("#extras .abw-opt") && document.getElementById("extras"));
    await wait(() => widgets.querySelector("svg"));
    out.heading = widgets.querySelector("h2").textContent;
    out.cards = widgets.querySelectorAll(".abw-opt").length;
    out.svgs = widgets.querySelectorAll("svg.abw-chart").length;
    out.places = widgets.querySelectorAll("div[data-widget]").length;
    out.notice = document.getElementById("widgetnotice").textContent;
    out.extras = [document.getElementById("extraslabel").textContent, document.getElementById("extrasbutton").hidden];
    out.frameScripts = document.getElementById("doc").getAttribute("sandbox");
    // each question's answer in the thread's draft, the question and labels from its spec, the reply left shut
    const answers = () => drafts.get(state.openId).picks.map((p) => [p.widget, p.picked, p.question, p.labels,
                                                                     p.comment]);
    widgets.querySelectorAll('[data-widget="s7-1"] .abw-opt')[1].click();
    out.first = [answers(), document.getElementById("reply").hidden, document.getElementById("text").value];
    const several = widgets.querySelectorAll('[data-widget="s7-6"] .abw-opt');
    several[0].click();
    several[1].click();
    out.both = answers();
    out.reply = ReplyText.reply({ text: "", picks: drafts.get(state.openId).picks, comments: [] });
    several[0].click();
    several[1].click();                                   // both unpicked: that answer goes
    out.unpicked = answers();
    // code blocks: listed under their message, their text exact; a Copy puts it on the clipboard. Headless Chrome has
    // no system clipboard for a page without focus (writeText rejects "Document is not focused"), so the clipboard is
    // a stand-in that records what it is given, and then one that refuses.
    const copied = [];
    Object.defineProperty(navigator, "clipboard", {configurable: true,
      value: {writeText: (t) => { copied.push(t); return Promise.resolve(); }}});
    out.headings = Array.from(widgets.querySelectorAll("section > h2"), (h) => h.textContent);
    out.codes = Array.from(widgets.querySelectorAll(".code pre"), (p) => p.textContent);
    out.codeSections = Array.from(widgets.querySelectorAll(".code"), (c) => c.closest("section").querySelector("h2").textContent);
    const copies = widgets.querySelectorAll(".code .copy");
    copies[0].click();
    await wait(() => copied.length === 1);
    out.copied = copied[0];
    out.copyLabel = copies[0].textContent;
    Object.defineProperty(navigator, "clipboard", {configurable: true,
      value: {writeText: () => Promise.reject(new Error("Write permission denied."))}});
    copies[1].click();
    await wait(() => copies[1].textContent === "Not copied");
    out.refused = document.getElementById("threaderror").textContent;
  } catch (e) {
    out.error = String(e);
  }
  await fetch("/report", { method: "POST", body: JSON.stringify(out) });
})();
"""


def serve_like_tailscale(test, sock, host, token=None, own=None, rewrite=None):
    """A stand-in for tailscale serve on a free local port, stopped when `test` ends: each request is passed on to the
    page's socket at `sock` with the headers tailscaled adds for the owner's phone asking for `host`, and the session
    cookie
    `token` (a value, or a function giving one when asked; else the browser's own cookie). `own(handler)` may answer a
    request itself (True when it did), and `rewrite(path, body)` may change an answer's body. Returns the server."""
    class Serve(http.server.BaseHTTPRequestHandler):
        """tailscale serve's part: the phone's request, passed on to the page's socket with its headers."""
        def log_message(self, *args):
            pass

        def _pass_on(self):
            if own and own(self):
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            headers = {"Host": "localhost", "Tailscale-User-Login": OWNER, "X-Forwarded-Host": host,
                       "X-Forwarded-Proto": "https", "X-Forwarded-For": PHONE_IP, "Content-Length": str(len(body))}
            session = token() if callable(token) else token
            cookie = "%s=%s" % (mailweb.COOKIE, session) if session else self.headers.get("Cookie")
            if cookie:
                headers["Cookie"] = cookie
            if self.headers.get("X-Agent-Board"):
                headers["X-Agent-Board"] = self.headers["X-Agent-Board"]
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(sock))
            s.sendall(("%s %s HTTP/1.1\r\n%s\r\n" % (self.command, self.path, "".join(
                "%s: %s\r\n" % kv for kv in headers.items()))).encode() + body)
            r = http.client.HTTPResponse(s)
            r.begin()
            got = r.read()
            s.close()
            if rewrite:
                got = rewrite(self.path, got)
            self.answer(r.status, [(k, v) for k, v in r.getheaders() if k not in ("Content-Length", "Connection")], got)

        def answer(self, status, headers, body):
            self.send_response(status)
            for k, v in headers + [("Content-Length", str(len(body)))]:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_POST = _pass_on

    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Serve)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    test.addCleanup(proxy.server_close)
    test.addCleanup(proxy.shutdown)
    return proxy


def serve_page(test, host, mail, handler=mailweb.Handler):
    """The real page server for `test` on its socket, in a folder of the test's own with its store there, answering
    the owner's phone on the tailnet for the desktop's tailnet name `host`. tailscale is stood in for (nothing
    funnelled; whois names the phone, logged in as the owner), agent-mail by `mail(cmd, input)`, which gives its (code,
    stdout, stderr) for a command and what it was given on stdin, and agent-orchestrate by the guard's `popen`, a
    stand_ins.Popen, its logs, the mailroom's config (naming the owner) and its store (empty: the threads' sessions and
    groups are read from it, sources.MAIL_DB) in that folder, and the desktop's keyring by a stand_ins.Keyring there.
    The server's log goes to a buffer while the test runs. `handler` answers each request (PageServer's). Returns
    (guard, store, socket path, keyring); all of it goes when the test ends."""
    real_stderr, sys.stderr = sys.stderr, io.StringIO()
    test.addCleanup(setattr, sys, "stderr", real_stderr)
    d = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, d)
    test.addCleanup(setattr, actions, "ORCH_LOGS", actions.ORCH_LOGS)
    test.addCleanup(setattr, sources, "MAIL_CONFIG", sources.MAIL_CONFIG)
    test.addCleanup(setattr, sources, "MAIL_DB", sources.MAIL_DB)
    actions.ORCH_LOGS, sources.MAIL_CONFIG = d / "orchestrate-logs", d / "config.json"
    sources.MAIL_CONFIG.write_text(json.dumps({"owner_name": "Alex", "owner_email": OWNER, "allowlist": [OWNER]}))
    sources.MAIL_DB = d / "mailroom.sqlite"
    fixtures.mailroom_db(sources.MAIL_DB).close()
    sock = d / "page.sock"
    store = mailwebstate.Store(d / "mail-web")

    def commands(cmd, timeout=60, env=None, input=None, cwd=None):
        if Path(cmd[0]).name == "tailscale":
            return answer({}) if cmd[1] == "serve" else answer(
                {"Node": {"StableID": PHONE, "Name": "phone.tail1.ts.net."}, "UserProfile": {"LoginName": OWNER}})
        return mail(cmd, input)
    keyring = stand_ins.Keyring(d / "keyring")
    guard = mailweb.Guard(dict(mailwebstate.DEFAULTS, devices=[PHONE], proxy_uids=[os.getuid()]), [OWNER], host,
                          str(sock), store, commands, stand_ins.Popen(), time.time, keyring.read, keyring.command)
    srv = mailweb.PageServer(sock, guard, handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    return guard, store, sock, keyring


@unittest.skipUnless(chrome.EXE, "google-chrome is not installed")
class PageTest(MailTest):
    """The phone page itself, in headless Chrome: the real server on its socket, behind a stand-in for tailscale serve
    that adds its headers and a signed-in session, and adds the test's script to the page."""

    def setUp(self):
        super().setUp()
        answers = {"list": answer(LISTED), "show": answer(SHOW), "mark": answer({"threads": [THREAD]})}
        guard, store, sock, _ = serve_page(self, DESK, lambda cmd, input: answers[cmd[2]])
        token = store.new_session("cid", PHONE, time.time(), 1)
        self.report = None
        test = self

        def own(handler):
            if handler.path == "/report":
                test.report = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
                handler.send_response(204)
                handler.end_headers()
                return True
            if handler.path == "/test.js":
                handler.answer(200, [("Content-Type", "text/javascript")], PAGE_TEST_JS.encode())
                return True
            return False

        def rewrite(path, body):
            return body.replace(b"</body>", b'<script src="test.js"></script></body>') if path == "/" else body
        self.proxy = serve_like_tailscale(self, sock, guard.host_port, token, own, rewrite)

    def test_options_plots_and_code_blocks_are_shown_under_the_mail_a_pick_is_kept_a_copy_is_exact(self):
        with chrome.Chrome() as c:
            c.page().go("http://127.0.0.1:%d/" % self.proxy.server_address[1])
            for _ in range(600):
                if self.report is not None:
                    break
                time.sleep(0.05)
        r = self.report
        self.assertIsNotNone(r, "the page never reported")
        self.assertNotIn("error", r, r)
        self.assertEqual((r["cards"], r["svgs"], r["places"]), (6, 2, 4))       # the two bad ones left out
        self.assertIn("fab (claude)", r["heading"])
        self.assertIn("could not be drawn", r["notice"])
        self.assertIn("widget s7-5 is shown as plain text: option 1's label must not hold a control character",
                      r["notice"])
        self.assertIn("The thread's document does not hold mail i9, so any code blocks in it are not listed here.",
                      r["notice"])
        self.assertEqual(r["frameScripts"], "allow-popups allow-popups-to-escape-sandbox")
        self.assertEqual(r["extras"], ["Options · Plots · Code", False])       # the kinds there: two plots, no file
        lift = ["s7-1", [1], "Which do you want?", ["Lift it at 18:00 today"], None]
        self.assertEqual(r["first"], [[lift], True, ""])
        self.assertEqual(r["both"], [lift, ["s7-6", [0, 1], "Which to build?", ["Plots", "Two lines"], None]])
        self.assertEqual(r["reply"], "> Which do you want?\n> 2. Lift it at 18:00 today\nMy answer: option 2\n\n"
                                     "> Which to build?\n> 1. Plots\n> 2. Two lines\nMy answer: options 1 and 2")
        self.assertEqual(r["unpicked"], [lift])            # a line separator in a label added no line, and is gone
        self.assertEqual(r["codes"], [COMMAND, "agent-mail inbox", "not mine to copy?"])       # exact, in order
        self.assertEqual(r["codeSections"][:2], [r["headings"][0]] * 2)                      # each under its mail
        self.assertIn("Alex", r["codeSections"][2])
        self.assertEqual((r["copied"], r["copyLabel"]), (COMMAND, "Copied"))
        self.assertEqual(r["refused"], "Not copied: Write permission denied.")


# The app's mail for the browser tests: 30 threads, every third unread, one with a subject too long for a line.
APP_THREADS = [dict(LISTED["threads"][0], id="18f2a9c0d1e2%04x" % i, newest=100 + i, last=1_800_000_000 - i * 3600,
                    unread=i % 3 == 0, count=1 + i % 4, agents=["agent%d (claude)" % (i % 5)],
                    subject=("Thread %d " % i) + ("averyveryverylongwordthatcannotbreak" * 3 if i == 4 else "subject"),
                    snippet="The start of thread %d's newest mail." % i)
               for i in range(30)]
APP_LISTED = {"threads": APP_THREADS, "counts": {"inbox": 30, "unread": 10, "snoozed": 0}}
# The board's layout for APP_THREADS' agents (agent<i % 5>, their sessions APP_SESSIONS[i % 5]): agent0 in Work's first
# sub-group, agent1 in Work itself, agent2 in Side projects' Reading, agent3 in Admin, agent4 in no group.
APP_SESSIONS = ["%08x-0000-4000-8000-000000000000" % (i + 1) for i in range(5)]
APP_LAYOUT = [{"name": "Work", "members": [APP_SESSIONS[1]], "subgroups": [
                  {"name": "Loader", "members": [APP_SESSIONS[0]]},
                  {"name": "Benchmarks", "members": []}, {"name": "Docs", "members": []}]},
              {"name": "Side projects", "members": [], "subgroups": [{"name": "Reading", "members": [APP_SESSIONS[2]]},
                                                                     {"name": "Ideas", "members": []}]},
              {"name": "Admin", "members": [APP_SESSIONS[3]], "subgroups": []},
              {"name": "Personal", "members": [], "subgroups": []}]


def place_app_agents():
    """APP_LAYOUT saved as the board's, and APP_THREADS' agents and newest mail in the mailroom's store
    (sources.MAIL_DB), so that each list carries its threads' group colours."""
    groups.save_groups(APP_LAYOUT, live_ids=set())
    con = sqlite3.connect(sources.MAIL_DB)
    con.executemany("insert into sessions values(?,?,?,?,?,?,?)", [
        ("tag%d" % i, "claude", sid, "agent%d" % i, "/x", 0, "desktop") for i, sid in enumerate(APP_SESSIONS)])
    con.executemany("insert into sent(id, tag, thread_id, to_addr, subject, sent_at) values(?,?,?,?,?,?)", [
        (t["newest"], "tag%d" % (i % 5), t["id"], OWNER, t["subject"], 0) for i, t in enumerate(APP_THREADS)])
    con.commit()
    con.close()


def rgb(css):
    """A computed CSS colour, "rgb(r, g, b)" or "color(srgb r g b)", as (r, g, b) from 0 to 255."""
    got = [float(v) for v in re.findall(r"[\d.]+", css)[:3]]
    return tuple(v * 255 for v in got) if css.startswith("color(srgb") else tuple(got)


# A second question, for the mail that asks two (app_show's thread 11), and one whose eight options picked make an
# answer taller than the reply gives its answers (thread 12's).
SHIP = {"question": "When should it ship?", "options": [{"label": "Today"}, {"label": "Monday"}, {"label": "Friday"}]}
TALL = {"question": "Which of these should the loader do before it ships, given the shared disk's quota and the "
                    "overnight runs that read from it?", "multiple": True,
        "options": [{"label": "Step %d: keep a cache of the last runs' results on the shared disk and evict the oldest "
                              "first" % n} for n in range(1, 9)]}


def app_show(thread):
    """mail_show's answer for an APP_THREADS thread: a document headed by its subject, holding a mail from its agent.
    Thread 7's mail has a file; thread 8's is one its document does not hold, so only a notice is said under it; thread
    9's asks the owner to choose (CHOICES); thread 11's asks two questions (CHOICES, then SHIP), thread 12's one (TALL)."""
    t = next(x for x in APP_THREADS if x["id"] == thread)
    i = APP_THREADS.index(t)
    mail = {"from": "agent%d (claude)" % (i % 5), "mine": False, "ts": 1_800_000_000, "unread": False, "status": None,
            "widgets": [], "attachments": []}
    messages, held = {7: ([dict(mail, key="s7", attachments=[{"index": 2, "name": "r.csv", "size": 2048}])],
                          '<details id="s7"></details>'),
                      8: ([dict(mail, key="s8")], ""),
                      9: ([dict(mail, key="s109", widgets=[{"id": "s109-1", "kind": "choices", "spec": CHOICES}])],
                          '<details id="s109"></details>'),
                      11: ([dict(mail, key="s111", widgets=[{"id": "s111-1", "kind": "choices", "spec": CHOICES},
                                                            {"id": "s111-2", "kind": "choices", "spec": SHIP}])],
                           '<details id="s111"></details>'),
                      12: ([dict(mail, key="s112", widgets=[{"id": "s112-1", "kind": "choices", "spec": TALL}])],
                           '<details id="s112"></details>')}.get(i, ([dict(mail, key="s%d" % (100 + i))],
                                                               '<details id="s%d"></details>' % (100 + i)))
    return {"thread": t, "messages": messages,
            "document": '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><h1 class="subject">%s</h1>%s'
                        '</body></html>' % (t["subject"], held)}


SAVE_SECONDS = 1.5                    # past the page's SAVE_MS, and the save it starts then


def desk_draft(text="", picks=(), comments=(), files=(), key=None):
    """A draft as the desktop keeps it (maildrafts.check_content)."""
    return {"text": text, "picks": list(picks), "comments": list(comments), "files": list(files), "key": key}


@unittest.skipUnless(chrome.EXE, "google-chrome is not installed")
class AppTest(MailTest):
    """The page as an app, in headless Chrome driven over the DevTools protocol (tests/chrome.py) as the owner's phone:
    412 by 915 CSS pixels, touch. The real server on its socket, behind serve_like_tailscale on localhost (a site
    passkeys can be made for, so the passkeys' origin is the page's there); agent-mail is stood in for and its calls
    kept, and the mail about an enrolment goes nowhere."""

    def setUp(self):
        super().setUp()
        self.calls, self.answered, boxes = [], [], {}    # answered: the lists answered; boxes: id -> its box now
        self.slow, self.mark_late, self.mark_fails = 0, 0, set()   # seconds a list, a mark takes; ops refused
        self.show_late, self.show_fails = {}, set()       # thread id -> seconds its mail_show takes; ids that fail
        self.fast, self.fast_fails = {}, set()            # thread id -> its fast mode; ids fast mode is refused for
        self.fast_threadless = set()                      # ids whose fast mode answer names no thread
        self.researcher_owners, self.researcher_modes = {}, {}
        self.researcher_names, self.researcher_deliveries = {}, {}
        self.researcher_delivery_details = {}
        self.researcher_error = None
        self.replies = []                                 # (thread, key, text) of each reply, as agent-mail got it
        self.reply_fails = []                             # (exit code, why) for the next replies agent-mail fails

        def with_modes(t):
            thread = t["id"]
            t = dict(t, fast=self.fast.get(thread))
            if tag := self.researcher_owners.get(thread):
                t["researcher"] = {"agent": self.researcher_names[tag], "tag": tag,
                                   "on": self.researcher_modes.get(tag, False),
                                   "delivery": self.researcher_deliveries.get(tag)}
                if tag in self.researcher_delivery_details:
                    t["researcher"]["delivery_detail"] = self.researcher_delivery_details[tag]
            return t

        def mail(cmd, input):
            """agent-mail mailbox: a list of the box (or every box, with a query: the threads whose subject holds
            it) as the marks had left the threads when it was asked, answered `self.slow` seconds later; a thread
            (`show_late`, `show_fails`); a mark (`mark_late`, `mark_fails`); fast mode switched (`fast_fails`, and
            `fast_threadless`: an answer without the thread), on for five minutes, its effort still to be lowered
            (as the mailroom answers); the routed agent's shared Researcher setting; a reply kept in `self.replies`."""
            args = cmd[2:]
            self.calls.append(args)
            if args[0] == "answer":                       # a reply with no files: its text on stdin
                self.replies.append((cmd[-1], args[args.index("--key") + 1], input))
                if self.reply_fails:
                    code, why = self.reply_fails.pop(0)
                    return code, "", why
                return answer({"ok": True, "key": "i31", "status": "sending"})
            if args[0] == "show":
                time.sleep(self.show_late.get(cmd[-1], 0))
                if cmd[-1] in self.show_fails:
                    return 4, "", "agent-mail: thread %s could not be read" % cmd[-1]
                shown = app_show(cmd[-1])
                return answer(dict(shown, thread=with_modes(shown["thread"])))
            if args[0] == "researcher":
                thread, on = args[-2], args[-1] == "on"
                if self.researcher_error:
                    return 2, "", self.researcher_error
                tag = self.researcher_owners[thread]
                if args[1:3] != ["--expect-tag", tag]:
                    return 1, "", "Researcher owner changed. Refresh the thread before switching Researcher."
                self.researcher_modes[tag] = on
                self.researcher_deliveries[tag] = "pending"
                t = next(t for t in APP_THREADS if t["id"] == thread)
                return answer({"ok": True, "thread": with_modes(dict(t, box=boxes.get(thread, "inbox")))})
            if args[0] == "fast":
                thread, on = args[2], args[3] == "on"
                if thread in self.fast_fails:
                    return 1, "", "agent-mail: fast mode is for a thread one agent owns; this one is a group's"
                if thread in self.fast_threadless:
                    return answer({"ok": True})
                self.fast[thread] = {"since": time.time(), "until": time.time() + 300, "minutes": 5, "agent": "agent1",
                                     "effort": "lowering", "was": "xhigh",
                                     "note": "Back to xhigh when fast mode ends."} if on else None
                t = next(t for t in APP_THREADS if t["id"] == thread)
                return answer({"ok": True, "thread": dict(t, box=boxes.get(thread, "inbox"), fast=self.fast[thread])})
            if args[0] == "list":
                box, query = args[2], args[6] if "--query" in args else None
                threads = [with_modes(dict(t, box=boxes.get(t["id"], "inbox"))) for t in APP_THREADS]
                listed = answer(dict(APP_LISTED, threads=[t for t in threads if box in ("all", t["box"]) and (
                    query is None or query in t["subject"])]))
                time.sleep(self.slow)
                self.answered.append(args)
                return listed
            if args[0] == "mark":
                time.sleep(self.mark_late)
                if args[2] in self.mark_fails:
                    return 4, "", "agent-mail: the mailbox is locked"
            if args[0] == "mark" and args[2] in ("archive", "inbox", "snooze"):
                boxes[cmd[-1]] = {"archive": "archived", "inbox": "inbox", "snooze": "snoozed"}[args[2]]
            return answer({"show": lambda: app_show(cmd[-1]), "mark": lambda: {"threads": [cmd[-1]]}}[args[0]]())
        self.guard, self.store, sock, self.keyring = serve_page(self, "localhost", mail)
        place_app_agents()
        self.token = self.store.new_session("cid", PHONE, time.time(), 1)    # None: the browser's own cookie
        proxy = serve_like_tailscale(self, sock, self.guard.host_port, lambda: self.token)
        self.guard.origin = self.base = "http://localhost:%d" % proxy.server_address[1]
        saved = sources.mail_owner
        sources.mail_owner = lambda subject, body, *, alert: (True, "")
        self.addCleanup(setattr, sources, "mail_owner", saved)
        self.chrome = chrome.Chrome()
        self.addCleanup(self.chrome.close)
        self.page = self.chrome.page()

    def asked(self, op):
        return [c for c in self.calls if c[0] == op]

    def until(self, condition, what, seconds=10):
        """`condition()`'s value once it is true, asked every 50 ms; the test fails, saying `what` it waited for, when
        it is not within `seconds`."""
        ends = time.time() + seconds
        while not (got := condition()):
            if time.time() > ends:
                self.fail("waited %s seconds for %s" % (seconds, what))
            time.sleep(0.05)
        return got

    def swipe(self, i):
        """A finger's swipe left across thread `i`'s row, from near its right edge to near its left."""
        x, y, w = self.page.js("(() => { const r = document.querySelector(%s).getBoundingClientRect(); "
                               "return [r.left, r.top + r.height / 2, r.width]; })()"
                               % json.dumps('#threads li[data-id="%s"]' % APP_THREADS[i]["id"]))
        self.page.drag((x + w * 0.9, y), (x + w * 0.2, y))

    def listed(self):
        """Whether the list comes on screen, its rows drawn, with no thread over it (once one has slid away) and the
        list slid back into place, so a tap on a row lands where it is."""
        return self.page.wait("document.querySelector('#threads li[data-id]') && "
                              "!document.body.classList.contains('reading') && document.getElementById('threadview').hidden"
                              " && !document.getElementById('listview').inert && document.getElementById('gate').hidden"
                              " && !document.getElementById('listview').getAnimations().length")

    def open(self, i):
        """Tap thread `i` (a finger's tap, so the page has the user's activation) and wait for its document, the thread
        slid in, so a tap on its buttons lands where they are."""
        self.page.tap('#threads li[data-id="%s"]' % APP_THREADS[i]["id"])
        self.page.wait("document.body.classList.contains('reading') && document.getElementById('doc').srcdoc"
                       ".includes(%s) && !document.getElementById('threadview').getAnimations().length"
                       % json.dumps(APP_THREADS[i]["subject"]))

    def back(self):
        """Android's back gesture: the browser's own step back in the history, not the page's."""
        h = self.page.send("Page.getNavigationHistory")
        self.page.send("Page.navigateToHistoryEntry", {"entryId": h["entries"][h["currentIndex"] - 1]["id"]})
        self.listed()

    def test_the_app_opens_on_the_list_and_back_from_a_thread_returns_to_it_where_it_was(self):
        p = self.page
        p.go(self.base + "/#" + APP_THREADS[5]["id"])                # an address naming a thread opens no thread
        self.assertTrue(self.listed())
        self.assertEqual(p.js("[location.pathname, location.hash, history.state]"), ["/", "", None])
        p.js("document.getElementById('listscroll').scrollTop = 900")
        entries, lists = p.js("history.length"), len(self.asked("list"))
        self.open(12)
        self.assertEqual(p.js("history.length"), entries + 1)
        self.back()
        self.assertTrue(self.listed())
        self.assertEqual(p.js("document.getElementById('listscroll').scrollTop"), 900)       # where it was
        self.open(13)                  # another document: a frame of its own, which adds nothing to the history
        self.assertEqual(p.js("history.length"), entries + 1)
        self.back()
        self.assertEqual(p.js("[history.state, document.getElementById('listscroll').scrollTop]"), [None, 900])
        self.assertEqual(len(self.asked("list")), lists)                 # leaving a thread asks nothing again
        self.assertIn(["mark", "--op", "read", "--upto", "112", "--", APP_THREADS[12]["id"]], self.calls)

    def test_a_reload_while_a_thread_is_open_lands_on_the_list(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(2)
        p.reload()
        self.assertTrue(self.listed())
        self.assertEqual(p.js("[location.pathname, location.hash, history.state]"), ["/", "", None])

    def test_the_top_threads_are_read_ahead_and_open_without_asking_again(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.until(lambda: len(self.asked("show")) >= 4, "the top 4 threads read ahead")
        self.assertEqual(sorted(c[-1] for c in self.asked("show")), [t["id"] for t in APP_THREADS[:4]])
        self.calls.clear()
        self.open(1)
        self.assertEqual(self.asked("show"), [])                         # it was read ahead
        self.back()
        self.open(1)
        self.back()
        self.assertEqual(self.asked("list") + self.asked("show"), [])

    def test_the_manifest_is_linked_valid_and_installable_without_a_service_worker(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        got = p.send("Page.getAppManifest")
        self.assertEqual((got["url"], got["errors"]), (self.base + "/manifest.webmanifest", []))
        m = got["manifest"]
        self.assertEqual((m["name"], m["display"], m["startUrl"], m["scope"], m["id"]),
                         ("Agent Mail", "kStandalone", self.base + "/", self.base + "/", self.base + "/"))
        self.assertEqual([i["sizes"] for i in m["icons"]], ["192x192", "512x512", "512x512"])
        self.assertEqual(p.send("Page.getInstallabilityErrors")["installabilityErrors"], [])
        self.assertEqual(p.js("navigator.serviceWorker.getRegistrations().then((r) => r.length)"), 0)
        self.open(0)                    # mail read, and still nothing kept on the phone
        self.assertEqual(p.js("Promise.all([localStorage.length, sessionStorage.length, indexedDB.databases()"
                              ".then((d) => d.length), caches.keys().then((k) => k.length)])"), [0, 0, 0, 0])

    def test_nothing_scrolls_sideways_on_a_narrow_phone(self):
        p = self.chrome.page(width=360, height=780)
        p.go(self.base + "/")
        p.wait("document.querySelector('#threads li[data-id]')")
        self.assertEqual(p.js("[document.documentElement.scrollWidth, document.getElementById('listscroll').scrollWidth]"),
                         [360, 360])
        p.tap('#threads li[data-id="%s"]' % APP_THREADS[4]["id"])        # the subject that cannot break
        p.wait("document.body.classList.contains('reading') && !document.getElementById('replybutton').hidden")
        p.js("document.getElementById('replybutton').click()")                 # once its mail is in, Reply shows
        p.wait("document.getElementById('reply').classList.contains('up')")
        self.assertEqual(p.js("[document.documentElement.scrollWidth, document.getElementById('threadview').scrollWidth,"
                              " document.getElementById('reply').scrollWidth]"), [360, 360, 360])

    def test_a_row_swiped_left_is_archived_with_undo_and_a_pull_reads_the_list_again(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        t = APP_THREADS[1]
        self.swipe(1)
        p.wait("!document.querySelector(%s) && document.getElementById('snacktext').textContent === 'Archived'"
               % json.dumps('#threads li[data-id="%s"]' % t["id"]))
        self.assertEqual(self.marked(1), [["mark", "--op", "archive", "--upto", "101", "--", t["id"]]])
        p.wait("!document.getElementById('snackbar').getAnimations().length")   # slid in: the button stays put
        lists = len(self.answered)
        p.tap("#snackaction")                                              # Undo
        p.wait("document.querySelector(%s)" % json.dumps('#threads li[data-id="%s"]' % t["id"]))
        self.assertEqual(self.marked(2)[1], ["mark", "--op", "inbox", "--", t["id"]])
        self.until(lambda: len(self.answered) > lists, "the list read again after the Undo")
        lists = len(self.asked("list"))
        p.drag((200, 420), (200, 760), steps=12)                           # the list pulled down from its top
        self.until(lambda: len(self.asked("list")) > lists, "the list read again by the pull")
        self.assertEqual(len(self.asked("list")), lists + 1)

    def enrolled(self):
        """The phone enrolled from the Mac's link, with a virtual passkey that verifies the user at once (its id in
        `self.passkey`), and signed in with it (the browser's own session cookie), on the list."""
        self.token = None
        p = self.page
        p.send("WebAuthn.enable", {"enableUI": False})
        self.passkey = p.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
            "isUserVerified": True, "automaticPresenceSimulation": True}})["authenticatorId"]
        code, _ = self.store.mint_code(time.time(), 10)
        p.go(self.base + "/enrol#" + code)
        p.wait("!document.getElementById('gatebutton').hidden")
        self.assertEqual(p.js("[document.getElementById('gatebutton').textContent, location.pathname, location.hash]"),
                         ["Enrol this phone", "/", ""])                    # the code has left the address bar
        p.tap("#gatebutton")
        self.assertTrue(self.listed())

    def test_the_enrolment_link_enrols_the_phone_and_its_passkey_signs_in(self):
        p = self.page
        self.enrolled()
        self.assertEqual(len(self.store.passkeys()), 1)
        self.store.sign_out(forget=False)
        p.reload()
        p.wait("!document.getElementById('gatebutton').hidden")
        self.assertEqual(p.js("document.getElementById('gatebutton').textContent"), "Sign in with your fingerprint")
        p.tap("#gatebutton")
        self.assertTrue(self.listed())


    def test_the_orchestrator_is_messaged_from_the_list_approved_with_the_passkey_and_its_answer_said(self):
        p = self.page
        self.enrolled()
        orch = "[document.getElementById('orchsheet').classList.contains('up'), document.getElementById('orchtext')" \
               ".value, document.getElementById('orchbutton').hidden, document.getElementById('orchsend').disabled]"
        self.assertEqual(p.js(orch), [False, "", False, True])
        p.tap("#orchbutton")
        self.settled("#orchsheet")
        self.assertEqual(p.js(orch + ".concat([document.activeElement.id, document.getElementById('orchtext')"
                              ".maxLength])"), [True, "", True, True, "orchtext", actions.MAX_BRIEF])
        p.send("Input.insertText", {"text": "Spin up an agent on proj to add --help."})
        p.tap("#orchsend")
        p.wait("document.getElementById('snacktext').textContent === "
               "'The orchestrator is running; it will email you.'")
        self.assertEqual(p.js(orch), [False, "", False, True])                  # sent: the box empty, the sheet down
        (call,) = self.guard.popen.calls
        body = call["stdin"].decode()
        self.assertIn("via the agent board (the phone app, Agent Mail, over the tailnet), not by email", body)
        self.assertIn("\n\nSpin up an agent on proj to add --help.\n\nThis is message ", body)
        self.assertEqual(p.js("orchKey"), None)                                # the next message gets a key of its own
        p.wait("document.getElementById('snackbar').hidden")                   # gone, so it covers nothing tapped
        p.tap("#orchbutton")
        self.settled("#orchsheet")
        p.send("Input.insertText", {"text": "Close the \u200bagent"})          # pasted with a zero-width space
        p.tap("#orchsend")
        p.wait("document.getElementById('snacktext').textContent.startsWith('Not sent: the message holds invisible "
               "characters (U+200B)')")
        self.assertEqual(p.js(orch), [True, "Close the \u200bagent", True, False])          # still there to fix
        self.assertEqual(len(self.guard.popen.calls), 1)
        self.assertEqual([(r["kind"], r["ok"]) for r in sent.messages()],
                         [("orchestrate", True), ("orchestrate", False)])                # both on the Sent list

    def settled(self, selector):
        """Wait until the sheet `selector` is up and has stopped sliding, so a tap lands where its buttons are."""
        self.page.wait("document.querySelector(%s).classList.contains('up') && "
                       "!document.querySelector(%s).getAnimations().length" % (json.dumps(selector), json.dumps(selector)))

    def marked(self, n):
        """The mail_mark calls, once there are `n` of them."""
        self.until(lambda: len(self.asked("mark")) >= n, "%d marks" % n)
        return self.asked("mark")

    def test_a_reply_waits_for_its_own_thread_never_the_one_shown_before(self):
        # A thread that fails to come, or comes slowly, must not leave the last thread's agent named in the reply, nor
        # let a reply be written or sent before its own mail is on screen: the reply would instruct that thread's agent.
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        p.js("window.actions = []; const f = window.fetch; window.fetch = (u, o) => { "
             "if (o && o.body) window.actions.push(JSON.parse(o.body).action); return f(u, o); }")
        reply = "[document.getElementById('replybutton').hidden, document.getElementById('replyto').textContent]"
        self.open(0)
        self.assertEqual(p.js(reply), [False, "Reply to agent0 (claude)"])
        self.back()
        seven = APP_THREADS[7]["id"]
        self.show_fails.add(seven)
        p.tap('#threads li[data-id="%s"]' % seven)
        p.wait("!document.getElementById('threaderror').hidden")
        self.assertIn("thread %s could not be read" % seven, p.js("document.getElementById('threaderror').textContent"))
        self.assertEqual(p.js(reply), [True, "Reply to agent2 (claude)"])             # its own agent, and no Reply
        p.js("document.getElementById('replybutton').click()")                         # a click that reaches it anyway
        self.assertTrue(p.js("document.getElementById('reply').hidden"))
        p.js("draft().text = 'Ship it.'; send()")
        p.wait("document.getElementById('sendstatus').textContent.startsWith('Not sent')")
        self.assertNotIn("stepup_begin", p.js("window.actions"))                        # nothing asked to approve
        self.back()
        self.show_fails.clear()
        self.show_late[seven] = 2
        p.tap('#threads li[data-id="%s"]' % seven)
        p.wait("document.body.classList.contains('reading') && !document.getElementById('docload').hidden")
        self.assertEqual(p.js(reply + ".concat([document.getElementById('send').disabled])"),
                         [True, "Reply to agent2 (claude)", True])                     # while it comes
        p.wait("document.getElementById('docload').hidden", 10)
        self.assertEqual(p.js(reply + ".concat([document.getElementById('send').disabled])"),
                         [False, "Reply to agent2 (claude)", False])                   # once it is on screen

    def test_a_draft_is_not_kept_in_the_history_for_the_browser_to_restore(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(1)
        p.tap("#replybutton")
        self.settled("#reply")
        p.js("const t = document.getElementById('text'); t.value = 'a draft'; t.dispatchEvent(new Event('input'))")
        p.js("document.getElementById('orchtext').value = 'a message'")
        p.go(self.base + "/manifest.webmanifest")                        # away, as when Android discards the tab
        h = p.send("Page.getNavigationHistory")
        p.send("Page.navigateToHistoryEntry", {"entryId": h["entries"][h["currentIndex"] - 1]["id"]})
        p.wait("document.readyState === 'complete' && document.getElementById('text')")
        self.assertEqual(p.js("[document.getElementById('text').value, drafts.size, "
                              "document.getElementById('orchtext').value]"), ["", 0, ""])

    def test_signing_out_forgets_everything_even_threads_still_on_their_way(self):
        p = self.page
        for t in APP_THREADS[:8]:                              # read ahead, still coming at the sign-out
            self.show_late[t["id"]] = 1.5
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(5)
        p.tap("#replybutton")
        self.settled("#reply")
        p.js("const t = document.getElementById('text'); t.value = 'a draft'; t.dispatchEvent(new Event('input'))")
        self.back()
        p.tap("#orchbutton")
        self.settled("#orchsheet")
        p.send("Input.insertText", {"text": "a message to the orchestrator"})
        p.js("closeSheet()")
        p.wait("document.getElementById('scrim').hidden")                   # slid away: the menu can be tapped
        p.tap("#menubutton")
        p.tap("#signout")
        p.wait("!document.getElementById('gate').hidden")
        time.sleep(3.5)                                        # every read-ahead has come back by now
        self.assertEqual(p.js("[kept.size, fetching.size, lists.size, drafts.size, state.threads, state.lastId, "
                              "document.getElementById('text').value, document.getElementById('orchtext').value, "
                              "document.querySelectorAll('#threads li').length]"),
                         [0, 0, 0, 0, None, None, "", "", 0])

    def card(self, widget, n):
        """The selector of option `n` (from 1) of widget `widget` in the thread's options sheet."""
        return '#extras [data-widget="%s"] .abw-opt:nth-child(%d)' % (widget, n + 1)       # after its question

    def picked(self):
        """The options the sheet shows picked, as [widget id, option number from 1], in their order."""
        return self.page.js("Array.from(document.querySelectorAll('#extras .abw-opt.abw-on'), (c) => "
                            "[c.closest('[data-widget]').dataset.widget, "
                            "Array.from(c.parentNode.querySelectorAll('.abw-opt')).indexOf(c) + 1])")

    def answers(self):
        """The reply's answers as its sheet shows them: for each, the agent's words, the owner's answer, and the
        comment (None with no comment field)."""
        return self.page.js("Array.from(document.querySelectorAll('#answers .answer'), (a) => "
                            "[a.querySelector('.asked').textContent, a.querySelector('.mine').textContent, "
                            "a.querySelector('textarea') ? a.querySelector('textarea').value : null])")

    def long_press(self, selector):
        """A long press on the first element `selector` names, as Chrome on Android tells the page of one: a
        contextmenu event there (headless Chrome makes no long press of the touches it is sent)."""
        self.page.js("(() => { const e = document.querySelector(%s); e.scrollIntoView({block: 'nearest'}); "
                     "const r = e.getBoundingClientRect(); e.dispatchEvent(new MouseEvent('contextmenu', {bubbles: "
                     "true, cancelable: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2})); })()"
                     % json.dumps(selector))

    def test_options_picked_in_two_questions_are_both_kept_and_the_sheet_stays_up(self):
        # A pick must not fill the reply and shut the options, nor a pick in another question replace the first
        # one's line: each question's answer is kept apart, as on the Mac (Draft.pick).
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(11)
        p.tap("#extrasbutton")
        self.settled("#extrasheet")
        sheets = "[document.getElementById('extrasheet').classList.contains('up'), " \
                 "document.getElementById('reply').hidden]"
        p.tap(self.card("s111-1", 2))
        self.assertEqual(p.js(sheets), [True, True])                     # the options stay up, the reply shut
        p.tap(self.card("s111-2", 1))
        self.assertEqual(self.picked(), [["s111-1", 2], ["s111-2", 1]])
        p.tap(self.card("s111-1", 3))                                     # another option: only its answer changes
        self.assertEqual(self.picked(), [["s111-1", 3], ["s111-2", 1]])
        self.assertEqual(p.js(sheets), [True, True])
        p.tap("#extrasheet .sheetclose")
        p.wait("document.getElementById('scrim').hidden")
        self.assertEqual(p.js("document.getElementById('replylabel').textContent"), "Draft")
        p.tap("#replybutton")
        self.settled("#reply")
        self.assertEqual(self.answers(), [
            ["Which do you want?\n3. Lift it for this week only", "My answer: option 3", None],
            ["When should it ship?\n1. Today", "My answer: option 1", None]])
        self.assertEqual(p.js("document.getElementById('text').value"), "")      # the owner's words are left alone
        p.tap("#reply .sheetclose")
        p.wait("document.getElementById('scrim').hidden")
        self.back()
        self.assertEqual(p.js("document.querySelector(%s).textContent" % json.dumps(
            '#threads li[data-id="%s"] .draftmark' % APP_THREADS[11]["id"])), "Draft")      # answers alone are a draft
        self.open(11)
        self.assertEqual(self.picked(), [["s111-1", 3], ["s111-2", 1]])           # its cards show them again

    def test_a_long_press_picks_with_a_comment_and_an_answer_s_x_takes_it_out(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(11)
        p.tap("#extrasbutton")
        self.settled("#extrasheet")
        p.tap(self.card("s111-1", 2))
        self.long_press(self.card("s111-2", 3))          # the phone's right-click: picked, with a field for a comment
        self.settled("#reply")
        self.assertEqual(p.js("document.activeElement.matches('#answers textarea') && "
                              "document.activeElement.closest('.answer').dataset.answer"), "s111-2")
        self.assertIsNone(p.js("document.querySelector('.abw-menu')"))             # and no menu under the finger
        p.send("Input.insertText", {"text": "only once the tests pass"})
        self.assertEqual(self.answers(), [
            ["Which do you want?\n2. Lift it at 18:00 today", "My answer: option 2", None],
            ["When should it ship?\n3. Friday", "My answer: option 3", "only once the tests pass"]])
        p.tap('#answers [data-answer="s111-1"] button')                             # its x
        self.assertEqual(self.answers(), [["When should it ship?\n3. Friday", "My answer: option 3",
                                           "only once the tests pass"]])
        self.assertEqual(self.picked(), [["s111-2", 3]])                            # and its cards show it gone
        p.tap("#reply .sheetclose")
        p.wait("document.getElementById('scrim').hidden")
        p.tap("#extrasbutton")
        self.settled("#extrasheet")
        self.long_press(self.card("s111-2", 3))                     # again on the option picked: kept, as its comment
        self.settled("#reply")
        self.assertEqual(self.answers()[0][2], "only once the tests pass")
        p.tap("#discard")                                           # Discard: every answer goes, from the cards too
        p.wait("document.getElementById('snacktext').textContent === 'Draft discarded'")
        self.assertEqual(self.picked(), [])
        p.wait("!document.getElementById('snackbar').getAnimations().length")   # slid in: the button stays put
        p.tap("#snackaction")                                       # and Undo brings them back
        self.assertEqual(self.picked(), [["s111-2", 3]])

    def test_the_answers_are_sent_as_the_mac_sends_them_the_passkey_approving_exactly_that_text(self):
        p = self.page
        self.enrolled()
        self.open(11)
        p.js("window.asked = []; const f = window.fetch; window.fetch = (u, o) => { "
             "if (o && o.body) window.asked.push(JSON.parse(o.body)); return f(u, o); }")
        p.tap("#extrasbutton")
        self.settled("#extrasheet")
        p.tap(self.card("s111-1", 2))
        self.long_press(self.card("s111-2", 1))
        self.settled("#reply")
        p.send("Input.insertText", {"text": " and tell me when it starts "})
        p.js("document.getElementById('text').focus()")
        p.send("Input.insertText", {"text": " Thanks. "})
        p.tap("#send")
        p.wait("document.getElementById('snacktext').textContent.startsWith('Reply sending')")
        text = ("> Which do you want?\n> 2. Lift it at 18:00 today\nMy answer: option 2\n\n"
                "> When should it ship?\n> 1. Today\nMy answer: option 1\nMy comment: and tell me when it starts\n\n"
                "Thanks.")                                          # the Mac's form (ReplyText.compose)
        self.assertEqual([(thread, said) for thread, _, said in self.replies], [(APP_THREADS[11]["id"], text)])
        steps = [a for a in p.js("window.asked") if a["action"] in ("stepup_begin", "mail_reply")]
        self.assertEqual([a["action"] for a in steps], ["stepup_begin", "mail_reply"])
        self.assertEqual(json.loads(steps[1]["request"])["text"], text)
        self.assertEqual(steps[0]["digest"], hashlib.sha256(steps[1]["request"].encode()).hexdigest())
        self.assertEqual(self.picked(), [])                         # sent: the answers have gone with the draft
        self.assertFalse(p.js("hasDraft(%s)" % json.dumps(APP_THREADS[11]["id"])))

    def fingerprint(self, given):
        """Whether the passkey's next approvals find the owner's fingerprint (`given`), or are refused for want of
        it."""
        self.page.send("WebAuthn.setUserVerified", {"authenticatorId": self.passkey, "isUserVerified": given})

    def send_failing(self, status):
        """Tap Send and wait until the reply sheet says it was not sent, its status no longer `status`."""
        self.page.tap("#send")
        self.page.wait("!sending && document.getElementById('sendstatus').textContent.startsWith('Not sent: ') && "
                       "document.getElementById('sendstatus').textContent !== %s" % json.dumps(status))
        return self.page.js("document.getElementById('sendstatus').textContent")

    # the reply sheet held still: its text, its paperclip, an answer's x, and the line saying why (None when hidden)
    HELD = ("[document.getElementById('text').readOnly, document.getElementById('attach').disabled, "
            "!!document.querySelector('#answers button') && document.querySelector('#answers button').disabled, "
            "!document.getElementById('held') || document.getElementById('held').hidden ? null : "
            "document.getElementById('held').textContent]")

    def test_a_reply_that_may_have_reached_the_desktop_holds_still_and_goes_again_as_it_was(self):
        # as on the Mac (Draft.frozen): a send that failed with no refusal may have been kept by the mailroom, which
        # keeps a key's first reply, so a change sent again under that key would be lost
        p = self.page
        self.enrolled()
        self.open(11)
        p.tap("#extrasbutton")
        self.settled("#extrasheet")
        p.tap(self.card("s111-1", 2))
        p.tap("#extrasheet .sheetclose")
        p.wait("document.getElementById('scrim').hidden")
        p.tap("#replybutton")
        self.settled("#reply")
        p.send("Input.insertText", {"text": "Ship it."})
        self.reply_fails = [(1, "agent-mail: the connection was reset")]
        status = self.send_failing("")
        self.assertIn("the connection was reset", status)
        why = json.loads(CASES.read_text())["frozenWhy"]                        # the Mac's words (Draft.frozenWhy)
        self.assertEqual(p.js(self.HELD), [True, True, True, why])
        p.js("document.getElementById('text').focus()")
        p.send("Input.insertText", {"text": " Or wait."})
        self.assertEqual(p.js("document.getElementById('text').value"), "Ship it.")      # it takes no typing
        self.fingerprint(False)                       # an approval not given: the reply never left, and stays held
        status = self.send_failing(status)
        self.assertEqual(p.js(self.HELD), [True, True, True, why])
        self.fingerprint(True)
        p.tap("#reply .sheetclose")
        p.wait("document.getElementById('scrim').hidden")
        p.tap("#extrasbutton")
        self.settled("#extrasheet")
        p.tap(self.card("s111-1", 3))                                       # nor another answer
        p.wait("document.getElementById('snacktext').textContent.startsWith('An option picked was not taken')")
        self.assertEqual(p.js("document.getElementById('snacktext').textContent"),
                         "An option picked was not taken. " + why)
        self.assertEqual(self.picked(), [["s111-1", 2]])
        p.tap("#extrasheet .sheetclose")
        # the snackbar gone too, so it covers nothing tapped
        p.wait("document.getElementById('scrim').hidden && document.getElementById('snackbar').hidden")
        p.tap("#replybutton")
        self.settled("#reply")
        p.tap("#send")                                                       # sent again: the same reply, its key
        p.wait("document.getElementById('snacktext').textContent.startsWith('Reply sending')")
        (_, first, text), (_, again, resent) = self.replies
        self.assertEqual((again, resent), (first, text))
        self.assertEqual(text, "> Which do you want?\n> 2. Lift it at 18:00 today\nMy answer: option 2\n\nShip it.")

    def test_a_reply_refused_or_discarded_lets_go_of_its_key_and_can_change(self):
        p = self.page
        self.enrolled()
        self.open(11)
        p.tap("#replybutton")
        self.settled("#reply")
        p.send("Input.insertText", {"text": "Ship it."})
        self.fingerprint(False)                       # an approval not given: nothing left the phone, nothing held
        status = self.send_failing("")
        self.assertEqual((self.replies, p.js(self.HELD)), ([], [False, False, False, None]))
        self.fingerprint(True)
        self.reply_fails = [(mailbox.ANSWER_REFUSED, "agent-mail: the thread has no agent mail to answer")]
        self.send_failing(status)                     # refused: the mailroom kept nothing
        self.assertEqual(p.js(self.HELD), [False, False, False, None])
        p.js("document.getElementById('text').focus()")
        p.send("Input.insertText", {"text": " Today."})
        p.tap("#send")
        p.wait("document.getElementById('snacktext').textContent.startsWith('Reply sending')")
        (_, refused, _), (_, key, text) = self.replies
        self.assertNotEqual(key, refused)                                    # a reply of its own, as changed
        self.assertEqual(text, "Ship it. Today.")
        p.wait("document.getElementById('scrim').hidden")
        p.tap("#replybutton")
        self.settled("#reply")
        p.send("Input.insertText", {"text": "Next."})
        self.reply_fails = [(1, "agent-mail: the connection was reset")]
        self.send_failing("")
        self.assertTrue(p.js("document.getElementById('text').readOnly"))
        p.tap("#discard")                                                    # Discard lets go of the key
        p.wait("document.getElementById('scrim').hidden")
        p.tap("#replybutton")
        self.settled("#reply")
        p.send("Input.insertText", {"text": "Other."})
        p.tap("#send")
        p.wait("document.getElementById('snacktext').textContent.startsWith('Reply sending')")
        (_, held, _), (_, key, text) = self.replies[2:]
        self.assertEqual((key != held, text), (True, "Other."))

    # drafts kept on the desktop (agentboard/maildrafts.py; README, "Drafts"): the store the Mac's drafts go to too, in
    # this test's own folder (MailTest), here written to directly as the Mac would

    def draftmark(self, i):
        """The JavaScript for thread `i`'s Draft mark in the list (null with none)."""
        return "document.querySelector(%s)" % json.dumps('#threads li[data-id="%s"] .draftmark' % APP_THREADS[i]["id"])

    def reply_text(self):
        """The reply sheet up, and its text, once it is there."""
        self.page.tap("#replybutton")
        self.settled("#reply")
        return self.page.js("document.getElementById('text').value")

    def type_at_end(self, text):
        self.page.js("(() => { const t = document.getElementById('text'); t.focus(); "
                     "t.setSelectionRange(t.value.length, t.value.length); })()")
        self.page.send("Input.insertText", {"text": text})

    def on_desk(self, i):
        """Thread `i`'s draft on the desktop now (None for none)."""
        return maildrafts.Store().view(APP_THREADS[i]["id"])["draft"]

    def test_a_draft_started_on_the_mac_shows_on_the_phone_and_typing_there_is_saved_as_the_phone_s(self):
        p = self.page
        t = APP_THREADS[3]["id"]
        maildrafts.Store().put(t, 0, desk_draft("Started on the Mac."), "mac")
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        p.wait(self.draftmark(3))                                     # the desktop's list marks it
        self.assertIsNone(p.js(self.draftmark(2)))
        self.open(3)
        p.wait("document.getElementById('replylabel').textContent === 'Draft'")
        self.assertEqual(self.reply_text(), "Started on the Mac.")
        self.type_at_end(" Finished on the phone.")
        self.assertEqual(self.on_desk(3)["text"], "Started on the Mac.")    # not yet: a second after typing stops
        self.until(lambda: self.on_desk(3)["text"] == "Started on the Mac. Finished on the phone.", "the save", 5)
        self.assertEqual(maildrafts.Store().view(t)["updated"]["device"], "phone")

    def test_a_draft_written_on_the_phone_outlives_the_page(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(2)
        self.assertEqual(self.reply_text(), "")
        p.send("Input.insertText", {"text": "Half a thought"})
        p.reload()                                                    # at once, as when Android closes the app
        self.until(lambda: (self.on_desk(2) or {}).get("text") == "Half a thought", "the draft on the desktop", 5)
        self.assertTrue(self.listed())
        self.assertEqual(p.js("drafts.size"), 0)                      # nothing of it kept on the phone
        p.wait(self.draftmark(2))
        self.open(2)
        p.wait("document.getElementById('replylabel').textContent === 'Draft'")
        self.assertEqual(self.reply_text(), "Half a thought")

    def test_a_reply_whose_draft_cannot_be_saved_first_is_not_sent_and_the_sheet_says_why(self):
        # Send saves the draft with the reply's key first, so that the desktop empties it once the reply has gone; one
        # the desktop cannot save is not sent (README, "Drafts"), and the sheet says why, as for any save that fails
        p = self.page
        self.enrolled()
        self.open(6)
        self.reply_text()
        p.send("Input.insertText", {"text": "Merge it."})
        self.until(lambda: (self.on_desk(6) or {}).get("text") == "Merge it.", "the draft saved", 5)
        record = maildrafts.ROOT / "threads" / (APP_THREADS[6]["id"] + ".json")
        record.write_text("damaged")                                  # the desktop cannot read the draft's record now
        status = self.send_failing("")
        self.assertIn("the draft could not be saved on the desktop", status)
        self.assertIn("not valid JSON", status)
        self.assertEqual(self.replies, [])                            # nothing reached agent-mail
        p.wait("document.getElementById('draftnote').textContent.startsWith('Not saved on the desktop yet: ')")
        self.assertEqual(record.read_text(), "damaged")               # and the record is never written over

    def test_a_draft_changed_on_the_mac_meanwhile_shows_as_the_mac_left_it_and_keeps_the_phone_s_as_a_version(self):
        p = self.page
        t = APP_THREADS[5]["id"]
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(5)
        self.reply_text()
        p.send("Input.insertText", {"text": "The phone's words"})
        self.until(lambda: (self.on_desk(5) or {}).get("text") == "The phone's words", "the phone's save", 5)
        store = maildrafts.Store()
        store.put(t, store.view(t)["rev"], desk_draft("The Mac's words"), "mac")     # the Mac's save, unseen here
        self.type_at_end(", and more")
        p.wait("document.getElementById('draftnote').textContent === "
               "'This draft changed on your Mac since; your version is under Versions.'")
        self.assertEqual(p.js("document.getElementById('text').value"), "The Mac's words")
        self.assertEqual(self.on_desk(5)["text"], "The Mac's words")
        newest = store.view(t)["versions"][0]
        self.assertEqual((newest["why"], newest["device"], newest["words"]),
                         ("conflict", "phone", "The phone's words, and more"))
        p.tap("#versionsbutton")
        p.wait("!document.getElementById('versions').hidden")
        self.assertRegex(p.js("document.querySelector('#versions li').textContent"),
                         r"^Not taken \d\d:\d\d, phone: The phone's words, and more$")

    def test_discard_is_kept_on_the_desktop_and_the_snackbar_s_undo_brings_it_back(self):
        p = self.page
        t = APP_THREADS[6]["id"]
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(6)
        self.reply_text()
        p.send("Input.insertText", {"text": "Keep this, maybe"})
        p.tap("#discard")                                             # before its save: saved first, then discarded
        p.wait("document.getElementById('snacktext').textContent === 'Draft discarded'")
        self.until(lambda: self.on_desk(6) is None, "the draft emptied on the desktop")
        newest = maildrafts.Store().view(t)["versions"][0]
        self.assertEqual((newest["why"], newest["words"]), ("discarded", "Keep this, maybe"))
        self.assertEqual(p.js("document.getElementById('replylabel').textContent"), "Reply")
        p.wait("!document.getElementById('snackbar').getAnimations().length")   # slid in: the button stays put
        p.tap("#snackaction")
        self.assertEqual(p.js("document.getElementById('replylabel').textContent"), "Draft")    # back at once
        self.until(lambda: (self.on_desk(6) or {}).get("text") == "Keep this, maybe", "brought back on the desktop")
        p.wait("document.getElementById('snackbar').hidden")          # slid away, so it covers nothing tapped
        self.assertEqual(self.reply_text(), "Keep this, maybe")

    def test_a_version_is_brought_back_from_the_reply_s_versions(self):
        p = self.page
        t = APP_THREADS[13]["id"]
        store = maildrafts.Store()
        rev = store.put(t, 0, desk_draft("An older draft"), "mac")["rev"]
        rev = store.discard(t, rev, "mac")["rev"]
        store.put(t, rev, desk_draft("The newer one"), "mac")
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(13)
        p.wait("document.getElementById('replylabel').textContent === 'Draft'")
        self.assertEqual(self.reply_text(), "The newer one")
        p.tap("#versionsbutton")
        p.wait("!document.getElementById('versions').hidden")
        self.assertRegex(p.js("document.querySelector('#versions li').textContent"),
                         r"^Discarded \d\d:\d\d, Mac: An older draft$")
        p.tap("#versions li button")
        p.wait("document.getElementById('text').value === 'An older draft'")
        self.until(lambda: self.on_desk(13)["text"] == "An older draft", "restored on the desktop")
        newest = store.view(t)["versions"][0]
        self.assertEqual((newest["why"], newest["words"]), ("replaced", "The newer one"))   # what it replaced, kept

    def attach_file(self, path):
        """Pick `path` in the reply's file picker, as Android's would."""
        doc = self.page.send("DOM.getDocument")["root"]["nodeId"]
        node = self.page.send("DOM.querySelector", {"nodeId": doc, "selector": "#attach"})["nodeId"]
        self.page.send("DOM.setFileInputFiles", {"nodeId": node, "files": [str(path)]})

    def test_a_file_attached_on_the_phone_is_kept_on_the_desktop_and_the_reply_sends_it_from_there(self):
        p = self.page
        t = APP_THREADS[14]["id"]
        self.enrolled()
        self.open(14)
        p.js("window.asked = []; const f = window.fetch; window.fetch = (u, o) => { "
             "if (o && o.body) window.asked.push(JSON.parse(o.body.split('\\n')[0])); return f(u, o); }")
        self.reply_text()
        p.send("Input.insertText", {"text": "See the file."})
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        data = b"step,accuracy\n1,0.5\n"
        (d / "results.csv").write_bytes(data)
        self.attach_file(d / "results.csv")
        p.wait("document.getElementById('files').textContent.includes('results.csv')")
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(maildrafts.Store().file(t, digest), data)
        self.until(lambda: (self.on_desk(14) or {}).get("files") == [{"id": digest, "name": "results.csv",
                                                                      "size": len(data)}], "the draft naming it")
        self.attach_file(d / "results.csv")                           # again: not taken twice
        p.wait("document.getElementById('sendstatus').textContent === "
               "'Not attached: results.csv is in this reply already.'")
        p.tap("#send")
        p.wait("document.getElementById('snacktext').textContent.startsWith('Reply sending')")
        thread, _, said = self.replies[-1]
        self.assertEqual((thread, json.loads(said)), (t, {"text": "See the file.", "files": [
            {"name": "results.csv", "data": base64.b64encode(data).decode()}]}))
        approved = [json.loads(a["request"]) for a in p.js("window.asked") if a["action"] == "mail_reply"]
        self.assertEqual(approved[0]["files"], [{"name": "results.csv", "draft": digest}])   # its digest, not data
        time.sleep(SAVE_SECONDS)
        self.assertIsNone(self.on_desk(14))                           # sent: the desktop's draft emptied, and stays so

    def test_a_draft_from_the_mac_with_a_file_and_a_comment_is_sent_whole_from_the_phone(self):
        p = self.page
        t = APP_THREADS[15]["id"]
        store = maildrafts.Store()
        f = store.add_file(t, b"made on the Mac")
        comment = {"id": "c1", "message": "s115", "at": 3, "quote": "the loader's cache", "comment": "make it bigger"}
        store.put(t, 0, desk_draft("Here it is.", comments=[comment],
                                   files=[{"id": f["id"], "name": "mac.txt", "size": f["size"]}]), "mac")
        self.enrolled()
        self.open(15)
        p.wait("document.getElementById('replylabel').textContent === 'Draft'")
        self.assertEqual(self.reply_text(), "Here it is.")
        self.assertIn("mac.txt", p.js("document.getElementById('files').textContent"))
        self.assertEqual(p.js("(() => { const c = document.querySelector('#answers [data-comment=\"c1\"]'); return "
                              "[c.querySelector('.asked').textContent, c.querySelector('textarea').value]; })()"),
                         ["the loader's cache", "make it bigger"])     # what the reply sends of it, shown whole
        p.tap("#send")
        p.wait("document.getElementById('snacktext').textContent.startsWith('Reply sending')")
        thread, _, said = self.replies[-1]
        self.assertEqual((thread, json.loads(said)), (t, {
            "text": "> the loader's cache\nmake it bigger\n\nHere it is.",
            "files": [{"name": "mac.txt", "data": base64.b64encode(b"made on the Mac").decode()}]}))
        time.sleep(SAVE_SECONDS)
        self.assertIsNone(self.on_desk(15))

    def test_send_waits_until_answers_taller_than_their_room_are_scrolled_to_their_end(self):
        # as on the Mac (ComposeBox.unseen): nothing in the answers is sent unread
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(12)
        p.tap("#extrasbutton")
        self.settled("#extrasheet")
        for n in range(1, 9):
            p.tap(self.card("s112-1", n))
        p.tap("#extrasheet .sheetclose")
        p.wait("document.getElementById('scrim').hidden")
        p.tap("#replybutton")
        self.settled("#reply")
        waits = "[document.getElementById('send').disabled, !document.getElementById('answersmore').hidden]"
        self.assertEqual(p.js(waits), [True, True])
        scroll = "(() => { const a = document.getElementById('answers'); a.scrollTop = a.scrollHeight; })()"
        p.js(scroll)
        p.wait("!document.getElementById('send').disabled")
        self.assertEqual(p.js(waits), [False, False])
        p.tap("#reply .sheetclose")
        p.wait("document.getElementById('scrim').hidden")
        p.tap("#extrasbutton")
        self.settled("#extrasheet")
        p.tap(self.card("s112-1", 8))                               # the agent's words in them change: read again
        p.tap("#extrasheet .sheetclose")
        p.wait("document.getElementById('scrim').hidden")
        p.tap("#replybutton")
        self.settled("#reply")
        self.assertEqual(p.js(waits), [True, True])
        p.js(scroll)
        p.wait("!document.getElementById('send').disabled")

    def test_an_option_from_a_thread_left_behind_puts_nothing_in_another_s_reply(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(9)
        p.tap("#extrasbutton")
        self.settled("#extrasheet")
        p.js("window.card = document.querySelector('#extras .abw-opt')")             # a card of thread 9's, kept
        p.js("document.querySelector('#extrasheet .sheetclose').click()")
        self.back()
        other = APP_THREADS[20]["id"]                                  # not read ahead: it comes as it is opened
        self.show_late[other] = 2
        p.tap('#threads li[data-id="%s"]' % other)
        p.wait("document.body.classList.contains('reading') && !document.getElementById('docload').hidden")
        p.js("window.card.click()")                                                    # while thread 20 comes
        # (its draft is there, empty, from the moment it opens: the page reads it from the desktop)
        draft = "[drafts.has(%s) && [drafts.get(%s).text, drafts.get(%s).picks.length], " \
                "document.getElementById('reply').hidden]" % (json.dumps(other), json.dumps(other), json.dumps(other))
        self.assertEqual(p.js(draft), [["", 0], True])
        p.wait("document.getElementById('docload').hidden", 10)
        p.js("window.card.click()")                                                    # and once it is there
        self.assertEqual(p.js(draft), [["", 0], True])

    def test_a_change_the_desktop_refuses_is_said_and_the_list_puts_the_thread_back(self):
        p = self.page
        self.mark_fails.add("archive")
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.swipe(1)
        p.wait("document.getElementById('snacktext').textContent.startsWith('Not archived: ')")
        self.assertIn("the mailbox is locked", p.js("document.getElementById('snacktext').textContent"))
        p.wait("document.querySelector(%s)" % json.dumps('#threads li[data-id="%s"]' % APP_THREADS[1]["id"]))

    def test_a_read_mark_that_fails_is_said_and_the_thread_stays_unread(self):
        p = self.page
        self.mark_fails.add("read")
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(0)                                                                   # unread: marked read on opening
        p.wait("document.getElementById('snacktext').textContent.startsWith('Not marked read: ')")
        self.back()
        row = json.dumps('#threads li[data-id="%s"]' % APP_THREADS[0]["id"])
        p.wait("document.querySelector(%s).classList.contains('unread') && "
               "document.getElementById('unreadcount').textContent === '10'" % row)    # as the desktop has it

    def test_a_list_asked_before_a_change_does_not_undo_it(self):
        # The Mac's rule (MailModel.refresh): a list asked before a change, or answered while one is on its way, is
        # out of date, so an archived thread does not come back from it.
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.slow, self.mark_late = 2, 3
        lists, answered = len(self.asked("list")), len(self.answered)
        p.js("quietRefresh(), 0")                     # not waited for: it holds thread 1, in the inbox, and is on its way
        self.until(lambda: len(self.asked("list")) > lists, "the list asked for")
        self.swipe(1)                                                                  # archived while it is on its way
        row = json.dumps('#threads li[data-id="%s"]' % APP_THREADS[1]["id"])
        p.wait("!document.querySelector(%s)" % row)
        self.until(lambda: len(self.answered) > answered, "the earlier list answered")
        time.sleep(0.5)                                                                # time to draw it, were it taken
        self.assertFalse(p.js("!!document.querySelector(%s)" % row))
        self.until(lambda: len(self.answered) > answered + 1, "the list read after the archive", seconds=15)
        time.sleep(0.5)
        self.assertFalse(p.js("!!document.querySelector(%s)" % row))

    def test_the_two_threads_below_one_opened_are_read_ahead(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.until(lambda: len(self.asked("show")) >= 4, "the top 4 threads read ahead")
        self.calls.clear()
        self.open(10)
        self.until(lambda: len(self.asked("show")) >= 3, "thread 10 and the two below it")
        self.assertEqual(sorted(c[-1] for c in self.asked("show")), [t["id"] for t in APP_THREADS[10:13]])

    def test_snoozing_offers_the_mac_s_choices_and_a_date_and_time(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(2)
        p.tap("#snoozebutton")
        self.settled("#snoozesheet")
        labels = p.js("Array.from(document.querySelectorAll('#snoozechoices button > span:first-child'),"
                      " (s) => s.textContent)")
        self.assertEqual(labels, p.js("snoozePresets(Date.now()).map((c) => c.label)"))     # snooze.js's, as the Mac
        at = p.js("snoozePresets(Date.now()).find((c) => c.label === 'Tomorrow').at")
        p.tap("#snoozechoices button:nth-child(%d)" % (labels.index("Tomorrow") + 1))
        self.assertTrue(self.listed())
        self.assertFalse(p.js("!!document.querySelector('#threads li[data-id=\"%s\"]')" % APP_THREADS[2]["id"]))
        self.assertTrue(p.js("document.getElementById('snacktext').textContent.startsWith('Snoozed until ')"))
        self.assertEqual(self.marked(1), [["mark", "--op", "snooze", "--upto", "102", "--until", repr(float(at)), "--",
                                           APP_THREADS[2]["id"]]])
        self.open(5)
        p.tap("#snoozebutton")
        self.settled("#snoozesheet")
        pick = "(v) => { const i = document.getElementById('snoozeat'); i.value = v; i.dispatchEvent(new Event('input')); " \
               "return document.getElementById('snoozepicked').disabled; }"
        self.assertTrue(p.js("(%s)('2020-01-01T08:00')" % pick))                       # a time gone: no Snooze
        when = p.js("(() => { const d = new Date(Date.now() + 86400000); return d.getFullYear() + '-' + "
                    "String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') + 'T09:30'; })()")
        self.assertFalse(p.js("(%s)(%s)" % (pick, json.dumps(when))))
        at = p.js("new Date(%s).getTime() / 1000" % json.dumps(when))                # the phone's own time zone
        p.tap("#snoozepicked")
        self.assertTrue(self.listed())
        self.assertEqual(self.marked(2)[1], ["mark", "--op", "snooze", "--upto", "105", "--until", repr(float(at)), "--",
                                             APP_THREADS[5]["id"]])

    def test_the_extras_button_names_what_its_sheet_holds_and_escape_closes_the_sheet(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        self.open(1)
        self.assertTrue(p.js("document.getElementById('extrasbutton').hidden"))         # nothing to show
        self.back()
        self.open(7)
        self.assertEqual(p.js("[document.getElementById('extraslabel').textContent, "
                              "document.getElementById('extrasbutton').hidden]"), ["File", False])
        p.tap("#extrasbutton")
        self.settled("#extrasheet")
        self.assertIn("r.csv (2 KB)", p.js("document.getElementById('attachments').textContent"))
        entries = p.js("history.length")
        for kind in ("rawKeyDown", "keyUp"):                 # Esc, as Android's back gesture, closes the sheet first
            p.send("Input.dispatchKeyEvent", {"type": kind, "key": "Escape", "code": "Escape",
                                              "windowsVirtualKeyCode": 27})
        p.wait("!document.getElementById('extrasheet').classList.contains('up')")
        self.assertEqual(p.js("[document.body.classList.contains('reading'), history.length]"), [True, entries])
        self.back()
        self.open(8)
        self.assertEqual(p.js("document.getElementById('extraslabel').textContent"), "About this thread")   # a notice

    def test_search_asks_every_box_once_typing_stops_and_clearing_it_lists_the_box_again(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        lists = len(self.asked("list"))
        p.js("document.getElementById('query').focus()")
        for letter in "Thread 1":                             # typed a key at a time, well inside the 300 ms pause
            p.send("Input.insertText", {"text": letter})
        p.wait("document.querySelectorAll('#threads li[data-id]').length === 11")   # Thread 1 and 10 to 19
        self.assertEqual(self.asked("list")[lists:], [["list", "--box", "all", "--limit", "100", "--query", "Thread 1"]])
        self.assertEqual(p.js("[document.getElementById('boxes').hidden, document.getElementById('searching').textContent,"
                              " document.getElementById('summary').textContent]"),
                         [True, "Search: \u201cThread 1\u201d, in every box", "11 threads"])
        p.tap("#clearquery")
        p.wait("!document.getElementById('boxes').hidden && document.querySelectorAll('#threads li[data-id]').length === 30")
        self.until(lambda: len(self.asked("list")) > lists + 1, "the box listed again")
        self.assertEqual(self.asked("list")[lists + 1:], [["list", "--box", "inbox", "--limit", "100"]])

    def test_a_box_picked_again_shows_its_last_list_at_once(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        p.tap('#boxes button[data-box="archived"]')
        p.wait("document.querySelector('#threads li.empty')")
        self.assertEqual(p.js("document.querySelector('#threads li.empty').textContent"), "Nothing archived.")
        self.slow = 3                                         # the inbox's answer takes 3 seconds from now on
        lists = len(self.asked("list"))
        started = time.time()
        p.tap('#boxes button[data-box="inbox"]')
        p.wait("document.querySelector('#boxes button[data-box=\"inbox\"]').classList.contains('on') && "
               "document.querySelectorAll('#threads li[data-id]').length === 30")
        self.assertLess(time.time() - started, 2)            # drawn from the list kept, not the desktop's answer
        self.until(lambda: len(self.asked("list")) > lists, "the inbox asked for again")
        self.assertEqual(self.asked("list")[lists:], [["list", "--box", "inbox", "--limit", "100"]])   # asked again
        self.until(lambda: len(self.answered) > lists, "the inbox's late answer", seconds=self.slow + 5)

    def test_the_threads_kept_stay_within_their_budget_the_least_recently_used_going_first(self):
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        got = p.js("""(() => {
          kept.clear();
          keptChars = 0;
          const doc = (id, chars) => ({ thread: { id: id }, messages: [], document: "x".repeat(chars) });
          const part = Math.floor(CACHE_CHARS * 0.4);
          keep(doc("a", part), 1);
          keep(doc("b", part), 2);
          take("a");                                     // a is used: b is now the least recently used
          keep(doc("c", part), 3);                       // past the budget: b goes
          const after = [Array.from(kept.keys()), keptChars];
          keep(doc("a", 10), 0);                         // an answer to an earlier request does not replace a later one
          keep(doc("c", 10), 4);                         // a later one does
          return [CACHE_CHARS, after, Array.from(kept.keys()), keptChars, kept.get("a").show.document.length];
        })()""")
        part = int(got[0] * 0.4)
        self.assertEqual(got, [24_000_000, [["a", "c"], 2 * part], ["a", "c"], part + 10, part])

    def test_the_times_are_written_as_on_the_mac(self):
        # MailFmt.listTime and MailFmt.at's cases in the Mac's --selftest-mail, in UTC, now Wed 18 Nov 2026 14:20
        p = self.page
        p.send("Emulation.setTimezoneOverride", {"timezoneId": "UTC"})
        p.send("Emulation.setLocaleOverride", {"locale": "en-US"})
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        got = p.js("""(() => {
          const now = new Date("2026-11-18T14:20:00Z"), ts = (s) => Date.parse(s) / 1000;
          return [["2026-11-18T09:05:00Z", "2026-11-17T22:00:00Z", "2026-11-16T10:00:00Z", "2026-11-12T15:00:00Z",
                   "2026-11-12T14:00:00Z", "2025-12-01T10:00:00Z"].map((s) => listTime(ts(s), now)),
                  ["2026-11-18T17:00:00Z", "2026-11-21T08:00:00Z", "2026-11-30T08:00:00Z"].map((s) => aheadTime(ts(s), now))];
        })()""")
        self.assertEqual(got, [["09:05", "Yesterday", "Mon", "Thu", "12 Nov", "1 Dec 2025"],
                               ["17:00", "Sat 08:00", "Mon 30 Nov 08:00"]])

    def test_researcher_switch_uses_the_passkey_and_refreshes_shared_state(self):
        p = self.page
        first, second = [t["id"] for t in APP_THREADS[1:3]]
        self.researcher_owners = {first: "agent001", second: "agent001"}
        self.researcher_names = {"agent001": "agent A"}
        self.enrolled()
        self.open(1)
        switch = "document.getElementById('researcher')"
        pressed = switch + ".getAttribute('aria-pressed')"
        note = "document.getElementById('researchernote').textContent"
        self.assertEqual(p.js(switch + ".textContent"), "Researcher")
        self.assertEqual(p.js(pressed), "false")
        self.assertEqual("Off for agent A.", p.js(note))
        self.assertIn("Turn on Researcher to approve", p.js("document.getElementById('researcherhelp').textContent"))
        self.assertTrue(p.js("document.getElementById('fast').parentElement === " + switch + ".parentElement"))

        # This list took its snapshot before the switch. It must not reset the switch as it finally arrives.
        self.slow = 1
        lists = len(self.asked("list"))
        p.js("void quietRefresh()")
        self.until(lambda: len(self.asked("list")) > lists, "the list request before the switch")
        self.slow = 0
        p.tap("#researcher")
        p.wait(pressed + " === 'true' && !" + switch + ".disabled")
        self.assertEqual(self.asked("researcher"), [["researcher", "--expect-tag", "agent001", "--", first, "on"]])
        self.until(lambda: len(self.answered) >= lists + 2, "the old list and the refresh after the switch")
        self.assertEqual(p.js(pressed), "true")
        self.assertEqual(p.js("document.getElementById('fast').getAttribute('aria-pressed')"), "false")
        self.back()
        self.open(2)
        p.wait(pressed + " === 'true'")
        self.assertIn("Requested on for agent A", p.js(note))
        self.researcher_deliveries["agent001"] = "applied"
        p.js("void quietRefresh()")
        p.wait(note + " === 'agent A will ask you to approve its experiment plan in Mail.'")

        self.researcher_error = "agent-mail: unknown command researcher"
        p.tap("#researcher")
        p.wait("document.getElementById('snacktext').textContent.includes('unknown command researcher')")
        p.wait(pressed + " === 'true' && !" + switch + ".disabled")
        self.researcher_error = None
        p.tap("#researcher")
        p.wait(pressed + " === 'false' && !" + switch + ".disabled")
        self.assertEqual(self.asked("researcher"), [
            ["researcher", "--expect-tag", "agent001", "--", first, "on"],
            ["researcher", "--expect-tag", "agent001", "--", second, "off"],
            ["researcher", "--expect-tag", "agent001", "--", second, "off"]])
        self.back()
        self.open(0)
        self.assertTrue(p.js(switch + ".hidden"))  # an unavailable route or an older mailroom's response

    def test_researcher_refuses_a_changed_route_and_shows_control_delivery_after_refresh(self):
        p = self.page
        thread = APP_THREADS[1]["id"]
        self.researcher_owners[thread] = "agent001"
        self.researcher_names = {"agent001": "agent A", "agent002": "agent A"}
        self.enrolled()
        self.open(1)
        pressed = "document.getElementById('researcher').getAttribute('aria-pressed')"
        note = "document.getElementById('researchernote').textContent"

        # A newer sender takes over with the same display name after the thread was rendered.
        self.researcher_owners[thread] = "agent002"
        p.tap("#researcher")
        self.until(lambda: self.asked("researcher"), "the potentially stale switch")
        self.assertEqual(self.researcher_modes, {})
        p.wait("document.getElementById('snacktext').textContent.includes('owner changed')")
        self.assertEqual(self.researcher_modes, {})
        p.wait("!document.getElementById('researcher').disabled")
        self.assertEqual(p.js(pressed), "false")

        p.tap("#researcher")
        p.wait(pressed + " === 'true'")
        p.wait(note + ".includes('Delivery pending')")
        self.assertEqual(self.researcher_modes, {"agent002": True})
        self.assertEqual(self.asked("researcher"), [
            ["researcher", "--expect-tag", "agent001", "--", thread, "on"],
            ["researcher", "--expect-tag", "agent002", "--", thread, "on"]])
        detail = "Refresh the Researcher 0.7+ plugin on <laptop>."
        self.researcher_delivery_details["agent002"] = detail
        self.researcher_deliveries["agent002"] = "error"
        p.js("void quietRefresh()")
        p.wait(note + ".includes('Delivery failed')")
        self.assertEqual(p.js(pressed), "true")
        self.assertNotIn(detail, p.js(note))
        self.researcher_deliveries["agent002"] = "unavailable"
        p.js("void quietRefresh()")
        p.wait(note + ".endsWith(" + json.dumps(detail) + ")")
        self.assertIsNone(p.js("document.querySelector('#researchernote laptop')"))
        self.assertEqual(p.js(pressed), "true")
        self.researcher_delivery_details["agent002"] = " "
        p.js("void quietRefresh()")
        p.wait(note + ".endsWith('Agent unavailable.')")

    def test_fast_mode_shows_its_minutes_left_on_the_row_and_the_thread_s_switch_turns_it_on_and_off(self):
        p = self.page
        codex, plain = APP_THREADS[3]["id"], APP_THREADS[1]["id"]
        note = "Effort unchanged: the agent runs on laptop, where the mailroom presses no keys."
        self.fast[codex] = {"since": time.time() - 200, "until": time.time() + 100, "minutes": 5, "agent": "agent3",
                            "effort": "unchanged", "was": None, "note": note}
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        badge = "(id) => { const m = document.querySelector('#threads li[data-id=\"' + id + '\"] .fastmark'); " \
                "return m ? m.textContent : null; }"
        self.assertEqual(p.js("[(%s)(%s), (%s)(%s)]" % (badge, json.dumps(codex), badge, json.dumps(plain))),
                         ["Fast · 2 min", None])
        switch = "[document.getElementById('fastlabel').textContent, document.getElementById('fast').getAttribute(" \
                 "'aria-pressed'), document.getElementById('fastnote').hidden ? null : " \
                 "document.getElementById('fastnote').textContent]"
        self.open(3)
        self.assertEqual(p.js(switch), ["Fast · 2 min", "true", note])           # the note says effort is unchanged
        p.tap("#fast")
        p.wait("document.getElementById('fast').getAttribute('aria-pressed') === 'false' && "
               "!document.getElementById('fast').disabled")
        self.assertEqual(p.js(switch), ["Fast", "false", None])
        self.assertIn(["fast", "--", codex, "off"], self.calls)
        self.back()
        self.assertIsNone(p.js("(%s)(%s)" % (badge, json.dumps(codex))))         # off in the list at once
        self.open(1)
        self.assertEqual(p.js(switch), ["Fast", "false", None])
        p.tap("#fast")
        p.wait("document.getElementById('fast').getAttribute('aria-pressed') === 'true'")
        self.assertEqual(p.js(switch), ["Fast · 5 min", "true", "Lowering effort… Back to xhigh when fast mode ends."])
        self.assertIn(["fast", "--", plain, "on"], self.calls)
        self.fast[plain] = dict(self.fast[plain], effort="low",      # the mailroom's next poll has lowered it
                                note="Effort low until fast mode ends, then xhigh again.")
        p.js("quietRefresh()")
        p.wait("document.getElementById('fastnote').textContent === 'Effort low until fast mode ends, then xhigh again.'")
        self.back()
        self.assertEqual(p.js("(%s)(%s)" % (badge, json.dumps(plain))), "Fast · 5 min")
        self.fast_fails.add(APP_THREADS[2]["id"])                                 # a thread it cannot be used on
        self.open(2)
        p.tap("#fast")
        p.wait("document.getElementById('snacktext').textContent.startsWith('Fast mode not switched on: ')")
        self.assertIn("one agent owns", p.js("document.getElementById('snacktext').textContent"))
        self.assertEqual(p.js(switch), ["Fast", "false", None])
        self.back()
        self.fast_threadless.add(APP_THREADS[4]["id"])                            # an answer that names no thread
        self.open(4)
        p.tap("#fast")
        p.wait("document.getElementById('snacktext').textContent === "
               "\"Fast mode not switched on: the desktop's answer has no thread\"")
        self.assertEqual(p.js(switch), ["Fast", "false", None])
        # the minutes left, as the Mac counts them (MailFast.label in its --selftest-mail): rounded up, at most its
        # length, and "ending" once its end has passed
        got = p.js("""(() => {
          const now = new Date(1800000000000), f = (s, minutes) => ({ until: 1800000000 + s, minutes: minutes });
          return [fastLabel(f(420, 10), now), fastLabel(f(400, 10), now), fastLabel(f(61, 10), now),
                  fastLabel(f(1, 10), now), fastLabel(f(0, 10), now), fastLabel(f(-5, 10), now),
                  fastLabel(f(610, 10), now), fastLabel(f(90, 0.5), now), fastLabel(f(90), now)];
        })()""")
        self.assertEqual(got, ["Fast · 7 min", "Fast · 7 min", "Fast · 2 min", "Fast · 1 min", "Fast · ending",
                               "Fast · ending", "Fast · 10 min", "Fast · 1 min", "Fast · 2 min"])

    def test_the_app_is_dark_when_the_phone_is(self):
        for page, want in ((self.page, "rgb(255, 255, 255)"), (self.chrome.page(dark=True), "rgb(19, 19, 20)")):
            page.go(self.base + "/")
            page.wait("document.querySelector('#threads li.unread')")
            self.assertEqual(page.js("getComputedStyle(document.body).backgroundColor"), want)

    def test_a_grouped_row_is_a_plain_row_with_a_strip_of_its_sub_group_s_shade(self):
        # threads 0-4: agent0 (Work: Loader) unread, agent1 (Work) read, agent2 (Side projects: Reading) read,
        # agent3 (Admin) unread, agent4 (no group) read: each strip its sub-group's shade, else its group's colour,
        # as the desktop works them out from the kit's palette (agentboard/colours.py)
        coloured = colours.for_layout(groups.layout(), colours.load())
        shades = [colours.of(coloured, place)["shade"] for place in (
            ("Work", "Loader"), ("Work", None), ("Side projects", "Reading"), ("Admin", None), None)]
        self.assertEqual(len(set(shades)), 5, shades)
        # each row's ground, its text's colours, its blue dot and its strip: as drawn, then with "grouped" taken off,
        # as a row the desktop sent no colours with is drawn
        rows = """[...document.querySelectorAll('#threads li[data-id]')].slice(0, 5).map((li) => {
            const look = () => {
              const row = getComputedStyle(li.querySelector('.row'));
              return [row.backgroundColor, getComputedStyle(li.querySelector('.who')).color,
                      getComputedStyle(li.querySelector('.snip')).color,
                      getComputedStyle(li.querySelector('.dot.new')).backgroundColor, row.boxShadow];
            };
            const grouped = [li.className].concat(look());
            li.classList.remove('grouped');
            const plain = look();
            li.classList.add('grouped');
            return [grouped, plain];
        })"""
        # light and dark: an unread row on the page's ground, a read one on the read rows' grey (app.css)
        for page, ground, read in ((self.page, "rgb(255, 255, 255)", "rgb(244, 244, 245)"),
                                   (self.chrome.page(dark=True), "rgb(19, 19, 20)", "rgb(27, 27, 29)")):
            page.go(self.base + "/")
            page.wait("document.querySelector('#threads li.grouped')")
            for i, (shade, (grouped, plain)) in enumerate(zip(shades, page.js(rows))):
                unread = APP_THREADS[i]["unread"]
                self.assertEqual(grouped[0], "%s grouped" % ("unread" if unread else "read"), i)
                self.assertEqual(grouped[1:5], plain[:4], i)          # its ground, text and blue dot a plain row's
                self.assertEqual(grouped[1], ground if unread else read, i)
                self.assertEqual(grouped[5], "rgb(%d, %d, %d) 5px 0px 0px 0px inset"
                                 % tuple(int(shade[j:j + 2], 16) for j in (1, 3, 5)), i)
                self.assertEqual(plain[4], "none", i)                 # the strip is all a group adds
            self.assertTrue(page.js("document.getElementById('colourerror').hidden"))

    def test_a_list_without_its_groups_says_why_and_its_rows_are_plain(self):
        sources.MAIL_DB.unlink()                      # the store gone: no sessions, no groups
        p = self.page
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        p.wait("!document.getElementById('colourerror').hidden")
        self.assertIn("No group colours: FileNotFoundError: no database at",
                      p.js("document.getElementById('colourerror').textContent"))
        self.assertEqual(p.js("document.querySelectorAll('#threads li.grouped').length"), 0)
        self.assertEqual(p.js("getComputedStyle(document.querySelector('#threads li.read .row')).backgroundColor"),
                         "rgb(244, 244, 245)")           # a read row on its grey ground, as before

    # the desktop's keyring

    MARK = ("[document.getElementById('keyringmark').hidden, document.getElementById('keyringtext').textContent, "
            "document.getElementById('keyringmark').classList.contains('unknown')]")
    FIELD = "document.getElementById('keyringpassword').value"
    SAID = "document.getElementById('keyringsaid')"

    def unlock_sheet(self):
        """Tap the list's keyring mark, and wait for its sheet to be up and still."""
        self.page.tap("#keyringmark")
        self.settled("#keyringsheet")

    def type_password(self, text=TYPED):
        """Type `text` into the sheet's password field, as the keyboard does."""
        self.page.js("document.getElementById('keyringpassword').focus()")
        self.page.send("Input.insertText", {"text": text})

    def front(self, page):
        """Bring the tab `page` to the front, as the app on screen: the one behind it is hidden, and takes no touch."""
        self.chrome.send("Target.activateTarget", {"targetId": page.send("Target.getTargetInfo")["targetInfo"]
                                                   ["targetId"]})

    def unlocked_with(self, text):
        """Tap Unlock, and wait for the sheet to say `text`."""
        self.page.tap("#keyringunlock")
        self.page.wait("%s.textContent === %s" % (self.SAID, json.dumps(text)))

    def test_the_locked_keyring_is_marked_on_the_list_and_its_sheet_unlocks_it_with_the_passkey(self):
        """The password field takes no autofill and has no length of its own, and is emptied as it is sent; the passkey
        approves a nonce, the password goes once, after the unlock's line, and reaches keyring-unlock's stdin as typed."""
        p = self.page
        self.keyring.lock()
        self.enrolled()
        self.assertEqual(p.js(self.MARK), [False, "Desktop browser locked", False])
        self.assertEqual(rgb(p.js("getComputedStyle(document.getElementById('keyringmark')).color")), (197, 34, 31))
        self.unlock_sheet()
        self.assertEqual(p.js("(() => { const f = document.getElementById('keyringpassword'); return [f.type, "
                              "f.getAttribute('autocomplete'), f.hasAttribute('maxlength'), f.value, "
                              "document.activeElement === f, document.getElementById('keyringunlock').disabled, "
                              "%s.textContent]; })()" % self.SAID),
                         ["password", "one-time-code", False, "", True, True,
                          "Chrome's 'Default keyring': locked · 'Login': locked"])
        p.js("window.sent = []; const f = window.fetch; window.fetch = (u, o) => { window.sent.push([o.body, "
             "document.getElementById('keyringpassword').value]); return f(u, o); }")
        self.type_password()
        self.assertFalse(p.js("document.getElementById('keyringunlock').disabled"))
        self.keyring.answer(0, {"result": "unlocked", "locked": False})
        self.keyring.lock("unlocked")
        self.unlocked_with("Unlocked. The desktop's browser can use its passwords again.")
        self.assertEqual(p.js("[%s, document.getElementById('keyringmark').hidden, %s.className]" % (self.FIELD,
                                                                                                     self.SAID)),
                         ["", True, "status ok"])
        self.assertEqual([r["stdin"] for r in self.keyring.runs()], [TYPED.encode()])
        (begin, field_then), (unlock, field_after) = p.js("window.sent")
        line, after = unlock.split("\n", 1)
        request = json.loads(line)
        self.assertEqual((json.loads(begin)["approves"], json.loads(begin)["digest"], request["action"], after),
                         ("keyring_unlock", hashlib.sha256(request["request"].encode()).hexdigest(), "keyring_unlock",
                          TYPED))
        self.assertTrue(mailbox.KEY_RE.fullmatch(json.loads(request["request"])["nonce"]), request["request"])
        self.assertEqual(set(json.loads(request["request"])), {"nonce"})
        self.assertNotIn(TYPED, begin + line)                           # only after the line, and only once approved
        self.assertEqual((field_then, field_after), ("", ""))             # emptied as it was sent

    def test_each_answer_of_an_unlock_is_said_as_the_mac_says_it(self):
        p = self.page
        self.keyring.lock()
        self.enrolled()
        self.unlock_sheet()
        both_locked = stand_ins.collections(True, True)
        for code, said, text, how in (
                (1, {"result": "wrong", "locked": True, "tried": {"default": "wrong", "login": "wrong"},
                     "collections": both_locked},
                 "Wrong password. Still locked: Chrome's 'Default keyring' and 'Login'.", "bad"),
                (4, {"result": "partial", "locked": True, "tried": {"default": "wrong", "login": "unlocked"},
                     "collections": stand_ins.collections(True, False)},
                 "Unlocked 'Login', but Chrome's 'Default keyring' has a different password.", "bad"),
                (4, {"result": "partial", "locked": False, "tried": {"default": "unlocked", "login": "error"},
                     "collections": stand_ins.collections(False, True), "error": "UnlockWithMasterPassword: timed out"},
                 "Unlocked Chrome's 'Default keyring', but 'Login' could not be unlocked: UnlockWithMasterPassword: "
                 "timed out.", "ok"),
                (2, {"result": "rate_limited", "locked": True, "retry_after": 125},
                 "Too many wrong passwords: try again in 3 min.", "bad"),
                (3, {"result": "error", "locked": None, "error": "no session bus"}, "Not unlocked: no session bus", "bad"),
                (0, {"result": "unlocked", "locked": False}, "Unlocked. The desktop's browser can use its passwords "
                 "again.", "ok")):
            self.keyring.answer(code, said)
            self.type_password()
            self.unlocked_with(text)
            self.assertEqual(p.js("[%s.className, %s]" % (self.SAID, self.FIELD)), ["status " + how, ""], code)
        actions.AUDIT = self.root / "agentmail" / "mailbox.py" / "actions.jsonl"        # under a file: not recorded
        self.keyring.answer(1, {"result": "wrong", "locked": True})
        self.type_password()
        self.unlocked_with("Wrong password. (the board could not record this action: [Errno 17] File exists: '%s')"
                           % (self.root / "agentmail" / "mailbox.py"))
        self.guard.unlock_command = ["/nonexistent/agent-board", "keyring-unlock"]       # a desktop that cannot run it
        self.type_password()
        self.unlocked_with("Not unlocked: keyring-unlock could not start: [Errno 2] No such file or directory: "
                           "'/nonexistent/agent-board'; the board could not record this action: [Errno 17] File "
                           "exists: '%s'" % (self.root / "agentmail" / "mailbox.py"))
        self.assertEqual(len(self.keyring.runs()), 7)

    def test_a_keyring_whose_state_cannot_be_read_is_marked_grey_and_its_sheet_says_why(self):
        p = self.page
        self.keyring.lock("unknown", "Chrome's keyring (the \"default\" alias) could not be read: busctl timed out")
        p.go(self.base + "/")
        self.assertTrue(self.listed())
        p.wait("!document.getElementById('keyringmark').hidden")
        self.assertEqual(p.js(self.MARK), [False, "Desktop keyring unknown", True])
        self.unlock_sheet()
        self.assertEqual(p.js(self.SAID + ".textContent"), "The board can't tell whether Chrome's keyring on the "
                         "desktop is locked: Chrome's keyring (the \"default\" alias) could not be read: busctl timed "
                         "out")

    def test_the_password_field_is_emptied_on_closing_hiding_leaving_and_signing_out_and_never_restored(self):
        p = self.page
        self.keyring.lock()
        self.enrolled()
        self.unlock_sheet()
        self.type_password()
        self.assertEqual(p.js(self.FIELD), TYPED)
        p.tap("#keyringsheet .sheetclose")
        p.wait("document.getElementById('keyringsheet').hidden")
        self.assertEqual(p.js(self.FIELD), "")                            # closed
        self.unlock_sheet()
        self.type_password()
        self.front(self.chrome.page())                                     # another app over it: the page hidden
        p.wait("document.visibilityState === 'hidden'")
        self.assertEqual(p.js(self.FIELD), "")
        self.front(p)
        p.wait("document.visibilityState === 'visible'")
        self.type_password()
        p.js("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")   # left for another page
        self.assertEqual(p.js(self.FIELD), "")
        self.type_password()
        p.go(self.base + "/manifest.webmanifest")                          # away, and back
        h = p.send("Page.getNavigationHistory")
        p.send("Page.navigateToHistoryEntry", {"entryId": h["entries"][h["currentIndex"] - 1]["id"]})
        self.assertTrue(self.listed())
        self.assertEqual(p.js("[%s, document.getElementById('keyringsheet').hidden]" % self.FIELD), ["", True])
        self.unlock_sheet()
        self.type_password()
        p.js("signOut()")                                                  # the sheet up, the password typed
        p.wait("!document.getElementById('gate').hidden")
        self.assertEqual(p.js("[%s, document.getElementById('keyringsheet').classList.contains('up'), "
                              "document.getElementById('keyringmark').hidden]" % self.FIELD), ["", False, True])
        self.assertEqual(self.keyring.runs(), [])

    def test_chrome_never_offers_to_save_the_desktop_password(self):
        """Read from Chrome's own record of what its password manager decides (chrome://password-manager-internals),
        after an unlock and then the strongest sign Chrome takes of a form sent: the field gone after a request
        succeeded. autocomplete="off" and "current-password" are asked about then, and "new-password" at once."""
        logs = self.chrome.page()
        logs.go("chrome://password-manager-internals/")
        p = self.page
        self.front(p)                                                      # the internals log behind it, recording
        self.keyring.lock()
        self.enrolled()
        self.unlock_sheet()
        self.type_password()
        self.keyring.answer(1, {"result": "wrong", "locked": True})
        self.unlocked_with("Wrong password.")
        p.js("document.getElementById('keyringsheet').remove(); refreshKeyring()")
        time.sleep(1)
        said = logs.js("document.body.innerText")
        self.assertIn("type=password, renderer_id", said)                  # it saw the field
        self.assertIn("autocomplete=one-time-code", said)
        self.assertNotIn("Decision: ASK the user", said)
        self.assertNotIn("Show password prompt", said)


class EnrolActionTest(MailTest):
    """mail_web_enrol: the Mac's one-time code, as the page's enrolment address."""

    def test_the_code_comes_as_the_page_s_address_and_stays_off_the_record(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        store = mailwebstate.Store(d / "mail-web")
        tailscale = AgentMail(answer({"BackendState": "Running", "Self": {"DNSName": "Desk.tail1.ts.net."}}))
        got = actions.dispatch({"action": "mail_web_enrol"}, None, None, run=tailscale, store=store, now=1000.0)
        self.assertEqual(got["lapses"], 1600.0)
        prefix = "https://desk.tail1.ts.net:8466/enrol#"
        self.assertTrue(got["url"].startswith(prefix), got["url"])
        code = got["url"][len(prefix):]
        self.assertNotIn(code, actions.AUDIT.read_text())
        self.assertTrue(store.take_code(code, 1001.0))
        got = actions.dispatch({"action": "mail_web_enrol"}, None, None, run=AgentMail((1, "", "not running")),
                               store=store, now=1000.0)
        self.assertEqual(got, {"ok": False, "error": "the phone page is not set up: CommandError: not running"})
        saved = mailwebstate.CONFIG
        self.addCleanup(setattr, mailwebstate, "CONFIG", saved)
        mailwebstate.CONFIG = d / "mailweb.json"
        mailwebstate.CONFIG.write_text('{"port": 443}')
        got = actions.dispatch({"action": "mail_web_enrol"}, None, None, run=tailscale, store=store, now=1000.0)
        self.assertFalse(got["ok"])
        self.assertIn("not one Funnel can open", got["error"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ReplyTextTest(unittest.TestCase):
    """web/mail/replytext.js, the phone's copy of the Mac's reply text (ReplyText and MailWidgets.answer), run under
    node on the cases both copies are checked on: mac/Sources/AgentBoard/reply-text-cases.json, which the Mac's
    --selftest-mail runs its own copy on."""

    def test_the_phone_puts_a_reply_together_as_the_mac_does(self):
        cases = json.loads(CASES.read_text())
        js = """const R = require(%s), c = JSON.parse(require("fs").readFileSync(%s, "utf8"));
          console.log(JSON.stringify({trim: c.trim.map((t) => R.trim(t.text)),
                                      plain: c.plain.map((t) => R.plain(t.text, t.lines)),
                                      answers: c.answers.cases.map((t) => R.answer(t.post, c.answers.widgets)),
                                      replies: c.replies.map((t) => R.reply(t))}));""" % (
            json.dumps(str(mailweb.WEB / "replytext.js")), json.dumps(str(CASES)))
        got = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        self.assertTrue(all((cases["trim"], cases["plain"], cases["replies"], cases["answers"]["cases"])))
        for kind, want in (("trim", "trimmed"), ("plain", "plain"), ("replies", "reply")):
            self.assertEqual(len(got[kind]), len(cases[kind]), kind)
            for case, out in zip(cases[kind], got[kind]):
                with self.subTest(kind, what=case["what"]):
                    self.assertEqual(out, case[want])
        self.assertEqual(len(got["answers"]), len(cases["answers"]["cases"]))
        for case, out in zip(cases["answers"]["cases"], got["answers"]):
            with self.subTest("answers", what=case["what"]):
                self.assertEqual(out, case["taken"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SnoozeTest(unittest.TestCase):
    """web/mail/snooze.js, run under node in UTC on the cases the Mac's --selftest-mail checks of the same rule
    (Snooze.presets in mac/Sources/AgentBoard/Mail.swift)."""

    def presets(self, iso):
        js = ("const {snoozePresets} = require(%s); console.log(JSON.stringify(snoozePresets(Date.parse(%s))))"
              % (json.dumps(str(mailweb.WEB / "snooze.js")), json.dumps(iso)))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True,
                             env=dict(os.environ, TZ="UTC")).stdout
        utc = datetime.timezone.utc
        return ["%s %s" % (c["label"], datetime.datetime.fromtimestamp(c["at"], utc).strftime("%a %d %H:%M"))
                for c in json.loads(out)]

    def test_the_choices_are_the_mac_s(self):
        self.assertEqual(self.presets("2026-11-18T14:20:00Z"), ["Later today Wed 18 17:00", "Tomorrow Thu 19 08:00",
                                                                "This weekend Sat 21 08:00", "Next week Mon 23 08:00"])
        self.assertEqual(self.presets("2026-11-18T14:40:00Z")[0], "Later today Wed 18 18:00")      # 17:40 rounds up
        self.assertEqual(self.presets("2026-11-18T12:30:00Z")[0], "Later today Wed 18 16:00")      # 15:30 rounds up
        self.assertEqual(self.presets("2026-11-18T21:40:00Z")[0], "Tomorrow Thu 19 08:00")         # not tomorrow's 01:00
        self.assertEqual(self.presets("2026-11-20T23:00:00Z"),                       # Friday night: the weekend is tomorrow
                         ["Tomorrow Sat 21 08:00", "Next week Mon 23 08:00"])
        self.assertEqual(self.presets("2026-11-21T10:00:00Z"),                       # Saturday: no weekend
                         ["Later today Sat 21 13:00", "Tomorrow Sun 22 08:00", "Next week Mon 23 08:00"])
        self.assertEqual(self.presets("2026-11-22T10:00:00Z"),                       # Sunday: next week is tomorrow
                         ["Later today Sun 22 13:00", "Tomorrow Mon 23 08:00"])
        self.assertEqual(self.presets("2026-11-23T06:00:00Z")[-1], "Next week Mon 30 08:00")   # early Monday


class SettingsTest(unittest.TestCase):
    def test_the_settings_must_be_usable(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "mailweb.json"
            self.assertEqual(mailwebstate.settings(path), mailwebstate.DEFAULTS)
            for key, bad, said in (("port", 80, "port must be"), ("port", "8466", "port must be"),
                                   ("port", True, "port must be"), ("port", 8443, "not one Funnel can open"),
                                   ("port", 10000, "not one Funnel can open"), ("devices", "nPhone", "devices must be"),
                                   ("devices", [""], "devices must be"), ("proxy_uids", 0, "proxy_uids must be"),
                                   ("proxy_uids", [-1], "proxy_uids must be"), ("proxy_uids", [True], "proxy_uids"),
                                   ("session_hours", 48, "session_hours"), ("enrol_minutes", 0, "enrol_minutes"),
                                   ("logins", "alex@github", "logins must be"), ("logins", [7], "logins must be")):
                path.write_text(json.dumps({key: bad}))
                with self.assertRaisesRegex(ValueError, said):
                    mailwebstate.settings(path)

    def test_the_logins_are_the_settings_else_the_owner_s_addresses(self):
        cfg = dict(mailwebstate.DEFAULTS)
        self.assertEqual(mailwebstate.logins(cfg, ["Owner@Example.com"]), ["owner@example.com"])
        self.assertEqual(mailwebstate.logins(dict(cfg, logins=[" Alex@GitHub "]), ["owner@example.com"]),
                         ["alex@github"])
        self.assertEqual(mailwebstate.logins(cfg), [OWNER])                  # the mailroom config's owner_email
        with self.assertRaisesRegex(ValueError, "answer no one"):
            mailwebstate.logins(cfg, [])

    def test_the_tailnet_name_is_tailscale_s(self):
        run = AgentMail(answer({"BackendState": "Running", "Self": {"DNSName": "Desk.tail1.ts.net."}}))
        saved = sources.find_bin
        sources.find_bin = lambda name: "/bin/" + name
        self.addCleanup(setattr, sources, "find_bin", saved)
        self.assertEqual(mailwebstate.tailnet_name(run), DESK)
        self.assertEqual(run.calls[0][0], ["/bin/tailscale", "status", "--json"])
        with self.assertRaisesRegex(RuntimeError, "no tailnet name.*NeedsLogin"):
            mailwebstate.tailnet_name(AgentMail(answer({"BackendState": "NeedsLogin", "Self": {"DNSName": ""}})))
        with self.assertRaisesRegex(sources.CommandError, "failed to connect"):
            mailwebstate.tailnet_name(AgentMail((1, "", "failed to connect to local tailscaled")))


if __name__ == "__main__":
    unittest.main()
