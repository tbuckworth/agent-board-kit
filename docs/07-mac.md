# 7. The Mac and the phone app

The Mac is a client of the Linux machine, and the main way in: the board is where you read and answer
your agents and start work. It can run:

- the **agent board** and the **to-do app** (menu-bar apps that talk to the Linux machine over ssh);
- its own Claude Code / Codex sessions that **mail through the Linux machine**, so your replies
  still reach them.

## SSH from the Mac to the Linux machine

Both apps hold one ssh connection open and run fixed commands on the other end. It must log in
without a prompt.

1. A private network is easiest: install [Tailscale](https://tailscale.com/) on both machines and
   use the Linux machine's tailnet name. A LAN address works at home only.
2. `~/.ssh/config` on the Mac:

   ```
   Host desktop
       HostName my-linux-box            # tailnet name or IP
       User alex
       IdentityFile ~/.ssh/id_ed25519
       ServerAliveInterval 30
   ```

3. `ssh-copy-id desktop`, then check `ssh -o BatchMode=yes desktop true` prints nothing and exits 0.

If your alias is not `desktop`, set `DESKTOP_HOST` in `kit.env` and tell each app (below).

## Build and install the apps (on the Mac)

Needs macOS 13+ and the Command Line Tools (`xcode-select --install`); Xcode is not needed. Both
apps are built from source on your Mac with SwiftPM and ad-hoc signed, so macOS does not quarantine
them, and they need no special permissions.

```bash
git clone <this repo> ~/agent-board-kit && cd ~/agent-board-kit

cd board/mac && ./build-app.sh && ./install.sh && cd ../..      # AgentBoard: ⌥⌘A
cd todo/mac  && ./build-app.sh && ./install.sh && cd ../..      # ToDo: ⌥⌘T

# only if your ssh alias is not "desktop":
defaults write org.agentkit.agentboard host my-alias
defaults write org.agentkit.todo host my-alias
```

`install.sh` copies the app to `/Applications`, adds a login item (menu bar only at login) and starts
it. Re-run both after `git pull` to update. There are also `scripts/mac-install.sh` in `board/` and
`todo/` that build on the Mac from the Linux machine over ssh (`MAC_HOST=<alias of the Mac>`).

On the Linux machine the apps run `~/.local/bin/agent-board` and `~/.local/bin/todo-desk`, which
`./install.sh` puts there. Check from the Mac:
`ssh desktop ~/.local/bin/agent-board snapshot --pretty | head` and
`ssh desktop ~/.local/bin/todo-desk config`.

The board's "Open in Kitty" opens an agent's tmux session in the [kitty](https://sw.kovidgoyal.net/kitty/)
terminal if you use it; "Copy attach command" works with any terminal.

On its first launch the board asks to send notifications: allow them. It posts one when a tile turns
red (clicking it opens that agent) and, if you turned them on, 10 minutes before a meeting. After an
update, rebuild and reinstall both apps, then choose **New build ready: relaunch** in the running
board.

### The board on the Mac: what it keeps, and optional extras

- **Mail tab drafts** live in `~/Library/Application Support/AgentBoard/mail-drafts/`, one per
  thread, with a copy of each file until it is sent or discarded. Attachments you open are fetched
  into a private, quarantined folder under the Mac's temporary folder. Files too big for a reply can be
  copied to the Linux machine instead (`~/.local/state/agent-board/mail-files/` there).
- **Screen time on the Time page** (optional) comes from [ActivityWatch](https://activitywatch.net/)
  if it runs on the Mac:
  `defaults write org.agentkit.agentboard activityWatch http://localhost:5600`. Unset, the board
  asks it nothing.
- **Meeting alerts** (optional) are set up on the Linux machine (`meeting_alerts` and `google_token`
  in `~/.config/agent-board/config.json`; [the board's README](../board/README.md#meeting-alerts-optional)).
  **Join** opens the meeting in the Chrome profile signed in to the calendar's account, when Chrome
  has one, else in your default browser.

### The to-do app

The sidebar starts with **Triage** (→ or space next, ← or a archive, w this week, s snooze, d done,
x delete, g agent, 1 2 3 or h m l importance, u urgent). Double-clicking an agent's name on a task
opens its latest mail to you in Gmail, in the Chrome profile signed in to `OWNER_EMAIL` if Chrome has
one, else the default browser. Self-checks that never put a window on screen:
`/Applications/ToDo.app/Contents/MacOS/ToDo --selftest` and `--selftest-clicks`.

## The phone app (Agent Mail)

**Agent Mail** puts the Mail tab on your phone: the list, threads, replies with files, options and plots,
fast mode, drafts shared with the Mac, and messages to the orchestrator. It is a page the Linux machine serves
on your own Tailscale network only, which Chrome installs on the home screen; each sign-in, reply and
orchestrator message needs your fingerprint (a passkey). It is built and tested on Android with Chrome. How it is
protected, and every setting: [board/README.md, "Mail on the phone (Agent Mail)"](../board/README.md#mail-on-the-phone-agent-mail).

1. **Tailscale** on the Linux machine and the phone, the same login. In the Tailscale admin console, turn on
   **MagicDNS** and **HTTPS certificates**. Never turn on Funnel for this port: the page refuses to answer while
   it is public.
2. **`WITH_PHONE_APP="yes"`** in `kit.env`, then `./install.sh` on the Linux machine. That puts `cryptography`
   in the kit's venv and installs the `agent-board-mail-web` service (not started until step 4).
3. **Find your phone's node id** on the Linux machine:

   ```bash
   tailscale status --json | jq -r '.Peer[] | "\(.ID)  \(.HostName)"'
   ```

4. **Name it** in `~/.config/agent-board/mailweb.json` (`proxy_uids` `[0]` is tailscaled running as root), then
   start the service:

   ```bash
   echo '{"devices": ["YOUR-PHONE-NODE-ID"], "proxy_uids": [0]}' > ~/.config/agent-board/mailweb.json
   ./install.sh --start
   ```

   If your Tailscale login is not `OWNER_EMAIL` (a GitHub login, say), add `"logins": ["you@github"]`.

5. **Publish it on your tailnet** (port 8466, the default):

   ```bash
   sudo tailscale serve --bg --https=8466 unix:$HOME/.local/state/agent-board/mail-web/page.sock
   ```

   (`sudo tailscale set --operator=$USER` once lets you run `tailscale serve` without sudo.)

6. **Enrol the phone from the Mac**: right-click the board's menu-bar icon → **Phone…** → **Show an enrolment
   code**. Scan the QR code with the phone, tap **Enrol this phone**, and touch the fingerprint sensor. The
   enrolment is recorded as a security alert: it shows in the Mail tab and as a Mac notification.
7. **Install it**: in Chrome, ⋮ → **Add to home screen** → **Install**.

Check: `kit doctor` shows the phone app's lines green, and the app opens your Mail tab's list.

## Agents on the Mac (optional)

Sessions on the Mac can use the same mailbox. The Mac never talks to Gmail: `agent-mail send` and
`reply` hand the mail to the Linux machine over ssh, which records the Mac session as its owner and
sends it. Replies are delivered back over ssh to that exact, still-running Mac session; if the Mac is
asleep the reply is held and you get a notice, and a reply to a session that has exited goes to the
orchestrator with its thread (Mac sessions are never resumed automatically). Files you attach to a
Mail-tab reply to a Mac session are copied to the Mac over ssh (`remote_files_timeout`, 120 s). On the
Mac, `agent-group` runs each command on the Linux machine over ssh, as `agent-mail` does, so Mac
sessions can be members of group chats (`NAME@mac`).

1. On the Mac: clone the kit, copy your `kit.env` to `~/.config/agent-kit/kit.env`, and run
   `./install.sh` (it detects macOS and uses the **mac role**: commands, hooks, instructions, and a
   mailroom config with `mail_proxy` set to `DESKTOP_HOST` and direct sending off).
2. On the Linux machine: set `MAC_HOST` in `kit.env` to the Linux machine's ssh alias for the Mac and
   re-run `./install.sh`. The Linux machine must be able to `ssh -o BatchMode=yes <MAC_HOST> true`
   (Tailscale SSH or a key in the Mac's `~/.ssh/authorized_keys`, with Remote Login on in System
   Settings → General → Sharing).
3. `kit doctor` on both machines.

## What differs on macOS

| Thing | Linux | macOS |
|---|---|---|
| mailroom daemon, orchestrator, dispatch | systemd user services | not supported as the server: use the Mac as a client |
| per-agent memory caps (`agent-NAME.scope`) | yes | no-op (no systemd) |
| board metrics (`/proc`, `nvidia-smi`) | yes | the collector is Linux-only |
| `agent-restart` | apt + reboot + resume | not available |
| `agent-mail`, `agent-send`, `agent-status`, `agent-spawn`, `agent-close` | yes | yes (mail through the proxy) |
| `agent-group` (group chats) | yes | yes (runs on the Linux machine over ssh) |

Running the whole server side on a Mac (launchd instead of systemd, no memory caps, no board
metrics) is possible in principle but untested.
