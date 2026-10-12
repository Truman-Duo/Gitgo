#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
INSTALL_ROOT="${GITGO_MACOS_INSTALL_ROOT:-$HOME/Library/Application Support/Gitgo/app}"
BIN_DIR="${GITGO_MACOS_BIN_DIR:-$HOME/.local/bin}"
UPDATE_PATH=1
START_MARKER="# >>> Gitgo managed PATH >>>"
END_MARKER="# <<< Gitgo managed PATH <<<"
BASH_PROFILE=.bash_profile
while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-root) INSTALL_ROOT="$2"; shift 2 ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --no-path-update) UPDATE_PATH=0; shift ;;
    -h|--help) echo 'Usage: install.sh [--install-root DIR] [--bin-dir DIR] [--no-path-update]'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -f "$SCRIPT_DIR/product.json" ] || { echo 'Release contract missing' >&2; exit 1; }
PLATFORM="$(/usr/bin/plutil -extract platform raw -o - "$SCRIPT_DIR/product.json")"
PRODUCT_ID="$(/usr/bin/plutil -extract product_id raw -o - "$SCRIPT_DIR/product.json")"
ARCHITECTURE="$(/usr/bin/plutil -extract architecture raw -o - "$SCRIPT_DIR/product.json")"
COMMAND="$(/usr/bin/plutil -extract primary_command raw -o - "$SCRIPT_DIR/product.json")"
ALIASES=(); index=0
while alias="$(/usr/bin/plutil -extract "command_aliases.$index" raw -o - "$SCRIPT_DIR/product.json" 2>/dev/null)"; do
  ALIASES+=("$alias"); index=$((index + 1))
done
[ "$PLATFORM" = macos ] && [ "$ARCHITECTURE" = "$(uname -m)" ] || { echo 'Package platform/architecture mismatch' >&2; exit 1; }
[ -x "$SCRIPT_DIR/internal/gitgo-host/gitgo-host" ] || { echo 'Native Host missing' >&2; exit 1; }
source "$SCRIPT_DIR/installer-common.sh"
install_transaction
