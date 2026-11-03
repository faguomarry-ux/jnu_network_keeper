#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
PYTHON_BIN="$(command -v python3 || true)"
CONFIG_PATH=""
INTERVAL=60
JITTER=120
FOLLOW_DHCP_ARG=''
WIFI_INTERFACE=''
WIFI_CONNECTION=''
while (($#)); do
  if [[ "$1" == --follow-dhcp ]]; then FOLLOW_DHCP_ARG=--follow-dhcp; shift; continue; fi
  (($# >= 2)) || { echo "Missing value for $1" >&2; exit 2; }
  case "$1" in
    --user) SERVICE_USER="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --config) CONFIG_PATH="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --jitter) JITTER="$2"; shift 2 ;;
    --wifi-interface) WIFI_INTERFACE="$2"; shift 2 ;;
    --wifi-connection) WIFI_CONNECTION="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -n "$WIFI_INTERFACE" || -n "$WIFI_CONNECTION" ]]; then
  [[ -n "$WIFI_INTERFACE" && -n "$WIFI_CONNECTION" ]] || { echo 'Both Wi-Fi options are required.' >&2; exit 2; }
fi
[[ "$JITTER" =~ ^[0-9]+$ && "$JITTER" -le 900 ]] || { echo 'Jitter must be an integer from 0 to 900.' >&2; exit 2; }
if [[ -n "$FOLLOW_DHCP_ARG" && -z "$WIFI_INTERFACE" && ! -f /etc/jnu-network-guard.json ]]; then
  echo '--follow-dhcp requires a configured Wi-Fi guard or both Wi-Fi options.' >&2; exit 2
fi
[[ $EUID == 0 ]] || { echo 'Run this installer with sudo.' >&2; exit 1; }
[[ -x "$PYTHON_BIN" ]] || { echo 'Python 3.10+ is required.' >&2; exit 1; }
PYTHON_BIN="$(realpath -- "$PYTHON_BIN")"
ACCOUNT_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
[[ -n "$ACCOUNT_HOME" ]] || { echo 'Service account does not exist.' >&2; exit 1; }
CONFIG_PATH="${CONFIG_PATH:-${ACCOUNT_HOME}/.config/jnu_network_keeper/config.json}"
[[ -f "$CONFIG_PATH" ]] || { echo 'Run python3 -m jnu_network_keeper init as the service user first.' >&2; exit 1; }
CONFIG_PATH="$(realpath -- "$CONFIG_PATH")"
[[ "$INTERVAL" =~ ^[0-9]+$ && "$INTERVAL" -ge 10 ]] || { echo 'Interval must be an integer >= 10.' >&2; exit 2; }
runuser -u "$SERVICE_USER" -- "$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
# Validate saved credentials under the actual account, before enabling the service.
(cd "$PROJECT_DIR" && runuser -u "$SERVICE_USER" -- "$PYTHON_BIN" -c '
import sys
from pathlib import Path
from jnu_network_keeper.config import load_settings
s = load_settings(Path(sys.argv[1]))
s.interval = int(sys.argv[2])
s.interval_jitter = int(sys.argv[3])
s.validate()
assert s.username and s.password, "Missing saved credentials; run init first"
assert __import__("os").access(Path(sys.argv[1]).parent, __import__("os").W_OK), "Config directory must be writable by the service user"
' "$CONFIG_PATH" "$INTERVAL" "$JITTER")
if [[ -n "$WIFI_INTERFACE" ]]; then
  command -v nmcli >/dev/null || { echo 'Wi-Fi recovery requires NetworkManager/nmcli.' >&2; exit 1; }
  # The privileged helper uses a root-owned copy and a system Python, never a
  # user-writable interpreter/package at boot. -I ignores Python env overrides.
  /usr/bin/python3 -I -c 'import sys; assert sys.version_info >= (3,10), "System Python 3.10+ required for Wi-Fi guard"'
  install -D -o root -g root -m 0755 "$PROJECT_DIR/jnu_network_keeper/linux_network.py" /usr/local/libexec/jnu-network-guard.py
  /usr/bin/python3 -I /usr/local/libexec/jnu-network-guard.py prepare --interface "$WIFI_INTERFACE" --connection "$WIFI_CONNECTION"
elif [[ -f /etc/jnu-network-guard.json ]]; then
  # Refresh an already opted-in helper on normal upgrades as well.
  install -D -o root -g root -m 0755 "$PROJECT_DIR/jnu_network_keeper/linux_network.py" /usr/local/libexec/jnu-network-guard.py
fi
if [[ -f /etc/jnu-network-guard.json ]]; then
  install -o root -g root -m 0644 "$PROJECT_DIR/deploy/systemd/jnu-network-guard.service" /etc/systemd/system/jnu-network-guard.service
  install -o root -g root -m 0644 "$PROJECT_DIR/deploy/systemd/jnu-network-guard.timer" /etc/systemd/system/jnu-network-guard.timer
  systemd-analyze verify /etc/systemd/system/jnu-network-guard.service /etc/systemd/system/jnu-network-guard.timer
fi
unit_quote() {
  "$PYTHON_BIN" -c 'import json,sys; print(json.dumps(sys.argv[1],ensure_ascii=False).replace("%","%%"))' "$1"
}
UNIT_PATH=/etc/systemd/system/jnu-network-keeper.service
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Jinan University ePortal Network Keeper
Wants=network.target
After=network.target
StartLimitIntervalSec=0

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=600
TimeoutStartSec=60
User=${SERVICE_USER}
WorkingDirectory=$(unit_quote "$PROJECT_DIR")
Environment=PYTHONUNBUFFERED=1
ExecStart=$(unit_quote "$PYTHON_BIN") -m jnu_network_keeper run --config $(unit_quote "$CONFIG_PATH") --interval ${INTERVAL} --jitter ${JITTER} ${FOLLOW_DHCP_ARG}
Restart=always
RestartSec=30
TimeoutStopSec=45
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
LimitCORE=0

[Install]
WantedBy=multi-user.target
EOF
systemd-analyze verify "$UNIT_PATH"
systemctl daemon-reload
if [[ -n "$WIFI_INTERFACE" ]]; then
  systemctl enable --now jnu-network-guard.timer
fi
systemctl enable jnu-network-keeper.service
systemctl restart jnu-network-keeper.service
echo 'Installed and started jnu-network-keeper.service'
echo 'Logs: journalctl -u jnu-network-keeper -f'
