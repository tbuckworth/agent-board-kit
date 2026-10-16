# 2. The agents' Gmail account

The mailroom uses the Gmail API with one OAuth scope, `gmail.modify` (read, label, send; it cannot
permanently delete). You create a mailbox for the agents, register a small private OAuth "app" in
Google Cloud, and give it consent once. About 15 minutes.

## Step 1: create the agents' Gmail account

1. Create a new Google account at <https://accounts.google.com/signup>, for example
   `yourname.agents@gmail.com`. Use it only for agents. Never use your own mailbox: agents send
   from it, and the mailroom reads everything that arrives in it.
2. Turn on 2-Step Verification for it (Google asks for this anyway).
3. Put the address in `AGENT_EMAIL` in `~/.config/agent-kit/kit.env`.

Plus-addressing (`yourname.agents+TAG@gmail.com`) works out of the box; the mailroom uses it as
one of the ways to route replies, so do not turn anything off.

## Step 2: a Google Cloud project with the Gmail API

Use any Google account you like for the project (the agents' account is simplest).

1. Open <https://console.cloud.google.com/>, create a project (e.g. `agent-mailroom`).
2. **APIs & Services → Library → Gmail API → Enable.**
3. **Google Auth Platform** (formerly "OAuth consent screen") → **Get started**:
   - App name: anything (e.g. `agent mailroom`); support email: yours.
   - **Audience: External.**
   - Contact email: yours. Finish.
4. **Data Access → Add or remove scopes**: add `https://www.googleapis.com/auth/gmail.modify`
   (paste it under "Manually add scopes"). Save.
5. **Audience → Publish app → Confirm** (status "In production"). Do this. An app left in
   "Testing" gets refresh tokens that **expire after 7 days**, and the mailroom would stop every
   week. A personal app with fewer than 100 users does not need Google's verification; you will
   just see an "unverified app" warning once, during consent. (If you consented while the app was
   still in Testing, run `agent-mail-auth` again after publishing.)

## Step 3: the OAuth client file

1. **Google Auth Platform → Clients → Create client → Application type: Desktop app** → Create.
2. **Download JSON.** Save it on the Linux machine as `~/.config/agent-mail/credentials.json`:

   ```bash
   mkdir -p ~/.config/agent-mail && chmod 700 ~/.config/agent-mail
   mv ~/Downloads/client_secret_*.json ~/.config/agent-mail/credentials.json   # or scp it over
   chmod 600 ~/.config/agent-mail/credentials.json
   ```

This file identifies your private app; it is not a login. Still, keep it private.

## Step 4: give consent (after installing)

Consent needs the `agent-mail-auth` command, which the installer adds. It is a step in
[03-install.md](03-install.md#give-gmail-consent). Do steps 1-3 and 5 now.

## Step 5: your own mailbox

- Add the agents' address to your contacts, or make a filter for `from:(yourname.agents@gmail.com)`
  with **Never send it to Spam**. Agent mail is plain and automated; the first ones can land in spam.
- Agents mail you with subjects ending in `[#tag]`. Reply normally, from any device. Do not change
  the subject's tag or the Reply-To address.
- Your address must be the one in `OWNER_EMAIL`, and your provider must DKIM-sign your mail.
  The mailroom delivers an instruction only if Gmail's `Authentication-Results` shows a DKIM pass
  for your domain, because a `From:` header alone is trivial to forge.

## What the mailroom does in the agents' mailbox

- Polls the inbox history every 30 seconds.
- Labels what it handled: `mailroom/delivered` (delivered to a session or the orchestrator, marked
  read) and `mailroom/held` (left unread for you to look at: unknown sender, no session found, failed
  DKIM).
- Sends as `AGENT_FROM_NAME (<session name>)` with a per-session tag in the subject and Reply-To.

You can open the agents' mailbox in a browser at any time to see what was sent and held;
`agent-mail inbox --held` and `agent-mail show N` show the same from the shell.

## Revoking

Remove the app's access at <https://myaccount.google.com/permissions> (signed in as the agents'
account) and delete `~/.config/agent-mail/token.json`. To rotate, run `agent-mail-auth` again.
