# 实施计划：Commit 工作流重构

> 当前迭代目标：Box 可视化 + 选择合并 + Sync/Push 分离
> 
> 基于 `iterations/commit工作流重构.md` 落地为具体代码变更

---

## 实施步骤

### Step 0: 项目清理

**目的：** 把不该进 git 的产物排除掉，为后续迭代建立干净的 .gitignore

- 写 `.gitignore`（`__pycache__/`、`build/`、`dist/`、`*.exe`、`*.spec`）
- `git rm --cached` 移除已追踪的产物文件
- commit: `chore: add .gitignore and clean tracked build artifacts`

### Step 1: core.py — 新增 `push_to_backup`

**目的：** 为 Push 按钮准备后端能力，Sync 相关的函数不动

- 新增 `push_to_backup(backup_path, remote="origin") -> bool`
  - 调用 `git -C {backup} push {remote}`
  - 超时 60s，捕获 `subprocess` 异常
  - 返回成功/失败
- 新增 `GitProgress` 回调类型或复用现有的 `Callable[[int, int, str], None]`

**涉及文件：** `core.py`（末尾追加，不修改现有函数）

### Step 2: gui_main.py — 新增 FormalCommit 数据类

**目的：** 区分 workspace commit 和已合成正式 commit

- 在 `gui_main.py` 顶部（或单独 section）定义：

```python
@dataclass
class FormalCommit:
    message: str
    number: int
    prefix: str
    synced: bool = False
    pushed: bool = False
```

- `MainWindow.__init__` 新增：
  - `self.formal_commits: list[FormalCommit] = []`
  - `self.selected_workspace: set[int] = set()`
  - `self.selected_formal: int | None = None`

### Step 3: gui_main.py — 替换 commit 区域为 Box 布局

**目的：** 用可视化 box 替代纯文本 commit 列表

**当前控件（移除）：**
- `self.commit_list`（QPlainTextEdit）
- `self.commit_msg`（QTextEdit）
- `self.refresh_commits_btn`
- 步骤 2 整个 `QGroupBox` 重写

**新控件（替换为三大区域）：**

#### 3a. Workspace Commits 面板（左上）

- 自定义 `WorkspaceCommitBox`（QFrame 子类）：
  - 布局：type + subject（加粗首行）| 第二行：date + author
  - 选中态：背景色变为浅蓝 + 左 border 高亮
  - 鼠标点击切换选中
  - 支持 Ctrl+点击多选、Shift+点击范围选
- 容器：`QScrollArea` + `QVBoxLayout`，按时间倒序
- 配套按钮：`刷新列表` + `合并选中`（灰色不可点 → 选中 ≥2 条时可点）

#### 3b. Formal Commits 面板（左下）

- 自定义 `FormalCommitBox`（QFrame 子类）：
  - 显示 `[PREFIX-N] type: subject`
  - 第二行：synced 状态标记 + 日期
  - 选中态：蓝色边框
- 容器：`QScrollArea` + `QVBoxLayout`
- 配套按钮：`删除`（移除本地 formal box 记录）

#### 3c. 合并交互逻辑

```
用户选中 ≥2 个 workspace box
    → "合并选中" 可点
    → 点击弹出 QDialog
    → Dialog 内含 QTextEdit，预填来自 build_commit_template 的合并模板
    → 用户编辑后点"确认"
    → 调用 build_commit_template + 编号分配
    → 新 FormalCommit 追加到 self.formal_commits
    → Formal Commits 面板刷新显示新 box
    → 被合并的 workspace box 变为灰色 + "已合并"标记
```

### Step 4: gui_main.py — 执行区拆分为 Sync + Push

**目的：** 将"开始同步"一步拆为两步，只有 formal commit 到位后才可操作

**当前按钮（移除）：**
- `self.sync_btn`（▶ 开始同步）

**新按钮：**
- `self.sync_btn`（Sync 到备份仓库）：
  - 需选中一个 formal box 才可点
  - 执行：文件拷贝 → git add → git commit（复用 `sync_to_backup`）
  - 成功后 `formal.synced = True`，box 样式更新（加绿色 synced 标记）
  - 过程中进度条显示阶段文字

- `self.push_btn`（Push 到 GitHub）：
  - 需有一个 `synced=True` 的 formal box 才可点
  - 执行：`push_to_backup(config.backup_path)`
  - 成功后 `formal.pushed = True`，box 样式更新（加 pushed 标记）
  - 过程中进度条显示 "正在 push..."

- 进度条改造：
  - `self.progress_label`（QLabel）显示当前阶段文字
  - `self.progress_bar` 百分比进度
  - 完成时颜色翻转：绿底=成功，红底=失败

### Step 5: 主窗口布局重组

**目的：** 重新组织三步操作的面板结构，清晰展示当前阶段

```
┌──────────────────────────────────────────────┐
│  ┌───────────────────┐  ┌──────────────────┐ │
│  │  Step 1: 扫描对比   │  │  Step 2: Commit  │ │
│  │  [🔍 扫描对比]      │  │  (现在变成 Commit │ │
│  │  文件列表表格        │  │   整合区域)       │ │
│  └───────────────────┘  │  ┌─── Workspace ─┐ │ │
│                          │  │ ┌─box┐┌─box┐ │ │ │
│                          │  │ └────┘└────┘ │ │ │
│                          │  └──────────────┘ │ │
│                          │  ┌─── Formal ────┐ │ │
│                          │  │ ┌─box┐        │ │ │
│                          │  │ └────┘        │ │ │
│                          │  └──────────────┘ │ │
│                          └──────────────────┘ │
│  ┌──────────────────────────────────────────┐ │
│  │  Step 3: 执行                              │ │
│  │  进度条: ████████░░ 80%                    │ │
│  │  状态: 正在拷贝文件 (12/15)...              │ │
│  │  [Sync 到备份仓库]  [Push 到 GitHub]       │ │
│  └──────────────────────────────────────────┘ │
│  日志输出: ...                                 │
└──────────────────────────────────────────────┘
```

具体实现方式：`QSplitter` 水平分割（左=文件，右=commit），右半再垂直分割（上=commit 区域，下=执行区）

### Step 6: 适配 CUI（暂缓）

CUI 当前迭代不动，仅保证导入不报错。

---

## 涉及文件清单

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `.gitignore` | **新增** | 排除构建产物 |
| `core.py` | 追加 | 新增 `push_to_backup()` |
| `gui_main.py` | 重写 commit/执行区 | ~120 行删除，~350 行新增 |
| 其他文件 | 不动 | config.py / cui_main.py / build.py |

## 风险与注意事项

1. **`sync_to_backup` 的 progress_callback 已用 `(int, int, str)` 签名** — Push 操作没有"当前/总数"概念，需要为 Push 扩展或新写回调签名，或者 Push 阶段固定传 `(0, 1, msg)` + 最终 `(1, 1, "done")`
2. **Qt 线程安全** — Sync 和 Push 都是耗时操作，必须走 `QThread` + worker 模式（当前 `SyncWorker` 模式可复用，新增 `PushWorker`）
3. **没有 running queue** — 当前设计允许"正在 Sync 时点 Push"等竞态，需在操作开始时 disable 所有操作按钮，完成后恢复

## 验证方式

1. `python -m sync_tool` 启动正常，无回溯
2. 扫描后"刷新 Commit" → workspace box 列表按时间倒序显示
3. 点击 box 切换选中态（视觉反馈）
4. Ctrl+点击 多选 → "合并选中" 可点
5. 点"合并选中" → 弹出编辑框 → 确认 → formal box 出现
6. 选中 formal box → Sync 可点 → 执行成功 → formal box 显示 synced
7. Sync 完成后 → Push 可点 → 执行成功 → formal box 显示 pushed
8. 进度条在 Sync 和 Push 过程中实时更新
9. 操作过程中所有按钮 disable，操作完成后恢复
