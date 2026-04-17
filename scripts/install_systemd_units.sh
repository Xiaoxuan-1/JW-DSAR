#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC_DIR="$ROOT_DIR/deploy/systemd"
SYSTEMD_DIR="${JWDSAR_SYSTEMD_DIR:-$(systemctl show -p UnitPath --value | tr ':' '\n' | awk 'NR==1')}"
SECRETS_DIR="${JWDSAR_SECRETS_DIR:-$ROOT_DIR/secrets}"
KEY_FILE="$SECRETS_DIR/dashscope_api_key.txt"
SERVICE_USER="${JWDSAR_SERVICE_USER:-$USER}"
SERVICE_GROUP="${JWDSAR_SERVICE_GROUP:-$SERVICE_USER}"
PYTHON_BIN="${JWDSAR_PYTHON_BIN:-$(command -v python3)}"

if [[ -z "${PYTHON_BIN}" || ! -x "${PYTHON_BIN}" ]]; then
  echo "Python executable not found. Set JWDSAR_PYTHON_BIN explicitly."
  exit 1
fi

_escape_sed() {
  printf '%s' "$1" | sed -e 's/[&|]/\\&/g'
}

render_unit() {
  local src="$1"
  local dst="$2"
  local root_esc python_esc user_esc group_esc key_esc
  root_esc="$(_escape_sed "$ROOT_DIR")"
  python_esc="$(_escape_sed "$PYTHON_BIN")"
  user_esc="$(_escape_sed "$SERVICE_USER")"
  group_esc="$(_escape_sed "$SERVICE_GROUP")"
  key_esc="$(_escape_sed "$KEY_FILE")"
  sed \
    -e "s|__JWDSAR_ROOT__|$root_esc|g" \
    -e "s|__JWDSAR_PYTHON__|$python_esc|g" \
    -e "s|__JWDSAR_USER__|$user_esc|g" \
    -e "s|__JWDSAR_GROUP__|$group_esc|g" \
    -e "s|__JWDSAR_KEY_FILE__|$key_esc|g" \
    "$src" | sudo tee "$dst" >/dev/null
}

echo "[0/5] Ensure secrets directory exists..."
sudo install -d -m 750 "$SECRETS_DIR"
sudo chown root:"$SERVICE_GROUP" "$SECRETS_DIR" || true

if [[ ! -s "$KEY_FILE" ]]; then
  cat <<EOF

DashScope API Key file not found:
  $KEY_FILE

You can paste your key now (input will be visible). If you're in a shared terminal,
press Ctrl+C and create the file via a safer method (e.g., editor) instead.

EOF
  read -r -p "DASHSCOPE_API_KEY: " DASHSCOPE_API_KEY_PLAIN
  if [[ -z "${DASHSCOPE_API_KEY_PLAIN// /}" ]]; then
    echo "Empty key, skip writing $KEY_FILE."
  else
    sudo bash -c "umask 077; printf '%s' \"\$0\" > \"$KEY_FILE\"" "$DASHSCOPE_API_KEY_PLAIN"
    sudo chown root:"$SERVICE_GROUP" "$KEY_FILE" || true
    sudo chmod 640 "$KEY_FILE"
    echo "Wrote $KEY_FILE (owner root:$SERVICE_GROUP, mode 640)."
  fi
else
  echo "Key file exists: $KEY_FILE"
  sudo chown root:"$SERVICE_GROUP" "$KEY_FILE" || true
  sudo chmod 640 "$KEY_FILE" || true
fi

echo "[1/5] Copy systemd unit files..."
render_unit "$UNIT_SRC_DIR/jwdsar-web.service" "$SYSTEMD_DIR/jwdsar-web.service"
render_unit "$UNIT_SRC_DIR/jwdsar-generate.service" "$SYSTEMD_DIR/jwdsar-generate.service"
sudo cp "$UNIT_SRC_DIR/jwdsar-generate.timer" "$SYSTEMD_DIR/"

echo "[2/5] Reload systemd..."
sudo systemctl daemon-reload

echo "[3/5] Enable and start services..."
sudo systemctl enable --now jwdsar-web.service
sudo systemctl enable --now jwdsar-generate.timer

echo "[4/5] Show status..."
sudo systemctl --no-pager --full status jwdsar-web.service || true
sudo systemctl --no-pager --full status jwdsar-generate.timer || true

echo "[5/5] Next trigger:"
if command -v rg >/dev/null 2>&1; then
  systemctl list-timers --all | rg jwdsar-generate || true
else
  systemctl list-timers --all | grep -E "jwdsar-generate|UNIT" || true
fi

cat <<'EOF'

Done.
Useful commands:
  journalctl -u jwdsar-web -f
  journalctl -u jwdsar-generate --since today
  systemctl list-timers --all | rg jwdsar-generate   # or: ... | grep jwdsar-generate
  systemctl restart jwdsar-web.service                # reload new code after update

EOF
