#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REXTERM_REPO_URL:-https://github.com/SeaXen/rexterm.git}"
REPO_REF="${REXTERM_REPO_REF:-main}"
INSTALL_DIR="${REXTERM_INSTALL_DIR:-/opt/rexterm}"
INSTALL_USER="${REXTERM_RUN_USER:-${SUDO_USER:-$(id -un)}}"
SERVICE_NAME="${REXTERM_SERVICE_NAME:-rexterm-host}"
PORT="${REXTERM_PORT_OVERRIDE:-2344}"
ENABLE_SYSTEMD="${REXTERM_ENABLE_SYSTEMD:-1}"
AUTH_REQUIRED="${REXTERM_AUTH_REQUIRED_OVERRIDE:-1}"
AUTH_USER="${REXTERM_AUTH_USERNAME_OVERRIDE:-admin}"
TEMP_PASSWORD=""

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run as root/sudo. Example:" >&2
    echo "  curl -fsSL https://raw.githubusercontent.com/SeaXen/rexterm/main/scripts/install-vps.sh | sudo bash" >&2
    exit 1
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

random_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

sha256_text() {
  local value="$1"
  python3 - "$value" <<'PY'
import hashlib, sys
print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
PY
}

upsert_env() {
  local key="$1"
  local value="$2"
  local file="$3"
  python3 - "$file" "$key" "$value" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text().splitlines()
needle = key + "="
for i, line in enumerate(lines):
    if line.startswith(needle):
        lines[i] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n")
PY
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y git python3 tmux ca-certificates curl
}

clone_or_update_repo() {
  mkdir -p "$(dirname "$INSTALL_DIR")"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch --tags --prune origin
  else
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
  if git -C "$INSTALL_DIR" rev-parse --verify --quiet "refs/tags/$REPO_REF" >/dev/null; then
    git -C "$INSTALL_DIR" checkout "tags/$REPO_REF"
  else
    git -C "$INSTALL_DIR" checkout "$REPO_REF"
    git -C "$INSTALL_DIR" pull --ff-only origin "$REPO_REF" || true
  fi
}

prepare_env() {
  local env_file="$INSTALL_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    cp "$INSTALL_DIR/.env.example" "$env_file"
  fi

  upsert_env REXTERM_PORT "$PORT" "$env_file"
  upsert_env REXTERM_HOST_PORT "$PORT" "$env_file"
  upsert_env REXTERM_AUTH_REQUIRED "$AUTH_REQUIRED" "$env_file"
  upsert_env REXTERM_AUTH_USERNAME "$AUTH_USER" "$env_file"

  local token
  token="$(python3 - "$env_file" <<'PY'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text().splitlines()
value = ""
for line in text:
    if line.startswith("REXTERM_SHARED_TOKEN="):
        value = line.split("=", 1)[1]
        break
print(value)
PY
)"
  if [[ -z "$token" || "$token" == "change-me" ]]; then
    token="$(random_secret)"
    upsert_env REXTERM_SHARED_TOKEN "$token" "$env_file"
  fi

  local current_hash current_raw password password_hash
  current_hash="$(python3 - "$env_file" <<'PY'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text().splitlines()
vals = {}
for line in text:
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        vals[k] = v
print(vals.get("REXTERM_AUTH_PASSWORD_SHA256", ""))
PY
)"
  current_raw="$(python3 - "$env_file" <<'PY'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text().splitlines()
vals = {}
for line in text:
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        vals[k] = v
print(vals.get("REXTERM_AUTH_PASSWORD", ""))
PY
)"

  if [[ -z "$current_hash" && -z "$current_raw" && "$AUTH_REQUIRED" == "1" ]]; then
    if [[ -t 0 && -t 1 ]]; then
      read -r -p "Rexterm login username [$AUTH_USER]: " typed_user || true
      if [[ -n "${typed_user:-}" ]]; then
        AUTH_USER="$typed_user"
        upsert_env REXTERM_AUTH_USERNAME "$AUTH_USER" "$env_file"
      fi
      while true; do
        read -r -s -p "Set Rexterm login password: " password
        echo
        read -r -s -p "Confirm password: " password2
        echo
        if [[ -z "$password" ]]; then
          echo "Password cannot be empty." >&2
          continue
        fi
        if [[ "$password" != "$password2" ]]; then
          echo "Passwords did not match. Try again." >&2
          continue
        fi
        break
      done
      password_hash="$(sha256_text "$password")"
      upsert_env REXTERM_AUTH_PASSWORD_SHA256 "$password_hash" "$env_file"
      upsert_env REXTERM_AUTH_PASSWORD "" "$env_file"
    else
      TEMP_PASSWORD="$(random_secret | tr -d '_-' | cut -c1-20)"
      password_hash="$(sha256_text "$TEMP_PASSWORD")"
      upsert_env REXTERM_AUTH_PASSWORD_SHA256 "$password_hash" "$env_file"
      upsert_env REXTERM_AUTH_PASSWORD "" "$env_file"
    fi
  fi
}

ensure_owner() {
  if id "$INSTALL_USER" >/dev/null 2>&1; then
    chown -R "$INSTALL_USER":"$INSTALL_USER" "$INSTALL_DIR"
  fi
}

install_systemd() {
  if [[ "$ENABLE_SYSTEMD" != "1" ]]; then
    return 0
  fi
  if ! command_exists systemctl; then
    echo "systemctl not found; skipping service install." >&2
    return 0
  fi
  REXTERM_RUN_USER="$INSTALL_USER" REXTERM_SERVICE_NAME="$SERVICE_NAME" bash "$INSTALL_DIR/scripts/install-host-systemd.sh"
}

print_summary() {
  local primary_ip
  primary_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  echo "Rexterm installed"
  echo "- dir: $INSTALL_DIR"
  echo "- ref: $REPO_REF"
  echo "- user: $INSTALL_USER"
  echo "- port: $PORT"
  if [[ "$ENABLE_SYSTEMD" == "1" ]] && command_exists systemctl; then
    echo "- systemd service: $SERVICE_NAME"
  fi
  echo
  echo "Open locally:"
  echo "- http://127.0.0.1:$PORT/"
  if [[ -n "$primary_ip" ]]; then
    echo "Open on LAN/VPS:"
    echo "- http://$primary_ip:$PORT/"
  fi
  echo
  echo "Security"
  echo "- Rexterm binds to 0.0.0.0, so protect the port with a firewall/reverse proxy if exposed beyond your LAN."
  echo "- Auth stays enabled by default."
  if [[ -n "$TEMP_PASSWORD" ]]; then
    echo "- Temporary login username: $AUTH_USER"
    echo "- Temporary login password: $TEMP_PASSWORD"
    echo "- Change it immediately after first login."
  else
    echo "- Login username: $AUTH_USER"
  fi
  echo
  echo "Useful commands"
  if [[ "$ENABLE_SYSTEMD" == "1" ]] && command_exists systemctl; then
    echo "- systemctl status $SERVICE_NAME --no-pager"
    echo "- journalctl -u $SERVICE_NAME -n 100 --no-pager"
    echo "- systemctl restart $SERVICE_NAME"
  else
    echo "- cd $INSTALL_DIR && ./scripts/run-host.sh"
  fi
}

main() {
  need_root
  install_packages
  clone_or_update_repo
  prepare_env
  ensure_owner
  install_systemd
  print_summary
}

main "$@"
