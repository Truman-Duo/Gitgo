"""Realistic data pools for TestDataFactory.

每个池子里的值都是从真实项目中提取的，不是随机字符串。
"""

# ── 文件路径池 ────────────────────────────────────────────

FILE_PATHS = [
    "backend/core/auth.py",
    "backend/core/session.py",
    "backend/core/sync_session.py",
    "backend/core/config.py",
    "backend/core/history.py",
    "backend/core/contract.py",
    "backend/core/llm_config.py",
    "backend/core/daemon/__init__.py",
    "backend/core/daemon/client.py",
    "backend/core/loop/executor.py",
    "backend/core/loop/llm.py",
    "backend/core/loop/manager.py",
    "backend/core/loop/gate.py",
    "backend/core/loop/tools.py",
    "backend/core/policy/__init__.py",
    "backend/core/policy/lessons.py",
    "backend/core/policy/contract.py",
    "backend/core/knowledge/models.py",
    "backend/core/knowledge/manager.py",
    "backend/core/knowledge/harvest.py",
    "backend/core/dispatch/dispatcher.py",
    "backend/adapters/local_file_adapter.py",
    "backend/adapters/local_git_runner.py",
    "cli/dashboard/src/components/App.tsx",
    "cli/dashboard/src/components/CommandBar.tsx",
    "cli/dashboard/src/hooks/useChat.ts",
    "cli/dashboard/src/main.tsx",
    "tests/test_auth.py",
    "tests/test_contract.py",
    "tests/test_lesson.py",
    "docs/README.md",
    "docs/VERSION.md",
    "mcp_server.py",
    "mcp_tools/loop.py",
    ".gitignore",
    "pyproject.toml",
]

# ── Lesson 规则模板池 ─────────────────────────────────────

LESSON_RULES = [
    "if modifying {file}, then must run {tool} first",
    "when {file} changes, must update {other} too",
    "if {file} is touched, then must verify contract signatures",
    "before editing {file}, must {tool} the workspace",
    "when adding new {category} to {file}, must add corresponding tests",
    "if {file} shows repeated modifications, consider refactoring",
    "禁止直接修改 {file} without prior scan check",
    "必须先在 {file} 上运行 {tool} 才能进行 sync",
    "if {file} imports change, must rebuild dependency graph",
    "when {file} is deleted, must update all {other} references",
    "if {action} in {file}, then must {tool} before {action2}",
    "禁止在没有 {tool} 的情况下直接 {action} {file}",
    "{file} is a hot file — consider splitting into smaller modules",
    "when migrating {file} to {other}, must update CI config",
    "if {category} logic changes in {file}, must notify {other}",
]

# ── 工具名池 ──────────────────────────────────────────────

TOOL_NAMES = [
    "scan", "status", "formalize", "sync", "push",
    "recall_grep", "recall_semantic", "recall_rag",
    "trial_list", "trial_triage", "reject", "round_complete",
    "cache_stats", "loop_status",
]

ACTIONS = [
    "modifying", "deleting", "refactoring", "adding",
    "renaming", "merging", "splitting", "extracting",
]

CATEGORIES = [
    "feature", "middleware", "adapter", "test", "config",
    "migration", "plugin", "API endpoint", "database schema",
]

# ── 信号类型池 ────────────────────────────────────────────

SIGNAL_TYPES = [
    "lesson_trigger",
    "contract_drift",
    "identity_integrity",
    "dependency_chain",
    "policy_warning_consecutive",
    "rejection_chain",
    "tool_failed",
]

# ── Commit 类型池 ─────────────────────────────────────────

COMMIT_TYPES = ["feat", "fix", "docs", "refactor", "test", "chore", "perf", "ci"]

COMMIT_SCOPES = ["auth", "core", "ui", "daemon", "policy", "knowledge",
                 "sync", "dispatch", "loop", "dashboard", "cli", "tests"]

# ── Agent 角色池 ──────────────────────────────────────────

AGENT_ROLES = ["planner", "executor", "reviewer", "worker", "observer"]

# ── 常见关键词 ────────────────────────────────────────────

SEARCH_QUERIES = [
    "auth", "login", "scan", "sync", "contract", "drift",
    "database", "config", "test", "import", "refactor",
    "migration", "security", "performance", "API", "dashboard",
]

# ── Task descriptions ─────────────────────────────────────

TASK_DESCRIPTIONS = [
    "修复 auth 模块的登录安全问题",
    "重构 session 管理为异步模式",
    "更新 dashboard 的命令栏 IME 支持",
    "实现新的 contract drift 检测规则",
    "优化 daemon 的文件监控性能",
    "添加 memory snapshot 的增量备份",
    "修复 formalize 时的编号重复 bug",
    "迁移 test_auth 到 pytest 参数化",
    "增加 sync 前的依赖图检查",
    "重构 knowledge harvest 的 LLM prompt",
]

# ── Severity 池 ───────────────────────────────────────────

SEVERITIES = ["low", "medium", "high", "critical"]
SEVERITY_WEIGHTS = [1, 3, 2, 1]  # medium 最常见

# ── HistoryManager operation 类型（13 种）──────────────────

HISTORY_OPERATIONS = [
    "scan", "formalize", "sync", "push",
    "triage_accept", "triage_promote", "triage_discard",
    "delete_formal", "dissolve_formal",
    "policy_check_result", "governance_drift",
    "unprocessed_signal", "fact_derived",
]
