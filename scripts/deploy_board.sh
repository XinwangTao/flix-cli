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
tar czf "$TAR" -C "$SRC" src pyproject.toml README.md LICENSE assets scripts deploy

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

# Network may be unavailable (board on an isolated link): fall back to the
# existing install instead of failing the whole deploy.
if ! uv pip install --python "$DIR/.venv/bin/python" -e "$DIR" >/dev/null 2>&1; then
  echo "[deploy] install failed (no PyPI?), reusing existing environment"
  "$DIR/.venv/bin/python" -c "import flixcli" || {
    echo "[deploy] flixcli not importable and no network; aborting" >&2; exit 1; }
fi

sed -e "s|@DIR@|$DIR|g" -e "s|@WEB_PORT@|$WEB_PORT|g"     "$DIR/deploy/flixd.service" > /etc/systemd/system/flixd.service

systemctl daemon-reload
systemctl enable flixd >/dev/null 2>&1 || true
systemctl restart flixd
sleep 4
systemctl is-active flixd
curl -fsS -m 5 "http://127.0.0.1:$WEB_PORT/healthz"; echo
REMOTE

echo "[deploy] done: http://$HOST:$WEB_PORT/"
