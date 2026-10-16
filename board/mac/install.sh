#!/bin/bash
# On the Mac: install the built app into /Applications, start it at login (a LaunchAgent, menu bar only),
# and make sure exactly one copy runs. Safe to rerun after every build.
set -euo pipefail
cd "$(dirname "$0")"
SRC=build/AgentBoard.app
APP=/Applications/AgentBoard.app
LABEL=org.agentkit.agentboard
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
[ -d "$SRC" ] || { echo "no build yet: run ./build-app.sh"; exit 1; }

rm -rf "$APP.new"
ditto "$SRC" "$APP.new"
rm -rf "$APP"
mv "$APP.new" "$APP"
echo "installed $APP"

if [ ! -f "$PLIST" ]; then
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$APP/Contents/MacOS/AgentBoard</string><string>--hidden</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
    <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
EOF
  echo "login item: $PLIST"
fi

# one board, one hotkey: stop copies running from anywhere but /Applications (e.g. the build folder)
installed_running=0
for pid in $(pgrep -x AgentBoard || true); do
  path=$(ps -o comm= -p "$pid" || true)
  if [[ "$path" == "$APP/"* ]]; then installed_running=1; else kill "$pid" && echo "stopped $path ($pid)"; fi
done
if [ "$installed_running" = 1 ]; then
  echo "the installed copy is running: it offers 'New build ready: relaunch'"
else
  sleep 1
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl kickstart "gui/$(id -u)/$LABEL"
  echo "started $APP (menu bar; ⌥⌘A opens the window)"
fi
