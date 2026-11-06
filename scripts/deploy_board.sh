#!/usr/bin/env bash
# Deploy flixd/RelayDesk to a Linux board (e.g. the Quectel QCM6490 devkit).
#
#   scripts/deploy_board.sh [ssh-host] [remote-dir] [web-port]
#
# Idempotent: first run bootstraps uv + Python 3.12 + venv, later runs only
# refresh the source tree and restart the service.
set -euo pipefail

HOST="${1:-quecpi-alpha}"
DIR="${2:-/opt/flixcli}"
WEB_PORT="${3:-8892}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
TAR="/tmp/flix-src-deploy.tgz"

echo "[deploy] packaging $SRC"
tar czf "$TAR" -C "$SRC" src pyproject.toml README.md LICENSE assets scripts

echo "[deploy] copying to $HOST:$DIR"
ssh -o BatchMode=yes "$HOST" "mkdir -p '$DIR'"
scp -o BatchMode=yes "$TAR" "$HOST:/tmp/flix-src-deploy.tgz" >/dev/null

ssh -o BatchMode=yes "$HOST" bash -s -- "$DIR" "$WEB_PORT" <<'REMOTE'
set -euo pipefail
DIR="$1"; WEB_PORT="$2"
export PATH="$HOME/.local/bin:$PATH"

tar xzf /tmp/flix-src-deploy.tgz -C "$DIR"

if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "[deploy] bootstrapping uv + python 3.12"
  command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  uv python install 3.12
  uv venv "$DIR/.venv" --python 3.12
fi

uv pip install --python "$DIR/.venv/bin/python" -e "$DIR" >/dev/null

cat > /etc/systemd/system/flixd.service <<UNIT
[Unit]
Description=flixd (RelayDesk daemon)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/flixd --web-port $WEB_PORT
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
MemoryMax=384M
TasksMax=256
Nice=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable flixd >/dev/null 2>&1 || true
systemctl restart flixd
sleep 4
systemctl is-active flixd
curl -fsS -m 5 "http://127.0.0.1:$WEB_PORT/healthz"; echo
REMOTE

echo "[deploy] done: http://$HOST:$WEB_PORT/"
