#!/bin/bash
# Build AgentBoard.app with SwiftPM (Command Line Tools are enough; no Xcode) and ad-hoc sign it.
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release
APP=build/AgentBoard.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$(swift build -c release --show-bin-path)/AgentBoard" "$APP/Contents/MacOS/AgentBoard"
cp Info.plist "$APP/Contents/Info.plist"
if [ ! -f build/AppIcon.icns ]; then
  "$APP/Contents/MacOS/AgentBoard" --icon build/AppIcon.iconset && iconutil -c icns build/AppIcon.iconset -o build/AppIcon.icns
fi
cp build/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
codesign --force --sign - "$APP" >/dev/null
echo "built $(pwd)/$APP"
