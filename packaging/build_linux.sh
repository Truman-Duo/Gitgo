#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
PYTHON_BIN="${GITGO_PYTHON:-}"
BUN_BIN=""
RIPGREP_BIN="${GITGO_RIPGREP_PATH:-}"
RIPGREP_NOTICES="${GITGO_RIPGREP_NOTICES:-}"
OUTPUT=""
ARCHIVE=""
SKIP_ARCHIVE=0

cleanup_bun_artifacts() {
  find "$ROOT" -maxdepth 1 -type f -name '.*.bun-build' -delete
}
trap cleanup_bun_artifacts EXIT

usage() {
  cat <<'EOF'
Usage: packaging/build_linux.sh [options]
  --python PATH    Python 3.12 build runtime (default: .venv/bin/python)
  --bun PATH       Bun executable (default: command -v bun)
  --ripgrep PATH   Standalone ripgrep >=14 binary
  --ripgrep-notices PATH  Complete third-party notices matching that binary
  --output DIR     Staged release directory
  --archive FILE   Output .tar.gz path
  --skip-archive   Stage files without creating a tarball
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --bun) BUN_BIN="$2"; shift 2 ;;
    --ripgrep) RIPGREP_BIN="$2"; shift 2 ;;
    --ripgrep-notices) RIPGREP_NOTICES="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --archive) ARCHIVE="$2"; shift 2 ;;
    --skip-archive) SKIP_ARCHIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$(uname -s)" != "Linux" ]; then
  echo "Linux packages must be built on Linux" >&2
  exit 1
fi
case "$(uname -m)" in x86_64|amd64) ARCH=x86_64 ;; aarch64|arm64) ARCH=aarch64 ;; *) echo "Unsupported Linux architecture: $(uname -m)" >&2; exit 1 ;; esac

if [ -z "$PYTHON_BIN" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PYTHON_BIN="$ROOT/.venv/bin/python"; else PYTHON_BIN="$(command -v python3.12 || command -v python3 || true)"; fi
fi
if [ -z "$BUN_BIN" ]; then BUN_BIN="$(command -v bun || true)"; fi
if [ ! -x "$PYTHON_BIN" ]; then echo "Python build runtime not found: $PYTHON_BIN" >&2; exit 1; fi
if [ ! -x "$BUN_BIN" ]; then echo "Bun runtime not found: $BUN_BIN" >&2; exit 1; fi
if [ -z "$RIPGREP_BIN" ]; then RIPGREP_BIN="$(command -v rg || true)"; fi
if [ ! -x "$RIPGREP_BIN" ] || [ ! -f "$RIPGREP_NOTICES" ]; then
  echo "Release requires standalone ripgrep and matching complete notices; pass --ripgrep and --ripgrep-notices" >&2
  exit 1
fi
"$PYTHON_BIN" -B "$ROOT/scripts/release_artifacts.py" --check

if [ -z "$OUTPUT" ]; then OUTPUT="$ROOT/dist-terminal/linux-$ARCH"; fi
STAGE="$(dirname "$OUTPUT")/$(basename "$OUTPUT")"
if [ -z "$ARCHIVE" ]; then ARCHIVE="$ROOT/dist-installer/gitgo-linux-$ARCH.tar.gz"; fi
case "$STAGE" in /|"$HOME"|"$ROOT") echo "Refusing unsafe output directory: $STAGE" >&2; exit 1 ;; esac

"$PYTHON_BIN" -B -c 'import sqlite3; from backend.core.storage.runtime import validate_sqlite_runtime; validate_sqlite_runtime(); print("Build runtime SQLite", sqlite3.sqlite_version)'
if ! "$PYTHON_BIN" -B -c 'import PyInstaller; print("PyInstaller", PyInstaller.__version__)'; then
  echo "PyInstaller is missing from $PYTHON_BIN; install it in the build environment" >&2
  exit 1
fi

if [ -e "$STAGE" ] && { [ ! -d "$STAGE" ] || [ -n "$(ls -A "$STAGE")" ]; }; then
  echo "Build output must be an empty directory; choose a new --output" >&2
  exit 1
fi
mkdir -p "$STAGE/internal"
(
  cd "$ROOT/cli/dashboard"
  "$BUN_BIN" test src/input/runtime.test.tsx src/backend/client.test.ts
)
"$BUN_BIN" build "$ROOT/cli/dashboard/src/main.tsx" --compile --outfile "$STAGE/gitgo"
(
  cd "$ROOT/cli/dashboard"
  "$BUN_BIN" run build
)
mkdir -p "$STAGE/dashboard/scripts" "$STAGE/dashboard/dist"
cp "$ROOT/cli/dashboard/scripts/launch.mjs" "$ROOT/cli/dashboard/scripts/runtime.mjs" "$STAGE/dashboard/scripts/"
cp "$ROOT/cli/dashboard/dist/cli.js" "$ROOT/cli/dashboard/dist/trace.js" "$STAGE/dashboard/dist/"

HOST_BUILD="$ROOT/build/terminal-host-linux-$ARCH"
"$PYTHON_BIN" -B -m PyInstaller \
  --noconfirm --clean --onedir \
  --name gitgo-host \
  --distpath "$STAGE/internal" \
  --workpath "$HOST_BUILD" \
  --specpath "$HOST_BUILD" \
  "$ROOT/backend/core/native_host_entry.py"

"$PYTHON_BIN" -B "$ROOT/scripts/stage_search_engine.py" \
  --binary "$RIPGREP_BIN" --notices "$RIPGREP_NOTICES" \
  --destination "$STAGE/internal/gitgo-host"

"$PYTHON_BIN" -B "$ROOT/scripts/render_product_manifest.py" \
  --source "$SCRIPT_DIR/product.json" \
  --platform linux \
  --architecture "$ARCH" \
  --output "$STAGE/product.json" \
  --shell-output "$STAGE/product.env"
cp "$SCRIPT_DIR/linux/install.sh" "$STAGE/install.sh"
cp "$SCRIPT_DIR/unix/installer-common.sh" "$STAGE/installer-common.sh"
cp "$SCRIPT_DIR/linux/uninstall.sh" "$STAGE/uninstall.sh"
cp "$ROOT/LICENSE.md" "$STAGE/LICENSE.md"
chmod 755 "$STAGE/gitgo" "$STAGE/internal/gitgo-host/gitgo-host" "$STAGE/install.sh" "$STAGE/uninstall.sh"
chmod 644 "$STAGE/product.json" "$STAGE/product.env" "$STAGE/LICENSE.md"

PRIMARY_COMMAND="$($PYTHON_BIN -B -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["primary_command"])' "$STAGE/product.json")"
while IFS= read -r alias; do
  [ -n "$alias" ] || continue
  ln -s "$PRIMARY_COMMAND" "$STAGE/$alias"
done < <("$PYTHON_BIN" -B -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1], encoding="utf-8"))["command_aliases"]))' "$STAGE/product.json")

"$PYTHON_BIN" -B "$ROOT/scripts/smoke_packaged_runtime.py" --host "$STAGE/internal/gitgo-host/gitgo-host"

if [ "$SKIP_ARCHIVE" -eq 0 ]; then
  "$PYTHON_BIN" -B "$ROOT/scripts/unix_release_manifest.py" --stage "$STAGE" --bun "$BUN_BIN"
  ARCHIVE_DIR="$(dirname "$ARCHIVE")"
  mkdir -p "$ARCHIVE_DIR"
  if [ -e "$ARCHIVE" ] || [ -e "$ARCHIVE.sha256" ]; then
    echo "Refusing to overwrite an existing archive; choose a new --archive" >&2
    exit 1
  fi
  tar -C "$(dirname "$STAGE")" -czf "$ARCHIVE" "$(basename "$STAGE")"
  (cd "$ARCHIVE_DIR" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")
  echo "Linux archive: $ARCHIVE"
  echo "SHA-256: $ARCHIVE.sha256"
fi
echo "Linux terminal release staged at $STAGE"
cleanup_bun_artifacts
trap - EXIT
