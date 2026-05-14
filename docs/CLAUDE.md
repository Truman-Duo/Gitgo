# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 工作流背景

我的开发工作流有两个目录：

- **Workspace（工程版）** — 本地开发目录，放着很多项目。每个项目有自己的 git 仓库做本地版本管理，**从不 push**
- **Backup repo（正式版）** — 另一个文件夹，每个项目对应一个正式仓库，是真正会 commit 并 push 到 GitHub 的

gitgo 的作用：当 workspace 里的项目开发到"差不多了"的时候，把它同步到 backup repo，生成一个规范的正式 commit，后续手动 push 到 GitHub。

## Commands

```bash
# GUI 模式（默认）
python -m gitgo

# 终端 CUI 模式
python -m gitgo --mode cui

# 配置管理
python -m gitgo --mode config

# 安装依赖
pip install -r requirements.txt

# 调试打包（带控制台，可见 stderr 输出）
python build.py --debug

# 正式打包（无控制台，交付用）
python build.py
```

### 构建"先测再打"工作流

```bash
# Step 1: 打调试版 → dist/gitgo_debug.exe
python build.py --debug

# Step 2: 双击 gitgo_debug.exe 测试功能（控制台会显示 Qt/Python 错误）

# Step 3: 测试通过后打正式版
python build.py
```

产物：`dist/gitgo.exe`（单文件，双击运行，无需 Python 环境）
调试产物：`dist/gitgo_debug.exe`（带控制台窗口）

## Architecture

### Module layout

| Module | Responsibility |
|---|---|
| `__main__.py` | CLI 入口，`--mode gui\|cui\|config` 参数分发，crash 日志 |
| `backend/core/` | 引擎层：SyncSession 状态机 + Config/I18n/History/Plugin 全部后端逻辑 |
| `backend/core/sync_session.py` | 工作流状态机 SyncSession（全 step 方法覆盖） |
| `backend/core/config.py` | `Config` dataclass + `ConfigManager` 读写 JSON（`sync_config.json`） |
| `backend/core/operations/` | 底层 git 操作：scan/sync/git/security/utils |
| `backend/core/daemon/` | P2-C 持久守护进程（watchdog + trial 轮询 + stdin 命令） |
| `backend/adapters/` | 文件/Git 适配器：Local/SSH 双实现 |
| `backend/models/` | 数据模型：RepoNode/FileAccess/SyncStatus/IncomingChange |
| `backend/core/governance/` | **P4** 治理层：quality/patterns/graph/releases/state_bundle（5 模块，~500行） |
| `backend/remote/` | 远程连接器（GitHub/GitLab API） |
| `cli/` | CLI verb 实现（headless，零 Qt/Rich 依赖，17 个 mode） |
| `frontend/gui_main.py` | GUI 薄入口 |
| `frontend/main_window.py` | PySide6 桌面主窗口，QStackedWidget 导航项目列表↔工作区 |
| `frontend/workspace/` | 项目工作区子包，10 Mixin 组合 + PanelState 显式状态容器 |
| `frontend/workspace/panel_state.py` | ★ PanelState — 跨 Mixin 共享状态显式容器（39+ 属性，标注 P/C） |
| `frontend/project_list.py` | 项目列表首页，表格+同步状态+定时刷新+右键菜单 |
| `frontend/widgets.py` | 可复用控件门面 re-export（CommitCanvas 等） |
| `frontend/workers.py` | 5 种后台 Worker（Scan/Sync/Push/TrialCheck/Triage） |
| `mcp_server.py` | **P2-D** MCP Server (FastMCP, 17 tools) |
| `examples/agent_loop.py` | **P5-B** Reference Agent — suggest→confirm→execute 完整循环 |
| `cui/main.py` | CUI 门面（原 cui_main.py），Rich 终端界面入口 |
| `themes/` | 主题系统：`ThemeColors` 类 + `build_qss(t)` 动态 QSS |
| `build.py` | PyInstaller 打包脚本，`--onefile --noconsole` |

### Key design decisions

- **文件对比**：基于 SHA256 哈希，支持大文件流式读取（8MB chunk），额外通过 hash 映射检测重命名
- **排除规则**：合并 `.gitignore` + `config.force_exclude`，支持 `**/xxx`、`xxx/**`、`/xxx` 等 glob 模式
- **Commit 整合**：解析 workspace 的 git log（自上次 sync 基点之后），剥离 `[PREFIX-N]` 前缀后按 Conventional Commits 分类（feat/fix/docs/...），聚合多个本地 commit 为一个正式 commit 模板，打开系统编辑器供用户编辑确认
- **同步编号**：从 backup repo 的 git log 中正则匹配 `[PREFIX-N]` 找到最大编号后自增
- **Push 安全检查**：push 前自动扫描待推送 commit 中的敏感信息（API key、密码、私钥、token 等正则模式），命中则弹出警告列表让用户确认或取消，降低敏感信息泄露风险
- **线程模型**（GUI）：`ScanWorker` + `SyncWorker` 通过 Qt Signal/Slot 汇报进度，避免 UI 冻结
- **首次运行**：CUI 走配置向导，GUI 弹出 `QDialog` 目录选择器
- **同步基点**：成功后记录 workspace 的 HEAD hash 到 `config.sync_base`，下次只读取此 hash 之后的 commit
- **Mixin 状态管理**：WorkspacePanel 聚合 10 个 Mixin，共享状态通过 `PanelState` 对象（`frontend/workspace/panel_state.py`）显式管理。每个属性标注 Producer/Consumer，新增跨 Mixin 属性必须先在 `PanelState.__init__` 定义。Mixin 内通过 `self.state.xxx` 访问。

### Configuration (`sync_config.json`)

存放于 exe 同目录或 `~/.vernier/` 下，多项目格式：

```json
{
  "projects": [
    {
      "name": "MyProject",
      "note": "可选备注",
      "workspace": { "file_access": { "kind": "local", "path": "D:/Workspace/MyProject" } },
      "release": { "file_access": { "kind": "local", "path": "D:/Backup/MyProject" } },
      "trial": { "file_access": { "kind": "local", "path": "D:/Trial/MyProject" } },
      "commit_format": { "prefix": "MYAPP", "number_start": 0, "padding": false },
      "force_exclude": ["CLAUDE.md", ".git/", "__pycache__/", "*.pyc"],
      "sync_base": "abc123def..."
    }
  ]
}
```

---

## gitgo 当前架构（v0.21）

### 核心概念：三维模型

| 角色 | 代号 | 说明 |
|---|---|---|
| **Workspace（工程版）** | `w` | 本地开发目录，有 git 但从不 push |
| **Release（正式版）** | `r` | 真正 push 到 GitHub 的正式仓库 |
| **Trial（试用版）** | `t` | 第三方试用仓库，只读拉取、只接受 cherry-pick |

每个角色背后是一个 **RepoNode**，包裹两个访问层：

```
RepoNode
 ├── git_url       # git 远程地址（git clone/push 用）
 └── file_access   # 文件级访问（读/写）
      ├── LocalFileAccess    # 本地文件系统
      ├── SSHFileAccess      # SSH 远程 (Phase 3)
      └── SMBFileAccess      # SMB 共享 (Phase 6)
```

### Trial 三叉决策

当 Trial 仓库提交了新的 commit（IncomingChange），用户三选一：

```
                  ┌─ accept  ──→ release repo（cherry-pick 到正式版）
IncomingChange ──┼─ promote ─→ workspace（git fetch → incoming/* 分支，本地继续开发）
                  └─ discard  ─→ 忽略
```

- accept：从 trial 仓库 cherry-pick 到 release 仓库，生成正式 commit
- promote：`git fetch trial` 到 workspace 创建 `incoming/*` 分支，保留完整历史
- discard：标记已读，下次不再提示（不删除 trial 仓库）

### 适配器模式

```
FileAdapter (ABC)          GitRunner (ABC)
 ├── LocalFileAdapter       ├── LocalGitRunner
 ├── SSHFileAdapter         └── SSHGitRunner
 └── SMBFileAdapter
```

- **FileAdapter**：文件级操作（walk、copy、compare、read/write）
- **GitRunner**：git 操作（clone、fetch、push、cherry-pick、log）

### 插件系统

在同步工作流的关键节点开放 **8 个 Hook 接口**，允许第三方挂载自定义逻辑（详见 `docs/Plugin_API.md`）：

| Hook | 阶段 | 说明 |
|------|------|------|
| `on_scan_complete` | Scan | 过滤/排序/标注文件 |
| `on_commit_select` | Commit | 推荐合并哪些 commit |
| `on_commit_message` | Commit | 自动生成正式 commit message |
| `on_sync_start` | Sync | 同步前校验（返回非空则中断） |
| `on_sync_complete` | Sync | 同步后回调 |
| `on_push_start` | Push | Push 前校验 |
| `on_push_complete` | Push | Push 后回调 |
| `on_triage_recommend` | Trial | 推荐三叉决策 |

- **2 层搜索路径**：`{exe}/plugins/` + `~/.vernier/plugins/`
- 每个插件是 Python 文件/包，暴露 `plugin_class` 全局变量（`SyncPlugin` 子类）
- 每个项目通过 `commit_format.plugins: list[str]` 指定启用的插件列表
- **3 个参考插件**：`auto_merge` / `slack_notify` / `jira_link`

### 远程连接器（已实现）

```
RemoteConnector (ABC)
 ├── BareConnector       # 裸仓库
 ├── GitHubConnector     # GitHub API（PR、Issue、Release）
 └── GitLabConnector     # GitLab API（PR、Issue、Release）
```

Release 和 Trial 节点可选绑定 RemoteConnector。CLI：`gitgo --mode release --release-action create-release --project X`。

### SyncSession 状态机（已实现）

共享状态机，驱动 GUI / CUI / Daemon / CLI 四种前端：

```
IDLE → SCANNING → FORMALIZING → SYNCING → PUSHING → IDLE
                      └→ TRIAGE_CHECKING → TRIAGE_ACTION → IDLE
```

**完整 step 方法覆盖**：`step_scan() / step_load_commits() / step_check_trial() / step_triage_accept() / step_triage_promote() / step_triage_discard() / step_create_formal_commit() / step_sync() / step_push() / step_delete_formal() / step_dissolve_formal() / step_create_release()` 等 24 个方法。<br>
**状态驱动闭环（v0.12）**：所有前端必须通过 step 方法操作 core 状态，不得直接 mutation。

### 阶段完成状态

| 阶段 | 内容 | 状态 |
|------|------|------|
| **P1** | Runtime Foundation — 数据模型 3 角色 + SyncSession 状态机 + FileScanner | ✅ v0.10 |
| **P2** | Semantic + Persistence — streaming JSON + 9 op types history + persistent daemon + MCP Server | ✅ v0.12 |
| **P3** | AI-Augmented — triage hook + suggest CLI + AI_Protocol + Commit Proposal + diff_summary | ✅ v0.15 |
| **P4** | Governance Layer — quality metrics + change patterns + semantic graph + release reasoning | ✅ v0.20 |
| **P5** | Protocol & Ecosystem — Protocol v1.0 + Reference Agent + Plugin API + State Bundle | ✅ v0.21 |
| **Remote** | GitHub/GitLab 连接器 + `--mode release` CLI | ✅ v0.14 |
| **P0** | GUI Track — B-1 + F-1 架构调整 | ⬜ 待执行 |

详见 `docs/iterations/` 和 `docs/HANDOFF.md`。

---

# Qt UI Development Guide (PySide6)

## 语言与框架约定

- 信号槽语法统一用新式写法：`widget.signal.connect(slot)`
- 禁止使用 `exec_()` 这类 Qt4 遗留命名，使用 `exec()`
- 自定义 widget 的 `__init__` 签名必须清晰

## UI 设计原则

### 信息分组（最高优先级）

添加任何 widget 前，先问：**它属于哪个功能组？**

将界面元素按功能归属分组，同组的东西在视觉上必须相邻、对齐、用相同的容器包裹。
不允许把不同功能的控件随意堆在同一个 layout 里。

分组方式（按优先级）：

1. **语义分组**：同一操作目标的控件放在一起（如"文件操作"：打开、保存、另存为）
2. **频率分组**：高频操作放在视觉重心，低频操作收进菜单或折叠区
3. **主次分组**：主操作（Primary）用 accent 色突出，次操作（Secondary）用低对比度

实现规则：
- 每个语义组封装为一个独立的 widget 类或 layout 函数，不在主窗口 `__init__` 里平铺
- 组与组之间用 `spacing_lg` 间距或分割线（`QFrame` / `setFrameShape`）隔开
- 组内元素间距用 `spacing_sm` 或 `spacing_md`

### 视觉层级

每个界面必须有清晰的三层权重：

```
主体内容（最大面积，fg_primary）
  └─ 辅助信息（fg_secondary，字号降一级或透明度 0.7）
       └─ 操作控件（按需突出，accent 只用于 1-2 个主操作）
```

- 同一层级的元素，字号、颜色、间距必须一致
- 禁止在同一屏幕里出现超过 2 种 accent 色用途
- 图标和文字同行时，垂直居中对齐，图标尺寸 = `font_size_base + 2`

### 对齐与呼吸感

- 所有同级元素必须共享同一条对齐基线（左对齐或网格对齐，不允许参差不齐）
- 内容区左右边距统一用 `spacing_lg`，不允许某些组有边距、某些没有
- 相邻的两个功能区之间，间距必须大于同组内元素间距（层级感靠间距差体现）
- 列表类内容行高 = `font_size_base * 2`，保证可点击区域和可读性

## 可扩展架构（为将来留空间）

### 三层分离原则

UI 代码按以下三层严格分离，禁止跨层直接耦合：

```
数据 / 逻辑层      ← 不知道任何 Qt widget 的存在
      ↕ 信号 / 回调
组件层（widgets/） ← 只知道自己，通过信号对外通信
      ↕ 组装
页面层（views/）   ← 负责布局组合，不含业务逻辑
```

**改样式不动逻辑，改逻辑不动布局**——这是将来能快速迭代设计的前提。

### 组件设计规则

每个自定义 widget：

- 暴露信号而不是暴露子控件：`data_changed = Signal(dict)` 而不是 `self.input_field`
- 接受数据驱动的 `update(data: dict)` 方法，而不是外部直接 `setText`
- 视觉变体（大/小/强调/静默）通过构造参数 `variant="primary"|"secondary"|"ghost"` 控制，不在外部 override 样式

```python
class ActionButton(QPushButton):
    def __init__(self, label: str, variant: str = "primary", parent=None):
        super().__init__(label, parent)
        self.setProperty("variant", variant)  # QSS 通过 property selector 控制样式
```

### QSS 用 property selector，不用子类

将来改设计只需改 QSS，不改 Python 代码：

```css
QPushButton[variant="primary"] { background: {accent}; }
QPushButton[variant="secondary"] { background: transparent; border: 1px solid {border}; }
QPushButton[variant="ghost"] { background: transparent; border: none; color: {fg_secondary}; }
```

### 预留扩展点清单

以下位置在初次实现时必须预留，即使当前不用：

| 位置 | 预留方式 |
|------|----------|
| 主窗口顶部 | 一个空的 `QWidget` header 区，高度 `spacing_lg * 2` |
| 侧边栏 | `QSplitter` 包裹主内容区，初始可折叠宽度为 0 |
| 每个功能组底部 | `addStretch()` 占位，方便后续插入新控件 |
| 状态栏 | `QStatusBar` 预留，即使当前内容为空 |
| 工具栏操作区 | 用 `QActionGroup` 管理，不要写死 `QPushButton` 排列 |

## 布局规范

- **禁止**使用 `move()` / `resize()` / `setGeometry()` 做绝对定位
- 所有布局通过 `QVBoxLayout` / `QHBoxLayout` / `QGridLayout` / `QFormLayout` 组合实现
- `addStretch()` 和 `setSizePolicy()` 优先于固定尺寸
- layout 的 `setContentsMargins` 和 `setSpacing` 必须显式设置，不依赖默认值
- **对齐锁定用 `setFixedWidth` 而非 `setMinimumWidth`**：当 stretch=0 时，QHBoxLayout 按 `sizeHint()` 分配实际宽度。不同 widget 类型的 sizeHint 不同（QLabel ≠ QWidget 包裹的 layout）→ `setMinimumWidth` 只设下限不能保证对齐。需对齐的列必须用 `setFixedWidth` 锁死宽度。
- **QSS `border-left-width` 影响 content rect**：Qt 的 border 不仅视觉绘制，还会吃掉 content rect 的对应像素。设计 padding/margin 时需把 border-width 计入偏移。
- 主窗口结构：`QMainWindow → centralWidget → QVBoxLayout → [内容区]`
- 可复用的 widget 组合封装为独立类，放在 `widgets/` 目录

## 主题系统（三模式）

项目支持三种主题模式，通过 `themes/` 包管理。

### 颜色令牌

颜色令牌定义在 `themes/light.py` 和 `themes/dark.py` 中，通过 `ThemeColors` 类封装（支持属性访问 `t.bg` / `t.txt`）：

```python
from themes import get_theme

t = get_theme()
widget.setStyleSheet(f"color:{t.txt2}; background:{t.bg2};")
```

**核心令牌**：

| 令牌 | 说明 |
|---|---|
| `bg` / `bg2` / `bg3` | 主背景/次级/三级背景 |
| `txt` / `txt2` / `txt3` | 主文本/次级/辅助文本 |
| `bdr` / `bdr2` | 分割线 0.5px / 边框 1px |
| `accent` | 强调色（按钮、选中） |
| `blue_bg` / `blue` / `blue_txt` / `blue_bdr` | 蓝色系（hover/选中态） |

### ThemeColors 类

```python
class ThemeColors:
    def __init__(self, colors: dict):
        self._c = colors
    @property
    def bg(self): return self._c["bg"]
    @property
    def accent(self): return self._c["accent"]
    # ...
```

### 模式切换

```python
from themes import set_theme, get_qss

set_theme("dark")   # "dark" | "light" | "system"
app.setStyleSheet(get_qss("dark"))  # _build_qss(t) 运行时生成完整 QSS
```

- `system` 模式通过 `_detect_system_theme()` 读 Windows 注册表检测
- 所有 widget 主题刷新通过 `ThemeMixin._apply_theme_colors()` 统一触发
- `_restyle(widget)` 用 `unpolish/polish` 强制 Qt 重新计算 QSS

### QSS 规范

- 全局 QSS 通过 `themes/__init__.py` 的 `_build_qss(t)` 函数生成，运行期间颜色令牌插值
- **widget 级精确样式**：通过 `_apply_theme_colors()` 逐 widget 调用 `setStyleSheet()`（覆盖 22+ 具名 widget）
- 全局 QSS 覆盖：QMainWindow、QPushButton、QTableWidget、QTabBar、QTreeWidget、QScrollBar 等
- QSS 使用 `#objectName` 选择器针对具体 widget（如 `QFrame#action_bar`、`QLabel#explorer_header`）
- **禁止**在 widget __init__ 中硬编码颜色值，始终通过 `get_theme()` 读取当前颜色

### QSS 状态驱动模式（v0.10+ — CommitBox 重构后）

**核心原则**：用 QSS property selector 替代动态 `setStyleSheet()`。状态切换走 `setProperty` + `unpolish/polish`，不覆写 stylesheet。

```python
# ✅ 正确 — 状态驱动
widget.setProperty("selected", True)
widget.style().unpolish(widget)
widget.style().polish(widget)

# ❌ 错误 — 动态覆写样式（触发 Qt 全局样式重算，导致闪烁）
widget.setStyleSheet("background: blue;")
```

**QSS 中对应**：
```css
QFrame#ws_card[selected="true"] { background: {t.blue_bg}; border-color: {t.blue}; }
```

**属性选择器优先级（Qt 特定）**：Qt QSS 不支持 CSS specificity。相同 specificity 的选择器，**后定义的覆盖先定义的**。必须把高优先级状态（如 `[selected="true"]`）写在 QSS 文件末尾。

**_polish_all 模式**：如果 widget 的子组件也需要响应属性变化（如合并态下所有子 Label 变灰），必须遍历子 widget 逐一 `unpolish/polish`：
```python
def _polish_all(self):
    for w in [self, self.type_lbl, self.summary_lbl, self.meta_lbl, self.cb]:
        w.style().unpolish(w)
        w.style().polish(w)
```

**setUpdatesEnabled 批量操作**：逐项 addWidget 时包裹 `setUpdatesEnabled(False/True)` 防止中间态渲染闪烁。务必 `try/finally` 确保恢复：
```python
container.setUpdatesEnabled(False)
try:
    for item in items:
        layout.addWidget(create_box(item))
finally:
    container.setUpdatesEnabled(True)
```

### QSS 动态生成示例

```python
# themes/__init__.py
def _build_qss(t: ThemeColors) -> str:
    return f"""
    QMainWindow, QWidget {{ background-color: {t.bg}; color: {t.txt}; }}
    QPushButton {{ background-color: {t.bg2}; border: .5px solid {t.bdr2}; }}
    QPushButton:hover {{ background-color: {t.blue_bg}; border-color: {t.blue}; }}
    QTabBar::tab:selected {{ border-bottom: 2px solid {t.accent}; }}
    QTreeWidget::item:hover {{ background: {t.bg2}; }}
    QFrame#action_bar {{ background: {t.bg2}; border-bottom: .5px solid {t.bdr}; }}
    """
```

### Qt6 API 迁移注意

PySide6/Qt6 中移除了一些 Qt5 遗留方法，以下是本项目已遇到的：

| 旧方法 (Qt5/PySide2) | 新方法 (Qt6/PySide6) | 所在文件 |
|---|---|---|
| `widget.foreground()` | `widget.foregroundRole()` | `builder.py:_BranchLineStyle.drawPrimitive` |
| `widget.style().unpolish(polish)` | 移除调用（QSS 已足够） | `theme.py`（已删除） |

`foreground()` 在 Qt6 中不存在，直接调用会引发 `AttributeError`。由于该方法在 `QProxyStyle.drawPrimitive` 中（被 C++ paint 事件频繁调用），异常在 C++/Python bridge 间反复传播，最终导致进程 segfault/abort。

**修复原则**：widget 未完全初始化时，禁止调用 `unpolish()/polish()`。QSS 全局样式已覆盖 widget 渲染，无需强制重绘。

## 自定义绘制（QPainter）

需要 QPainter 的场景（图表、进度条、自定义 indicator）：

- 在 `paintEvent` 里启用 `RenderHint.Antialiasing`
- 颜色从 `ThemeManager.token()` 读取，转换为 `QColor`
- `sizeHint()` 必须返回合理值
- 动画数值通过 `QPropertyAnimation` 驱动，不在 `paintEvent` 里做时间计算

## 动画规范

- 使用 `QPropertyAnimation` + `QEasingCurve`
- 界面过渡默认时长：150ms（微交互）/ 250ms（面板展开）/ 400ms（页面切换）
- 缓动曲线：进入用 `OutCubic`，退出用 `InCubic`，弹性用 `OutBack`
- 多段动画用 `QSequentialAnimationGroup` / `QParallelAnimationGroup`，不用 `QTimer` 模拟
- **动画对象必须持有引用**（存为实例属性 `self._anim`），防止 GC 回收导致动画中断、widget 卡在 opacity 0

## Splitter 使用规范

- `setStretchFactor` 必须在 `addWidget` 之后调用，否则被静默忽略
- 内层 splitter 用 `setChildrenCollapsible(False)` 防止拖拽时子对象折叠归零
- 嵌套 splitter 中，外层手柄拖拽会触发内层 resize 重计算，容易导致跳动
- **多步骤纵向面板优先使用扁平 QVBoxLayout + QScrollArea**，只在确实需要独立拖拽时才引入内层 splitter

### 嵌套 QSplitter 防跳变规则（经验证）

场景：水平 QSplitter 内嵌垂直 QSplitter，拖拽外层手柄时子面板跳变/空白。

1. **禁用 setFixedWidth/Height**：QSplitter 子对象用 `setMinimumWidth/Height` 设下限，不用 `setFixedWidth/Height`。固定尺寸与 splitter 拖拽逻辑冲突——Qt 尝试调整子对象大小时遇到不可改变的固定值，导致 handle 位置计算与视觉不一致，进入 layout 循环（尝试分配 → 固定尺寸阻挡 → 重新计算 → 再次冲突）。

2. **对称保护**：嵌套 splitter 的每一层，**所有**子对象都要设 `setMinimumHeight/Width`，不能只保护其中一个。

3. **setSizes 设初始比例**：初始尺寸用 `setSizes([pixel, pixel, ...])` 显式分配，不依赖 `sizeHint`。

4. **示例**（Workshop Tab 三列水平 + 中间列垂直二分）：
```python
ws_hsplitter = QSplitter(Qt.Horizontal)
ws_hsplitter.setChildrenCollapsible(False)
ws_hsplitter.addWidget(explorer)    # setMinimumWidth(100)
ws_hsplitter.addWidget(ctr_widget)  # stretch=1
ws_hsplitter.addWidget(diff_panel)  # setMinimumWidth(100)
ws_hsplitter.setSizes([138, 800, 150])

# 内层垂直 splitter
center_splitter = QSplitter(Qt.Vertical)
center_splitter.setChildrenCollapsible(False)
center_splitter.addWidget(commit_frame)  # setMinimumHeight(100)
center_splitter.addWidget(msg_frame)     # setMinimumHeight(54) ← 必须对称保护！
```

## 描述 UI 改动的方式

当你收到 UI 相关的修改请求，请按以下格式理解和回应：

1. **先确认 widget 树**：受影响的 widget 类型和层级
2. **再确认布局变化**：哪个 layout 的哪个位置发生了什么变化
3. **最后处理样式**：通过 token 映射，不引入新的硬编码值
4. 如果描述不够明确，**列出 2-3 个可能的理解**，让用户选择，不要猜测后直接动代码

## 禁止行为清单

- ❌ `widget.setStyleSheet("color: #333")` —— 硬编码颜色
- ❌ `widget.move(100, 200)` —— 绝对定位
- ❌ `QTimer` 驱动动画帧
- ❌ 在 `__init__` 里嵌套超过 3 层的匿名 layout
- ❌ 跨文件直接访问其他 widget 的子控件（应暴露信号或方法）
- ❌ 修改 UI 前不确认主题模式就写死颜色
- ❌ `setStretchFactor` 在 `addWidget` 之前调用
- ❌ 内层 splitter 使用 `setChildrenCollapsible(True)`（会导致拖拽时折叠归零）
