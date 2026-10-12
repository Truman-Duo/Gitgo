#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
INSTALL_ROOT="$SCRIPT_DIR"
BIN_DIR="${GITGO_LINUX_BIN_DIR:-$HOME/.local/bin}"
KEEP_CONFIG=0
UPDATE_PATH=1
START_MARKER="# >>> Gitgo managed PATH >>>"
END_MARKER="# <<< Gitgo managed PATH <<<"

usage() {
  cat <<'EOF'
Usage: uninstall.sh [--bin-dir DIR] [--keep-config] [--no-path-update]
XDG runtime databases and every project directory are always preserved.
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
if [ ! -f "$INSTALL_ROOT/product.json" ] || [ ! -f "$INSTALL_ROOT/product.env" ]; then
  echo "Missing installed product marker" >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$INSTALL_ROOT/product.env"
if [ "${GITGO_PRODUCT_PLATFORM:-}" != "linux" ]; then
  echo "Installed product marker is not for Linux" >&2
  exit 1
fi
: "${GITGO_PRODUCT_COMMAND:?missing product command}"
GITGO_PRODUCT_ALIASES="${GITGO_PRODUCT_ALIASES:-}"
PRODUCT_ID="${GITGO_PRODUCT_ID:?}"
PLATFORM=linux
source "$INSTALL_ROOT/installer-common.sh"
INSTALL_ROOT="$(safe_path "$INSTALL_ROOT")"
BIN_DIR="$(safe_path "$BIN_DIR")"
owned_install || fail "Installed directory ownership cannot be confirmed"

# Preserve app, links, PATH and the last retry record on deletion failure.
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
remove_owned_link "$GITGO_PRODUCT_COMMAND"
for alias in $GITGO_PRODUCT_ALIASES; do remove_owned_link "$alias"; done

remove_managed_block() {
  profile="$1"
  [ -f "$profile" ] || return 0
  temporary="${profile}.gitgo-uninstall-$$"
  awk -v start="$START_MARKER" -v end="$END_MARKER" '
    $0 == start { skip = 1; next }
    $0 == end { skip = 0; next }
    !skip { print }
  ' "$profile" > "$temporary"
  mv "$temporary" "$profile"
}
if [ "$UPDATE_PATH" -eq 1 ]; then
  remove_managed_block "$HOME/.zprofile"
  remove_managed_block "$HOME/.profile"
  remove_managed_block "$HOME/.config/fish/conf.d/gitgo.fish"
fi

CONFIG_ROOT="$HOME/.gitgo"
SECRET_INDEX="$CONFIG_ROOT/provider_secrets.json"
if [ "$KEEP_CONFIG" -eq 0 ]; then
  rm -f "$CONFIG_ROOT/config.json" "$CONFIG_ROOT/commit-config.json" \
    "$CONFIG_ROOT/llm_config.json" "$SECRET_INDEX"
  rmdir "$CONFIG_ROOT" 2>/dev/null || true
fi

rm -rf "$INSTALL_ROOT"
echo "Gitgo was uninstalled. XDG runtime databases and project directories were preserved."
