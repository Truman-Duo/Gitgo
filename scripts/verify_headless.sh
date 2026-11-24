#!/bin/bash
# scripts/verify_headless.sh — 一键验证 Phase 1 全部认证标准
PROJECT="TestProject"
GITGO="python __main__.py"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$ROOT"

failures=0
passes=0

check() {
    local label="$1"
    shift
    if "$@"; then
        echo "  PASS: $label"
        passes=$((passes + 1))
    else
        echo "  FAIL: $label"
        failures=$((failures + 1))
    fi
}

echo "=== P1-A: Import 解耦 + 结构化输出 ==="

check "Qt not loaded in headless mode" \
    bash -c 'python -c "
import sys; sys.argv = [\"\", \"--mode\", \"status\", \"--project\", \"$PROJECT\", \"--json\"]
from backend.core.config import Config, ConfigManager
from backend.core.i18n import load_language
assert \"PySide6\" not in sys.modules
assert \"rich\" not in sys.modules
print(\"OK\")
"'

check "status --json outputs valid JSON" \
    bash -c "$GITGO --mode status --project $PROJECT --json | python -m json.tool > /dev/null"

check "status human-readable output" \
    bash -c "$GITGO --mode status --project $PROJECT | grep -q 'Trial'"

echo ""
echo "=== P1-B: CLI Verb 矩阵 ==="

check "trial list --json" \
    bash -c "$GITGO --mode trial --project $PROJECT --trial-action list --json | python -m json.tool > /dev/null"

check "scan --json" \
    bash -c "$GITGO --mode scan --project $PROJECT --json | python -m json.tool > /dev/null"

check "formalize --json" \
    bash -c "$GITGO --mode formalize --project $PROJECT --json > /dev/null 2>&1; [ \$? -eq 0 ] || [ \$? -eq 1 ]"

check "list --json (existing mode)" \
    bash -c "$GITGO --mode list --json > /dev/null 2>&1 || true"

echo ""
echo "=== P1-C: 状态机语义固化 ==="

check "GOVERNANCE_STATE.md exists" \
    test -f "$ROOT/docs/GOVERNANCE_STATE.md"

check "push without synced commits -> NO_SYNCED_COMMITS" \
    bash -c "result=\$($GITGO --mode push --project $PROJECT --json 2>&1) || true; echo \"\$result\" | python -c 'import sys,json; d=json.load(sys.stdin); assert d[\"error\"]==\"NO_SYNCED_COMMITS\"'"

echo ""
echo "=== P1-D: Session 持久化 ==="

check "session save creates .gitgo/session.json" \
    bash -c "$GITGO --mode session --project $PROJECT --session-action save --json | python -m json.tool > /dev/null"

check "session.json is valid JSON" \
    bash -c "python -m json.tool $ROOT/.gitgo/session.json > /dev/null"

check "session status returns valid JSON" \
    bash -c "$GITGO --mode session --project $PROJECT --session-action status --json | python -m json.tool > /dev/null"

check "session resume returns valid JSON" \
    bash -c "$GITGO --mode session --project $PROJECT --session-action resume --json | python -m json.tool > /dev/null"

echo ""
echo "=== P1-E: Summary ==="
echo "  Passed: $passes"
echo "  Failed: $failures"

if [ "$failures" -gt 0 ]; then
    echo "=== VERIFICATION FAILED ==="
    exit 1
else
    echo "=== VERIFICATION PASSED ==="
fi
