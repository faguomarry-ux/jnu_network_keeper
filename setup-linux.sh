#!/usr/bin/env bash
# Run: bash setup-linux.sh
# Prompts for campus credentials, then installs/enables a systemd boot service.
# No campus password belongs in this script or in command-line arguments.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash setup-linux.sh [options]
  --user USER             Service/config owner (default: sudo caller/current user)
  --python PATH           Python 3.10+ executable (default: python3 on PATH)
  --config PATH           Absolute config path (default: service user's config)
  --interval SECONDS      Probe interval, 10..900 (default: 60)
  --jitter SECONDS        Additional random delay, 0..900 (default: 120)
  --follow-dhcp           Follow source IPv4 of the configured Wi-Fi (opt-in)
  --gateway-ip IPV4       Optional actual campus gateway IPv4 (not this PC's IP)
  --source-address IPV4   Optional THIS Linux server's campus IPv4
  --service NAME         Optional service name selected in the portal
  --wifi-interface IFACE Optional NetworkManager Wi-Fi device to recover
  --wifi-connection UUID Saved Wi-Fi profile UUID; required with --wifi-interface
  --reuse-config          Use existing credentials without prompting
  --accounts              Interactively configure 1..3 ordered accounts (stop keeper first)
  --failover-threshold N  Failed submitted logins per slot, used with --accounts (default: 100000)
  --help                  Show this help
EOF
}
die() { echo "Error: $*" >&2; exit 2; }
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
PYTHON_BIN="$(command -v python3 || true)"
CONFIG_PATH=''
INTERVAL=60
JITTER=120
RECOVERY_ARGS=()
REUSE_CONFIG=0
INIT_COMMAND=init
ACCOUNT_ARGS=()
NETWORK_ARGS=()
WIFI_ARGS=()
WIFI_INTERFACE=''
WIFI_CONNECTION=''
while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --reuse-config) REUSE_CONFIG=1; shift ;;
    --accounts) INIT_COMMAND=init-accounts; shift ;;
    --follow-dhcp) RECOVERY_ARGS=(--follow-dhcp); shift ;;
    --user|--python|--config|--interval|--jitter|--gateway-ip|--source-address|--service|--wifi-interface|--wifi-connection|--failover-threshold)
      (($# >= 2)) || die "Missing value for $1"
      case "$1" in
        --user) SERVICE_USER="$2" ;;
        --python) PYTHON_BIN="$2" ;;
        --config) CONFIG_PATH="$2" ;;
        --interval) INTERVAL="$2" ;;
        --jitter) JITTER="$2" ;;
        --wifi-interface) WIFI_INTERFACE="$2" ;;
        --wifi-connection) WIFI_CONNECTION="$2" ;;
        --failover-threshold) ACCOUNT_ARGS=(--failover-threshold "$2") ;;
        *) NETWORK_ARGS+=("$1=$2") ;;
      esac
      shift 2 ;;
    *) die "Unknown option: $1" ;;
  esac
done
if ((REUSE_CONFIG)) && [[ "$INIT_COMMAND" == init-accounts ]]; then
  die '--accounts cannot be combined with --reuse-config.'
fi
if ((${#ACCOUNT_ARGS[@]})); then
  [[ "$INIT_COMMAND" == init-accounts && "${ACCOUNT_ARGS[1]}" =~ ^[0-9]+$ && "${ACCOUNT_ARGS[1]}" -ge 1 && "${ACCOUNT_ARGS[1]}" -le 2147483647 ]] || die '--failover-threshold requires --accounts and an integer from 1 to 2147483647.'
fi
if [[ -n "$WIFI_INTERFACE" || -n "$WIFI_CONNECTION" ]]; then
  [[ -n "$WIFI_INTERFACE" && -n "$WIFI_CONNECTION" ]] || die 'Use --wifi-interface and --wifi-connection together.'
  WIFI_ARGS=(--wifi-interface "$WIFI_INTERFACE" --wifi-connection "$WIFI_CONNECTION")
fi
[[ "$JITTER" =~ ^[0-9]+$ && "$JITTER" -le 900 ]] || die 'Jitter must be an integer from 0 to 900.'
if ((${#RECOVERY_ARGS[@]})) && [[ -z "$WIFI_INTERFACE" && ! -f /etc/jnu-network-guard.json ]]; then
  die '--follow-dhcp requires a configured Wi-Fi guard or both Wi-Fi options.'
fi
[[ $(uname -s) == Linux ]] || die 'This installer requires Linux with systemd.'
for REQUIRED in systemctl systemd-analyze getent realpath; do
  command -v "$REQUIRED" >/dev/null || die "Required command missing: $REQUIRED"
done
[[ -d /run/systemd/system ]] || die 'systemd is not running as the system service manager.'
[[ "$INTERVAL" =~ ^[0-9]+$ && "$INTERVAL" -ge 10 && "$INTERVAL" -le 900 ]] || die 'Interval must be an integer from 10 to 900.'
[[ -x "$PYTHON_BIN" ]] || die 'Python 3.10+ not found; install it or use --python.'
PYTHON_BIN="$(realpath -- "$PYTHON_BIN")"
ACCOUNT_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
[[ -n "$ACCOUNT_HOME" ]] || die "Unknown service user: $SERVICE_USER"
if [[ $EUID != 0 && "$SERVICE_USER" != "$(id -un)" ]]; then
  die 'Selecting another service user requires: sudo bash setup-linux.sh --user USER'
fi
if [[ $EUID != 0 ]]; then
  command -v sudo >/dev/null || die 'sudo is required to install the systemd service.'
  # Validate sudo now, before prompting for and writing the campus credentials.
  sudo -v
fi
as_service_user() {
  if [[ $EUID == 0 ]]; then
    runuser -u "$SERVICE_USER" -- env HOME="$ACCOUNT_HOME" "$@"
  else
    "$@"
  fi
}
cd "$PROJECT_DIR"
as_service_user "$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
if [[ -z "$CONFIG_PATH" ]]; then
  if [[ $EUID != 0 || "$SERVICE_USER" == "$(id -un)" ]]; then
    CONFIG_PATH="$(as_service_user "$PYTHON_BIN" -c 'from jnu_network_keeper.config import default_config_path; print(default_config_path())')"
  else
    # sudo's caller may have XDG variables belonging to root. Resolve under the
    # target account with only its HOME; an explicit --config always wins.
    CONFIG_PATH="${ACCOUNT_HOME}/.config/jnu_network_keeper/config.json"
  fi
fi
CONFIG_PATH="$(realpath -m -- "$CONFIG_PATH")"
if ((REUSE_CONFIG)); then
  [[ -f "$CONFIG_PATH" ]] || die 'No saved config; omit --reuse-config to enter credentials.'
  ((${#NETWORK_ARGS[@]} == 0)) || die 'Omit --reuse-config when changing gateway, source address or service.'
else
  as_service_user "$PYTHON_BIN" -m jnu_network_keeper "$INIT_COMMAND" --config "$CONFIG_PATH" --interval "$INTERVAL" --jitter "$JITTER" "${ACCOUNT_ARGS[@]}" "${RECOVERY_ARGS[@]}" "${NETWORK_ARGS[@]}"
fi
INSTALL_ARGS=("$PROJECT_DIR/deploy/systemd/install.sh" --user "$SERVICE_USER" --python "$PYTHON_BIN" --config "$CONFIG_PATH" --interval "$INTERVAL" --jitter "$JITTER" "${RECOVERY_ARGS[@]}" "${WIFI_ARGS[@]}")
if [[ $EUID == 0 ]]; then
  bash "${INSTALL_ARGS[@]}"
else
  sudo bash "${INSTALL_ARGS[@]}"
fi
echo 'Setup complete: starts at boot, without an SSH login.'
echo 'Status: sudo systemctl status jnu-network-keeper --no-pager'
echo 'Logs:   sudo journalctl -u jnu-network-keeper -f'
