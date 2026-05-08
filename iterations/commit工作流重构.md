# 迭代：Commit 工作流重构 — Box + 选择合并 + 分离 Push

> 优先级 当前 | 对应 iterations/README.md 中的 [当前] 条目

## 目标

解决当前 commit 整合体验的问题：

| 现状 | 目标 |
|---|---|
| workspace commit 纯文本列表 | 每个 commit 一个可视化 box，按时间排列 |
| 自动合并所有 commit，用户只能编辑文字 | 用户鼠标多选 box → 合成正式 commit |
| 合并后直接生成模板文字到文本框 | 先弹出编辑器输入正式信息 → 确认后才生成正式 box |
| sync 按钮同时做 add+commit | sync（add+commit）与 push 分离为两步 |
| 无 push 功能 | 正式 commit box 存在后才可 push |

## 用户工作流

```
┌─────────────────────────────────────────┐
│  Workspace Commits（按时间 ↑）           │
│  ┌─────────────────────────────────────┐│
│  │ ┌─ box ──────────────────────────┐ ││
│  │ │ feat: add login page           │ ││  ← 可选中（点击切换 / Ctrl+多选）
│  │ │ 2026-05-08 by Duo              │ ││
│  │ └────────────────────────────────┘ ││
│  │ ┌─ box ──────────────────────────┐ ││
│  │ │ fix: fix login page crash      │ ││
│  │ │ 2026-05-08 by Duo              │ ││
│  │ └────────────────────────────────┘ ││
│  │ ┌─ box ──────────────────────────┐ ││
│  │ │ refactor: extract auth module  │ ││
│  │ │ 2026-05-07 by Duo              │ ││
│  │ └────────────────────────────────┘ ││
│  └─────────────────────────────────────┘│
│                                        │
│  [合并选中为正式 Commit]                 │
│                                        │
├─────────────────────────────────────────┤
│  Formal Commits（已合并的正式 commit）    │
│  ┌─ box ──────────────────────────┐    │
│  │ [ANBM-5] feat: add login ...   │    │  ← 可选中作为目标
│  │ Synced: 2026-05-08             │    │
│  └────────────────────────────────┘    │
│                                        │
├─────────────────────────────────────────┤
│  [Sync 到备份仓库]  [Push 到 GitHub]    │
│   ↑ 有选中 formal box 才可点            │
│                                        │   ↑ 有已 sync 的 formal box 才可点
└─────────────────────────────────────────┘
```

## 改动范围

### 1. gui_main.py — Commit 区域重构

**当前 commit 区域（"步骤 2: Commit 整合"）** 包含：
- `commit_list`（QPlainTextEdit，只读，显示 commit 列表）
- `commit_msg`（QTextEdit，可编辑，显示模板）

**改为：**

- **Workspace Commits 面板**（左侧或上方）：
  - 自定义 `CommitBox` 控件（QFrame + QVBoxLayout）:
    - 显示 type、subject、author、date（从 `CommitInfo` 取）
    - 选中状态通过背景色/border 变化反馈
    - 支持鼠标点击切换选中（Ctrl 多选 / Shift 范围选）
  - 用 `QScrollArea` + `QVBoxLayout` 按时间倒序排列
  - "刷新"按钮 + "合并选中"按钮

- **Formal Commits 面板**（右侧或下方）：
  - 同样 box 布局，但风格区分（如蓝色边框）
  - 每个 formal box 显示：`[PREFIX-N] type: subject`
  - 点击选中作为当前操作目标
  - "删除"按钮（移除 formal box，不删备份仓库）

- **合并交互**：
  1. 选中 N 个 workspace box → 点击"合并选中"
  2. 弹出 `QDialog` 包含一个 `QTextEdit`，预填合并模板
  3. 用户编辑确认 → 关闭 dialog → 新的 formal commit box 出现在 Formal 面板
  4. 合并操作不写入 git，只作为本地状态

### 2. 执行区域 — Sync 与 Push 分离

当前执行区域只有一个 "开始同步" 按钮（add + commit 一步到位）。

**改为：**
- **Sync 按钮**：git add + git commit（当前行为），将选中的 formal commit 写入备份仓库
  - 需要先选中一个 formal box
- **Push 按钮**：git push
  - 需要备份仓库已有 formal commit（即 sync 成功后）
  - 灰色不可点 → 有已 sync 的 formal box 后点亮

### 3. 进度条 — 每步操作实时反馈

当前进度条只在文件扫描/对比时有用，Sync 和 Push 操作缺乏进度反馈。

**改为每个操作都有独立进度反馈：**

- **Sync 进度**：
  - 进度条显示当前阶段文字：`拷贝文件 (3/12)` → `git add ...` → `git commit ...`
  - 百分比随文件拷贝进度走
  - 完成后进度条变绿（成功）或变红（失败）

- **Push 进度**：
  - 进度条显示：`git push 到 origin...`
  - 完成后显示推送结果（分支/commit）
  - 失败时显示错误信息

- **合并操作**（合并 workspace box 为 formal box）：
  - 操作轻量无需进度条，但完成/失败通过状态栏提示

当前 `core.py` 的 `sync_to_backup` 已有 `progress_callback` 机制，Push 的新函数沿用同一模式。

### 4. gui_main.py — 数据模型扩展

当前 `MainWindow` 只有 `self.commits: list[CommitInfo]`。

**新增：**
```python
class FormalCommit:
    message: str          # 完整 commit message
    number: int           # [PREFIX-N] 中的 N
    prefix: str           # PREFIX
    synced: bool = False  # 是否已 commit 到备份仓库
    pushed: bool = False  # 是否已 push

# MainWindow 中新增
self.formal_commits: list[FormalCommit] = []
self.selected_commits: set[int] = set()       # 选中的 workspace box 索引
self.selected_formal: Optional[int] = None     # 选中的 formal box 索引
```

### 5. core.py — 新增 push_to_remote

```python
def push_to_backup(backup: str | Path) -> bool:
    """执行 git push（推送到备份仓库的远程 origin）"""
```

当前 `sync_to_backup` 只做 add + commit，保持不变。

### 6. CUI 模式（cui_main.py）

CUI 暂不改动（非当前迭代目标），保持现有 commit 交互不变。

## 不做的事

- 不涉及多项目管理（那是 P0 迭代的范畴）
- 不改 config.py 配置结构
- 不改文件对比/扫描逻辑
- CU 暂不改动

## 验证方式

1. 启动 GUI，扫描后点"刷新 Commit" → 看到 workspace commit box 列表
2. 点击 box 切换选中（背景色变化）
3. Ctrl+点击 多选多个 box
4. 点"合并选中" → 弹出编辑框 → 编辑后确认 → 新 formal box 出现
5. 选中 formal box → "Sync" 变可点 → 点 Sync 执行 add+commit
6. "Push" 在 Sync 成功后变可点 → 点 Push 执行 git push
