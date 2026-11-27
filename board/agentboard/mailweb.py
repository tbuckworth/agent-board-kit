"""The Mail tab on the owner's phone: a web page served from the desktop to the owner's tailnet alone (README, "Mail on
the phone"). `agent-board mail-web` listens only on a Unix socket in its state folder (mailwebstate.SOCKET, mode
0600); `tailscale serve` publishes it to the tailnet over HTTPS (`unix:` target), so the traffic is encrypted from the
phone to the desktop (WireGuard, then TLS), and tells the server who is asking: Tailscale-User-Login, the client's
tailnet address in X-Forwarded-For, the address it asked for in X-Forwarded-Host. tailscaled sets each of these
itself and clears any the client sent.

A connection is taken only when the process at its other end (SO_PEERCRED, at accept, before a byte is read) runs as
a uid in the settings' `proxy_uids` (tailscaled's: root), and only MAX_CONNECTIONS at once; a request's line and
headers must arrive within HEADER_SECONDS, and its body within a deadline scaled to its length (body_seconds). Then
every request must pass, in turn (Guard.device), and one device may have PER_DEVICE requests going at once:
1. the page is not reachable from the public through Funnel (funnel_exposure: a funnelled port that is the page's, or
   any funnelled target on the page's port or on its socket, in the background config or a foreground session;
   checked every FUNNEL_CHECK seconds; a check that fails, or a status it cannot read, answers nothing), and the
   request did not come through Funnel;
2. it came over HTTPS for exactly the desktop's tailnet name at the page's port (X-Forwarded-Proto, -Host);
3. its login is one the page answers (the settings' `logins`, by default the owner's addresses), and `tailscale whois`
   on its address names a device in the settings' `devices` (the owner's phone) logged in as that same login.
Then the mail needs a signed-in session: a passkey (WebAuthn, webauthn.py) with the user verified, which gives an
HttpOnly, Secure, SameSite=Strict cookie lasting `session_hours`. A passkey is enrolled only with the one-time code the
Mac shows (mailwebstate.Store.mint_code), under a user handle of its own, and the owner is told each time by a
security alert. Each action in STEP_UP (a reply, a Researcher switch, a message to the orchestrator, unlocking the
desktop's keyring) needs the passkey again (a step-up), over a challenge bound to that action, its exact request and
the session's passkey: a reply instructs an agent, Researcher changes how an agent runs, the orchestrator can act as
the owner, and an unlock carries the owner's desktop login password. Fast mode (mail_fast) carries no words to an
agent, so the session is enough for it, as for marks. Failed signature checks on an enrolled passkey, and enrolment tries that fail (a wrong code, a
passkey that does not check out), are each limited to FAILURES and ENROL_TRIES in FAILURE_WINDOW seconds. Each purpose
keeps its own challenges, so no call can push out another's.

The page's API runs the board's own validated mail_* and draft_* actions and its orchestrate action (actions.py), so the
phone can do what the Mac's tab and its Orchestrator button do and nothing else; an attachment comes only to the page's
own script (it asks with X-Agent-Board). With the board config's keyring_unlock on, it also unlocks the desktop's login
keyring as the Mac's lock mark does, with the core's own command (UNLOCK_COMMAND, keyring.py), and says the keyring's
state with the session's status (Guard.keyring). An unlock's request is a nonce the page makes for that submission:
what the step-up approves is that nonce, and the password follows the request's line, so no digest of the password is
made, and it goes only to the command's stdin (run_unlock), from the request's own bytes, which are zeroed once it is
answered. Every answer, refusals and errors included, carries the security headers (SECURITY_HEADERS): nothing is
cached or framed.

It guards against the network: the internet, other devices, a session used elsewhere. It cannot guard against a
program running on the desktop as the owner or as root, which can read the mail and change the page itself (README).
"""
import base64
import binascii
import collections
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

from . import actions, config, keyring, lockdown, mailbox, mailwebstate, sources, webauthn

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web" / "mail"
MANIFEST = WEB / "manifest.webmanifest"
ICON_RE = re.compile(r"/icons/[a-z0-9-]+\.png")          # an icon the manifest names: a PNG in web/mail/icons/


def manifest_icons(manifest=MANIFEST):
    """The icons the app's manifest names, as PAGES entries ({path: (file, type)}), so the manifest is their one list.
    Raises ValueError for an icon whose path is not ICON_RE's, which could reach outside web/mail/icons/."""
    pages = {}
    for icon in json.loads(manifest.read_text())["icons"]:
        if not ICON_RE.fullmatch(icon["src"]):
            raise ValueError("%s: icon %r is not a PNG in web/mail/icons/" % (manifest, icon["src"]))
        pages[icon["src"]] = (WEB / icon["src"].lstrip("/"), "image/png")
    return pages


# the page's own files, served as they are; widgets.js is the Mac tab's widget renderer, served from its one copy (the
# Mac app's SwiftPM resource), so both draw options and plots with the same code. The manifest and its icons make the
# page an installable app (README, "Mail on the phone": Chrome installs it from its menu with no service worker).
PAGES = {"/": (WEB / "index.html", "text/html; charset=utf-8"),
         "/enrol": (WEB / "index.html", "text/html; charset=utf-8"),
         "/app.js": (WEB / "app.js", "text/javascript; charset=utf-8"),
         "/snooze.js": (WEB / "snooze.js", "text/javascript; charset=utf-8"),
         "/replytext.js": (WEB / "replytext.js", "text/javascript; charset=utf-8"),
         "/app.css": (WEB / "app.css", "text/css; charset=utf-8"),
         "/widgets.js": (ROOT / "mac" / "Sources" / "AgentBoard" / "widgets.js", "text/javascript; charset=utf-8"),
         "/passkeys.js": (ROOT / "web" / "passkeys.js", "text/javascript; charset=utf-8"),    # the voice page's too
         "/manifest.webmanifest": (MANIFEST, "application/manifest+json"),
         **manifest_icons()}
# with a session alone; the STEP_UP actions need the passkey again too, and mail_fast does not (it carries no words)
MAIL_ACTIONS = ("mail_list", "mail_show", "mail_mark", "mail_fast")
# the drafts (maildrafts.py), with a session alone, as marks: a draft reaches no agent until a reply sends it, which
# needs the passkey. Each change says it came from the phone (DRAFT_DEVICE), whatever the request says.
DRAFT_ACTIONS = ("draft_list", "draft_get", "draft_put", "draft_discard", "draft_restore", "draft_file_add")
DRAFT_DEVICE = "phone"
ORCH_SOURCE = "phone"                # where the page's messages to the orchestrator say they come from (a key of
                                     # actions.ORCH_SOURCES), whatever the request says
PART_RE = re.compile(r"/part/([^/]+)/([0-9]{1,9})")        # the whole path; mail_part_action checks the key and index
# bytes of one API request: the largest reply (its files as base64, and the rest), with a passkey's approval of it
MAX_BODY = mailbox.MAX_REPLY_REQUEST + 65536
UNLOCK = "keyring_unlock"            # the API action that unlocks the desktop's keyring: the password follows its line
# keyring-unlock as every caller runs it (README, "Unlock the desktop"): the system's Python in isolated mode, on this
# checkout's agent-board
UNLOCK_COMMAND = [*lockdown.INTERPRETER, str(ROOT / "bin" / "agent-board"), "keyring-unlock"]
UNLOCK_SECONDS = 180                 # an unlock's answer, past the most its bounded reads and D-Bus calls take
HARDEN_SECONDS = 10                  # for keyring-unlock to make itself undumpable, the first thing it does
API_HEADER = "X-Agent-Board"         # every API call carries it: another site's page cannot without asking first
COOKIE = "__Host-mailweb"            # __Host-: the browser keeps it to this host, Secure, at path /
COOKIE_ATTRIBUTES = "Path=/; Secure; HttpOnly; SameSite=Strict"      # setting it and clearing it, alike
CHALLENGE_SECONDS = 120              # how long a passkey challenge may wait to be used
CHALLENGES = {"enrol": 4, "signin": 8, "stepup": 8}  # each purpose's challenges waiting at once; the oldest goes first
FAILURES, ENROL_TRIES, FAILURE_WINDOW = 5, 5, 900    # failed signatures, failed enrolment tries, in a window (seconds)
FUNNEL_CHECK = 30                    # seconds between checks that the page is not reachable from the public
WHOIS_SECONDS = 60                   # how long a tailnet address's device is remembered
TIMEOUT = 30                         # seconds a connection may wait for any one read before it is dropped
HEADER_SECONDS = 10                  # seconds a request's line and headers may take to arrive, all together
BODY_SECONDS, BODY_RATE, BODY_MAX_SECONDS = 10, 65536, 600   # a body's deadline: 10 s and 1 s per 64 KiB, at most 600
MAX_CONNECTIONS = 16                 # connections served at once; one more waits SLOT_SECONDS (in its own thread)
SLOT_SECONDS = 1                     # (a closed connection's slot is free only once its thread has seen it close)
PER_DEVICE = 8                       # requests from one device at once, once their headers are read
RP_NAME = "Agent Mail"               # the app's name, shown when a passkey is made; only the host (rp id) is checked
# The enrolment's alert kind, in the mailroom's fixed set of security kinds: with Gmail on, the mailroom sends it
# through Gmail even when other mail to the owner is kept for the Mail tab; with Gmail off, it keeps it for the Mail
# tab and records it in security-alerts.jsonl, which the board passes to the Mac app as a notification (README,
# "Security alerts on the Mac").
ENROL_ALERT = "phone-enrolment"
SECURITY_HEADERS = (("Cache-Control", "no-store"), ("X-Frame-Options", "DENY"), ("X-Content-Type-Options", "nosniff"),
                    ("Referrer-Policy", "no-referrer"),
                    # The page's own policy. A thread is shown in a sandboxed iframe (srcdoc), which inherits this
                    # policy on top of its own, so its inline styles and data: images must be allowed here too. The
                    # app's manifest is its own file (manifest-src), and its icons are images of its own (img-src).
                    ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self' "
                     "'unsafe-inline'; img-src 'self' data:; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
                     "form-action 'none'; frame-ancestors 'none'"),
                    # A browser grants the microphone to the whole origin, so once the voice page (voiceweb.py) has
                    # it, this page would too: here it is refused outright.
                    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"))


def body_seconds(size):
    """How long a body of `size` bytes may take to arrive: BODY_SECONDS and a second per BODY_RATE bytes, capped."""
    return min(BODY_SECONDS + size // BODY_RATE, BODY_MAX_SECONDS)


def log(message):
    """One line on stderr (the unit's journal) for what happens before a request is read."""
    sys.stderr.write("mail-web: %s\n" % message)
    sys.stderr.flush()


def _same_socket(target, socket_path):
    """Whether Unix-socket path `target`, as tailscaled resolves it (a relative path against its working folder, /),
    is the page's socket at `socket_path`: the same file (device and inode) when both are there, or the same path
    once links and dots are resolved (before the page has made its socket)."""
    target = os.path.join("/", target)
    try:
        a, b = os.stat(target), os.stat(socket_path)
        if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
            return True
    except (FileNotFoundError, NotADirectoryError):
        pass                    # one of them is not there (yet): the paths decide
    return os.path.realpath(target) == os.path.realpath(socket_path)


def _reaches_page(target, port, socket_path):
    """Whether a funnelled proxy or forward `target` ("http://HOST:PORT/x", "HOST:PORT", "unix:PATH") reaches the page:
    any host on its port `port` (a hairpin through serve's own listener, or an address in any spelling), or a Unix
    socket that is its `socket_path` (_same_socket). Raises ValueError for a target whose port cannot be read."""
    if target.startswith("unix:"):
        return _same_socket(target[len("unix:"):], socket_path)
    return urllib.parse.urlsplit(target if "://" in target else "tcp://" + target).port == port


def funnel_exposure(port, socket_path, run):
    """Why the page (tailnet port `port`, Unix socket `socket_path`) is reachable from the public through Funnel, or
    None: in `tailscale serve status --json`'s config and in each foreground session's (Foreground), a funnelled host
    port (AllowFunnel) that is `port`, or whose web handlers proxy, or whose TCP forward goes, to the page. Raises
    sources.CommandError when tailscale fails; a status it cannot read raises too (AttributeError, ValueError...)."""
    status = sources.call("tailscale", ["serve", "status", "--json"], run)
    for cfg in [status] + list((status.get("Foreground") or {}).values()):
        for hostport, on in (cfg.get("AllowFunnel") or {}).items():
            public = hostport.rsplit(":", 1)[-1]
            if not on:
                continue
            if public == str(port):
                return "its port %s is open to the public through Funnel" % public
            for path, h in (((cfg.get("Web") or {}).get(hostport) or {}).get("Handlers") or {}).items():
                if h.get("Proxy") and _reaches_page(h["Proxy"], port, socket_path):
                    return "public port %s's %s proxies to the page" % (public, path)
            forward = ((cfg.get("TCP") or {}).get(public) or {}).get("TCPForward")
            if forward and _reaches_page(forward, port, socket_path):
                return "public port %s forwards to the page" % public
    return None


def whois(address, run):
    """(stable node id, its tailnet name, its login) of the device at tailnet `address` (`tailscale whois --json`).
    Raises sources.CommandError when tailscale knows no such device."""
    got = sources.call("tailscale", ["whois", "--json", address], run)
    node, user = got.get("Node") or {}, got.get("UserProfile") or {}
    return (node.get("StableID") or "", (node.get("Name") or "").rstrip(".").lower(),
            (user.get("LoginName") or "").lower())


def for_phone(document):
    """A thread's document as the phone shows it: links open in a new tab, out of the sandboxed frame, and attachment
    links (mailbox-part:KEY/INDEX) are made inert there, since a navigation from the frame carries no session; the
    page lists each message's attachments and fetches them itself. Raises ValueError for a document with no <head>,
    whose links would stay in the frame."""
    if "<head>" not in document:
        raise ValueError("the thread's document has no <head>, so its links cannot be made to leave the frame")
    document = document.replace('href="mailbox-part:', 'data-part="')
    return document.replace("<head>", '<head><base target="_blank">', 1)


def enrol_user():
    """The passkey's user as the phone's passkey manager shows it: the owner's address (else "owner") and first name
    (else "Owner"), from the mailroom's config."""
    return sources.owner() or "owner", config.owner_name(default="Owner")


class UnlockFailed(Exception):
    """keyring-unlock could not be run, or did not answer as it does (README, "Unlock the desktop"). The message never
    holds the password."""


def run_unlock(command, password):
    """keyring-unlock's answer to `password` (a view of the password's bytes, as the command takes them on its stdin):
    `command` (UNLOCK_COMMAND, or a test's stand-in) is run with only the environment the core needs
    (keyring.ENVIRONMENT), so nothing such as LD_PRELOAD passes to it, and with the password on its stdin and nowhere
    else, neither in its arguments nor its environment nor a file. Its stdin is one end of a Unix socket pair, not a
    pipe: /proc/<pid>/fd/0 cannot be opened on a socket (ENXIO), so no other process of the owner's can take hold of it
    while the command starts. The password is written only once the command has made itself undumpable
    (lockdown.undumpable), and straight from `password` (no copy is made here); it is not written at all to a command
    that ends first, whose answer then says why. Our end is then closed, which ends the command's input. The command is
    stopped when it has not hardened within HARDEN_SECONDS, or not answered within UNLOCK_SECONDS. Returns its one JSON
    line, parsed: an object whose "result" is one of keyring.UNLOCK_EXIT, which it exited with. Raises UnlockFailed when
    it cannot start, does not harden or answer in time, or answers anything else."""
    env = {k: os.environ[k] for k in keyring.ENVIRONMENT if k in os.environ}
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            p = subprocess.Popen(command, stdin=theirs, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        except OSError as e:
            raise UnlockFailed("keyring-unlock could not start: %s" % e) from None
        finally:
            theirs.close()                     # the command's now, alone
        deadline = time.monotonic() + HARDEN_SECONDS
        while p.poll() is None and not lockdown.undumpable(p.pid):
            if time.monotonic() > deadline:
                p.kill()
                p.communicate()
                raise UnlockFailed("keyring-unlock did not make itself undumpable within %d s, so the password was "
                                   "not sent" % HARDEN_SECONDS)
            time.sleep(0.01)
        if p.returncode is None:
            try:
                ours.sendall(password)
            except (BrokenPipeError, ConnectionResetError):
                pass                           # it ended meanwhile: its answer says why
    finally:
        ours.close()
    try:
        out, err = p.communicate(timeout=UNLOCK_SECONDS)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise UnlockFailed("keyring-unlock gave no answer within %d s, so it was stopped; the desktop may count it as "
                           "a wrong password" % UNLOCK_SECONDS) from None
    said = out.decode(errors="replace").strip()
    try:
        answer = None if "\n" in said else json.loads(said)
    except ValueError:
        answer = None
    if not (isinstance(answer, dict) and answer.get("result") in keyring.UNLOCK_EXIT):
        why = (err.decode(errors="replace").strip() or said)[-300:] or "it printed nothing"
        raise UnlockFailed("keyring-unlock exited %d without its answer: %s" % (p.returncode, why))
    if p.returncode != keyring.UNLOCK_EXIT[answer["result"]]:
        raise UnlockFailed("keyring-unlock answered %s but exited %d" % (answer["result"], p.returncode))
    return answer


class Refusal(Exception):
    """A request not answered: its HTTP status and why (logged, and shown on the page when it is the page's own)."""

    def __init__(self, status, why):
        super().__init__(why)
        self.status = status


class Guard:
    """The server's checks and their state: who may ask (proxy uids, logins, devices), the passkeys' site (host and
    origin), the challenges waiting, the failures counted, whether the port is reachable from the public, and the
    desktop keyring's last state. `run` is sources.run or a test's stand-in (for tailscale, the mailroom's agent-mail,
    and systemctl, which stops an orchestrator run that did not take its message), `popen` subprocess.Popen or a
    test's stand-in (for agent-orchestrate, which actions.orchestrate starts); `clock` gives the time. `read_keyring`
    gives the keyring's state as keyring.status does, or is None while the keyring unlock is off
    (config.keyring_unlock); `unlock_command` is keyring-unlock (UNLOCK_COMMAND): a test's stand-ins for both, as the
    real ones are the owner's own keyring."""

    def __init__(self, cfg, logins, host, socket_path, store, run, popen, clock, read_keyring=None,
                 unlock_command=None):
        self.cfg, self.logins, self.host, self.store, self.run, self.clock = cfg, logins, host, store, run, clock
        self.popen, self.read_keyring, self.unlock_command = popen, read_keyring, unlock_command
        self.socket_path = socket_path
        self.host_port = "%s:%d" % (host, cfg["port"])
        self.origin = mailwebstate.page_url(host, cfg["port"])
        self.lock = threading.Lock()
        self.challenges = {p: collections.OrderedDict() for p in CHALLENGES}   # purpose -> challenge -> its binding
        self.failures = {"signin": collections.deque(), "enrol": collections.deque()}     # when each failed
        self.devices = {}                              # tailnet address -> (whois answer, when asked)
        self.busy = collections.Counter()              # device -> its requests going now
        self.funnel = (None, None)                     # (why the port is public or None, when checked)
        self.keyring_read = (None, None)               # (the keyring's state for the page, when read)

    def public(self):
        """Why the page must answer nothing, or None: it is reachable from the public, or the check failed (with
        tailscale's error) or met a status it cannot read. A result is kept for FUNNEL_CHECK seconds; a failed check
        is not. tailscale runs outside the lock."""
        with self.lock:
            why, at = self.funnel
        if at is not None and self.clock() - at < FUNNEL_CHECK:
            return why
        try:
            why = funnel_exposure(self.cfg["port"], self.socket_path, self.run)
        except Exception as e:          # noqa: BLE001 -- whatever the check meets, it fails closed
            return ("could not check whether the page is open to the public (%s: %s), so it answers nothing"
                    % (type(e).__name__, e))
        with self.lock:
            self.funnel = (why, self.clock())
        return why

    def keyring(self, fresh=False):
        """The desktop keyrings' state for the page, {"state": Chrome's keyring's, "locked" | "unlocked" | "unknown",
        "error": why it is unknown, or None, "collections": each watched keyring's {"path", "label", "locked", "error"}
        by name, or None}, as read_keyring (keyring.status) last read it, read again once that is keyring.REFRESH
        seconds old (as often as the board reads it), or now when `fresh`; read_keyring runs outside the lock. A read
        that raises is the state "unknown", with why, and is not kept. None while the keyring unlock is off
        (read_keyring None): the page then shows no mark."""
        if self.read_keyring is None:
            return None
        with self.lock:
            st, at = self.keyring_read
        if not fresh and at is not None and self.clock() - at < keyring.REFRESH:
            return st
        try:
            got = self.read_keyring()
        except Exception as e:          # noqa: BLE001 -- whatever the read meets, the page says the state is unknown
            return {"state": "unknown", "error": "its state could not be read: %s" % sources.error_text(e),
                    "collections": None}
        st = {"state": got["state"], "error": got["error"], "collections": got["collections"]}
        with self.lock:
            self.keyring_read = (st, self.clock())
        return st

    def device(self, headers, devices=None):
        """(stable id, name) of the device asking. Raises Refusal unless the request passes checks 1 to 3, the device
        being one of `devices` (the settings' `devices` by default: another page's own list, such as the voice
        page's)."""
        why = self.public()
        if why:
            raise Refusal(503, why)
        if headers.get("Tailscale-Funnel-Request"):
            raise Refusal(403, "came through Tailscale Funnel")
        asked = headers.get("X-Forwarded-Host") or ""
        if asked.lower() != self.host_port or headers.get("X-Forwarded-Proto") != "https":
            raise Refusal(403, "asked for %r over %s, not https://%s" % (asked, headers.get("X-Forwarded-Proto"),
                                                                       self.host_port))
        who = (headers.get("Tailscale-User-Login") or "").strip().lower()
        if who not in self.logins:
            raise Refusal(403, "the tailnet login %r is not one the page answers" % who if who
                          else "no tailnet login (not through tailscale serve)")
        address = (headers.get("X-Forwarded-For") or "").strip()
        if not address:
            raise Refusal(403, "no client address from tailscale serve")
        with self.lock:
            known, at = self.devices.get(address, (None, 0.0))
        if known is None or self.clock() - at >= WHOIS_SECONDS:
            try:
                known = whois(address, self.run)
            except sources.CommandError as e:
                raise Refusal(403, "tailscale knows no device at %s: %s" % (address, e)) from None
            with self.lock:
                self.devices[address] = (known, self.clock())
        node, name, login = known
        if node not in (self.cfg["devices"] if devices is None else devices):
            raise Refusal(403, "device %s (%s) is not one the page answers" % (name or address, node))
        if login != who:
            raise Refusal(403, "device %s is logged in as %r, not %r" % (name, login, who))
        return node, name

    def enter(self, device):
        """Count a request from `device` as going. Raises Refusal (503) when PER_DEVICE are going already."""
        with self.lock:
            if self.busy[device] >= PER_DEVICE:
                raise Refusal(503, "%d requests from this device are going already" % PER_DEVICE)
            self.busy[device] += 1

    def leave(self, device):
        with self.lock:
            self.busy[device] -= 1

    # challenges and failures

    def challenge(self, purpose, device, **bound):
        """A new challenge for `purpose` from `device`, bound to `bound` (a session's passkey, the action a step-up
        approves and its request's digest)."""
        c = secrets.token_bytes(32)
        with self.lock:
            waiting = self.challenges[purpose]
            waiting[c] = dict(bound, device=device, ends=self.clock() + CHALLENGE_SECONDS)
            while len(waiting) > CHALLENGES[purpose]:
                waiting.popitem(last=False)
        return c

    def take_challenge(self, client_data_json, purpose, device):
        """The challenge clientDataJSON names, taken (each is used once) from those issued for `purpose`, with what it
        is bound to. Raises Refusal unless it is one of them, was issued to `device` and has not lapsed."""
        try:
            c = webauthn.unb64url(json.loads(client_data_json).get("challenge", ""))
        except (ValueError, AttributeError, RecursionError, webauthn.Refused):
            raise Refusal(400, "the client data names no challenge") from None
        with self.lock:
            got = self.challenges[purpose].pop(c, None)
        if not got or got["device"] != device or got["ends"] <= self.clock():
            raise Refusal(403, "the %s challenge is unknown, used, lapsed or another device's" % purpose)
        return c, got

    def check_failures(self, kind):
        """Raises Refusal (429) while the `kind` ("signin" or "enrol") failures in FAILURE_WINDOW seconds reach its
        limit (FAILURES, ENROL_TRIES)."""
        limit, said = {"signin": (FAILURES, "too many failed sign-ins"),
                       "enrol": (ENROL_TRIES, "too many enrolment tries")}[kind]
        with self.lock:
            times = self.failures[kind]
            while times and times[0] <= self.clock() - FAILURE_WINDOW:
                times.popleft()
            if len(times) >= limit:
                wait = int(times[0] + FAILURE_WINDOW - self.clock()) // 60 + 1
                raise Refusal(429, "%s; try again in %d minutes" % (said, wait))

    def failed(self, kind):
        with self.lock:
            self.failures[kind].append(self.clock())


def _credential(req, *names):
    """The base64url fields `names` of req["credential"] as bytes. Raises Refusal when one is missing or garbled."""
    cred = req.get("credential")
    if not isinstance(cred, dict):
        raise Refusal(400, "no credential")
    try:
        return [webauthn.unb64url(cred.get(n)) for n in names]
    except webauthn.Refused:
        raise Refusal(400, "the credential's %s is not base64url" % "/".join(names)) from None


class LineReader:
    """A connection's input as Handler reads it, with no buffer that could keep the body: each line of the request and
    its headers is found by peeking (MSG_PEEK) into a scratch buffer, zeroed after each look, and then exactly that
    line is taken. So the body (a reply's text and files, and an unlock's password) stays in the kernel until _api_call
    reads it straight into its own buffer (readinto), which is zeroed once the request is answered. A line costs two system calls, where an
    unbuffered file takes one a byte, each of which waits for the GIL behind any busy thread."""
    PEEK = 1024                                  # bytes looked at, at most, to find a line's end

    def __init__(self, sock):
        self.sock, self.scratch = sock, bytearray(self.PEEK)

    def readline(self, limit=-1):
        """The next line, with its newline: at most `limit` bytes when it is not negative; less at the input's end."""
        line = b""
        while limit < 0 or len(line) < limit:
            want = self.PEEK if limit < 0 else min(self.PEEK, limit - len(line))
            n = self.sock.recv_into(self.scratch, want, socket.MSG_PEEK)
            end = self.scratch.find(b"\n", 0, n) + 1 or n
            self.scratch[:n] = bytes(n)
            if not n:
                break
            line += self.sock.recv(end)          # what was looked at, up to the line's end, is there to take
            if line.endswith(b"\n"):
                break
        return line

    def readinto(self, buf):
        return self.sock.recv_into(buf)

    def close(self):
        pass                                     # the connection is the server's to close


class Handler(http.server.BaseHTTPRequestHandler):
    """One request; the server carries its Guard as `guard`."""
    server_version = "agent-board-mail"
    timeout = TIMEOUT
    device_name = None
    security_headers = SECURITY_HEADERS      # every answer's; a page of its own on this server sets its own
    devices = None                           # the devices a request is answered for: None, the settings' `devices`

    def setup(self):
        """The connection's input as a LineReader, in place of the buffered file http.server reads with, which would
        keep a copy of the body (and of a password it carries)."""
        super().setup()
        self.rfile.close()                       # never read from
        self.rfile = LineReader(self.connection)

    def version_string(self):
        """The Server header: the server's name alone, without Python's version."""
        return self.server_version

    def address_string(self):
        return "-"                     # a Unix socket's peer has no address; tailscale's X-Forwarded-For is logged

    def handle_one_request(self):
        """One request, its line and headers cut off when they take more than HEADER_SECONDS to arrive."""
        self.deadline = self._deadline(HEADER_SECONDS, "request line and headers")
        try:
            super().handle_one_request()
        finally:
            self.deadline.cancel()

    def parse_request(self):
        ok = super().parse_request()   # the headers are in once this returns
        self.deadline.cancel()
        return ok

    def _deadline(self, seconds, what):
        """A started timer that drops the connection when `what` has not arrived within `seconds`; cancel it once it
        has."""
        def cut_off():
            self.log_message("dropped: its %s took over %d seconds", what, seconds)
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass                   # it has closed already
        timer = threading.Timer(seconds, cut_off)
        timer.start()
        return timer

    def _send(self, code, body, ctype, extra=()):
        self.answered = True
        self.send_response(code)
        for k, v in ((("Content-Type", ctype), ("Content-Length", str(len(body)))) + self.security_headers
                     + tuple(extra)):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        """An error the HTTP layer answers by itself (a malformed request, a method the page does not take), with the
        security headers like every other answer."""
        self.log_message("code %d, message %s", code, message)
        self.close_connection = True
        self._send(code, ((message or "error") + "\n").encode(), "text/plain; charset=utf-8")

    def _json(self, code, obj, extra=()):
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def _refuse(self, refusal, api):
        self.log_message("refused %s: %s", self.path, refusal)
        if api:
            return self._json(refusal.status, {"ok": False, "error": str(refusal)})
        self._send(refusal.status, b"Forbidden\n" if refusal.status == 403 else (str(refusal) + "\n").encode(),
                   "text/plain; charset=utf-8")

    def _cookie(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return None

    def _set_cookie(self, value, max_age):
        return ("Set-Cookie", "%s=%s; Max-Age=%d; %s" % (COOKIE, value, max_age, COOKIE_ATTRIBUTES))

    def _session(self, device):
        """The signed-in session of this request's cookie, on `device`. Raises Refusal (401) without one."""
        s = self.server.guard.store.session(self._cookie(), self.server.guard.clock())
        if not s or s["device"] != device:
            raise Refusal(401, "not signed in")
        return s

    def _device(self):
        """The device asking (Guard.device), its request counted as going until it is answered (Guard.enter)."""
        device, self.device_name = self.server.guard.device(self.headers, self.devices)
        self.server.guard.enter(device)
        self.entered = device
        return device

    def do_GET(self):
        self._counted(self._get)

    def do_POST(self):
        self._counted(self._post)

    def _counted(self, handle):
        """Run `handle`, the request counted as going for its device until it ends. Anything it raises but a Refusal
        (each answered where it is raised) or a closed connection is logged with its traceback and answered 500, with
        the security headers as every answer is, unless an answer has begun: no request goes unanswered for want of a
        check."""
        self.entered, self.answered = None, False
        try:
            handle()
        except ConnectionError as e:     # closed by the other end, or by a deadline (_deadline): no one to answer
            self.log_message("the connection closed before %s was answered (%s)", self.path, e)
        except Exception as e:  # noqa: BLE001 -- whatever a request meets, it is answered and on the record
            self.log_message("failed on %s: %s", self.path,
                             "".join(traceback.format_exception(type(e), e, e.__traceback__)).strip()
                             .replace("\n", " | "))
            if not self.answered:
                self._refuse(Refusal(500, "the desktop failed on this request (%s); its journal says why"
                                     % type(e).__name__), api=self.command == "POST")
        finally:
            if self.entered:
                self.server.guard.leave(self.entered)

    def _get(self):
        g = self.server.guard
        path = urllib.parse.urlsplit(self.path).path
        try:
            device = self._device()
            if path in PAGES:
                file, ctype = PAGES[path]
                return self._send(200, file.read_bytes(), ctype)
            m = PART_RE.fullmatch(path)
            if not m or self.headers.get(API_HEADER) != "1":
                return self._send(404, b"Not found\n", "text/plain; charset=utf-8")
            self._session(device)
        except Refusal as r:
            return self._refuse(r, api=False)
        res = actions.dispatch({"action": "mail_part", "key": m.group(1), "index": int(m.group(2))},
                               lambda: [], lambda: [], run=g.run)
        if not res.get("ok"):
            self.log_message("mail_part failed: %s", res.get("error"))
            return self._send(404, (res.get("error") or "no such part").encode() + b"\n", "text/plain; charset=utf-8")
        try:
            data = base64.b64decode(res["data"], validate=True)
        except binascii.Error:
            why = "the part's data from agent-mail is not base64"
            self.log_message("%s: %s", path, why)
            return self._send(500, why.encode() + b"\n", "text/plain; charset=utf-8")
        name = urllib.parse.quote(res["name"], safe="")
        self._send(200, data, "application/octet-stream",
                   (("Content-Disposition", "attachment; filename*=UTF-8''" + name),))

    def _post(self):
        self._api_call("/api", API, MAX_BODY)

    def _api_call(self, path, api, max_body):
        """One call of `api` (action -> handler) POSTed to `path` by the page's own script: its device checked, its
        body (at most `max_body` bytes) read as one line of JSON, an object with an action (the pages' JSON.stringify
        writes no line break), and after an unlock's line (keyring_unlock, when `api` has it) the password, which only
        api_keyring_unlock is given, as an offset into the request's bytes. The body is read straight from the
        connection (LineReader) into one bytearray, zeroed once the request is answered, whatever happens; only the
        JSON line is copied out of it."""
        body = bytearray()
        try:
            device = self._device()
            if urllib.parse.urlsplit(self.path).path != path or self.headers.get(API_HEADER) != "1":
                return self._send(404, b"Not found\n", "text/plain; charset=utf-8")
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise Refusal(400, "Content-Length is not a number") from None
            if not 0 < size <= max_body:
                raise Refusal(413, "a request is 1 to %d bytes" % max_body)
            body = bytearray(size)
            timer = self._deadline(body_seconds(size), "body")
            try:
                got, view = 0, memoryview(body)
                while got < size:
                    n = self.rfile.readinto(view[got:])
                    if not n:
                        break
                    got += n
                del view
            finally:
                timer.cancel()
            if got != size:
                raise Refusal(400, "the request ended before its Content-Length")
            cut = body.find(b"\n")
            try:
                req = json.loads(bytes(memoryview(body)[:cut]) if cut >= 0 else bytes(body))
            except (ValueError, RecursionError):
                req = None
            if not isinstance(req, dict) or not isinstance(req.get("action"), str):
                raise Refusal(400, "a request is a JSON object with an action, on one line")
            handler = api.get(req["action"])
            if handler is None:
                raise Refusal(400, "action must be one of " + ", ".join(api))
            if req["action"] == UNLOCK:
                return self.api_keyring_unlock(req, device, body, None if cut < 0 else cut + 1)
            if cut >= 0:
                raise Refusal(400, "only %s carries anything after its request's line" % UNLOCK)
            handler(self, req, device)
        except Refusal as r:
            self._refuse(r, api=True)
        finally:
            body[:] = bytes(len(body))               # with the password, when the request carried one

    # the API: each takes (the request, the device) and answers, or raises Refusal

    def api_status(self, req, device):
        """Whether this phone is signed in, whether any passkey is enrolled, the files a reply carries, the characters
        a message to the orchestrator may have (actions.MAX_BRIEF), and, once signed in, the desktop keyring's state
        (Guard.keyring; null while the keyring unlock is off)."""
        try:
            self._session(device)
            signed_in = True
        except Refusal:
            signed_in = False
        self._json(200, {"ok": True, "signed_in": signed_in, "enrolled": bool(self.server.guard.store.passkeys()),
                         "limits": {"files": mailbox.MAX_FILES, "bytes": mailbox.MAX_FILE_BYTES,
                                    "message": actions.MAX_BRIEF},
                         "keyring": self.server.guard.keyring() if signed_in else None})

    def api_enrol_begin(self, req, device):
        """The options for creating a passkey, for the enrolment code the Mac showed (used up when right; a wrong one
        counts toward ENROL_TRIES), under a user handle of its own."""
        g = self.server.guard
        g.check_failures("enrol")
        if not g.store.take_code(req.get("code"), g.clock()):
            g.failed("enrol")
            raise Refusal(403, "that enrolment code is wrong, used or lapsed; show a new one on the Mac")
        c = g.challenge("enrol", device)
        self.log_message("enrolment begun")
        name, display = enrol_user()
        self._json(200, {"ok": True, "options": {
            "challenge": webauthn.b64url(c), "rp": {"id": g.host, "name": RP_NAME},
            "user": {"id": webauthn.b64url(secrets.token_bytes(16)), "name": name, "displayName": display},
            "pubKeyCredParams": [{"type": "public-key", "alg": webauthn.ES256}], "timeout": CHALLENGE_SECONDS * 1000,
            "attestation": "none", "excludeCredentials": [{"type": "public-key", "id": k} for k in g.store.passkeys()],
            "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True,
                                       "userVerification": "required"}}})

    def api_enrol_finish(self, req, device):
        """The new passkey, checked, kept and signed in with, once the owner has been told of it (a security alert,
        ENROL_ALERT); an enrolment the owner cannot be told of does not happen."""
        g = self.server.guard
        g.check_failures("enrol")
        cid, cdj, ad, pk = _credential(req, "id", "clientDataJSON", "authenticatorData", "publicKey")
        c, _ = g.take_challenge(cdj, "enrol", device)
        try:
            cred = webauthn.registration(g.host, g.origin, c, cid, cdj, ad, pk,
                                         req["credential"].get("publicKeyAlgorithm"))
        except webauthn.Refused as e:
            g.failed("enrol")
            raise Refusal(403, "passkey not enrolled: %s" % e) from None
        # a security alert: whoever enrolled can read the Mail tab, so the mailroom makes sure it reaches the owner
        # another way too (Gmail, or with Gmail off the Mac app's notification), and refuses it when it cannot
        sent, why = sources.mail_owner(
            "Phone mail: a passkey was enrolled",
            "A passkey for the agent-mail page on your phone was enrolled at %s, on device %s.\n\nIf that was not you: "
            "on your Mac, right-click Agent Board's menu-bar icon, choose Phone…, then Forget the phone.\n"
            % (time.strftime("%H:%M on %d %b %Y", time.localtime(g.clock())), self.device_name),
            alert=ENROL_ALERT)
        if not sent:
            raise Refusal(503, "passkey not enrolled: the owner could not be told of it (%s)" % why)
        g.store.add_passkey(webauthn.b64url(cred.id), webauthn.b64url(cred.public_key), cred.sign_count, device,
                            g.clock())
        self.log_message("passkey enrolled")
        self._signed_in(webauthn.b64url(cred.id), device)

    def api_signin_begin(self, req, device):
        """The options for signing in with an enrolled passkey. They name no credential (allowCredentials is empty):
        the passkey is discoverable (enrolled with residentKey required), and a credential id handed out here would
        let anything on the phone's node send bad signatures for it and lock sign-in."""
        g = self.server.guard
        g.check_failures("signin")
        if not g.store.passkeys():
            raise Refusal(403, "no passkey is enrolled; enrol one from the Mac")
        self._json(200, {"ok": True, "options": self._get_options(g.challenge("signin", device), {})})

    def api_signin_finish(self, req, device):
        """A signed-in session, for a passkey's signature over the sign-in challenge."""
        self.server.guard.check_failures("signin")
        cid = self._verified(req, "signin", device)[0]
        self.log_message("signed in")
        self._signed_in(cid, device)

    def api_signout(self, req, device):
        token = self._cookie()
        if token:
            self.server.guard.store.end_session(token)
        self.log_message("signed out")
        self._json(200, {"ok": True}, (self._set_cookie("", 0),))

    def api_mail(self, req, device):
        """mail_list, mail_show, mail_mark and mail_fast, and the drafts' actions, as the board's actions do them; a
        draft's change from DRAFT_DEVICE."""
        self._session(device)
        g = self.server.guard
        if req["action"] in DRAFT_ACTIONS:
            res = actions.dispatch(dict(req, device=DRAFT_DEVICE), lambda: [], lambda: [])
        else:
            res = actions.dispatch(req, lambda: [], lambda: [], run=g.run)
        if not res.get("ok"):
            self.log_message("%s failed: %s", req["action"], res.get("error"))
        elif req["action"] == "mail_show":
            try:
                res["document"] = for_phone(res["document"])
            except ValueError as e:
                self.log_message("mail_show: %s", e)
                raise Refusal(500, str(e)) from None
        self._json(200, res)

    def api_stepup_begin(self, req, device):
        """The options for approving one step-up action with the session's passkey: the challenge is bound to the
        action it `approves` (one of STEP_UP), to the SHA-256 (hex) of the exact `request` text that action will
        carry, and to that passkey."""
        s = self._session(device)
        approves, digest = req.get("approves"), req.get("digest")
        if not (isinstance(approves, str) and approves in STEP_UP):
            raise Refusal(400, "approves must be one of " + ", ".join(STEP_UP))
        if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise Refusal(400, "digest must be the request's SHA-256 in hex")
        g = self.server.guard
        c = g.challenge("stepup", device, approves=approves, digest=digest, passkey=s["passkey"])
        self._json(200, {"ok": True, "options": self._get_options(c, {s["passkey"]: None})})

    def api_mail_command(self, req, device):
        """A reply (mail_reply) or a Researcher switch (mail_researcher), run once the passkey approved exactly its
        request (_stepped_up); the outcome is logged and the board's answer sent as JSON."""
        body = self._stepped_up(req, device)
        action = req["action"]
        res = actions.dispatch(dict(body, action=action), lambda: [], lambda: [], run=self.server.guard.run)
        outcome = ("sent" if action == "mail_reply" else "saved") if res.get("ok") else "failed"
        self.log_message("%s %s: %s", action, outcome, res.get("status") or res.get("error") or body.get("thread"))
        self._json(200, res)

    def api_orchestrate(self, req, device):
        """A message to the orchestrator (the orchestrate request's text), started once the passkey approved exactly it
        (_stepped_up), its header saying it came from the phone (ORCH_SOURCE) whatever the request says. It goes on
        sent.py's record as the Mac's do."""
        body = self._stepped_up(req, device)
        res = actions.dispatch(dict(body, action="orchestrate", source=ORCH_SOURCE), lambda: [], lambda: [],
                               popen=self.server.guard.popen, run=self.server.guard.run)
        self.log_message("orchestrate %s: %s", "started" if res.get("ok") else "failed",
                         res.get("log") or res.get("error"))
        self._json(200, res)

    def api_keyring_unlock(self, req, device, body, start):
        """Unlock the desktop's login keyring with the password in body[start:] (the request's bytes after its line;
        `start` None when nothing follows it), once the session's passkey has approved this unlock (_stepped_up), and
        only while the board config's keyring_unlock is on (refused with keyring.OFF, before anything else, when it is
        off): its
        request is {"nonce": N}, N a new random key (mailbox.KEY_RE's form) that the page makes for each submission,
        so the approval covers this one submission and nothing made from the password. Nothing about the password is
        looked at before the approval checks out. It is checked as keyring-unlock checks it (keyring.password_end),
        then goes only to the command's stdin (run_unlock), as a view of `body`, which _api_call zeroes. Answers the
        command's own answer as "unlock", and the keyring's state read straight after (Guard.keyring) as "keyring".
        Only the outcome goes on the record, the journal and actions.jsonl (actions.audit, whose failure to write is
        added to the answer's error); refusals are logged as every refusal is, and never hold the password."""
        g = self.server.guard
        if g.unlock_command is None:
            raise Refusal(403, keyring.OFF)
        asked = self._stepped_up(req, device)
        nonce = asked.get("nonce")
        if set(asked) != {"nonce"} or not (isinstance(nonce, str) and mailbox.KEY_RE.fullmatch(nonce)):
            raise Refusal(400, 'an unlock\'s request is {"nonce": a new random key}')
        if start is None:
            raise Refusal(400, "the password must follow the request's line")
        try:
            end = keyring.password_end(body, start, len(body))
        except keyring.BadInput as e:
            raise Refusal(400, str(e)) from None
        try:
            res = run_unlock(g.unlock_command, memoryview(body)[start:end])
        except UnlockFailed as e:
            self.log_message("keyring_unlock: error: %s", e)
            failed = actions.audit({"action": UNLOCK, "result": "error"}, {"ok": False, "error": str(e)})
            raise Refusal(502, failed["error"]) from None
        self.log_message("keyring_unlock: %s", res["result"])
        answer = actions.audit({"action": UNLOCK, "result": res["result"]},
                               {"ok": True, "unlock": res, "keyring": g.keyring(fresh=True)})
        if answer.get("error"):
            self.log_message("keyring_unlock: %s", answer["error"])
        self._json(200, answer)

    # helpers

    def _get_options(self, challenge, keys):
        return {"challenge": webauthn.b64url(challenge), "rpId": self.server.guard.host,
                "timeout": CHALLENGE_SECONDS * 1000, "userVerification": "required",
                "allowCredentials": [{"type": "public-key", "id": k} for k in keys]}

    def _stepped_up(self, req, device):
        """The request (a JSON object) of step-up action req["action"], once the session's passkey has approved exactly
        it: req["request"] is that request as JSON text, without its action, and req["credential"] the passkey's
        signature over the challenge stepup_begin bound to that action and that text. Raises Refusal without a session,
        for an approval of another action or of other text, and for text that is not UTF-8 or not a JSON object."""
        self._session(device)
        action, text = req["action"], req.get("request")
        if not isinstance(text, str):
            raise Refusal(400, "request must be the %s request, as JSON text" % action)
        try:
            raw = text.encode()
        except UnicodeEncodeError:
            raise Refusal(400, "the request holds a lone surrogate (half of a UTF-16 pair), which is not text a "
                               "passkey can approve") from None
        _, bound = self._verified(req, "stepup", device)
        if bound["approves"] != action:
            raise Refusal(403, "the passkey approved %s, not %s" % (bound["approves"], action))
        if hashlib.sha256(raw).hexdigest() != bound["digest"]:
            raise Refusal(403, "the request is not the one the passkey approved")
        try:
            body = json.loads(text)
        except (ValueError, RecursionError):
            raise Refusal(400, "the request is not JSON") from None
        if not isinstance(body, dict):
            raise Refusal(400, "the request is not a JSON object")
        return body

    def _verified(self, req, purpose, device):
        """(credential id, what the challenge is bound to) of a passkey's valid signature over a `purpose` challenge
        issued to `device`; its new sign count kept. A step-up's must come from its session's passkey. Raises Refusal;
        only a failed check of an enrolled passkey's signature counts toward the sign-in limit."""
        g = self.server.guard
        cid_raw, cdj, ad, sig = _credential(req, "id", "clientDataJSON", "authenticatorData", "signature")
        c, bound = g.take_challenge(cdj, purpose, device)
        cid = webauthn.b64url(cid_raw)
        known = g.store.passkeys().get(cid)
        if known is None or bound.get("passkey", cid) != cid:
            raise Refusal(403, "that passkey is not enrolled here")
        try:
            count = webauthn.assertion(g.host, g.origin, c, webauthn.Credential(
                cid_raw, webauthn.unb64url(known["public_key"]), known["sign_count"]), cdj, ad, sig)
        except webauthn.Refused as e:
            g.failed("signin")
            raise Refusal(403, "passkey refused: %s" % e) from None
        g.store.set_sign_count(cid, count)
        return cid, bound

    def _signed_in(self, cid, device):
        g = self.server.guard
        token = g.store.new_session(cid, device, g.clock(), g.cfg["session_hours"])
        self._json(200, {"ok": True}, (self._set_cookie(token, g.cfg["session_hours"] * 3600),))

    def log_message(self, fmt, *args):
        """One line per request on stderr (the unit's journal), with the tailnet login and the device asking."""
        headers = getattr(self, "headers", None)       # not there for a request too broken to parse
        super().log_message("%s %s " + fmt, (headers.get("Tailscale-User-Login") if headers else None) or "-",
                            self.device_name or (headers.get("X-Forwarded-For") if headers else None) or "-", *args)


# the actions that need the passkey again (a step-up: stepup_begin, then Handler._stepped_up), and their handlers;
# _api_call calls the unlock's itself, with the request's bytes
STEP_UP = {"mail_reply": Handler.api_mail_command, "mail_researcher": Handler.api_mail_command,
           "orchestrate": Handler.api_orchestrate,
           UNLOCK: Handler.api_keyring_unlock}
API = {"status": Handler.api_status, "enrol_begin": Handler.api_enrol_begin, "enrol_finish": Handler.api_enrol_finish,
       "signin_begin": Handler.api_signin_begin, "signin_finish": Handler.api_signin_finish,
       "signout": Handler.api_signout, "stepup_begin": Handler.api_stepup_begin, **STEP_UP,
       **{a: Handler.api_mail for a in MAIL_ACTIONS + DRAFT_ACTIONS}}


class PageServer(sources.UnixServer):
    """The page's server on the Unix socket at `path` (sources.UnixServer: mode 0600, an old socket there replaced),
    taking a connection only from a process running as one of its Guard's `proxy_uids`, and MAX_CONNECTIONS at once
    (one more waits SLOT_SECONDS for a slot, in its own thread, then is closed). `handler` answers each request:
    Handler, or a subclass that serves another page too (voiceweb.handler())."""

    def __init__(self, path, guard, handler=Handler):
        self.guard, self.slots = guard, threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(path, handler, lambda: guard.cfg["proxy_uids"], "the page", refused=lambda uid: log(
            "refused a connection from uid %d, not tailscale serve's (proxy_uids)" % uid))

    def process_request_thread(self, request, client_address):
        """The connection, in its own thread, once one of MAX_CONNECTIONS slots is free; waiting SLOT_SECONDS for one
        here keeps the accept loop free."""
        if not self.slots.acquire(timeout=SLOT_SECONDS):
            log("refused a connection: %d are being served already" % MAX_CONNECTIONS)
            self.shutdown_request(request)
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def serve(handler=Handler):
    """Serve until stopped (`agent-board mail-web`): on its Unix socket (mailwebstate.SOCKET), in its state folder
    (made 0700 here), for the tailnet port mailweb.json sets, for the tailnet logins it lists (by default the owner's
    addresses, from the mailroom's config) on the devices it lists, through the proxy uids it lists, asking for the
    desktop's tailnet name (from tailscale). The requests it answers run agent-mail and start agent-orchestrate, and
    record what they do in actions.jsonl, and the owner's messages to the orchestrator in sent.jsonl, as `agent-board
    act` does. With the board config's keyring_unlock on, they also read the desktop keyring's state on the session bus
    and run keyring-unlock, whose outcome goes in actions.jsonl too. Raises ValueError when the settings are bad or list no device, proxy uid or login; RuntimeError when the
    page is reachable from the public, when tailscale gives no name, or when something that is not a socket is at the
    socket's path; and sources.CommandError when tailscale fails. `handler` answers each request (PageServer's). It
    first makes itself undumpable, with no core files (lockdown.harden), as session cookies and mail, and with the
    keyring unlock on the desktop password, pass through it."""
    # harden() also sets the hard core-file limit to 0 for every process mail-web starts (agent-mail, orchestrator
    # runs): none of them needs a core file, so that is acceptable
    lockdown.harden()
    cfg = mailwebstate.settings()
    if not cfg["devices"] or not cfg["proxy_uids"]:
        raise ValueError("%s must list the phone's Tailscale node id (devices) and tailscaled's uid (proxy_uids); "
                         "README, \"Mail on the phone\", says how to find them" % mailwebstate.CONFIG)
    logins = mailwebstate.logins(cfg)
    why = funnel_exposure(cfg["port"], mailwebstate.SOCKET, sources.run)
    if why:
        raise RuntimeError("the page is reachable from the public through Tailscale Funnel (%s); it will not run so"
                           % why)
    host = mailwebstate.tailnet_name(sources.run)
    mailwebstate.STATE.mkdir(mode=0o700, parents=True, exist_ok=True)       # where the socket goes
    store = mailwebstate.Store(mailwebstate.STATE)
    on = config.keyring_unlock()
    guard = Guard(cfg, logins, host, mailwebstate.SOCKET, store, sources.run, subprocess.Popen, time.time,
                  (lambda: keyring.status(keyring.session_bus())) if on else None, UNLOCK_COMMAND if on else None)
    srv = PageServer(mailwebstate.SOCKET, guard, handler)
    print("agent-board mail-web: on %s for %s on %s through uids %s at %s"
          % (mailwebstate.SOCKET, ", ".join(logins), ", ".join(cfg["devices"]), cfg["proxy_uids"], guard.origin),
          flush=True)
    srv.serve_forever()
