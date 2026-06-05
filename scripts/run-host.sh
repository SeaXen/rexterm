#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load local private config when present. .env is gitignored.
# Preserve explicit one-shot overrides, e.g. REXTERM_BACKEND_PORT=2345 ./scripts/run-host.sh
CLI_REXTERM_BACKEND_PORT="${REXTERM_BACKEND_PORT:-}"
if [[ -f "$APP_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$APP_DIR/.env"
  set +a
fi

if [[ -n "$CLI_REXTERM_BACKEND_PORT" ]]; then
  export REXTERM_BACKEND_PORT="$CLI_REXTERM_BACKEND_PORT"
else
  # Docker uses REXTERM_BACKEND_PORT as container-internal 8080; host mode should use the public port.
  export REXTERM_BACKEND_PORT="${REXTERM_HOST_PORT:-${REXTERM_PORT:-${REXTERM_BACKEND_PORT:-2344}}}"
fi
export REXTERM_DATA_DIR="${REXTERM_DATA_DIR:-$APP_DIR/data}"
export REXTERM_STATIC_DIR="${REXTERM_STATIC_DIR:-$APP_DIR/static}"
export REXTERM_SHARED_TOKEN="${REXTERM_SHARED_TOKEN:-change-me}"
export REXTERM_AUTH_REQUIRED="${REXTERM_AUTH_REQUIRED:-1}"
export REXTERM_AUTH_USERNAME="${REXTERM_AUTH_USERNAME:-admin}"
export REXTERM_AUTH_SESSION_TTL="${REXTERM_AUTH_SESSION_TTL:-604800}"
export REXTERM_HISTORY_LIMIT="${REXTERM_HISTORY_LIMIT:-300}"
export REXTERM_SHELL="${REXTERM_SHELL:-/bin/bash}"

# Public-repo friendly tmux path: use host tmux if installed, otherwise use vendored Debian tmux rootfs.
if ! command -v tmux >/dev/null 2>&1 && [[ -x "$APP_DIR/vendor-rootfs-tmux/usr/bin/tmux" ]]; then
  export PATH="$APP_DIR/vendor-rootfs-tmux/usr/bin:$PATH"
  export LD_LIBRARY_PATH="$APP_DIR/vendor-rootfs-tmux/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "Rexterm host mode needs tmux. Install tmux or keep vendor-rootfs-tmux/ in the repo." >&2
  exit 1
fi

mkdir -p "$REXTERM_DATA_DIR" "$REXTERM_DATA_DIR/sessions" "$REXTERM_DATA_DIR/tmux" "$REXTERM_DATA_DIR/uploads"
cd "$APP_DIR"
exec python3 "$APP_DIR/app/server.py"
