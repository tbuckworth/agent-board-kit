#!/bin/bash
# From the desktop: build on the Mac (scripts/mac-build.sh), then install into /Applications with a login item
# (mac/install.sh). Same settings as mac-build.sh: MAC_HOST (default `mac`), MAC_BUILD_DIR (default
# `agent-board-build` under the Mac's home directory).
set -euo pipefail
cd "$(dirname "$0")/.."
MAC_HOST=${MAC_HOST:-mac}
MAC_BUILD_DIR=${MAC_BUILD_DIR:-agent-board-build}
MAC_BUILD_DIR=${MAC_BUILD_DIR#\~/}          # "~/x" means x under the home directory
export MAC_HOST MAC_BUILD_DIR
scripts/mac-build.sh
ssh "$MAC_HOST" "$(printf '%q' "$MAC_BUILD_DIR")/mac/install.sh"
