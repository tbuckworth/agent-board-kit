#!/bin/bash
# From the desktop: copy this component's source to the Mac and build the app there.
# MAC_HOST: the ssh alias of the Mac (default `mac`). MAC_BUILD_DIR: where the source goes on the Mac,
# relative to its home directory unless absolute (default `agent-board-build`). It is a scratch copy kept in step
# with `rsync --delete`: never point it at a folder you edit on the Mac.
# Simpler alternative: clone the kit on the Mac itself and run mac/build-app.sh and mac/install.sh there.
set -euo pipefail
cd "$(dirname "$0")/.."
MAC_HOST=${MAC_HOST:-mac}
MAC_BUILD_DIR=${MAC_BUILD_DIR:-agent-board-build}
MAC_BUILD_DIR=${MAC_BUILD_DIR#\~/}          # "~/x" means x under the home directory
case "$MAC_BUILD_DIR" in
  ""|"/"|"."|"./"|"~"|"..") echo "MAC_BUILD_DIR=$MAC_BUILD_DIR is not a folder of its own; refusing to rsync --delete into it" >&2; exit 2 ;;
esac
rsync -a --delete --exclude .git --exclude .build --exclude build --exclude __pycache__ --exclude shots ./ "$MAC_HOST:$MAC_BUILD_DIR/"
ssh "$MAC_HOST" "cd $(printf '%q' "$MAC_BUILD_DIR")/mac && ./build-app.sh"
