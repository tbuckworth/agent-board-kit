"""The desktop's keyrings (gnome-keyring): whether the browser's is locked, and unlocking them with the password the
owner types on the Mac or in the phone's Agent Mail. A desktop that logs in by itself (GDM's automatic login) leaves the
keyrings locked after a reboot until someone unlocks them, and a Chromium-based browser waits at its "Authentication
required" dialog meanwhile. Such a browser keeps its key in the collection the Secret Service's "default" alias names
("the browser's keyring" here), which need not be the login keyring (it is often "Default keyring").

Off unless the owner turns it on (the board config's keyring_unlock, agentboard/config.py; OFF says how): until then
the board reads no state (make) and unlock_from refuses before it reads a password.

status() reads, with busctl, which collection the "default" alias names (ReadAlias, each time) and the Label and
Locked properties of that one and of login, and changes nothing; its state is the default collection's. unlock() tries
one password on each of the two that is locked, through the running daemon's
org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface.UnlockWithMasterPassword, in a
Secret Service session of the dh-ietf1024-sha256-aes128-cbc-pkcs7 kind (Diffie-Hellman, HKDF-SHA256, AES-128-CBC), so
the password crosses the session bus encrypted. Every call carries NO_AUTO_START, so no daemon is ever started for it,
and both go to the unique bus name of org.freedesktop.secrets's owner once its program is found to be gnome-keyring's
(the "daemon" setting), so the password reaches no other owner of the name. In gnome-keyring 46.1 that method only
unlocks: a collection that does not exist is an error (NoSuchObject), a wrong password is
org.gnome.keyring.Error.Denied with nothing kept, and no dialog opens on the desktop. The same source rules out the
other two ways in:

- `gnome-keyring-daemon --unlock` never reaches the running daemon. It starts a daemon of its own, which binds the
  control socket afresh (unlinking the running daemon's when given the same --control-directory) and unlocks, or
  creates, the login keyring inside itself; --start drops --unlock, and --replace stops the running daemon.
- The control socket's unlock (what pam_gnome_keyring uses) creates a missing login keyring, and keeps a wrong
  password as "the login password that failed": the next unlock at the desktop's own dialog then makes that wrong
  password the keyring's new one.

Attempts are limited to LIMIT wrong passwords in WINDOW seconds, from any caller, in attempts.json under the state
folder (when each attempt was made and how it ended, nothing else). An attempt is one password, however many
collections it is tried on, and is on record, as "pending", before the
password goes, and one the daemon may have judged without a clear answer counts as wrong, so a caller killed mid-way
or a failed write never lifts the limit. Each attempt's outcome goes to keyring.log there: never the password, nor
its length. The limit holds only for this module's callers: anything running as the owner can reach the daemon
itself.

The password lives in one bytearray that the caller owns, and every copy made here is a bytearray zeroed once used.
Python cannot zero the rest: the kernel's pipe buffers and the processes that passed the password on (ssh, or the
phone page's server), the copies cryptography and OpenSSL make while encrypting, the session key, which is an
immutable bytes, and pages swapped out meanwhile. Only the encrypted password goes on the bus. The command that reads
the password is hardened, and run on the system's Python in isolated mode, before it does (agentboard/lockdown.py).

D-Bus goes through PyGObject's Gio and the cipher through cryptography: Ubuntu's python3-gi and python3-cryptography.
"""
import json
import os
import re
import select
import time
from pathlib import Path

from . import config, lockdown, sources

SECRETS = "org.freedesktop.secrets"
SERVICE = "/org/freedesktop/secrets"
SERVICE_API = "org.freedesktop.Secret.Service"
COLLECTION_API = "org.freedesktop.Secret.Collection"
COLLECTIONS = "/org/freedesktop/secrets/collection/"     # every collection's object path starts so
LOGIN = COLLECTIONS + "login"
# the collections status reports and unlock tries: the browser's (the alias), then login; the Mac's KeyringNames.watched
# (Keyring.swift) is the same list
WATCHED = ("default", "login")
COLLECTION_PATH = re.compile("^" + re.escape(COLLECTIONS) + "[A-Za-z0-9_]+$")    # all ReadAlias may answer
INTERNAL = "org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface"
DENIED = "org.gnome.keyring.Error.Denied"                 # the daemon's answer to a wrong password
ALGORITHM = "dh-ietf1024-sha256-aes128-cbc-pkcs7"
# RFC 2409's second Oakley group, the daemon's "ietf-ike-grp-modp-1024" (egg/egg-dh.c), with generator 2
MODP1024 = int("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
               "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
               "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE65381FFFFFFFFFFFFFFFF", 16)
BUSCTL = "/usr/bin/busctl"           # by full path, so no stand-in earlier on PATH reads the state
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
STATE = sources.BOARD_STATE / "keyring"
CONFIG = sources.HOME / ".config/agent-board/keyring.json"
DEFAULTS = {"daemon": "/usr/bin/gnome-keyring-daemon"}     # the only program the password is sent to

MAX_PASSWORD = 1024        # bytes of UTF-8 on one line
READ_SECONDS = 30          # for the password to arrive and its input to end
# Five wrong passwords in fifteen minutes: the owner mistypes once or twice, not five times running, and waits at most
# a quarter of an hour if so; a guesser on one of the channels gets 480 tries a day.
LIMIT, WINDOW = 5, 15 * 60
# attempts.json's results that count as wrong: refused everywhere; opened one collection but not another (else a
# collection whose password a guesser knows would make every guess free); sent with no clear answer; still on record
# as going out. Only "unlocked" and attempts that never sent the password do not count.
COUNTED = ("wrong", "partial", "unanswered", "pending")
CALL_SECONDS = 25          # one D-Bus call; busctl's read is given 10
REFRESH = 60               # the board's read of the state
WAIT_POLL = 10             # keyring-status --wait's
# the environment keyring-unlock needs, and all a caller need give it: HOME (its state and settings, sources.HOME), the
# session bus's address (session_bus) and PATH; test_keyring's CommandTest runs it with these alone
ENVIRONMENT = ("PATH", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
STATUS_EXIT = {"unlocked": 0, "locked": 1, "unknown": 3}
UNLOCK_EXIT = {"unlocked": 0, "wrong": 1, "rate_limited": 2, "error": 3, "partial": 4}
OFF = ("the desktop keyring unlock is off: set \"keyring_unlock\": true in ~/.config/agent-board/config.json (README, "
       "\"Unlock the desktop\")")


class KeyringError(Exception):
    """The daemon or the bus could not be asked, or answered something other than yes or a wrong password."""


class Denied(KeyringError):
    """The daemon refused the password (org.gnome.keyring.Error.Denied)."""


class BadInput(ValueError):
    """The input is not a password this module takes. The message never repeats the input or its length."""


def session_bus():
    """The owner's session bus, found as every D-Bus client finds it: DBUS_SESSION_BUS_ADDRESS, else the `bus` socket in
    XDG_RUNTIME_DIR. Raises KeyringError when neither is set."""
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return os.environ["DBUS_SESSION_BUS_ADDRESS"]
    if os.environ.get("XDG_RUNTIME_DIR"):
        return "unix:path=" + os.path.join(os.environ["XDG_RUNTIME_DIR"], "bus")
    raise KeyringError("no session bus: neither DBUS_SESSION_BUS_ADDRESS nor XDG_RUNTIME_DIR is set")


def settings(path=CONFIG):
    """DEFAULTS overridden by the JSON object at `path` (sources.load_config): "daemon", the program the owner of
    org.freedesktop.secrets must be running for the password to be sent to it."""
    return sources.load_config(DEFAULTS, path)


def boot_id(path=BOOT_ID):
    """This boot's id (the kernel's boot_id). Raises OSError when it cannot be read."""
    return Path(path).read_text().strip()


def _read(bus, reply, *call):
    """One busctl call to org.freedesktop.secrets on the bus at the address `bus` (NO_AUTO_START, 10 s): the values of
    its reply, which must be of the D-Bus type `reply`, from busctl's JSON. Every argument comes after "--", so none
    is read as one of busctl's options. Raises KeyringError with busctl's words, or when it prints anything else."""
    code, out, err = sources.run([BUSCTL, "--address=" + bus, "--auto-start=no", "--timeout=10", "--json=short", "--",
                                  "call", SECRETS, *call], timeout=15, input="")
    if code != 0:
        raise KeyringError((err or out).strip()[-300:] or "busctl exited %d" % code)
    try:
        got = json.loads(out)
        if got["type"] != reply:
            raise KeyError("type")
        return got["data"]
    except (ValueError, KeyError, TypeError):
        raise KeyringError("busctl printed something unexpected: %s" % out.strip()[:200]) from None


def _collection(bus, path):
    """{"path", "label", "locked", "error"} of the collection at `path`, from its Label and Locked properties (GetAll);
    label and locked None, with the error, when they cannot be read."""
    try:
        props, = _read(bus, "a{sv}", path, "org.freedesktop.DBus.Properties", "GetAll", "s", COLLECTION_API)
        label, locked = props["Label"]["data"], props["Locked"]["data"]
        if not (isinstance(label, str) and isinstance(locked, bool)):
            raise KeyringError("its Label or Locked is not what the Secret Service says it is")
    except (KeyringError, ValueError, KeyError, TypeError) as e:
        return {"path": path, "label": None, "locked": None, "error": "%s could not be read: %s" % (path, e)}
    return {"path": path, "label": label, "locked": locked, "error": None}


def status(bus):
    """{"state": "locked" | "unlocked" | "unknown", "locked": True | False | None, "error": why the state is unknown
    or None, "boot_id": this boot's id, "collections": {"default": ..., "login": ...}}, from the bus at the address
    `bus`. Each collection is {"path", "label", "locked", "error"} (_collection); "default" is the one the "default"
    alias names now (ReadAlias), and gives the state, locked and error. An alias that names nothing, or cannot be read,
    is that collection's error, with path None, and so is an answer that is not a collection's path (COLLECTION_PATH).
    Reads with busctl (NO_AUTO_START) and writes and logs nothing. Raises
    OSError only when the boot id cannot be read."""
    try:
        alias, = _read(bus, "o", SERVICE, SERVICE_API, "ReadAlias", "s", "default")
        if alias == "/":
            raise KeyringError('the "default" alias names no collection')
        if not COLLECTION_PATH.match(alias):
            raise KeyringError("the \"default\" alias names %r, which is no collection's path" % alias[:120])
        default = _collection(bus, alias)
    except (KeyringError, ValueError) as e:
        why = "the browser's keyring (the \"default\" alias) could not be read: %s" % e
        default = {"path": None, "label": None, "locked": None, "error": why}
    login = dict(default) if default["path"] == LOGIN else _collection(bus, LOGIN)
    locked = default["locked"]
    return {"state": "unknown" if locked is None else "locked" if locked else "unlocked", "locked": locked,
            "error": default["error"], "boot_id": boot_id(), "collections": {"default": default, "login": login}}


def watch(sync):
    """status() of the session bus as a sources.Periodic read every REFRESH seconds, for the board's snapshot: in the
    background unless `sync`."""
    return sources.Periodic(lambda: status(session_bus()), REFRESH, None, sync)


def make(sync):
    """watch(sync) when the board config turns the keyring unlock on (config.keyring_unlock), else None: no state is
    read, and the snapshot carries no "keyring" (the Mac shows no lock mark)."""
    return watch(sync) if config.keyring_unlock() else None


def wait(bus, timeout, poll):
    """status() every `poll` seconds until the browser's keyring reads as unlocked or `timeout` seconds have passed: the
    last reading."""
    deadline = time.monotonic() + timeout
    st = status(bus)
    while st["state"] != "unlocked" and time.monotonic() < deadline:
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
        st = status(bus)
    return st


def password_end(buf, start, stop):
    """Where the password in buf[start:stop] (a bytearray, as UTF-8) ends: at `stop`, or before its one trailing
    newline. It is keyring-unlock's input rule, for every caller that checks a password before it is sent: one line of
    1 to MAX_PASSWORD bytes with no NUL, which may end in one newline, and does not end in a carriage return (what a
    client ending its lines CRLF would send; no password typed in a field ends so), which would only count as a wrong
    password. Raises BadInput, whose message never repeats the input or its length, for an empty password, a longer
    one, one holding another newline or a NUL, or one ending in a carriage return."""
    end = stop - 1 if stop > start and buf[stop - 1] == 0x0A else stop
    if end - start > MAX_PASSWORD:
        raise BadInput("the password is longer than %d bytes" % MAX_PASSWORD)
    if end == start:
        raise BadInput("no password was given")
    if buf.find(b"\n", start, end) >= 0 or buf.find(b"\0", start, end) >= 0:
        raise BadInput("the password must be one line with no NUL in it")
    if buf[end - 1] == 0x0D:
        raise BadInput("the password must not end in a carriage return")
    return end


def read_password(fd, timeout):
    """The password on the descriptor `fd`: one line of 1 to MAX_PASSWORD bytes, with its one trailing newline taken
    off (password_end), as a new bytearray the caller zeroes. It reads until the input ends, MAX_PASSWORD + 2 bytes
    have come or `timeout` seconds have passed, into a buffer it zeroes. Raises BadInput for a terminal, for input that
    does not end in time, and for input password_end refuses."""
    if os.isatty(fd):
        raise BadInput("the password must be piped in: a terminal would show it")
    buf = bytearray(MAX_PASSWORD + 2)
    view = memoryview(buf)
    try:
        got, deadline = 0, time.monotonic() + timeout
        while got < len(buf):
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise BadInput("the password's input did not end within %d s" % timeout)
            n = os.readv(fd, [view[got:]])
            if n == 0:
                break
            got += n
        return bytearray(view[:password_end(buf, 0, got)])
    finally:
        view[:] = bytes(len(buf))
        view.release()


def _zero(b):
    b[:] = bytes(len(b))


def _call(conn, dest, path, interface, method, args, reply):
    """One method call to the bus name `dest` over `conn`, with NO_AUTO_START: its reply (of type `reply`, a GVariant
    type string, or None for none). Raises Denied for org.gnome.keyring.Error.Denied, KeyringError for any other
    error."""
    from gi.repository import Gio, GLib
    try:
        return conn.call_sync(dest, path, interface, method, args, GLib.VariantType(reply) if reply else None,
                              Gio.DBusCallFlags.NO_AUTO_START, CALL_SECONDS * 1000, None)
    except GLib.Error as e:
        if Gio.DBusError.get_remote_error(e) == DENIED:
            raise Denied("the password was refused") from None
        raise KeyringError("%s: %s" % (method, e.message)) from None


def _daemon(conn, daemon):
    """The unique bus name of org.freedesktop.secrets's owner, once its process is found to run the program `daemon`
    (a binary replaced since it started counts). Raises KeyringError for no owner, or another program."""
    from gi.repository import GLib
    bus = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
    owner, = _call(conn, *bus, "GetNameOwner", GLib.Variant("(s)", (SECRETS,)), "(s)").unpack()
    pid, = _call(conn, *bus, "GetConnectionUnixProcessID", GLib.Variant("(s)", (owner,)), "(u)").unpack()
    try:
        exe = os.readlink("/proc/%d/exe" % pid)
    except OSError as e:
        raise KeyringError("could not tell which program owns %s: %s" % (SECRETS, e)) from None
    if exe.removesuffix(" (deleted)") != daemon:
        raise KeyringError("%s is owned by %s, not %s, so the password was not sent" % (SECRETS, exe, daemon))
    return owner


def _seal(password, key):
    """(iv, the password encrypted with AES-128-CBC under `key`): PKCS #7 by hand, so the padded password is a
    bytearray, zeroed once encrypted."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    iv = os.urandom(16)
    pad = 16 - len(password) % 16
    block = bytearray(len(password) + pad)
    try:
        block[:len(password)] = password
        block[len(password):] = bytes([pad]) * pad
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return iv, enc.update(block) + enc.finalize()
    finally:
        _zero(block)


def send(password, bus, daemon, paths):
    """Try `password` (a bytearray, left as it is) on each collection in `paths`, in order, on the bus at the address
    `bus`, when the owner of org.freedesktop.secrets runs the program `daemon`: OpenSession with ALGORITHM, then
    UnlockWithMasterPassword for each, with the password encrypted under the session's key, all to that owner's unique
    name, on one connection, which is closed after. Returns {path: (outcome, why)}: ("taken", None) when the daemon
    took the password (it also says yes for a collection unlocked already), ("wrong", None) when it refused it, and
    ("error", the daemon's words) otherwise, a collection that does not exist included. Raises KeyringError, before the
    password goes out, when the daemon cannot be asked (no bus, no daemon or another program on it, no session)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import dh
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from gi.repository import Gio, GLib
    try:
        conn = Gio.DBusConnection.new_for_address_sync(
            bus, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
    except GLib.Error as e:
        raise KeyringError("could not reach the session bus: %s" % e.message) from None
    try:
        owner = _daemon(conn, daemon)
        group = dh.DHParameterNumbers(MODP1024, 2)
        mine = group.parameters().generate_private_key()
        size = (MODP1024.bit_length() + 7) // 8
        ours = mine.public_key().public_numbers().y.to_bytes(size, "big")
        output, session = _call(conn, owner, SERVICE, SERVICE_API, "OpenSession",
                                GLib.Variant("(sv)", (ALGORITHM, GLib.Variant("ay", ours))), "(vo)").unpack()
        try:
            theirs = dh.DHPublicNumbers(int.from_bytes(bytes(output), "big"), group).public_key()
            # the daemon pads the shared secret to the prime's length before HKDF (egg_dh_gen_secret)
            shared = mine.exchange(theirs).rjust(size, b"\0")
        except (TypeError, ValueError) as e:
            raise KeyringError("the daemon's half of the session key is not usable: %s" % e) from None
        key = HKDF(algorithm=hashes.SHA256(), length=16, salt=None, info=None).derive(shared)
        out = {}
        for path in paths:
            iv, sealed = _seal(password, key)
            try:
                _call(conn, owner, SERVICE, INTERNAL, "UnlockWithMasterPassword",
                      GLib.Variant("(o(oayays))", (path, (session, iv, sealed, "text/plain"))), None)
                out[path] = ("taken", None)
            except Denied:
                out[path] = ("wrong", None)
            except KeyringError as e:
                out[path] = ("error", str(e))
        return out
    finally:
        conn.close_sync(None)                          # the daemon ends the session with the connection


def caller():
    """Who is asking, for the log: the address an SSH session came from, else the parent process's name."""
    ssh = os.environ.get("SSH_CONNECTION", "").split()
    if ssh:
        return "ssh from " + ssh[0]
    try:
        return "local, under " + Path("/proc/%d/comm" % os.getppid()).read_text().strip()
    except OSError:
        return "local, parent %d" % os.getppid()


def log(state, msg):
    sources.append_log(Path(state) / "keyring.log", msg)


def refused(state, why):
    """The result "error" for an attempt refused before its password was read or sent, logged in `state` with `why`,
    which never holds the input or its length."""
    log(state, "unlock (%s): refused: %s" % (caller(), why))
    return {"result": "error", "locked": None, "error": why}


def unlock(password, bus, state, daemon, now=None):
    """Try `password` (a bytearray, zeroed before this returns, whatever happens) on each WATCHED collection that is
    locked, the browser's first, one attempt at a time under a lock in the folder `state`, sending it (send) only to
    the program `daemon`. It is sent only when the browser's collection can be found and fewer than LIMIT attempts in the last
    WINDOW seconds count as wrong (COUNTED, in attempts.json in `state`, which holds this attempt once, as "pending"
    before the password goes and its outcome after). Returns {"result", "locked": the browser's keyring's state read after
    (None when it could not be read), "collections": status()'s, read after}, with "tried" ({name: "unlocked" |
    "wrong" | "error"} for each collection the password went to, matched by path after), "error" (why) when there is
    one, and "retry_after" (seconds) when rate-limited. The result is "unlocked" when everything tried opened and
    the browser's keyring reads as unlocked (nothing locked: nothing is sent), "partial" when the password opened some but
    not all, "wrong" when every one refused it, "rate_limited", or "error", which is also the result when the alias
    moves meanwhile or the browser's keyring cannot be read afterwards.
    Appends the outcome to keyring.log in `state`. `now` (the attempt's time) is read once the lock is held unless
    given. Raises OSError or ValueError when attempts.json cannot be read or written, before the password is sent."""
    state = Path(state)
    try:
        with sources.file_lock(state / "unlock.lock"):
            return _attempt(password, bus, state, daemon, time.time() if now is None else now)
    finally:
        _zero(password)


def _attempts(path):
    """The attempts in attempts.json at `path` ([] when there is none): [{"ts", "result"}]. Raises ValueError when the
    file holds anything else, so a damaged record stops unlocking rather than lifting the limit."""
    got = sources.read_json(path, [])
    if not (isinstance(got, list) and all(isinstance(a, dict) and isinstance(a.get("ts"), (int, float))
                                          and isinstance(a.get("result"), str) for a in got)):
        raise ValueError("%s is not a list of attempts" % path)
    return got


def _outcome(tried, default_unlocked):
    """The result of one password from `tried` ({name: "unlocked" | "wrong" | "error"}, not empty)."""
    got = set(tried.values())
    if got == {"unlocked"} and default_unlocked:
        return "unlocked"
    if "unlocked" in got:
        return "partial"
    return "wrong" if got == {"wrong"} else "error"


def _attempt(password, bus, state, daemon, now):
    before = status(bus)
    if before["state"] == "unknown":
        log(state, "unlock (%s): not sent: %s" % (caller(), before["error"]))
        return {"result": "error", "locked": None, "error": before["error"], "collections": before["collections"]}
    names = {}                                          # each locked collection's path: the WATCHED names it has
    for name in WATCHED:
        c = before["collections"][name]
        if c["locked"]:
            names.setdefault(c["path"], []).append(name)
    if not names:
        log(state, "unlock (%s): already unlocked, so the password was not sent" % caller())
        return {"result": "unlocked", "locked": False, "collections": before["collections"], "tried": {}}
    path = state / "attempts.json"
    kept = [a for a in _attempts(path) if now - a["ts"] < WINDOW]
    wrong = [a["ts"] for a in kept if a["result"] in COUNTED]
    if len(wrong) >= LIMIT:
        wait_s = int(min(wrong) + WINDOW - now) + 1
        log(state, "unlock (%s): refused: %d wrong passwords in the last %d minutes; the next try in %d s" % (
            caller(), len(wrong), WINDOW // 60, wait_s))
        return {"result": "rate_limited", "locked": before["locked"], "retry_after": wait_s,
                "collections": before["collections"]}
    this = {"ts": now, "result": "pending"}
    sources.write_json(path, kept + [this], mode=0o600)    # on record before the password goes, or it does not go
    whys = []
    try:
        sent = send(password, bus, daemon, list(names))
    except KeyringError as e:
        sent = {}
        whys.append(str(e))
    after = status(bus)
    now_at = after["collections"]["default"]["path"]
    moved = bool(sent) and now_at is not None and now_at != before["collections"]["default"]["path"]
    if moved:
        whys.append('the "default" alias moved during the attempt')
    elif sent and after["locked"] is None:
        whys.append(after["error"])
    seen = {c["path"]: c for c in after["collections"].values() if c["path"]}     # each tried one, read after, by path
    tried = {}
    for at, (outcome, why) in sent.items():
        now_locked = seen.get(at, {}).get("locked")
        opened = now_locked is False
        if outcome == "taken" and not opened:
            why = "the daemon took the password, but it reads as %s" % ("locked" if now_locked else "unknown")
        for name in names[at]:
            tried[name] = "unlocked" if outcome == "taken" and opened else "wrong" if outcome == "wrong" else "error"
        if why:
            whys.append("%s: %s" % (" and ".join(names[at]), why))
    readable = tried and not moved and after["locked"] is not None
    result = _outcome(tried, after["locked"] is False) if readable else "error"
    res = {"result": result, "locked": after["locked"], "collections": after["collections"], "tried": tried}
    if whys:
        res["error"] = "; ".join(whys)
    this["result"] = "unanswered" if result == "error" and sent else result
    sources.write_json(path, kept + [this], mode=0o600)
    counted = len(wrong) + (this["result"] in COUNTED)
    log(state, "unlock (%s): %s (%s)%s; %d counted as wrong in the last %d minutes" % (
        caller(), result, ", ".join("%s %s" % kv for kv in tried.items()) or "nothing tried",
        ": " + res["error"] if whys else "", counted, WINDOW // 60))
    return res


def unlock_from(fd, state):
    """What `agent-board keyring-unlock` does: make the process ready (lockdown.ready: hardened, the system's Python in
    isolated mode), read the password from the descriptor `fd` (read_password), then unlock() on the session bus
    (session_bus) with the "daemon" setting (settings), with `state` as its folder: unlock()'s result. A process not
    ready, the unlock turned off (config.keyring_unlock: OFF), and refused input, are the result "error" before
    anything is read or sent, logged without the input or its length. Anything else that fails is the result "error"
    too, with its reason, logged; this never raises."""
    not_ready = lockdown.ready()
    if not_ready:
        return refused(state, not_ready)
    try:
        on = config.keyring_unlock()
    except Exception as e:       # noqa: BLE001 -- a damaged board config is a refusal, never a crash
        return refused(state, "the board config could not be read: " + sources.error_text(e))
    if not on:
        return refused(state, OFF)
    try:
        password = read_password(fd, READ_SECONDS)
    except BadInput as e:
        return refused(state, str(e))
    except Exception as e:       # every failure answers "error": exit 1, Python's for a crash, means a wrong password
        return refused(state, "the password was not read: " + sources.error_text(e))
    try:
        return unlock(password, session_bus(), state, settings()["daemon"])
    except Exception as e:       # as above: the bus address, the attempts file, a missing python3-gi
        log(state, "unlock (%s): error: %s" % (caller(), sources.error_text(e)))
        return {"result": "error", "locked": None, "error": sources.error_text(e)}
    finally:
        _zero(password)
