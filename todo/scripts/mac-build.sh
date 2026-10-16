#!/bin/bash
# From the Linux desktop: copy the Mac app's source to the Mac over ssh and build it there (SwiftPM, no Xcode).
#   MAC_HOST=mac              ssh alias of the Mac (a host in ~/.ssh/config that logs in without a prompt)
#   MAC_BUILD_DIR=todo-mac-build
#                             build folder on the Mac, relative to its home or absolute. It is a scratch copy kept
#                             in step with `rsync --delete`: never point it at a folder you edit on the Mac.
# Building on the Mac itself needs no script: copy or clone the kit there, then
#   cd todo/mac && ./build-app.sh && ./install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
MAC_HOST=${MAC_HOST:-mac}
DEST=${MAC_BUILD_DIR:-todo-mac-build}
DEST=${DEST#\~/}
case "$DEST" in
  ""|"/"|"."|"./"|"~"|"..") echo "MAC_BUILD_DIR=$DEST is not a folder of its own; refusing to rsync --delete into it" >&2; exit 2 ;;
esac
rsync -a --delete --exclude .build --exclude build --exclude shots mac/ "$MAC_HOST:$DEST/"
ssh "$MAC_HOST" "cd $(printf %q "$DEST") && ./build-app.sh"
