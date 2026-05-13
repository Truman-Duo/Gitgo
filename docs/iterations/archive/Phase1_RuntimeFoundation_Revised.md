# Gitgo Phase 1: Runtime Foundation — 修订版

> 修订日期：2026-05-12 | 基于实际源码审计，逐模块验证 Qt 依赖

---

## 修订说明

原计划基于一个假设——"核心被 Qt 包裹，需要 2-6 周完成 Core Extraction"。
源码审计后的事实是：**core/ 已经是纯 Python，没有任何 Qt 依赖。** 真正的耦合仅在一处。

本修订版将计划从"架构重写"调整为"增量硬化"——在现有代码基础上逐层加固，
每个阶段独立可交付，不阻塞其他工作。

---

## 源码审计结论

以下模块已经 Qt-free，无需"抽取"——它们是现有的独立 runtime：

| 模块 | 文件 | Qt 依赖 | 结论 |
|------|------|---------|------|
| 状态机 | `core/sync_session.py` (648行) | 0 | 已是纯 Python 状态机 |
| 操作层 | `core/operations/` (4文件) | 0 | scan/sync/git/models 全是纯函数 |
| 数据模型 | `models/__init__.py` (114行) | 0 | RepoNode/FileAccess/IncomingChange |
| 适配器 | `adapters/` (6文件) | 0 | Local/SSH FileAdapter + GitRunner |
| 后端 | `backend/` (2文件) | 0 | FileScanner + submitter |
| 配置 | `config.py` (241行) | 0 | Config/ConfigManager |
| 插件 | `plugin_loader.py` + `plugins/` | 0 | 7 hook 点 |
| 历史 | `history.py` | 0 | HistoryManager |
| 国际化 | `i18n.py` | 0 | _tr() / load_language |

**唯一的 Qt 耦合点：**

```
__main__.py:21  from gui_main import entry as gui_entry   ← 无条件顶层 import
    └─ gui_main.py:13  from PySide6.QtCore import Qt       ← Qt 被加载
```

这意味着 `python -m gitgo --mode sync` 即使不需要 GUI，也会加载整个 PySide6。
修复方法：把 `gui_entry` 和 `cui_entry` 的 import 移入对应的 `if args.mode == ...` 分支。
**代码改动量：约 6 行。**

**现有的 headless 入口（已存在，但缺结构化输出）：**

- `_cmd_sync()` — `__main__.py:43-144`：scan → compare → commit → sync，使用底层函数
- `_cmd_daemon()` — `__main__.py:147-178`：使用 `SyncSession.run_full_workflow()`

两个函数已经可以在无 Qt 环境下工作（如果修好 import），但输出全是 `print()` 人类文本。

---

## 修订后阶段结构

| Stage | 名称 | 核心产出 | 预估 |
|-------|------|---------|------|
| P1-A | Import 解耦 + 结构化输出 | headless JSON CLI 可运行 | 1-2 周 |
| P1-B | CLI verb 矩阵 | 完整 workflow 可 CLI 操作 | 2-3 周 |
| P1-C | 状态机语义固化 | GOVERNANCE_STATE.md + governance state 守卫 | 1-2 周 |
| P1-D | Session 持久化 | .gitgo/session.json + 中断恢复 | 1-2 周 |
| P1-E | Headless 集成验证 | 全流程 headless 测试 | 1 周 |

**每个阶段独立可交付，完成后 Gitgo 即可在对应层面被 agent 使用。**

---

## P1-A: Import 解耦 + 结构化输出

### 当前状态

`__main__.py` 第 19-22 行无条件 import `gui_main` 和 `cui_main`：

```python
# __main__.py:19-22 — 当前代码
from config import Config, ConfigManager
from cui_main import entry as cui_entry    # ← 无条件加载 Rich
from gui_main import entry as gui_entry    # ← 无条件加载 PySide6
from i18n import available_languages, load_language
```

`_cmd_sync` (第 43 行) 和 `_cmd_daemon` (第 147 行) 已有完整 headless 逻辑，
但因为上述顶层 import，它们运行时 PySide6 已经被加载了。

### 目标1：Lazy import gui/cui entry

```python
# 修改后 — gui_entry / cui_entry 只在对应 mode 分支中 import
from config import Config, ConfigManager
from i18n import available_languages, load_language
# 删除: from cui_main import entry as cui_entry
# 删除: from gui_main import entry as gui_entry
```

然后在各 mode 分支中：

```python
if args.mode == "gui":
    from gui_main import entry as gui_entry   # 仅在 GUI 模式加载 Qt
    gui_entry()
elif args.mode == "cui":
    from cui_main import entry as cui_entry   # 仅在 CUI 模式加载 Rich
    cui_entry()
```

**改动量：`__main__.py` 约 6 行。**

### 目标2：SyncSession.status_dict()

在 `SyncSession` 上增加一个方法，输出机器可读的当前项目状态：

```python
# core/sync_session.py — 新增方法
def status_dict(self) -> dict:
    """返回机器可读的当前项目状态。"""
    from models import TrialAction
    trial_pending = sum(1 for c in self.incoming_changes
                        if c.triage == TrialAction.PENDING)
    return {
        "project": self.project.name,
        "stage": self.stage.name,
        "workspace": {
            "path": str(self.workspace_path),
            "entries_total": len(self.entries),
            "entries_changed": sum(1 for e in self.entries
                                   if e.status != "same" and e.selected),
        },
        "commits": {
            "workspace_total": len(self.commits),
            "formal_total": len(self.formal_commits),
            "formal_synced": sum(1 for fc in self.formal_commits if fc.synced),
            "formal_pushed": sum(1 for fc in self.formal_commits if fc.pushed),
        },
        "trial": {
            "configured": self.project.trial is not None
                          and bool(self.project.trial.file_access.path),
            "pending": trial_pending,
            "total": len(self.incoming_changes),
        },
    }
```

### 目标3：`gitgo status --json`

在 `__main__.py` 增加 `--mode status`：

```python
import json

def _cmd_status(cfg: Config, project_name: str, json_output: bool):
    matched = [p for p in cfg.projects if p.name == project_name]
    if not matched:
        if json_output:
            print(json.dumps({"error": "PROJECT_NOT_FOUND", "name": project_name}))
        else:
            print(f"错误: 未找到项目「{project_name}」")
        sys.exit(1)
    from core.sync_session import SyncSession
    session = SyncSession(matched[0], cfg)
    # status 命令触发 trial check（轻量）但不触发 scan（重）
    session.step_check_trial()
    if json_output:
        print(json.dumps(session.status_dict(), indent=2, ensure_ascii=False))
    else:
        _print_status_human(session)
```

同时更新 `_cmd_sync` 和 `_cmd_daemon`，增加 `--json` flag，输出结构化结果。

### P1-A 认证标准

- [ ] `python -m gitgo --mode sync --project X` 运行时 PySide6 不被 import
  - 验证：`python -v -m gitgo --mode sync --project X 2>&1 | grep -i pyside` 无输出
- [ ] `python -m gitgo --mode status --project X --json` 输出合法 JSON
- [ ] `python -m gitgo --mode sync --project X --json` 输出结构化的操作结果
- [ ] 现有 GUI 模式功能不受影响
- [ ] 现有 CUI 模式功能不受影响

---

## P1-B: CLI Verb 矩阵

### 当前状态

已有 CLI verb：
- `--mode sync --project X [--message M]` — 全流程 headless 同步
- `--mode daemon --project X [--skip-push] [--force-on-warning]` — 使用 SyncSession 全流程
- `--mode list` — 列出项目
- `--mode history` — 查看历史
- `--mode config` — 等效于 list

缺失的 verb（对应 SyncSession 已有的 step 方法）：
- Trial: `trial list`, `trial accept`, `trial promote`, `trial discard`
- Formalize: `formalize` (从 workspace commit 创建 formal commit)
- Scan: `scan` (仅扫描，不同步)
- Push: `push` (仅推送已有的 synced formal commit)

### 新增 CLI verbs

每个 verb 是 SyncSession 一个 step 方法的薄包装。核心辅助函数：

```python
def _init_session(cfg, project_name, with_scan=False):
    """初始化 SyncSession — 所有 CLI verb 共用。"""
    matched = [p for p in cfg.projects if p.name == project_name]
    if not matched:
        print(json.dumps({"error": "PROJECT_NOT_FOUND", "name": project_name}))
        sys.exit(1)
    from core.sync_session import SyncSession
    session = SyncSession(matched[0], cfg)
    if with_scan:
        session.step_scan()
        session.step_load_commits()
    session.step_check_trial()
    return session
```

#### trial list

```bash
gitgo trial list --project X [--json]
```

映射到 `session.step_check_trial()` → 返回 `list[IncomingChange]`。

#### trial accept / promote / discard

```bash
gitgo trial accept --project X --index N [--json]
gitgo trial promote --project X --index N [--json]
gitgo trial discard --project X --index N [--json]
```

映射到 `session.step_triage_incoming(index, action)` → 返回成功/失败 + 结构化错误。

#### formalize

```bash
gitgo formalize --project X [--indices 0,2,3] [--message "..."] [--json]
```

映射到 `session.step_create_formal_commit(selected_indices, message)` → 返回 FormalCommit 信息。

#### scan

```bash
gitgo scan --project X [--json]
```

映射到 `session.step_scan()` → 返回变更文件列表。

#### push

```bash
gitgo push --project X [--skip-security] [--json]
```

映射到 `session.step_push()` → 返回 (success, warnings)。

### P1-B 认证标准

- [ ] 所有 workflow 操作可通过 CLI 完成（无需 GUI）
- [ ] SSH 环境下可运行
  - 验证：`ssh localhost "cd /path/to/gitgo && python -m gitgo --mode status --project X --json"`
- [ ] 每个命令有 `--json` flag
- [ ] `--help` 输出完整可用

---

## P1-C: 状态机语义固化

### 当前状态

`SessionStage` 枚举定义了 10 个 **operational state**（IDLE / TRIAL_CHECKING / SCANNING / SELECTING / COMMITTING / SYNCING / PUSHING / FAILED / TRIAL_REVIEWING / INCOMING_CONFIRMING）。

这是"当前在执行什么操作"。Phase 1 计划要求的 governance state（workspace → trial → formalized → release_ready → published）是一个**不同的抽象层**——它描述的是"一个变更单元处于生命周期的哪个治理阶段"，而非"系统当前在执行什么操作"。

**关键设计决策**：governance state 不在 SyncSession 上作为新字段存储，而是在 CLI 层和 `status_dict()` 中根据现有字段（entries/commits/formal_commits 的 synced/pushed 状态 + trial incoming 状态）**计算得出**。这避免了 operational state 和 governance state 的存储冲突——两者服务于不同的消费者，不应混在一个数据结构里。

### 产出1：docs/GOVERNANCE_STATE.md

定义 governance 层状态机：

```
## 状态定义

### workspace (高熵开发区)
变更由开发者或 AI 在 workspace 中自由产生。
无约束，无编号，无正式 message 要求。

### trial (待治理输入层)
变更已进入 trial 仓库，等待人类审查和决策。
关键安全约束：不可直接从 trial 进入 published。

### curated (trial 已决策)
trial 中的变更已经过三叉决策：
- accepted  → 变更已 cherry-pick 到 release，等待 formal commit 整合
- promoted  → 变更已 fetch 到 workspace，等待开发者继续工作
- discarded → 已忽略

### formalized (语义单元已建立)
变更已整合为 formal commit（有编号、有语义分组、有结构化的 message）。
尚未 sync 到 release 仓库。

### release_ready (已同步到正式仓库)
Formal commit 已 sync 到 release 仓库（备份仓库已有该 commit）。
尚未 push 到远程。

### published (已发布，不可逆)
Formal commit 已 push 到远程仓库。
这是 governance 的最终态，不可逆。

## 合法转移

workspace ──→ trial           (promote: git fetch trial → workspace incoming/*)
workspace ──→ formalized      (formalize: 选中 workspace commit，生成 formal commit)
trial ──────→ curated         (accept / promote / discard)
curated ────→ formalized      (accept 的变更被整合为 formal commit)
formalized ─→ release_ready   (sync: 同步到 release 仓库)
release_ready → published     (push: 推送到远程)

## 非法转移（必须显式拒绝）

trial ──────→ published       错误: "trial cannot publish directly"
workspace ──→ published       错误: "no formalized boundary — must formalize first"
workspace ──→ release_ready   错误: "no formalized boundary — must formalize first"
formalized ─→ published       错误: "must sync before publish"
curated ────→ published       错误: "must formalize after accept"
```

### 产出2：转移守卫

在对应的 `step_*()` 方法中增加前置校验。已有的守卫（不需要改动）：

- `step_push()` 已有：检查 formal commit 是否已 synced（`if fc.synced and not fc.pushed`）
- `step_triage_incoming("accept")` 已有：检查 release 仓库已配置

新增的守卫（在 CLI 层，不修改 SyncSession）：

```python
# __main__.py — _cmd_push() 中的 governance guard
def _cmd_push(cfg, project_name, skip_security, json_output):
    session = _init_session(cfg, project_name)
    # Governance guard: 检查是否有已 synced 但未 pushed 的 commit
    ready = [fc for fc in session.formal_commits if fc.synced and not fc.pushed]
    if not ready:
        err = {"error": "NO_SYNCED_COMMITS",
               "message": "no synced formal commits to push — must sync first"}
        print(json.dumps(err) if json_output else err["message"])
        sys.exit(1)
    ...
```

### P1-C 认证标准

- [ ] `docs/GOVERNANCE_STATE.md` 文件存在，内容完整
- [ ] 每个非法转移有显式的错误输出（结构化 JSON 含 error code）
- [ ] 非法转移不会 silently continue

---

## P1-D: Session 持久化与恢复

### 当前状态

已持久化的：
- `ConfigManager.save()` → `sync_base`、`last_known_head`、`processed_incoming`
- `HistoryManager` → 每次同步的历史记录

缺失的：
- SyncSession 的运行时状态（entries、commits、formal_commits、stage）不持久化
- 中断后无法恢复（如 `formalize` 后 crash，formal_commits 列表丢失）

### 持久化格式

```json
// .gitgo/session.json — 每个项目独立
{
  "project": "MyProject",
  "updated_at": "2026-05-12T15:30:00",
  "stage": "IDLE",
  "entries_summary": {"total": 45, "new": 3, "modified": 5},
  "workspace_commits_since_base": 5,
  "formal_commits": [
    {
      "message": "[MYAPP-1] feat: add SSH adapter",
      "number": 1,
      "prefix": "MYAPP",
      "synced": true,
      "pushed": false,
      "source_indices": [0, 2],
      "created_at": "2026-05-12T10:00:00"
    }
  ],
  "incoming_summary": {"total": 3, "pending": 1},
  "last_operation": {
    "op": "formalize",
    "status": "success",
    "timestamp": "2026-05-12T10:00:00"
  }
}
```

### 实现

在 `SyncSession` 上增加两个方法（约 60 行）：

```python
# core/sync_session.py — 新增方法

def save_session(self) -> Path:
    """持久化当前 session 状态到 .gitgo/session.json"""
    import json
    from datetime import datetime
    from models import TrialAction
    session_dir = self.workspace_path / ".gitgo"
    session_dir.mkdir(exist_ok=True)
    data = {
        "project": self.project.name,
        "updated_at": datetime.now().isoformat(),
        "stage": self.stage.name,
        "entries_summary": {
            "total": len(self.entries),
            "new": sum(1 for e in self.entries if e.status == "new"),
            "modified": sum(1 for e in self.entries if e.status == "modified"),
        },
        "workspace_commits_since_base": len(self.commits),
        "formal_commits": [
            {
                "message": fc.message,
                "number": fc.number,
                "prefix": fc.prefix,
                "synced": fc.synced,
                "pushed": fc.pushed,
                "source_indices": list(fc.source_indices),
                "created_at": fc.created_at,
            }
            for fc in self.formal_commits
        ],
        "incoming_summary": {
            "total": len(self.incoming_changes),
            "pending": sum(1 for c in self.incoming_changes
                          if c.triage == TrialAction.PENDING),
        },
        "last_operation": self._last_op,  # step_*() 方法写这个字段
    }
    path = session_dir / "session.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path

@classmethod
def load_session(cls, project: ProjectConfig, config: Config) -> SyncSession | None:
    """从 .gitgo/session.json 恢复 session。返回 None 如果文件不存在。"""
    import json
    path = Path(project.workspace_path) / ".gitgo" / "session.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    session = cls(project, config)
    session.stage = SessionStage[data.get("stage", "IDLE")]
    for fc_data in data.get("formal_commits", []):
        fc = FormalCommit(
            message=fc_data["message"],
            number=fc_data["number"],
            prefix=fc_data["prefix"],
            synced=fc_data.get("synced", False),
            pushed=fc_data.get("pushed", False),
            source_indices=set(fc_data.get("source_indices", [])),
            created_at=fc_data.get("created_at", ""),
        )
        session.formal_commits.append(fc)
    return session
```

**设计约定**：完整恢复（entries/commits 列表）需要重新 scan，但 formal_commits 的恢复意味着中断后不需要重新 formalize。这是 pragmatic 的 checkpoint 策略——只持久化 governance 关键状态（formal_commits），操作性状态（entries/commits）通过重新扫描获得。

### 自动持久化时机

- `step_create_formal_commit()` 成功后 → `self.save_session()`
- `step_sync()` 成功后 → `self.save_session()`
- `step_triage_incoming()` 成功后 → `self.save_session()`
- `step_push()` 成功后 → `self.save_session()`

### CLI

```bash
gitgo session save --project X     # 显式持久化当前状态
gitgo session resume --project X   # 从 .gitgo/session.json 恢复
gitgo session status --project X   # 查看已保存的 session 摘要
```

### P1-D 认证标准

- [ ] key step 操作后自动调 `save_session()`
- [ ] formal commit 中断后可恢复（不需要重新选择 workspace commit 和编辑 message）
- [ ] session 文件是合法 JSON
- [ ] session 文件不依赖 GUI memory（`SyncSession.__dict__` 之外的任何 Qt 对象引用）

---

## P1-E: Headless 集成验证

### 验证矩阵

| # | 测试 | 方法 | 通过标准 |
|---|------|------|---------|
| 1 | Qt 隔离 | `python -v -m gitgo --mode sync --project X 2>&1 \| grep -c PySide6` | 输出 0 |
| 2 | SSH headless | `ssh localhost "cd ... && python -m gitgo --mode status --project X --json"` | 返回合法 JSON |
| 3 | 全流程 JSON | `python -m gitgo --mode daemon --project X --json` | 每一行输出可 JSON.parse |
| 4 | status JSON | `python -m gitgo --mode status --project X --json \| python -m json.tool` | 无解析错误 |
| 5 | Session 恢复 | kill 进程 → `python -m gitgo --mode session resume --project X` | formal_commits 存在 |
| 6 | CLI 全覆盖 | 依次执行 trial list / formalize / scan / push 等 verb | 无异常退出 |
| 7 | 非法转移拒绝 | `gitgo push --project X` 在无 synced commit 时执行 | 返回错误 JSON，非静默通过 |

### 认证脚本

```bash
#!/bin/bash
# scripts/verify_headless.sh — 一键验证 Phase 1 全部认证标准
set -e
PROJECT="TestProject"
GITGO="python -m gitgo"

echo "=== P1-A: Qt 隔离 ==="
QT_COUNT=$($GITGO --mode sync --project "$PROJECT" -v 2>&1 | grep -ci pyside || true)
[ "$QT_COUNT" -eq 0 ] && echo "PASS: Qt not loaded" || echo "FAIL: Qt loaded $QT_COUNT times"

echo "=== P1-A: JSON 输出 ==="
$GITGO --mode status --project "$PROJECT" --json | python -m json.tool > /dev/null && echo "PASS" || echo "FAIL"

echo "=== P1-B: CLI verb 覆盖 ==="
$GITGO --mode trial list --project "$PROJECT" --json > /dev/null 2>&1 && echo "PASS: trial list"
$GITGO --mode scan --project "$PROJECT" --json > /dev/null 2>&1 && echo "PASS: scan"
$GITGO --mode list --json > /dev/null 2>&1 && echo "PASS: list"

echo "=== P1-D: Session 持久化 ==="
$GITGO --mode session save --project "$PROJECT" --json > /dev/null 2>&1 && echo "PASS: save"
$GITGO --mode session status --project "$PROJECT" --json > /dev/null 2>&1 && echo "PASS: status"

echo "=== P1-E: All checks complete ==="
```

### P1-E 认证标准

- [ ] 全部 7 项验证通过
- [ ] 验证脚本 `scripts/verify_headless.sh` 存在且可执行
- [ ] CI 可自动运行验证脚本

---

## 与旧版计划的关键差异

| 维度 | 原 Phase 1 计划 | 修订版 |
|------|----------------|--------|
| P1-1 "Core Extraction" | 新建 gitgo_core/ 目录，2-6 周 | **已存在** — core/ 已是纯 Python；仅需修 6 行 import |
| P1-2 "State Machine" | 从零定义状态机 | SyncSession.stage 已有 10 个 operational state；新增 governance state 为互补层 |
| P1-3 "Structured API" | 新建结构化接口 | SyncSession.status_dict() 增量添加；已有 step_*() 返回值可包装 |
| P1-4 "First-Class CLI" | 从零建 CLI | _cmd_sync / _cmd_daemon 已存在；补全 trial/formalize/scan/push verb |
| P1-5 "Persistence" | 从零建持久化 | ConfigManager + HistoryManager 已有持久化模式；新增 session.json |
| P1-6 "Headless Validation" | 最终验证 | 合并为 P1-E，内容一致 |

## 核心原则

1. **不要为了"看起来对"而重构已经对的代码。** SyncSession 已经是 headless runtime。Phase 1 的目标不是"创建 runtime"，而是"把已有 runtime 暴露给 agent"。

2. **增量交付，每阶段独立可验收。** P1-A 完成后 headless JSON CLI 已可用，P1-B 完成后 CLI 全覆盖，以此类推。

3. **GUI 可并行推进。** core/ 和 frontend/ 的耦合仅为 callback hooks（on_log / on_progress / on_stage_changed），改 GUI 不会拖累 core，改 core 不会阻塞 GUI。

4. **Governmental state 和 Operational state 是两个不同层。** 不混在一个数据结构里。前者从现有字段计算得出，后者是 SyncSession.stage。

5. **Phase 1 完成后的真实里程碑：** agent 可以通过 `gitgo status --json` 理解项目治理状态，通过 `gitgo trial list/accept --json` 操作三叉决策，通过 `gitgo formalize/sync/push --json` 驱动完整的 headless workflow。此时 Gitgo 从"桌面工具"变为"AI-usable development runtime"。
