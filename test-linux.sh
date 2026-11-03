#!/usr/bin/env bash
# Read-only deployment checks + offline tests. Never disconnects or logs in.
set -euo pipefail
usage() {
  cat <<'EOF'
Usage: bash test-linux.sh [options]
  --python PATH        Python 3.10+ (default: python3)
  --user USER          Check as this service/config owner (root can switch user)
  --config PATH        Config path (default: installed service, then user default)
  --report-dir PATH    Parent directory for a new private report directory
  --offline            Skip external HTTP/portal GET probes; keep local checks
  --unit-only          Run only offline automated tests
  --skip-unit-tests    Run deployment diagnostics without the automated suite
  --help               Show help
Exit codes: 0=passed, 1=warnings, 2=failures/invalid options, 130=interrupted.
No sudo is requested automatically. No login POST, service restart, Wi-Fi change,
credential update, authentication-limit reset, or disruptive fault test is run.
EOF
}
die() { echo "Error: $*" >&2; exit 2; }
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$(command -v python3 || true)"
SERVICE_USER=''
UNIT_ONLY=0
ARGS=()
while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --unit-only) UNIT_ONLY=1; ARGS+=("$1"); shift ;;
    --offline|--skip-unit-tests) ARGS+=("$1"); shift ;;
    --python|--user|--config|--report-dir)
      (($# >= 2)) || die "Missing value for $1"
      [[ -n "$2" && "$2" != --* ]] || die "Missing value for $1"
      case "$1" in
        --python) PYTHON_BIN="$2" ;;
        --user) SERVICE_USER="$2" ;;
        *) ARGS+=("$1" "$2") ;;
      esac
      shift 2 ;;
    *) die "Unknown option: $1" ;;
  esac
done
[[ $(uname -s) == Linux ]] || die 'This entry point requires Linux. Use --help for usage.'
[[ -x "$PYTHON_BIN" ]] || die 'Python 3.10+ not found; use --python /absolute/path/python3.'
if [[ -z "$SERVICE_USER" && $UNIT_ONLY == 0 ]] && command -v systemctl >/dev/null && command -v timeout >/dev/null; then
  SERVICE_USER="$(timeout 10s systemctl show -p User --value jnu-network-keeper.service 2>/dev/null || true)"
fi
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
[[ "$SERVICE_USER" != -* && "$SERVICE_USER" != *$'\n'* ]] || die 'Invalid service user.'
ACCOUNT_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
[[ -n "$ACCOUNT_HOME" ]] || die 'Service user does not exist.'
cd "$PROJECT_DIR"
if [[ "$SERVICE_USER" != "$(id -un)" ]]; then
  [[ $EUID == 0 ]] || die 'The service belongs to another user. Use sudo bash test-linux.sh --user SERVICE_USER, or specify your own --user and --config.'
  command -v runuser >/dev/null || die 'runuser is required to select the service account.'
  runuser -u "$SERVICE_USER" -- "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || die 'Python 3.10+ is required.'
  exec runuser -u "$SERVICE_USER" -- env -u XDG_CONFIG_HOME -u JNU_NET_CONFIG HOME="$ACCOUNT_HOME" PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON_BIN" -m jnu_network_keeper.diagnostics "${ARGS[@]}"
fi
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || die 'Python 3.10+ is required.'
export PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON_BIN" -m jnu_network_keeper.diagnostics "${ARGS[@]}"
