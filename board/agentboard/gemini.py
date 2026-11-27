"""Gemini Live's ephemeral tokens, for the voice page (voiceweb.py; README, "Voice page"): the one request that carries
the Gemini API key, made on the desktop so that the key never reaches a browser.

voiceweb runs this file by itself once per conversation, on the system's Python in isolated mode, optionally under a
command the owner names (voice.json's gemini_key_command, such as a secrets manager's runner that puts the key in this
process's environment):

    [COMMAND...] python3 -I agentboard/gemini.py        the request on stdin, one JSON line on stdout

The request names where the key is (voiceweb.key_source): "key_env", an environment variable of this process, or
else "key_file", a file of the owner's alone (read_key_file: a regular file owned by this user, with no permission for
group or others). It takes the key from there and clears the rest of its environment (a secrets manager's other
secrets, which it does not need), sends the key only in the x-goog-api-key header of one POST to Google's auth_tokens
endpoint, and prints one line: {"ok": true, "token", "expire_time", "new_session_expire_time"}, or {"ok": false,
"error"} with the key cut out of the error. It writes no file and logs nothing. It uses the standard library alone and
nothing from agentboard, so the process holding the key loads only what it needs.

A token is made for one conversation (token_request): it may start one session (uses 1), only within
`new_session_seconds`, and every message after `expireTime` is refused. It carries the whole session's setup
(bidiGenerateContentSetup) with no fieldMask, so Google takes the setup entirely from the token and ignores the one the
browser sends: the model, voice, system instruction and tools are the desktop's alone. As Google's documentation has it
(aistudio.google.com/docs/live-api/ephemeral-tokens and /api-reference/generate-content/create-token): tokens work with
the Live API's v1beta only, on the BidiGenerateContentConstrained socket, the token passed as its access_token.
"""
import http.client
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request

HOST = "https://generativelanguage.googleapis.com"
SOCKET = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.%s.GenerativeService." \
         "BidiGenerateContentConstrained"
API_VERSION = re.compile(r"v[0-9]+(alpha|beta)?")
KEY = "GEMINI_API_KEY"             # the variable a key command puts the key in, unless voice.json names another
KEY_FILE_BYTES = 4096              # a key file is at most this long
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
HOW = ("put it in ~/.config/agent-board/gemini-key (mode 600), or name an environment variable that holds it in "
       "~/.config/agent-board/voice.json's gemini_key_env (README, \"Voice page\")")
TIMEOUT = 20                       # seconds Google may take to answer the mint
MAX_REQUEST = 256 * 1024           # bytes of request this reads from stdin
MAX_ANSWER = 1024 * 1024           # bytes of Google's answer it reads
TOKEN = re.compile(r"auth_tokens/[!-~]{1,4000}")     # the token: its resource name, printable ASCII


class MintError(Exception):
    """No token: why, for the owner to read (never the key)."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: urllib would send the key's header on to the new address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


URLOPEN = urllib.request.build_opener(NoRedirect).open     # what mint asks Google through: no redirects


def rfc3339(t):
    """Unix time `t` as the RFC 3339 UTC timestamp Google's Timestamp fields take."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def token_request(setup, now, new_session_seconds, conversation_seconds):
    """The auth_tokens request body for one conversation starting at `now`: one use, a session started within
    `new_session_seconds`, messages taken for `conversation_seconds`, and the whole setup `setup` locked in."""
    return {"uses": 1, "newSessionExpireTime": rfc3339(now + new_session_seconds),
            "expireTime": rfc3339(now + conversation_seconds), "bidiGenerateContentSetup": setup}


def checked_version(api_version):
    """`api_version` (v1beta, v1alpha...). Raises MintError for anything else, which could change the address."""
    if not isinstance(api_version, str) or not API_VERSION.fullmatch(api_version):
        raise MintError("the API version %r is not one like v1beta" % (api_version,))
    return api_version


def socket_url(api_version):
    """The Live API socket a token opens, without the token."""
    return SOCKET % checked_version(api_version)


def mint(body, api_version, key, urlopen=URLOPEN, timeout=TIMEOUT):
    """A token for auth_tokens request `body` (token_request), asked of Google with `key`: {"token", "expire_time",
    "new_session_expire_time"}, the times as Google's answer gives them (None if it does not). `urlopen` is URLOPEN,
    or a test's stand-in. Raises MintError."""
    url = "%s/%s/auth_tokens" % (HOST, checked_version(api_version))
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as r:
            got = json.loads(r.read(MAX_ANSWER))
    except urllib.error.HTTPError as e:
        raise MintError("Google refused the token (HTTP %d): %s" % (e.code, google_error(e))) from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        raise MintError("could not ask Google for a token: %s: %s" % (type(e).__name__, e)) from None
    name = got.get("name") if isinstance(got, dict) else None
    if not isinstance(name, str) or not TOKEN.fullmatch(name):
        raise MintError("Google's answer holds no token")
    return {"token": name, "expire_time": got.get("expireTime"),
            "new_session_expire_time": got.get("newSessionExpireTime")}


def google_error(e):
    """The message of Google's error answer `e` (an HTTPError), at most 300 characters."""
    try:
        said = json.loads(e.read(65536)).get("error", {}).get("message")
    except (ValueError, AttributeError, OSError):
        said = None
    return (said if isinstance(said, str) else e.reason or "no reason given")[:300]


def read_key_file(path):
    """The key in the file at `path`: its text, stripped, which must be one line. Refused (MintError, never with the
    key) unless the file is a regular file owned by this user that group and others may not read or write (mode 600 or
    400), at most KEY_FILE_BYTES long: a key other users could read is one to replace, not to use."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise MintError("no Gemini API key: %s does not exist; %s" % (path, HOW)) from None
    except OSError as e:
        raise MintError("no Gemini API key: %s could not be opened (%s)" % (path, e.strerror or e)) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise MintError("no Gemini API key: %s is not a regular file" % path)
        if st.st_uid != os.getuid():
            raise MintError("no Gemini API key: %s is not this user's" % path)
        if st.st_mode & 0o077:
            raise MintError("no Gemini API key: %s may be read by others (mode %o); make it private: chmod 600 %s"
                            % (path, stat.S_IMODE(st.st_mode), path))
        data = os.read(fd, KEY_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > KEY_FILE_BYTES:
        raise MintError("no Gemini API key: %s is over %d bytes, so it is no key" % (path, KEY_FILE_BYTES))
    key = data.decode("utf-8", "replace").strip()
    if not key or "\n" in key or "\r" in key:
        raise MintError("no Gemini API key: %s must hold the key alone, on one line" % path)
    return key


def main(stdin=sys.stdin, stdout=sys.stdout, environ=os.environ, urlopen=URLOPEN):
    """The mint as voiceweb runs it (the module's docstring): 0 with a token printed, 1 without one."""
    try:
        asked = json.loads(stdin.read(MAX_REQUEST))
    except ValueError:
        asked = None
    key_env = asked.get("key_env") if isinstance(asked, dict) else None
    key_env = key_env if isinstance(key_env, str) and ENV_NAME.fullmatch(key_env) else None
    key = environ.pop(key_env, "") if key_env else ""
    environ.clear()                          # a secrets manager's other secrets: this process needs none of them
    try:
        if not isinstance(asked, dict) or not isinstance(asked.get("request"), dict):
            raise MintError("the request on stdin is not a JSON object with a request")
        if key_env and not key:
            raise MintError("no Gemini API key: the environment variable %s (voice.json's gemini_key_env) is not set: "
                            "give it to agent-board-mail-web.service's environment, or have gemini_key_command set it"
                            % key_env)
        if not key_env:
            if not isinstance(asked.get("key_file"), str) or not asked["key_file"]:
                raise MintError("no Gemini API key: the request names neither a variable nor a file; " + HOW)
            key = read_key_file(asked["key_file"])
        out = dict(mint(asked["request"], asked.get("api_version"), key, urlopen), ok=True)
    except MintError as e:
        out = {"ok": False, "error": str(e).replace(key, "[the key]") if key else str(e)}
    stdout.write(json.dumps(out) + "\n")
    stdout.flush()
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
