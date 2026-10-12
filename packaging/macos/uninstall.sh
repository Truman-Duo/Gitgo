#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
INSTALL_ROOT="$SCRIPT_DIR"
BIN_DIR="${GITGO_MACOS_BIN_DIR:-$HOME/.local/bin}"
KEEP_CONFIG=0
UPDATE_PATH=1
START_MARKER="# >>> Gitgo managed PATH >>>"
END_MARKER="# <<< Gitgo managed PATH <<<"

usage() {
  cat <<'EOF'
Usage: uninstall.sh [--bin-dir DIR] [--keep-config] [--no-path-update]
Runtime databases and every project directory are always preserved.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --keep-config) KEEP_CONFIG=1; shift ;;
    --no-path-update) UPDATE_PATH=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$INSTALL_ROOT" in ""|/|"$HOME") echo "Unsafe install root: $INSTALL_ROOT" >&2; exit 1 ;; esac
if [ ! -f "$INSTALL_ROOT/product.json" ]; then echo "Missing installed product marker" >&2; exit 1; fi
if [ "$(/usr/bin/plutil -extract platform raw -o - "$INSTALL_ROOT/product.json")" != "macos" ]; then
  echo "Installed product marker is not for macOS" >&2
  exit 1
fi
COMMAND="$(/usr/bin/plutil -extract primary_command raw -o - "$INSTALL_ROOT/product.json")"
PRODUCT_ID="$(/usr/bin/plutil -extract product_id raw -o - "$INSTALL_ROOT/product.json")"
PLATFORM=macos
source "$INSTALL_ROOT/installer-common.sh"
INSTALL_ROOT="$(safe_path "$INSTALL_ROOT")"
BIN_DIR="$(safe_path "$BIN_DIR")"
owned_install || fail "Installed directory ownership cannot be confirmed"

# Remove credentials before changing command links, PATH or deleting the app.
# Failed cleanup leaves the complete retry entry point and durable index intact.
CONFIG_ROOT="$HOME/.gitgo"
SECRET_INDEX="$CONFIG_ROOT/provider_secrets.json"
if [ "$KEEP_CONFIG" -eq 0 ]; then
  if [ -f "$SECRET_INDEX" ]; then
    HOST="$INSTALL_ROOT/internal/gitgo-host/gitgo-host"
    [ -x "$HOST" ] || fail "Native Host missing; credential cleanup cannot proceed"
    GITGO_LLM_CONFIG_PATH="$CONFIG_ROOT/llm_config.json" \
      GITGO_LLM_SECRET_PATH="$SECRET_INDEX" \
      "$HOST" --gitgo-internal-role credential-cleanup
  fi
fi

remove_owned_link() {
  name="$1"
  link="$BIN_DIR/$name"
  target="$INSTALL_ROOT/$name"
  if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then rm "$link"; fi
}
remove_owned_link "$COMMAND"
index=0
while alias="$(/usr/bin/plutil -extract "command_aliases.$index" raw -o - "$INSTALL_ROOT/product.json" 2>/dev/null)"; do
  [ -n "$alias" ] && remove_owned_link "$alias"
  index=$((index + 1))
done

if [ "$UPDATE_PATH" -eq 1 ]; then
  remove_owned_path_blocks
fi

CONFIG_ROOT="$HOME/.gitgo"
SECRET_INDEX="$CONFIG_ROOT/provider_secrets.json"
if [ "$KEEP_CONFIG" -eq 0 ]; then
  rm -f "$CONFIG_ROOT/config.json" "$CONFIG_ROOT/commit-config.json" \
    "$CONFIG_ROOT/llm_config.json" "$SECRET_INDEX"
  rmdir "$CONFIG_ROOT" 2>/dev/null || true
fi

rm -rf "$INSTALL_ROOT"
echo "Gitgo was uninstalled. Runtime databases and project directories were preserved."
