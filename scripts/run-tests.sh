#!/usr/bin/env bash
# Run every component's unit tests in a throwaway HOME, so nothing touches your real config, state,
# tmux server, systemd user manager or mail. Usage: scripts/run-tests.sh [mailroom|board|todo|watchdog ...]
# The mailroom suites need the Google client libraries: PYTHON=~/.local/share/agent-kit/venv/bin/python
# scripts/run-tests.sh uses the kit venv (or have them installed for python3, --user installs included).
set -uo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/agent-kit-tests.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PY="${PYTHON:-python3}"
USERBASE="$("$PY" -m site --user-base 2>/dev/null || true)"   # keep --user site-packages visible under the fake HOME
want="${*:-mailroom board todo watchdog}"
fail=0

isolated() {   # isolated NAME DIR CMD...: run CMD in DIR with a fresh HOME and no session/bus/tmux environment
  local name="$1" dir="$2"; shift 2
  local R="$WORK/$name"; mkdir -p "$R/home" "$R/cfg" "$R/state" "$R/status" "$R/tmp" "$R/run"
  (cd "$dir" && env -u DBUS_SESSION_BUS_ADDRESS -u TMUX -u SSH_CONNECTION -u CLAUDE_CODE_SESSION_ID \
      -u CLAUDE_CONFIG_DIR -u CODEX_HOME -u AGENT_SPAWN_ROOT -u TODO_REPO -u GH_TOKEN -u GITHUB_TOKEN \
      HOME="$R/home" TMPDIR="$R/tmp" TMUX_TMPDIR="$R/tmp" XDG_RUNTIME_DIR="$R/run" \
      AGENT_MAIL_CONFIG_DIR="$R/cfg" AGENT_MAIL_STATE_DIR="$R/state" AGENT_STATUS_DIR="$R/status" \
      TODO_DESK_CONFIG_DIR="$R/home/.config/todo-desk" TODO_DESK_CACHE="$R/tmp/cache.json" \
      TODO_DESK_STATE="$R/state/todo" PYTHONUSERBASE="$USERBASE" PYTHONDONTWRITEBYTECODE=1 "$@") > "$R/out.txt" 2>&1
  local code=$?
  printf '%-34s %s\n' "$name" "$( (grep -E '^(Ran |OK|FAILED|# (pass|fail) )' "$R/out.txt" || true) | tr '\n' ' ')"
  if [ $code -ne 0 ]; then fail=1; echo "---- $name failed:"; tail -30 "$R/out.txt"; fi
}

for c in $want; do
  case "$c" in
    mailroom)   # suites mutate module globals: one process each
      for t in "$KIT"/mailroom/tests/test_*.py; do isolated "mailroom/$(basename "$t" .py)" "$KIT/mailroom" "$PY" "$t"; done ;;
    board)      isolated board "$KIT/board" "$PY" -m unittest discover -s tests ;;
    todo)       isolated todo/desk "$KIT/todo" "$PY" -m unittest discover -s desk/tests
                if command -v node >/dev/null; then isolated todo/web "$KIT/todo" node --test "$KIT"/todo/web/tests/*.test.js
                else echo "todo/web                           skipped (node not installed)"; fi ;;
    watchdog)   if [ -d "$KIT/cloud-watchdog/tests" ]; then
                  isolated cloud-watchdog "$KIT/cloud-watchdog" "$PY" -m unittest discover -s tests
                else echo "cloud-watchdog                     skipped (not present)"; fi ;;
    *) echo "unknown component: $c" >&2; exit 2 ;;
  esac
done
[ $fail -eq 0 ] && echo "all passed" || echo "SOME TESTS FAILED"
exit $fail
