#!/usr/bin/env bash
# Standalone stdlib implementation; preview by default, explicit --apply to purge.
set -euo pipefail
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  cat <<'EOF'
Usage: sudo bash uninstall-linux.sh [options]
  --apply                 Execute cleanup (default: preview only)
  --user USER             Config owner (default: installed service/sudo caller)
  --config PATH           Additional/custom config; repeatable
  --report-dir PATH       Custom diagnostic report parent; repeatable
  --extra-file PATH       Explicit backup/custom log to remove; repeatable
  --project-dir PATH      Verified source directory (default: this directory)
  --remove-project        Also delete that verified source directory
  --keep-wifi-settings    Keep NetworkManager settings instead of restoring backup
  --forget-wifi           Delete the selected Wi-Fi profile (can disconnect remote access)
  --receipt PATH          Private cleanup receipt (default: /var/lib/jnu-network-keeper-cleanup/receipt.json)
  --python PATH           System Python 3.10+ (default: /usr/bin/python3)
Check again: sudo bash check-cleanup-linux.sh
No network logout is sent. Shared system journal/browser/unknown backups are not erased.
EOF
  exit 0
fi
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN=/usr/bin/python3
ARGS=()
while (($#)); do
  case "$1" in
    --python)
      (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || { echo 'Missing --python value.' >&2; exit 2; }
      PYTHON_BIN="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
[[ $(uname -s) == Linux ]] || { echo 'Linux is required.' >&2; exit 2; }
[[ -x "$PYTHON_BIN" ]] || { echo 'System Python 3.10+ is required; use --python.' >&2; exit 2; }
"$PYTHON_BIN" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || exit 2
export PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON_BIN" -I "$PROJECT_DIR/cleanup-linux.py" uninstall "${ARGS[@]}"
