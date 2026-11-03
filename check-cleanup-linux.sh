#!/usr/bin/env bash
# Read-only verification; works without importing the keeper package.
set -euo pipefail
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  cat <<'EOF'
Usage: sudo bash check-cleanup-linux.sh [options]
  --receipt PATH       Cleanup receipt (default: /var/lib/jnu-network-keeper-cleanup/receipt.json)
  --forget-receipt     After a successful check, remove receipt and retained checker
  --python PATH        System Python 3.10+ (default: /usr/bin/python3)
Exit: 0=declared scope clean, 1=residue, 2=unknown/error, 130=interrupted.
After removing the source directory, use the retained standalone checker:
  sudo /usr/bin/python3 -I /var/lib/jnu-network-keeper-cleanup/check-cleanup.py check
EOF
  exit 0
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
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
exec "$PYTHON_BIN" -I "$SCRIPT_DIR/cleanup-linux.py" check "${ARGS[@]}"
