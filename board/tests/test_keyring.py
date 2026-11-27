"""The desktop keyring: reading its state, unlocking it, the input bound, the rate limit, the CLI and the board's
section. python3 -m unittest discover -s tests

Every test runs against a private dbus-daemon of its own, on which FakeKeyring stands in for gnome-keyring: the real
daemon is the owner's security store, and no test may unlock, create or change anything in it, nor read it but for its
Locked property. Everything else is real: busctl, Gio, cryptography, the files the commands write.
"""
import hashlib
import hmac
import json
import os
import pty
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fixtures  # noqa: E402
from agentboard import keyring, lockdown, mailweb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ME = os.readlink("/proc/self/exe")         # the program FakeKeyring runs in: the tests' "daemon"
SCRATCH = Path(tempfile.mkdtemp(prefix="kr-"))        # short: the private buses' sockets are made in it
PASSWORD = "correct horse ☃ battery"
_REAL = {}


def setUpModule():
    # Nothing here may reach the owner's session bus, even by a slip: the address every client would find points
    # nowhere, and the module's state folder is a scratch one.
    _REAL.update(env={k: os.environ.get(k) for k in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")},
                 state=keyring.STATE)
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/no-bus-here" % SCRATCH
    os.environ["XDG_RUNTIME_DIR"] = str(SCRATCH)
    keyring.STATE = SCRATCH / "state"


def tearDownModule():
    for k, v in _REAL["env"].items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    keyring.STATE = _REAL["state"]


BUS_CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>{listen}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


class PrivateBus:
    """A dbus-daemon of the test's own, with no service files (so nothing can be started on it): its address. Its
    socket is in a folder of the test's own, unless that folder's path is too long for one (dbus-daemon takes at most
    about 99 bytes, the socket's own name included: a deep TMPDIR), when it is a random name in the abstract namespace
    (Linux), which no file holds."""

    def __init__(self):
        d = Path(tempfile.mkdtemp(prefix="b-", dir=SCRATCH))
        listen = ("unix:dir=%s" % d if len(str(d)) <= 80
                  else "unix:abstract=agent-board-test-%s" % secrets.token_hex(8))
        (d / "bus.conf").write_text(BUS_CONF.format(listen=listen))
        self.proc = subprocess.Popen(["dbus-daemon", "--config-file=%s" % (d / "bus.conf"), "--nofork",
                                      "--print-address=1"], stdout=subprocess.PIPE, text=True)
        self.address = self.proc.stdout.readline().strip()

    def close(self):
        self.proc.terminate()
        self.proc.wait(10)
        self.proc.stdout.close()


XML = """<node>
  <interface name="org.freedesktop.Secret.Service">
    <method name="OpenSession">
      <arg name="algorithm" type="s" direction="in"/><arg name="input" type="v" direction="in"/>
      <arg name="output" type="v" direction="out"/><arg name="result" type="o" direction="out"/>
    </method>
    <method name="ReadAlias">
      <arg name="name" type="s" direction="in"/><arg name="collection" type="o" direction="out"/>
    </method>
  </interface>
  <interface name="org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface">
    <method name="ChangeWithMasterPassword">
      <arg name="collection" type="o" direction="in"/><arg name="original" type="(oayays)" direction="in"/>
      <arg name="master" type="(oayays)" direction="in"/>
    </method>
    <method name="CreateWithMasterPassword">
      <arg name="attributes" type="a{sv}" direction="in"/><arg name="master" type="(oayays)" direction="in"/>
      <arg name="collection" type="o" direction="out"/>
    </method>
    <method name="UnlockWithMasterPassword">
      <arg name="collection" type="o" direction="in"/><arg name="master" type="(oayays)" direction="in"/>
    </method>
  </interface>
  <interface name="org.freedesktop.Secret.Collection">
    <property name="Label" type="s" access="read"/>
    <property name="Locked" type="b" access="read"/>
  </interface>
</node>"""
DEFAULT = "Default_5fkeyring"           # the "Default keyring" the "default" alias names, which the browser uses


def path(name):
    """A collection's object path."""
    return keyring.COLLECTIONS + name


class FakeKeyring:
    """Stands in for gnome-keyring 46.1 on a PrivateBus, as its source has it (daemon/dbus/gkd-secret-service.c,
    gkd-secret-session.c): OpenSession of the dh-ietf1024-sha256-aes128-cbc-pkcs7 kind for each caller; ReadAlias, "/"
    for an alias that names nothing; UnlockWithMasterPassword, which says yes when the collection is unlocked already,
    answers NoSuchObject for one that does not exist, InvalidArgs for a secret that does not decrypt and
    org.gnome.keyring.Error.Denied for a wrong password; each collection's Label and Locked properties. Its half of the
    key exchange is its own (pow, and HKDF by hmac), so the client's is checked against another reading of the same
    steps. `cols` holds each collection by name ({label, password, locked}), by default as on a desktop: "login"
    and DEFAULT, both locked, with one password; `alias` is the collection the "default" alias names (None: none;
    one starting with "/": that object path as it is).
    `calls` keeps each method called, `tried` each collection an unlock was for, and `secrets` the raw secret values
    that came over the bus; `on_unlock`, when set, runs as each unlock comes in, `fail`, when set, is the D-Bus error
    each unlock of a collection it names ({name: error}) answers instead, and the collections named in `unreadable`
    answer no property."""

    def __init__(self, bus, cols=None, alias=DEFAULT):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.cols = cols if cols is not None else {
            "login": {"label": "Login", "password": PASSWORD, "locked": True},
            DEFAULT: {"label": "Default keyring", "password": PASSWORD, "locked": True}}
        self.alias = alias
        self.calls, self.tried, self.secrets, self.sessions = [], [], [], {}
        self.on_unlock, self.fail, self.unreadable = None, {}, set()
        self.info = Gio.DBusNodeInfo.new_for_xml(XML)
        self.ready = threading.Event()
        self.ctx = GLib.MainContext.new()
        self.thread = threading.Thread(target=self._serve, args=(bus.address,), daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("the fake keyring did not start")

    def locked(self, name):
        return self.cols[name]["locked"]

    def set_locked(self, locked):
        for c in self.cols.values():
            c["locked"] = locked

    def _serve(self, address):
        Gio, GLib = self.Gio, self.GLib
        self.ctx.push_thread_default()
        conn = Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        ifaces = {i.name: i for i in self.info.interfaces}
        for name in ("org.freedesktop.Secret.Service", "org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface"):
            conn.register_object(keyring.SERVICE, ifaces[name], self._method, None, None)
        for name in self.cols:
            conn.register_object(path(name), ifaces["org.freedesktop.Secret.Collection"], None, self._property, None)
        conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                       GLib.Variant("(su)", (keyring.SECRETS, 4)), None, 0, -1, None)
        self.loop = GLib.MainLoop.new(self.ctx, False)
        self.ready.set()
        self.loop.run()

    def close(self):
        src = self.GLib.idle_source_new()
        src.set_callback(lambda *a: self.loop.quit())
        src.attach(self.ctx)
        self.thread.join(10)

    def _property(self, conn, sender, at, iface, prop):
        if at.rsplit("/", 1)[1] in self.unreadable:
            return None                        # GDBus answers the caller with an error
        c = self.cols[at.rsplit("/", 1)[1]]
        return self.GLib.Variant("b", c["locked"]) if prop == "Locked" else self.GLib.Variant("s", c["label"])

    def _method(self, conn, sender, at, iface, method, params, inv):
        GLib = self.GLib
        self.calls.append(method)
        if method == "ReadAlias":
            name, = params.unpack()
            named = "/" if name != "default" or not self.alias else (
                self.alias if self.alias.startswith("/") else path(self.alias))
            return inv.return_value(GLib.Variant("(o)", (named,)))
        if method == "OpenSession":
            algorithm, given = params.unpack()
            if algorithm != keyring.ALGORITHM:
                return inv.return_dbus_error("org.freedesktop.DBus.Error.NotSupported", "not this algorithm")
            x = secrets.randbelow(keyring.MODP1024 - 3) + 2
            shared = pow(int.from_bytes(bytes(given), "big"), x, keyring.MODP1024).to_bytes(128, "big")
            prk = hmac.new(b"\0" * 32, shared, hashlib.sha256).digest()
            session = "/org/freedesktop/secrets/session/s%d" % len(self.sessions)
            self.sessions[session] = (sender, hmac.new(prk, b"\x01", hashlib.sha256).digest()[:16])
            mine = pow(2, x, keyring.MODP1024)
            unpadded = mine.to_bytes((mine.bit_length() + 7) // 8, "big")     # as gcrypt writes it (USG)
            return inv.return_value(GLib.Variant("(vo)", (GLib.Variant("ay", unpadded), session)))
        if method == "UnlockWithMasterPassword":
            collection, (session, iv, value, _type) = params.unpack()
            name = collection.removeprefix(keyring.COLLECTIONS) if collection.startswith(keyring.COLLECTIONS) else None
            self.tried.append(name)
            self.secrets.append(bytes(value))
            if self.on_unlock:
                self.on_unlock()
            if name in self.fail:
                return inv.return_dbus_error(self.fail[name], "the fake fails as asked")
            if self.sessions.get(session, (None,))[0] != sender:
                return inv.return_dbus_error("org.freedesktop.Secret.Error.NoSession", "no such session")
            if name not in self.cols:
                return inv.return_dbus_error("org.freedesktop.Secret.Error.NoSuchObject",
                                             "The collection does not exist")
            if not self.cols[name]["locked"]:
                return inv.return_value(None)
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            try:
                dec = Cipher(algorithms.AES(self.sessions[session][1]), modes.CBC(bytes(iv))).decryptor()
                unpad = padding.PKCS7(128).unpadder()
                given = unpad.update(dec.update(bytes(value)) + dec.finalize()) + unpad.finalize()
            except ValueError:
                return inv.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs",
                                             "The secret was transferred or encrypted in an invalid way.")
            if given != self.cols[name]["password"].encode():
                return inv.return_dbus_error(keyring.DENIED, "The password was invalid")
            self.cols[name]["locked"] = False
            return inv.return_value(None)
        return inv.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "the tests never call " + method)


class KeyringCase(unittest.TestCase):
    """A private bus with a fake keyring on it, as on a desktop and locked, and a scratch state folder, for each
    test."""

    def setUp(self):
        self.bus = PrivateBus()
        self.fake = FakeKeyring(self.bus)
        self.state = Path(tempfile.mkdtemp(prefix="state-", dir=SCRATCH))

    def tearDown(self):
        self.fake.close()
        self.bus.close()

    def log(self):
        return (self.state / "keyring.log").read_text()

    def other(self, cols=None, alias=DEFAULT):
        """Another fake keyring on a bus of its own, for this test: (bus, fake), closed after it."""
        bus = PrivateBus()
        fake = FakeKeyring(bus, cols, alias)
        self.addCleanup(bus.close)
        self.addCleanup(fake.close)
        return bus, fake


class StatusTest(KeyringCase):
    def test_the_state_is_chromes_default_keyring_with_login_beside_it_and_this_boot(self):
        st = keyring.status(self.bus.address)
        self.assertEqual((st["state"], st["locked"], st["error"]), ("locked", True, None))
        self.assertEqual(st["collections"], {
            "default": {"path": path(DEFAULT), "label": "Default keyring", "locked": True, "error": None},
            "login": {"path": path("login"), "label": "Login", "locked": True, "error": None}})
        self.assertEqual(st["boot_id"], Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        self.fake.cols["login"]["locked"] = False            # login unlocked, the browser's keyring not: still locked
        st = keyring.status(self.bus.address)
        self.assertEqual((st["state"], st["collections"]["login"]["locked"]), ("locked", False))
        self.fake.cols[DEFAULT]["locked"] = False
        self.assertEqual(keyring.status(self.bus.address)["state"], "unlocked")

    def test_the_alias_is_read_each_time(self):
        self.fake.cols["login"]["locked"] = False
        self.fake.alias = "login"
        st = keyring.status(self.bus.address)
        self.assertEqual((st["state"], st["collections"]["default"]["path"]), ("unlocked", path("login")))

    def test_an_alias_naming_no_collections_path_is_an_error_state(self):
        self.fake.alias = "/org/freedesktop/secrets/aliases/x"         # an object path, but not a collection's
        st = keyring.status(self.bus.address)
        self.assertEqual(st["state"], "unknown")
        self.assertIn("which is no collection's path", st["error"])

    def test_no_default_alias_is_an_error_state(self):
        self.fake.alias = None
        st = keyring.status(self.bus.address)
        self.assertEqual((st["state"], st["locked"]), ("unknown", None))
        self.assertIn('"default" alias names no collection', st["error"])
        self.assertEqual(st["collections"]["default"]["error"], st["error"])
        self.assertEqual(st["collections"]["login"]["locked"], True)

    def test_a_bus_with_no_keyring_is_unknown_and_starts_nothing(self):
        empty = PrivateBus()
        try:
            st = keyring.status(empty.address)
        finally:
            empty.close()
        self.assertEqual((st["state"], st["locked"]), ("unknown", None))
        self.assertIn("org.freedesktop.secrets", st["error"])

    def test_wait_returns_once_chromes_keyring_is_unlocked_and_gives_up_at_its_timeout(self):
        self.fake.cols["login"]["locked"] = False
        threading.Timer(0.3, lambda: self.fake.set_locked(False)).start()
        t0 = time.monotonic()
        self.assertEqual(keyring.wait(self.bus.address, timeout=5, poll=0.1)["state"], "unlocked")
        self.assertLess(time.monotonic() - t0, 3)
        self.fake.set_locked(True)
        t0 = time.monotonic()
        self.assertEqual(keyring.wait(self.bus.address, timeout=0.5, poll=0.1)["state"], "locked")
        self.assertLess(time.monotonic() - t0, 2)

    def test_the_board_snapshot_carries_the_state_and_lists_what_cannot_be_read(self):
        def snapshot(address):                   # the board's own reader, on the session bus the test names
            with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": address}):
                return fixtures.collector(tempfile.mkdtemp(dir=SCRATCH), keyring=keyring.watch(sync=True)).snapshot()

        def problems(snap):
            return [p["source"] for p in snap["problems"] if p["source"].startswith("keyring")]
        snap = snapshot(self.bus.address)
        self.assertEqual((snap["keyring"]["state"], problems(snap)), ("locked", []))
        bus, _ = self.other(cols={"login": {"label": "Login", "password": PASSWORD, "locked": True}}, alias=None)
        snap = snapshot(bus.address)
        self.assertEqual((snap["keyring"]["state"], problems(snap)), ("unknown", ["keyring (default)"]))


class UnlockTest(KeyringCase):
    def unlock(self, text, now=None):
        pw = bytearray(text.encode())
        res = keyring.unlock(pw, self.bus.address, self.state, ME, now=now)
        self.assertEqual(pw, bytearray(len(pw)), "the caller's buffer is zeroed")
        return res

    def results(self):
        return [a["result"] for a in json.loads((self.state / "attempts.json").read_text())]

    def assert_never_written(self, text):
        for p in self.state.rglob("*"):
            if p.is_file():
                self.assertNotIn(text.encode(), p.read_bytes(), p)

    def two_passwords(self):
        self.fake.cols["login"]["password"] = "login's own"
        self.fake.cols[DEFAULT]["password"] = "chrome's own"

    def test_the_right_password_unlocks_both_and_crosses_the_bus_encrypted(self):
        res = self.unlock(PASSWORD)
        self.assertEqual((res["result"], res["locked"], res["tried"]),
                         ("unlocked", False, {"default": "unlocked", "login": "unlocked"}))
        self.assertEqual(res["collections"]["default"]["locked"], False)
        self.assertEqual([c for c in self.fake.calls if c != "ReadAlias"],
                         ["OpenSession", "UnlockWithMasterPassword", "UnlockWithMasterPassword"])
        self.assertEqual(self.fake.tried, [DEFAULT, "login"], "the browser's keyring first, then login, in one session")
        for sealed in self.fake.secrets:
            self.assertNotIn(PASSWORD.encode(), sealed)
            self.assertEqual(len(sealed) % 16, 0)
        self.assertEqual(self.results(), ["unlocked"])
        self.assertEqual(set(json.loads((self.state / "attempts.json").read_text())[0]), {"ts", "result"})
        self.assertIn("unlocked", self.log())
        self.assert_never_written(PASSWORD)

    def test_a_wrong_password_is_wrong_once_and_never_logged_nor_its_length(self):
        wrong = "hunter2-is-not-it"
        res = self.unlock(wrong)
        self.assertEqual((res["result"], res["locked"], res["tried"]),
                         ("wrong", True, {"default": "wrong", "login": "wrong"}))
        self.assertTrue(self.fake.locked(DEFAULT) and self.fake.locked("login"))
        self.assertEqual(self.results(), ["wrong"], "one submission counts once, however many it tried")
        self.assert_never_written(wrong)
        # each line without the time it starts with, which can hold the length's digits (17:10, for 17 bytes)
        said = "\n".join(line.split(" ", 2)[2] for line in self.log().splitlines())
        self.assertNotIn(str(len(wrong.encode())), said)

    def test_with_login_unlocked_only_chromes_keyring_is_tried(self):
        self.fake.cols["login"]["locked"] = False
        res = self.unlock(PASSWORD)
        self.assertEqual((res["result"], res["tried"]), ("unlocked", {"default": "unlocked"}))
        self.assertEqual(self.fake.tried, [DEFAULT])

    def test_already_unlocked_never_sends_the_password(self):
        self.fake.set_locked(False)
        res = self.unlock("anything")
        self.assertEqual((res["result"], res["locked"], res["tried"]), ("unlocked", False, {}))
        self.assertEqual(self.fake.tried, [])
        self.assertNotIn("OpenSession", self.fake.calls)
        self.assertFalse((self.state / "attempts.json").exists())

    def test_logins_password_alone_is_partial_and_says_chromes_keyring_is_still_locked(self):
        self.two_passwords()
        res = self.unlock("login's own")
        self.assertEqual((res["result"], res["locked"], res["tried"]),
                         ("partial", True, {"default": "wrong", "login": "unlocked"}))
        self.assertEqual((self.fake.locked(DEFAULT), self.fake.locked("login")), (True, False))
        self.assertEqual(self.results(), ["partial"])
        self.assertIn("partial", keyring.COUNTED, "a keyring a guesser knows the password of makes no guess free")
        res = self.unlock("chrome's own")
        self.assertEqual((res["result"], res["tried"]), ("unlocked", {"default": "unlocked"}))

    def test_chromes_password_alone_is_partial_with_the_browser_unlocked(self):
        self.two_passwords()
        res = self.unlock("chrome's own")
        self.assertEqual((res["result"], res["locked"], res["tried"]),
                         ("partial", False, {"default": "unlocked", "login": "wrong"}))
        self.assertEqual(keyring.status(self.bus.address)["state"], "unlocked")

    def test_one_opened_and_the_other_failing_is_partial_with_why(self):
        self.fake.fail = {"login": "org.freedesktop.DBus.Error.Failed"}
        res = self.unlock(PASSWORD)
        self.assertEqual((res["result"], res["locked"], res["tried"]),
                         ("partial", False, {"default": "unlocked", "login": "error"}))
        self.assertIn("login: UnlockWithMasterPassword", res["error"])
        self.assertEqual(self.results(), ["partial"])

    def test_an_alias_naming_login_tries_it_once(self):
        self.fake.alias = "login"
        res = self.unlock(PASSWORD)
        self.assertEqual((res["result"], res["tried"]), ("unlocked", {"default": "unlocked", "login": "unlocked"}))
        self.assertEqual(self.fake.tried, ["login"])

    def test_an_alias_that_moves_meanwhile_is_an_error_and_counts(self):
        self.fake.on_unlock = lambda: setattr(self.fake, "alias", "login")
        res = self.unlock(PASSWORD)
        self.assertEqual(res["result"], "error")
        self.assertIn('"default" alias moved', res["error"])
        self.assertEqual(self.results(), ["unanswered"])

    def test_chromes_keyring_unreadable_after_is_an_error_not_partial(self):
        self.fake.cols[DEFAULT]["locked"] = False                    # only login is tried
        self.fake.on_unlock = lambda: self.fake.unreadable.add(DEFAULT)
        res = self.unlock(PASSWORD)
        self.assertEqual((res["result"], res["locked"], res["tried"]), ("error", None, {"login": "unlocked"}))
        self.assertIn("Default_5fkeyring could not be read", res["error"])
        self.assertEqual(self.results(), ["unanswered"])

    def test_an_alias_gone_after_is_an_error_with_why(self):
        self.fake.on_unlock = lambda: setattr(self.fake, "alias", None)
        res = self.unlock(PASSWORD)
        self.assertEqual(res["result"], "error")
        self.assertIn('"default" alias names no collection', res["error"])

    def test_no_default_alias_is_an_error_and_nothing_is_sent(self):
        self.fake.alias = None
        res = self.unlock(PASSWORD)
        self.assertEqual((res["result"], res["locked"]), ("error", None))
        self.assertIn('"default" alias names no collection', res["error"])
        self.assertNotIn("OpenSession", self.fake.calls)

    def test_five_wrong_submissions_in_fifteen_minutes_hold_the_sixth_back_unsent(self):
        t0 = 1_800_000_000.0
        for i in range(keyring.LIMIT):
            self.assertEqual(self.unlock("nope", now=t0 + 60 * i)["result"], "wrong")
        self.assertEqual(len(self.fake.secrets), 2 * keyring.LIMIT)
        held = self.unlock(PASSWORD, now=t0 + 300)
        self.assertEqual((held["result"], held["locked"], held["retry_after"]),
                         ("rate_limited", True, keyring.WINDOW - 300 + 1))
        self.assertEqual(len(self.fake.secrets), 2 * keyring.LIMIT, "a held-back try sends nothing")
        self.assertIn("refused", self.log())
        self.assertEqual(self.unlock(PASSWORD, now=t0 + keyring.WINDOW)["result"], "unlocked",
                         "once the oldest is 15 minutes old, a try goes through")

    def test_a_damaged_record_of_attempts_stops_unlocking(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "attempts.json").write_text('{"not": "a list"}')
        with self.assertRaises(ValueError):
            self.unlock(PASSWORD)
        self.assertNotIn("OpenSession", self.fake.calls)

    def test_no_keyring_on_the_bus_is_an_error_and_nothing_is_sent(self):
        empty = PrivateBus()
        try:
            pw = bytearray(PASSWORD.encode())
            res = keyring.unlock(pw, empty.address, self.state, ME)
        finally:
            empty.close()
        self.assertEqual((res["result"], res["locked"]), ("error", None))
        self.assertIn("could not be read", res["error"])
        self.assertEqual(pw, bytearray(len(pw)))

    def test_a_missing_collection_is_never_created(self):
        bus, fake = self.other(cols={}, alias=None)
        self.assertEqual(keyring.status(bus.address)["state"], "unknown")
        got = keyring.send(bytearray(PASSWORD.encode()), bus.address, ME, [path("login")])
        self.assertEqual(got[path("login")][0], "error")
        self.assertIn("does not exist", got[path("login")][1])
        self.assertNotIn("CreateWithMasterPassword", fake.calls)

    def test_send_says_wrong_for_each_collection_that_refuses(self):
        self.two_passwords()
        got = keyring.send(bytearray(b"login's own"), self.bus.address, ME, [path(DEFAULT), path("login")])
        self.assertEqual(got, {path(DEFAULT): ("wrong", None), path("login"): ("taken", None)})
        self.assertEqual((self.fake.locked(DEFAULT), self.fake.locked("login")), (True, False))

    def test_another_program_owning_the_name_never_gets_the_password(self):
        with self.assertRaises(keyring.KeyringError) as e:
            keyring.send(bytearray(PASSWORD.encode()), self.bus.address, keyring.DEFAULTS["daemon"], [path(DEFAULT)])
        self.assertIn("not /usr/bin/gnome-keyring-daemon, so the password was not sent", str(e.exception))
        self.assertNotIn("OpenSession", self.fake.calls)
        res = keyring.unlock(bytearray(PASSWORD.encode()), self.bus.address, self.state, keyring.DEFAULTS["daemon"])
        self.assertEqual((res["result"], res["locked"]), ("error", True))
        self.assertNotIn("OpenSession", self.fake.calls)
        self.assertEqual(self.results(), ["error"], "an attempt whose password never went out does not count")

    def test_the_attempt_is_on_record_before_the_password_goes(self):
        seen = []
        self.fake.on_unlock = lambda: seen.append(json.loads((self.state / "attempts.json").read_text()))
        self.unlock("nope")
        self.assertEqual([a["result"] for a in seen[0]], ["pending"])
        self.assertEqual(self.results(), ["wrong"])

    def test_a_password_sent_with_no_clear_answer_counts_as_wrong_once(self):
        self.fake.fail = dict.fromkeys(self.fake.cols, "org.freedesktop.DBus.Error.NoReply")
        for i in range(keyring.LIMIT):
            res = self.unlock(PASSWORD, now=1_800_000_000.0 + i)
            self.assertEqual((res["result"], res["tried"]), ("error", {"default": "error", "login": "error"}))
        self.assertEqual(self.results(), ["unanswered"] * keyring.LIMIT)
        self.assertEqual(self.unlock(PASSWORD, now=1_800_000_010.0)["result"], "rate_limited")


def pipe_with(data, close=True):
    """A pipe whose writer, a thread of its own, writes `data` and then, with `close`, closes its end: (read end, write
    end). A thread, because a pipe holds as little as 4 KiB once a user has many, and the reader may stop early."""
    r, w = os.pipe()

    def write():
        try:
            os.write(w, data)
        except BrokenPipeError:            # the reader stopped and closed its end: what a refusal does
            pass
        if close:
            os.close(w)
    threading.Thread(target=write, daemon=True).start()
    return r, w


class ReadPasswordTest(unittest.TestCase):
    def read(self, data, timeout=5):
        r, _ = pipe_with(data)
        try:
            return keyring.read_password(r, timeout)
        finally:
            os.close(r)

    def refused(self, data, timeout=5):
        with self.assertRaises(keyring.BadInput) as e:
            self.read(data, timeout)
        msg = str(e.exception)
        self.assertNotIn(data.strip(b"\n\0")[:8].decode(errors="replace") or "\x00", msg)
        self.assertNotIn(str(len(data)), msg.replace(str(keyring.MAX_PASSWORD), ""))
        return msg

    def test_one_line_with_or_without_its_newline(self):
        self.assertEqual(self.read(b"pass word\n"), bytearray(b"pass word"))
        self.assertEqual(self.read(b"pass word"), bytearray(b"pass word"))
        self.assertEqual(self.read(PASSWORD.encode() + b"\n"), bytearray(PASSWORD.encode()))

    def test_the_bound_is_1024_bytes_of_utf8(self):
        self.assertEqual(len(self.read(b"x" * 1024 + b"\n")), 1024)
        self.assertEqual(len(self.read(b"x" * 1024)), 1024)
        self.assertIn("longer than 1024 bytes", self.refused(b"y" * 1025))
        self.assertIn("longer than 1024 bytes", self.refused(b"y" * 1025 + b"\n"))
        self.assertIn("longer than 1024 bytes", self.refused("é".encode() * 513))
        self.refused(b"z" * 100_000)

    def test_another_newline_a_nul_or_nothing_is_refused(self):
        self.assertIn("one line", self.refused(b"secret\n\n"))
        self.assertIn("one line", self.refused(b"sec\nret"))
        self.assertIn("NUL", self.refused(b"sec\0ret\n"))
        self.assertIn("no password", self.refused(b"\n"))
        self.assertIn("no password", self.refused(b""))

    def test_a_carriage_return_at_the_end_is_refused(self):
        """A client ending its lines CRLF: refused, not sent to count as a wrong password."""
        self.assertIn("carriage return", self.refused(b"secret\r\n"))
        self.assertIn("carriage return", self.refused(b"secret\r"))
        self.assertEqual(self.read(b"sec\rret\n"), bytearray(b"sec\rret"))

    def test_input_that_never_ends_is_refused_in_time(self):
        r, w = pipe_with(b"secret\n", close=False)
        try:
            t0 = time.monotonic()
            with self.assertRaises(keyring.BadInput):
                keyring.read_password(r, 0.3)
            self.assertLess(time.monotonic() - t0, 2)
        finally:
            os.close(r)
            os.close(w)

    def test_a_terminal_is_refused(self):
        master, slave = pty.openpty()
        try:
            with self.assertRaises(keyring.BadInput):
                keyring.read_password(slave, 1)
        finally:
            os.close(master)
            os.close(slave)


class CommandTest(KeyringCase):
    """bin/agent-board's keyring commands, run as on the desktop: on the system's Python in isolated mode, with a
    scratch home whose board config turns the keyring unlock on and whose keyring.json names the program FakeKeyring
    runs in as the daemon, and the private bus as the session bus."""

    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp(prefix="home-", dir=SCRATCH))
        self.config(ME)
        self.switch(True)

    def config(self, daemon):
        (self.home / ".config/agent-board").mkdir(parents=True, exist_ok=True)
        (self.home / ".config/agent-board/keyring.json").write_text(json.dumps({"daemon": daemon}))

    def switch(self, on):
        """The board config's keyring_unlock (agentboard/config.py), on or off (the file without it: the default)."""
        (self.home / ".config/agent-board/config.json").write_text(json.dumps({"keyring_unlock": True} if on else {}))

    def argv(self, *args, isolated=True):
        return [lockdown.SYSTEM_PYTHON] + (["-I", "-B"] if isolated else ["-B"]) + [str(ROOT / "bin" / "agent-board"),
                                                                                  *args]

    def env(self):
        """The environment keyring-unlock needs (keyring.ENVIRONMENT, all the phone page gives it) and no more: the
        scratch home and the private bus."""
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "DBUS_SESSION_BUS_ADDRESS": self.bus.address,
               "XDG_RUNTIME_DIR": str(SCRATCH)}
        self.assertEqual(set(env), set(keyring.ENVIRONMENT))
        return env

    def run_board(self, *args, stdin=b"", isolated=True):
        return subprocess.run(self.argv(*args, isolated=isolated), input=stdin, env=self.env(), capture_output=True,
                              timeout=60)

    def unlock(self, stdin, isolated=True):
        r = self.run_board("keyring-unlock", stdin=stdin, isolated=isolated)
        lines = r.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        return r.returncode, json.loads(lines[0])

    def files(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")
                      if p.is_file() and not p.is_relative_to(self.home / ".config"))

    def test_while_turned_off_unlock_refuses_before_it_reads_and_status_reads_nothing(self):
        """Off by default (no keyring_unlock in the board config): keyring-unlock answers "error" saying how to turn it
        on, without reading its stdin or asking the bus; keyring-status exits 3 (unknown), saying the same."""
        self.switch(False)
        code, res = self.unlock(PASSWORD.encode() + b"\n")
        self.assertEqual((code, res["result"], res["error"]), (3, "error", keyring.OFF))
        self.assertEqual(self.fake.calls, [])
        r = self.run_board("keyring-status", "--json")
        self.assertEqual((r.returncode, r.stdout), (3, b""))
        self.assertIn(b"keyring_unlock", r.stderr)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.files(), [".local/state/agent-board/keyring/keyring.log"])    # the refusal, logged

    def test_unlock_answers_one_json_line_with_its_exit_code(self):
        code, res = self.unlock(b"wrong\n")
        self.assertEqual((code, res["result"], res["locked"]), (1, "wrong", True))
        code, res = self.unlock(b"x" * 2000)
        self.assertEqual((code, res["result"], res["locked"]), (3, "error", None))
        self.assertNotIn("2000", res["error"])
        self.fake.cols["login"]["password"] = "login's own"
        code, res = self.unlock(b"login's own\n")
        self.assertEqual((code, res["result"], res["locked"]), (4, "partial", True))
        code, res = self.unlock(PASSWORD.encode() + b"\n")
        self.assertEqual((code, res["result"], res["locked"]), (0, "unlocked", False))

    def test_unlock_hardens_itself_before_it_reads_the_password(self):
        p = subprocess.Popen(self.argv("keyring-unlock"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=self.env())
        try:
            self.assertFalse(lockdown.undumpable(os.getpid()))           # this test's own process is not hardened
            end, hardened = time.monotonic() + 20, False
            while time.monotonic() < end and not hardened:
                hardened = lockdown.undumpable(p.pid)
                time.sleep(0.01)
            self.assertTrue(hardened, "not dumpable, so its stdin is out of other processes' reach, before it reads")
            out, err = p.communicate(PASSWORD.encode() + b"\n", timeout=60)
        finally:
            p.kill()
            p.wait()
        self.assertEqual(json.loads(out)["result"], "unlocked", err)

    def test_the_phone_page_s_server_unlocks_through_it_with_a_socket_for_its_stdin(self):
        """mailweb.run_unlock as the page's server runs it: the command's environment alone (keyring.ENVIRONMENT), a
        Unix socket for its stdin, the password written once it is undumpable; each answer comes back as it gave it."""
        with mock.patch.dict(os.environ, self.env()):
            self.fake.cols["login"]["password"] = "login's own"
            run = lambda typed: mailweb.run_unlock(self.argv("keyring-unlock"), memoryview(bytearray(typed)))  # noqa
            self.assertEqual(run(b"login's own")["result"], "partial")
            self.assertEqual(run(b"nope")["result"], "wrong")
            self.assertEqual(run(PASSWORD.encode())["result"], "unlocked")

    def test_unlock_refuses_to_run_but_isolated_and_never_reads(self):
        code, res = self.unlock(PASSWORD.encode(), isolated=False)
        self.assertEqual((code, res["result"]), (3, "error"))
        self.assertIn("/usr/bin/python3 -I", res["error"])
        self.assertEqual(self.fake.calls, [])

    def test_unlock_sends_nothing_to_a_program_that_is_not_the_daemon(self):
        self.config(keyring.DEFAULTS["daemon"])          # the fake is not gnome-keyring-daemon
        code, res = self.unlock(PASSWORD.encode())
        self.assertEqual((code, res["result"]), (3, "error"))
        self.assertIn("so the password was not sent", res["error"])
        self.assertNotIn("OpenSession", self.fake.calls)

    def test_unlock_records_nothing_but_its_own_files_and_never_the_password(self):
        self.unlock(PASSWORD.encode() + b"\n")
        state = ".local/state/agent-board/keyring/"
        self.assertEqual(self.files(), [state + "attempts.json", state + "keyring.log", state + "unlock.lock"])
        for p in self.home.rglob("*"):
            if p.is_file():
                self.assertNotIn(PASSWORD.encode(), p.read_bytes())

    def test_unlock_held_back_exits_2(self):
        for _ in range(keyring.LIMIT):
            self.unlock(b"nope\n")
        code, res = self.unlock(PASSWORD.encode())
        self.assertEqual((code, res["result"]), (2, "rate_limited"))
        self.assertGreater(res["retry_after"], 0)

    def test_status_reads_and_writes_nothing(self):
        r = self.run_board("keyring-status", "--json")
        self.assertEqual((r.returncode, json.loads(r.stdout)["state"]), (1, "locked"), r.stderr)
        self.assertEqual(self.files(), [])
        self.fake.set_locked(False)
        r = self.run_board("keyring-status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("unlocked", r.stdout.decode())

    def test_status_wait_times_out_non_zero(self):
        r = self.run_board("keyring-status", "--wait", "--timeout", "0", "--json")
        self.assertEqual((r.returncode, json.loads(r.stdout)["state"]), (1, "locked"), r.stderr)
        self.fake.set_locked(False)
        r = self.run_board("keyring-status", "--wait", "--timeout", "30")
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
