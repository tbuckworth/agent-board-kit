#!/usr/bin/env bash
# Install or update agent-board-kit for this user. Idempotent: re-run it after editing
# ~/.config/agent-kit/kit.env or pulling a new version. See README.md and docs/.
#   ./install.sh                 first run: creates ~/.config/agent-kit/kit.env for you to edit
#   ./install.sh                 second run: installs (nothing is started)
#   ./install.sh --start         also enables the services and timers
#   ./install.sh --dry-run       shows what would change
#   ./install.sh --help          all options
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null || { echo "python3 is required (3.9+)" >&2; exit 1; }
exec python3 "$here/bin/kit" install "$@"
