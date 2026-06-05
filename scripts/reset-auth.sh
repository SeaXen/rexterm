#!/bin/bash
set -euo pipefail

DATA_DIR="${REXTERM_DATA_DIR:-/data}"
RECOVERY_FILE="$DATA_DIR/auth_recovery_code"

if [[ ! -f "$RECOVERY_FILE" ]]; then
  echo "No active recovery code. Generating new one..."
else
  echo "Replacing existing recovery code..."
fi

CODE=$(python3 -c "
import secrets, json, os, time
code = secrets.token_urlsafe(24)
payload = {'code': code, 'created_at': time.time()}
with open('$RECOVERY_FILE', 'w') as f:
    json.dump(payload, f)
os.chmod('$RECOVERY_FILE', 0o600)
print(code)
")

echo
echo "=== ONE-TIME RECOVERY CODE (shown only once) ==="
echo "$CODE"
echo
echo "Use this code on the login page → 'Forgot password?' to reset username/password."
echo "The code is single-use and will be deleted after successful recovery."
echo
echo "File location (root only): $RECOVERY_FILE"
