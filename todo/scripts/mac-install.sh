#!/bin/bash
# From the Linux desktop: build on the Mac (scripts/mac-build.sh), then install /Applications/ToDo.app there with a
# login item (mac/install.sh). Same settings as mac-build.sh (MAC_HOST, MAC_BUILD_DIR), plus, optionally:
#   TODO_HELPER=/path/to/todo-desk   the helper the app runs on the desktop (default ~/.local/bin/todo-desk)
#   TODO_HOST=desktop                the ssh alias the Mac uses for the desktop (default desktop)
set -euo pipefail
cd "$(dirname "$0")/.."
MAC_HOST=${MAC_HOST:-mac}
DEST=${MAC_BUILD_DIR:-todo-mac-build}
DEST=${DEST#\~/}
scripts/mac-build.sh
ssh "$MAC_HOST" "cd $(printf %q "$DEST") && TODO_HELPER=$(printf %q "${TODO_HELPER:-}") TODO_HOST=$(printf %q "${TODO_HOST:-}") ./install.sh"
