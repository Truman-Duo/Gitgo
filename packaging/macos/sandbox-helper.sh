#!/bin/bash
# Explicit operator entry. The normal user installer does not enable this gate.
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
# A managed app belongs to its ordinary user. Running its active Host as root
# cannot acquire that user's installation admission/lease. Keep administration
# on the explicitly selected, independently verified release package instead.
if [ "$(basename "$SCRIPT_DIR")" = active ] && { [ -e "$SCRIPT_DIR/../control/registration.json" ] || [ -L "$SCRIPT_DIR/../control/registration.json" ]; }; then
  echo 'MACOS_HELPER_MANAGED_LOCATION: Helper administration requires the extracted release package, outside the user-managed installation.' >&2
  echo 'Run sandbox-helper.sh from that verified package and select its internal/gitgo-host bundle. Run Gitgo itself as your ordinary user.' >&2
  exit 2
fi
# Keep CLI dispatch inside the selected frozen Host. This removes the adjacent
# script input; it does not authenticate the full local package or enable it
# for production. The Host still checks root and ordinary managed admission.
exec "$SCRIPT_DIR/internal/gitgo-host/gitgo-host" --gitgo-internal-role macos-helper-admin "$@"
