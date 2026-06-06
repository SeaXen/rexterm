#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="${REXTERM_SERVICE_NAME:-rexterm-host}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
USER_NAME="${REXTERM_RUN_USER:-${SUDO_USER:-$(id -un)}}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This installer writes $SERVICE_PATH; run with sudo/root." >&2
  exit 1
fi

cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Rexterm host universal terminal
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/scripts/run-host.sh
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME.service"
systemctl --no-pager --full status "$SERVICE_NAME.service" || true
