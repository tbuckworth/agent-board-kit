"""The phone page's settings and stored state, shared by its server (mailweb.py) and the Mac's actions on it
(actions.py: an enrolment code, signing the phone out). README, "Mail on the phone".

Settings: ~/.config/agent-board/mailweb.json (AGENT_BOARD_MAILWEB_CONFIG names another) overrides DEFAULTS. `devices`
lists the Tailscale stable node ids (`tailscale status --json`, a peer's "ID") of the devices the page answers, and
`proxy_uids` the uids whose connections it takes (tailscaled's, root: 0). Both are empty until set, and the server
will not start without them. `logins` lists the tailnet logins (Tailscale-User-Login) it answers; empty, the default,
means the owner's addresses in the mailroom's config (owner_email, then allowlist). `voice_devices` lists the devices
the voice page (/voice, voiceweb.py) answers, and only that page: a device in it alone reaches nothing of the Mail
page, and a device of `devices` reaches /voice only when it is listed here too. Empty, the default, the voice page
answers no one.

State: in STATE (mode 0700), each file private (0600), written whole and renamed into place, every change under a
lock that both processes take (the server's socket, SOCKET, is there too):
- passkeys.json: the enrolled passkeys, by credential id (base64url): public key, sign count, device, when;
- sessions.json: the signed-in sessions, by the SHA-256 of their cookie, never the cookie itself;
- enrol.json: the one enrolment code waiting, by its SHA-256, and when it lapses.
"""
import contextlib
import fcntl
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from . import sources

FUNNEL_PORTS = (443, 8443, 10000)    # the ports Tailscale Funnel can open to the public: the page never uses one
CONFIG = Path(os.environ.get("AGENT_BOARD_MAILWEB_CONFIG") or sources.HOME / ".config/agent-board/mailweb.json")
STATE = sources.BOARD_STATE / "mail-web"
SOCKET = STATE / "page.sock"     # where the server listens; `tailscale serve ... unix:<this>` publishes it
DEFAULTS = {
    "port": 8466,           # the tailnet port `tailscale serve` publishes the page on (never one of FUNNEL_PORTS)
    "devices": [],          # the Tailscale stable node ids the page answers (the owner's phone)
    "voice_devices": [],    # those the voice page (/voice) answers, and nothing else of the server
    "proxy_uids": [],       # the uids whose connections it takes: tailscaled's (root, 0)
    "logins": [],           # the tailnet logins it answers; empty: the owner's addresses (mailroom config)
    "session_hours": 12,    # how long a sign-in lasts
    "enrol_minutes": 10,    # how long an enrolment code shown on the Mac lasts
}


def settings(path=None):
    """DEFAULTS with the file at `path`'s (default CONFIG) overrides. Raises ValueError for a port that is not a whole
    number from 1024 to 65535 or is one of FUNNEL_PORTS, devices, voice_devices or logins that are not lists of
    non-empty strings, proxy_uids that are not a list of uids, or hours and minutes outside 1 to 24 and 1 to 10."""
    path = path or CONFIG
    cfg = sources.load_config(DEFAULTS, path)

    def whole(v, lo, hi):
        return not isinstance(v, bool) and isinstance(v, int) and lo <= v <= hi
    if not whole(cfg["port"], 1024, 65535) or cfg["port"] in FUNNEL_PORTS:
        raise ValueError("%s: port must be a whole number from 1024 to 65535 and not one Funnel can open (%s)"
                         % (path, ", ".join(map(str, FUNNEL_PORTS))))
    for key, what in (("devices", "Tailscale node ids"), ("voice_devices", "Tailscale node ids"),
                      ("logins", "tailnet logins")):
        if not (isinstance(cfg[key], list) and all(isinstance(d, str) and d.strip() for d in cfg[key])):
            raise ValueError("%s: %s must be a list of %s" % (path, key, what))
    if not (isinstance(cfg["proxy_uids"], list) and all(whole(u, 0, 2 ** 32 - 1) for u in cfg["proxy_uids"])):
        raise ValueError("%s: proxy_uids must be a list of uids" % path)
    if not whole(cfg["session_hours"], 1, 24):
        raise ValueError("%s: session_hours must be a whole number from 1 to 24" % path)
    if not whole(cfg["enrol_minutes"], 1, 10):
        raise ValueError("%s: enrol_minutes must be a whole number from 1 to 10" % path)
    return cfg


def logins(cfg, owner_addresses=None):
    """The tailnet logins the page answers, lower case: the settings' `logins`, else the owner's addresses
    (`owner_addresses`, default sources.owner_addresses(): the mailroom config's owner_email and allowlist). Raises
    ValueError when both are empty, as the page would then answer no one."""
    got = [x.strip().lower() for x in cfg["logins"]]
    if not got:
        got = [x.lower() for x in (sources.owner_addresses() if owner_addresses is None else owner_addresses)]
    if not got:
        raise ValueError("%s names no logins, and the mailroom's config no owner address (owner_email): the page "
                         "would answer no one. Set logins to your Tailscale login (`tailscale status --json`, "
                         "User.<id>.LoginName)" % CONFIG)
    return got


def tailnet_name(run):
    """The desktop's name on the tailnet, lower case (`tailscale status --json`, Self.DNSName). `run` is sources.run or
    a test's stand-in for it. Raises sources.CommandError when tailscale fails, and RuntimeError when it gives no
    name (Tailscale is not logged in)."""
    status = sources.call("tailscale", ["status", "--json"], run)
    name = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    if not name:
        raise RuntimeError("tailscale gives this desktop no tailnet name (BackendState %s)"
                           % status.get("BackendState"))
    return name


def page_url(host, port, path=""):
    """The page's address on the tailnet: https on the desktop's tailnet name `host` at `port`, then `path`. It is
    also the passkeys' origin."""
    return "https://%s:%d%s" % (host, port, path)


def _digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


class Store:
    """The page's stored state in `folder` (STATE, or a test's own)."""

    def __init__(self, folder):
        self.folder = Path(folder)

    @contextlib.contextmanager
    def _locked(self):
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.folder / "lock", "a") as fh:
            os.chmod(fh.name, 0o600)
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield

    def _read(self, name):
        got = sources.read_json(self.folder / name, {})
        if not isinstance(got, dict):
            raise ValueError("%s must hold a JSON object" % (self.folder / name))
        return got

    def _write(self, name, obj):
        sources.write_json(self.folder / name, obj, 0o600)

    # enrolment codes

    def mint_code(self, now, minutes):
        """A new enrolment code, lapsing after `minutes`; it replaces any code still waiting. (code, lapses at)."""
        code = secrets.token_urlsafe(18)
        with self._locked():
            self._write("enrol.json", {"code": _digest(code), "lapses": now + minutes * 60})
        return code, now + minutes * 60

    def take_code(self, code, now):
        """Whether `code` is the enrolment code waiting and has not lapsed; the right one is used up, and a lapsed one
        ends. A wrong code leaves the waiting one as it is (the server limits the tries)."""
        if not isinstance(code, str) or not code:
            return False
        with self._locked():
            waiting = self._read("enrol.json")
            if not waiting:
                return False
            lapsed = now >= waiting.get("lapses", 0)
            right = hmac.compare_digest(waiting.get("code", ""), _digest(code))
            if right or lapsed:
                self._write("enrol.json", {})
        return right and not lapsed

    # passkeys

    def passkeys(self):
        """{credential id (base64url): {"public_key" (base64url DER), "sign_count", "device", "enrolled"}}."""
        with self._locked():
            return self._read("passkeys.json")

    def add_passkey(self, cred_id, public_key, sign_count, device, now):
        with self._locked():
            keys = self._read("passkeys.json")
            keys[cred_id] = {"public_key": public_key, "sign_count": sign_count, "device": device, "enrolled": now}
            self._write("passkeys.json", keys)

    def set_sign_count(self, cred_id, count):
        with self._locked():
            keys = self._read("passkeys.json")
            if cred_id in keys:
                keys[cred_id]["sign_count"] = count
                self._write("passkeys.json", keys)

    # sessions

    def new_session(self, cred_id, device, now, hours):
        """A new signed-in session for passkey `cred_id` on `device`, lasting `hours`: its cookie value."""
        token = secrets.token_urlsafe(32)
        with self._locked():
            sessions = {k: v for k, v in self._read("sessions.json").items() if v["ends"] > now}
            sessions[_digest(token)] = {"passkey": cred_id, "device": device, "started": now,
                                        "ends": now + hours * 3600}
            self._write("sessions.json", sessions)
        return token

    def session(self, token, now):
        """The session whose cookie is `token`, while it lasts, else None."""
        if not isinstance(token, str) or not token:
            return None
        with self._locked():
            s = self._read("sessions.json").get(_digest(token))
        return s if s and s["ends"] > now else None

    def end_session(self, token):
        with self._locked():
            sessions = self._read("sessions.json")
            if sessions.pop(_digest(token), None) is not None:
                self._write("sessions.json", sessions)

    def sign_out(self, forget):
        """End every session; with `forget`, also forget every passkey, so the phone must be enrolled again.
        (sessions ended, passkeys forgotten)."""
        with self._locked():
            ended = len(self._read("sessions.json"))
            self._write("sessions.json", {})
            forgot = 0
            if forget:
                forgot = len(self._read("passkeys.json"))
                self._write("passkeys.json", {})
        return ended, forgot
