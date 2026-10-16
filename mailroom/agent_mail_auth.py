#!/usr/bin/env python3
"""One-time authorisation of the agents Gmail account for the mailroom.

    agent-mail-auth [--account AGENTS_ADDRESS] [--client-secrets PATH] [--port N] [--no-browser]

(`agent-mail-auth` is the installed wrapper; it runs this file with the kit's Python.)

Asks for gmail.modify only (read, label, send; no permanent delete) and stores the
refresh token at ~/.config/agent-mail/token.json (mode 600). The expected account is
config `account` (~/.config/agent-mail/config.json) unless --account is given. It refuses
to save a token for any other account, so consenting with the wrong Google login cannot
succeed. The OAuth client file is a "Desktop app" client downloaded from Google Cloud
Console (default ~/.config/agent-mail/credentials.json).

On a headless machine (over SSH): pick a fixed port and skip the browser, e.g.
`agent-mail-auth --port 8765 --no-browser`, forward that port from your laptop with
`ssh -L 8765:localhost:8765 <host>`, and open the printed URL in the laptop's browser.

Needs google-auth-oauthlib and google-api-python-client (see requirements.txt).
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmail import config  # noqa: E402

SCOPES = config.SCOPES
# The fields of an "authorized_user" file, copied from the OAuth result.
SAVED_FIELDS = ("client_id", "client_" + "secret", "refresh_" + "token")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent-mail-auth", description=__doc__.split("\n\n")[0])
    ap.add_argument("--account", help="the agents Gmail address (default: config `account`)")
    ap.add_argument("--client-secrets", default=str(config.CLIENT_SECRETS_PATH),
                    help="OAuth client file for a Desktop app (default: %(default)s)")
    ap.add_argument("--port", type=int, default=0,
                    help="fixed local port for the OAuth redirect (default 0: a random free port)")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not try to open a browser; print the URL to open elsewhere (headless/SSH)")
    a = ap.parse_args(argv)

    config.CONF_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(config.CONF_DIR, 0o700)

    expected = (a.account or config.load().get("account") or "").strip().lower()
    if not expected:
        print("No agents account: pass --account ADDRESS or set `account` in %s." % config.CONFIG_PATH, file=sys.stderr)
        return 2
    client = Path(os.path.expanduser(a.client_secrets))
    if not client.is_file():
        print("No OAuth client file at %s. Create a Desktop-app OAuth client in Google Cloud Console "
              "(Gmail API enabled), download its JSON there, or pass --client-secrets PATH." % client, file=sys.stderr)
        return 2
    dest = config.TOKEN_PATH

    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    print("OAuth client     : %s" % client)
    print("Saved to         : %s" % dest)
    print("Expected account : %s" % expected)
    print()
    if a.no_browser:
        print("Open the URL printed below in a browser. Sign in as the AGENTS account (not your personal one).")
        if a.port:
            print("On a headless machine: from your laptop run `ssh -L %d:localhost:%d <host>`, then open the" % (a.port, a.port))
            print("printed URL in your laptop's browser; Google redirects back through that tunnel.")
        else:
            print("On a headless machine, rerun with a fixed port (e.g. --port 8765 --no-browser) and forward it")
            print("from your laptop with `ssh -L 8765:localhost:8765 <host>`; a random port cannot be forwarded ahead.")
    else:
        print("A browser tab will open. Sign in as the AGENTS account (not your personal one).")
    print("If Google shows \"Google hasn't verified this app\", choose Advanced, then continue to your app's")
    print("name, then Allow. The only permission requested is Gmail read/label/send (gmail.modify).")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(client), scopes=SCOPES)
    creds = flow.run_local_server(
        port=a.port,
        open_browser=not a.no_browser,
        login_hint=expected,   # pre-selects the account in the chooser
        prompt="consent",      # always mint a refresh token
    )

    profile = build("gmail", "v1", credentials=creds).users().getProfile(userId="me").execute()
    who = profile.get("emailAddress", "")
    if who.lower() != expected:
        print("\nRefusing to save: you authorised %s, not %s." % (who, expected))
        print("Run again and pick the agents account in the browser.")
        return 1

    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    saved = dict({"type": "authorized_user"}, **{k: getattr(creds, k) for k in SAVED_FIELDS})
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(saved, f, indent=2)
    os.chmod(dest, 0o600)

    print("\nAuthorised as %s. Mailbox has %s messages." % (who, profile.get("messagesTotal", "?")))
    print("Saved to %s (mode 600)." % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
