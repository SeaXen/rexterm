#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${REXTERM_DATA_DIR:-$APP_DIR/data}"
export APP_DIR DATA_DIR
mkdir -p "$DATA_DIR"

read -r -p "Username [admin]: " username
username="${username:-admin}"
while true; do
  read -r -s -p "Password: " password
  printf '\n'
  read -r -s -p "Confirm password: " confirm
  printf '\n'
  if [[ "$password" != "$confirm" ]]; then
    echo "Passwords do not match." >&2
    continue
  fi
  if [[ ${#password} -lt 8 ]]; then
    echo "Password must be at least 8 characters." >&2
    continue
  fi
  break
done

export REXTERM_SET_AUTH_USER="$username"
export REXTERM_SET_AUTH_PASS="$password"
python3 - <<'PY'
import hashlib, json, os, pathlib, time
root = pathlib.Path(os.environ.get('REXTERM_DATA_DIR') or os.environ['DATA_DIR']) if 'DATA_DIR' in os.environ else pathlib.Path(os.environ['APP_DIR']) / 'data'
root.mkdir(parents=True, exist_ok=True)
user = os.environ['REXTERM_SET_AUTH_USER'].strip()
password = os.environ['REXTERM_SET_AUTH_PASS']
(root / 'auth_account.json').write_text(json.dumps({
    'username': user,
    'password_sha256': hashlib.sha256(password.encode()).hexdigest(),
    'updated_at': time.time(),
}, indent=2) + '\n')
(root / 'auth_sessions.json').write_text('{}\n')
print(f'Wrote server-side Rexterm account for user: {user}')
PY

if systemctl is-active --quiet rexterm-host 2>/dev/null; then
  systemctl restart rexterm-host
  echo "Restarted rexterm-host."
else
  echo "Account written. Restart Rexterm if it is already running."
fi
