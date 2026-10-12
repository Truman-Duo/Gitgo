#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
PYTHON_BIN="${GITGO_PYTHON:-$ROOT/.venv/bin/python}"
BUN_BIN="$(command -v bun || true)"
BASE_REF="upstream/master"
VERIFY_ONLY=0
ALLOW_DIRTY=0
BUILD_ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --python) PYTHON_BIN="$2"; BUILD_ARGS+=("--python" "$2"); shift 2 ;;
    --bun) BUN_BIN="$2"; BUILD_ARGS+=("--bun" "$2"); shift 2 ;;
    --output|--archive|--codesign-identity|--ripgrep|--ripgrep-notices) BUILD_ARGS+=("$1" "$2"); shift 2 ;;
    --skip-codesign|--skip-archive) BUILD_ARGS+=("$1"); shift ;;
    --base-ref) BASE_REF="$2"; shift 2 ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    -h|--help)
      echo "Usage: packaging/release_macos.sh [--verify-only] [--allow-dirty] [build options]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ ! -x "$PYTHON_BIN" ]; then echo "Python runtime not found: $PYTHON_BIN" >&2; exit 1; fi
if [ ! -x "$BUN_BIN" ]; then echo "Bun runtime not found: $BUN_BIN" >&2; exit 1; fi

cd "$ROOT"
if [ "$ALLOW_DIRTY" -eq 0 ] && [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
  echo "Release worktree is not clean. Commit changes or use --allow-dirty for a rehearsal." >&2
  exit 1
fi
git rev-parse --verify "$BASE_REF^{commit}" >/dev/null
MESSAGE_PATTERN='^(\[GITGO-[0-9]+\] )?(feat|fix|docs|style|refactor|perf|test|chore)\([a-z0-9_-]+\): .{1,60}$'
while IFS= read -r message; do
  if [[ ! "$message" =~ $MESSAGE_PATTERN ]]; then
    echo "Invalid release commit message: $message" >&2
    exit 1
  fi
done < <(git log --no-merges --format=%s "$BASE_REF..HEAD")

"$PYTHON_BIN" -B -c 'from backend.core.storage.runtime import validate_sqlite_runtime; validate_sqlite_runtime()'
"$PYTHON_BIN" -B scripts/verify_release_privacy.py --root "$ROOT"
"$PYTHON_BIN" -B -m pytest tests -q
(
  cd cli/dashboard
  "$BUN_BIN" test
  "$BUN_BIN" run build
)
if [ "$VERIFY_ONLY" -eq 0 ]; then
  "$SCRIPT_DIR/build_macos.sh" --python "$PYTHON_BIN" --bun "$BUN_BIN" "${BUILD_ARGS[@]}"
fi
echo "macOS release verification passed. Nothing was pushed or installed."
