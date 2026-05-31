# Gitgo Phase 1: Runtime Foundation

> 日期：2026-05-12 | 基于实际源码审计，逐模块验证 Qt 依赖

---

## 背景：架构重心转移

Gitgo 的原定位是"Git GUI 桌面工具"。随着 SyncSession 状态机、适配器体系、Trial 三叉工作流的落地，代码库已经具备了**AI-可用开发运行时（AI-usable development runtime）**的雏形——只是尚未暴露给 agent。

Phase 1 的目标不是"重写架构"，而是**增量硬化已有代码**，让 agent 能够通过 CLI 理解和操作 Gitgo 的全部工作流。

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
| 国际化 | `i18n.py` | 0 | `_tr()` / `load_language` |

**唯一的 Qt 耦合点：**

```
__main__.py:21  from gui_main import entry as gui_entry   ← 无条件顶层 import
    └─ gui_main.py:13  from PySide6.QtCore import Qt       ← Qt 被加载
```

这意味着 `python -m gitgo --mode sync` 即使不需要 GUI，也会加载整个 PySide6。
修复方法：把 `gui_entry` 和 `cui_entry` 的 import 移入对应的 `if args.mode == ...` 分支。
**代码改动量：约 6 行。**

---

## 阶段总览

| Stage | 名称 | 核心产出 | 预估 | 里程碑 |
|-------|------|---------|------|--------|
| P1-A | Import 解耦 + 结构化输出 | headless JSON CLI 可运行 | 1-2 周 | 首次 headless 运行 |
| P1-B | CLI verb 矩阵 | 完整 workflow 可 CLI 操作 | 2-3 周 | **Agent 可用** |
| P1-C | 状态机语义固化 | GOVERNANCE_STATE.md + 转移守卫 | 1-2 周 | 非法转移显式拒绝 |
| P1-D | Session 持久化 | .gitgo/session.json + 中断恢复 | 1-2 周 | 中断后可恢复 |
| P1-E | Headless 集成验证 | 全流程 headless 测试 | 1 周 | CI 可自动验证 |

**每个阶段独立可交付。P1-B 是第一个真实里程碑——agent 首次可以完全通过 CLI 操作 Gitgo。**

---

## 并行执行：P0 GUI Track

Phase 1 期间，GUI 侧的开发（右侧面板重构、主题增强、动画优化）作为**独立的 P0 track 并行推进**，不受 Phase 1 进度影响。

core/ 和 frontend/ 的耦合仅为 callback hooks（`on_log` / `on_progress` / `on_stage_changed`），改 GUI 不会拖累 core，改 core 不会阻塞 GUI。

```
P0 (GUI Track):   F-8 → RSB Widget → 动画系统 → ...
                       ↓ 独立执行，无依赖
Phase 1 (Runtime): P1-A → P1-B → P1-C/P1-D → P1-E
```

---

## P1-A: Import 解耦 + 结构化输出

### 目标1：Lazy import gui/cui entry

当前 `__main__.py` 第 19-22 行无条件 import：

```python
# 当前代码
from config import Config, ConfigManager
from cui_main import entry as cui_entry    # ← 无条件加载 Rich
from gui_main import entry as gui_entry    # ← 无条件加载 PySide6
from i18n import available_languages, load_language
```

修改后——gui_entry / cui_entry 只在对应 mode 分支中 import：

```python
# 修改后
from config import Config, ConfigManager
from i18n import available_languages, load_language
# 删除顶层 import cui_entry / gui_entry

# 在 mode 分支中：
if args.mode == "gui":
    from gui_main import entry as gui_entry
    gui_entry()
elif args.mode == "cui":
    from cui_main import entry as cui_entry
    cui_entry()
```

**改动量：`__main__.py` 约 6 行。**

### 目标2：SyncSession.status_dict()

在 `SyncSession` 上增加机器可读的状态输出：

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

在 `__main__.py` 增加 `--mode status`，触发 trial check（轻量）但不触发 scan（重）：

```bash
gitgo status --project X        # 人类可读
gitgo status --project X --json # 结构化 JSON
```

### P1-A 认证标准

- [ ] `python -m gitgo --mode sync --project X` 运行时 PySide6 不被 import
  - 验证：`python -v -m gitgo --mode sync --project X 2>&1 | grep -i pyside` 无输出
- [ ] `python -m gitgo --mode status --project X --json` 输出合法 JSON
- [ ] `python -m gitgo --mode sync --project X --json` 输出结构化的操作结果
- [ ] 现有 GUI 模式功能不受影响
- [ ] 现有 CUI 模式功能不受影响

---

## P1-B: CLI Verb 矩阵 ← Agent 可用里程碑

P1-B 完成后，agent 可以完全通过 CLI 操作 Gitgo 的所有工作流。这是 Phase 1 的第一个真实里程碑。

### 已有 CLI verb

- `--mode sync --project X [--message M]` — 全流程 headless 同步
- `--mode daemon --project X [--skip-push] [--force-on-warning]` — SyncSession 全流程
- `--mode list` — 列出项目
- `--mode history` — 查看历史
- `--mode status --project X` — **P1-A 新增**，项目状态查询

### 新增 CLI verbs

每个 verb 是 SyncSession 一个 step 方法的薄包装。

#### `trial` 子命令组

```bash
gitgo trial list --project X [--json]              # 列出 incoming changes
gitgo trial accept --project X --index N [--json]   # 接受 → cherry-pick 到 release
gitgo trial promote --project X --index N [--json]  # 提升 → fetch 到 workspace
gitgo trial discard --project X --index N [--json]  # 丢弃
```

#### `formalize`

```bash
gitgo formalize --project X [--indices 0,2,3] [--message "..."] [--json]
```

从选中的 workspace commit 创建 formal commit。

#### `scan`

```bash
gitgo scan --project X [--json]
```

仅扫描变更文件，不同步。映射到 `session.step_scan()`。

#### `push`

```bash
gitgo push --project X [--skip-security] [--json]
```

仅推送已有的 synced formal commit。映射到 `session.step_push()`。

### 核心辅助函数（所有 verb 共用）

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

### P1-B 认证标准

- [ ] 所有 workflow 操作可通过 CLI 完成（无需 GUI）
- [ ] SSH 环境下可运行
  - 验证：`ssh localhost "cd /path/to/gitgo && python -m gitgo --mode status --project X --json"`
- [ ] 每个命令有 `--json` flag
- [ ] `--help` 输出完整可用
- [ ] Agent 可自主完成一次完整的 formalize → sync → push 流程

---

## P1-C: 状态机语义固化

### 双层状态机设计

Gitgo 有两层状态，服务于不同消费者：

| 层 | 定义 | 位置 | 消费者 |
|----|------|------|--------|
| **Operational state** | 系统当前在执行什么操作 | `SessionStage` 枚举 (10 states) | GUI/CUI 进度指示 |
| **Governance state** | 变更单元处于生命周期的哪个治理阶段 | GOVERNANCE_STATE.md + 从字段计算 | Agent / CLI 决策 |

**关键设计决策：** governance state 不在 SyncSession 上作为新字段存储，而是从现有字段（entries/commits/formal_commits 的 synced/pushed + trial incoming 状态）**计算得出**。这避免了双层状态的存储冲突。

### Governance 状态机（Fork 结构）

```
                    ┌─────────┐
                    │ 外部输入  │
                    └────┬────┘
                         │
                         ▼
                   ┌──────────┐
                   │  TRIAL    │ (待治理输入层)
                   │  审查·决策  │
                   └────┬─────┘
                        │ 三叉决策
              ┌─────────┼─────────┐
              │         │         │
              ▼         ▼         ▼
          accept    promote   discard
              │         │         │
              │         ▼         │
              │    ┌─────────┐    │
              │    │WORKSPACE │    │
              │    │ (继续开发) │    │
              │    └─────────┘    │
              │         │         │
              ▼         │         │
         ┌─────────┐    │         │
         │FORMALIZED│◄──┘         │
         │ (语义单元) │              │
         └────┬─────┘              │
              │                    │
              ▼                    │
       ┌─────────────┐             │
       │RELEASE_READY│             │
       │  (已同步)     │             │
       └──────┬──────┘             │
              │                    │
              ▼                    │
       ┌──────────┐                │
       │PUBLISHED  │                │
       │ (已发布·终态)│               │
       └──────────┘                │
                                   │
         WORKSPACE ──→ formalize ──┘
           ↑                       
           │ 自由开发（高熵）
           │
```

### 合法转移

```
workspace  ──→ trial            (promote: git fetch trial → workspace incoming/*)
workspace  ──→ formalized       (formalize: 选中 workspace commit → formal commit)
trial      ──→ curated          (accept / promote / discard 三叉决策)
curated    ──→ formalized       (accept 的变更整合为 formal commit)
formalized ──→ release_ready    (sync: 同步到 release 仓库)
release_ready → published       (push: 推送到远程)
```

### 非法转移（必须显式拒绝）

```
trial      ──→ published       错误: "trial cannot publish directly"
workspace  ──→ published       错误: "no formalized boundary — must formalize first"
workspace  ──→ release_ready   错误: "no formalized boundary — must formalize first"
formalized ──→ published       错误: "must sync before publish"
curated    ──→ published       错误: "must formalize after accept"
```

### 转移守卫

在 CLI 层（不修改 SyncSession）增加前置校验。已有的守卫：

- `step_push()` 已有：检查 formal commit 是否已 synced
- `step_triage_incoming("accept")` 已有：检查 release 仓库已配置

新增守卫示例：

```python
def _cmd_push(cfg, project_name, skip_security, json_output):
    session = _init_session(cfg, project_name)
    ready = [fc for fc in session.formal_commits if fc.synced and not fc.pushed]
    if not ready:
        err = {"error": "NO_SYNCED_COMMITS",
               "message": "no synced formal commits to push — must sync first"}
        print(json.dumps(err) if json_output else err["message"])
        sys.exit(1)
    # ... proceed with push
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

### 持久化策略：Checkpoint 而非 Snapshot

**设计约定：** 完整恢复（entries/commits 列表）需要重新 scan，但 formal_commits 的恢复意味着中断后不需要重新 formalize。这是 pragmatic 的 checkpoint 策略——**只持久化治理关键状态（formal_commits）**，操作性状态（entries/commits）通过重新扫描获得。

`entries_summary` 字段仅用于**人类查看** session 文件的概要信息，不用于恢复逻辑。

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

- `save_session()` → 写 `.gitgo/session.json`
- `load_session()` (classmethod) → 从文件恢复，返回 `SyncSession | None`

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

### 验证脚本

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

| 维度 | 原 Phase 1 计划 | 本版 |
|------|----------------|------|
| P1-1 "Core Extraction" | 新建 gitgo_core/ 目录，2-6 周 | **已存在** — core/ 已是纯 Python；仅需修 6 行 import |
| P1-2 "State Machine" | 从零定义状态机 | SyncSession.stage 已有 10 个 operational state；新增 governance state 为互补层 |
| P1-3 "Structured API" | 新建结构化接口 | SyncSession.status_dict() 增量添加；已有 step_*() 返回值可包装 |
| P1-4 "First-Class CLI" | 从零建 CLI | _cmd_sync / _cmd_daemon 已存在；补全 trial/formalize/scan/push verb |
| P1-5 "Persistence" | 从零建持久化 | ConfigManager + HistoryManager 已有持久化模式；新增 session.json |
| P1-6 "Headless Validation" | 最终验证 | 合并为 P1-E，内容一致 |

---

## 核心原则

1. **不要为了"看起来对"而重构已经对的代码。** SyncSession 已经是 headless runtime。Phase 1 的目标不是"创建 runtime"，而是"把已有 runtime 暴露给 agent"。

2. **增量交付，每阶段独立可验收。** P1-A 完成后 headless JSON CLI 已可用。P1-B 完成后 agent 可完全通过 CLI 操作 Gitgo。

3. **P0 GUI 可并行推进。** core/ 和 frontend/ 的耦合仅为 callback hooks，改 GUI 不会拖累 core，改 core 不会阻塞 GUI。

4. **Governance state 和 Operational state 是两个不同层。** 不混在一个数据结构里。前者从现有字段计算得出，后者是 SyncSession.stage。

5. **Phase 1 完成后的真实里程碑：** agent 可以通过 `gitgo status --json` 理解项目治理状态，通过 `gitgo trial list/accept --json` 操作三叉决策，通过 `gitgo formalize/sync/push --json` 驱动完整的 headless workflow。此时 Gitgo 从"桌面工具"变为"AI-usable development runtime"。

6. **P1-B 是第一个可被 agent 使用的里程碑。** P1-A 提供了 `status --json` 和 headless sync，但 P1-B 才补全了 trial/formalize/scan/push 的完整 CLI 矩阵。P1-B 完成后 agent 即可参与治理工作流。

---

## 相关文件

- `docs/iterations/CommitBox_v2_重构设计.md` — GUI 侧 CommitBox 重构（P0 track，已完成）
- `docs/VERSION.md` — 版本记录
- `docs/HANDOFF.md` — 交接文档
- `docs/iterations/README.md` — 迭代计划总览
