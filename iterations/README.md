# 迭代计划

> 本文件记录 sync_tool 后续待实现的功能迭代。按优先级排列，每个迭代独立，可择一实现。

---

## 已完成

### [v0.2] CUI 同步 — 功能等效于 GUI

**2026-05-08.** CUI 终端界面已重构，支持多项目管理 + box 合并 + Sync/Push 分离，与 GUI 功能等效。

---

### [v0.1] Commit 工作流重构 — Box + 选择合并 + 分离 Push

**2026-05-08.** 已完成。commit 列表改为可视化 box，支持多选合并为正式 commit，Sync（add+commit）与 Push 分离为两步。

**核心改动：** gui_main.py commit 区域重构、FormalCommit 数据模型、Sync/Push 两按钮拆分、core.py 新增 push_to_backup。

**详见：** `iterations/commit工作流重构.md`

---

### [v0.1] 多项目管理

**2026-05-08.** 已完成。config.py 重构为 `ProjectConfig` + `Config.projects[]` 多项目模型，旧配置自动迁移。GUI 增加项目列表首页（类似微信好友列表），点击进入项目操作界面。

**核心改动：** config.py 配置模型重构（单项目 → 多项目列表）、GUI 项目列表首页 + WorkspacePanel 提取、Workers 使用 ProjectConfig 路径。

**详见：** `iterations/多项目管理.md`

---

## 待办迭代

### [P0] GUI 界面优化

- 窗口和内部面板可自由调整大小（完善 QSplitter 行为）
- .exe 添加图标文件
- 操作过程添加动画效果（可开关）
- 深色/浅色主题切换

### [P0] CUI 同步 — 功能等效于 GUI

CUI 终端界面需支持多项目管理 + box 合并 + Sync/Push 分离。

### [P1] 程序国际化

界面文字不硬编码，后期通过语言文件适配多语言。

### [P1] 优化代码与缩减体积

- 清理未使用依赖
- UPX 压缩配置调优
- 精简 PyInstaller 打包产物

### [P1] 同步前差异预览

选中文件后显示 workspace vs backup 的文本差异对比（类似 git diff）。

**详见：** `iterations/差异预览.md`

### [P2] CLI 模式增强

- `--mode sync --project A` 直接执行 A 的同步（跳过 UI）
- `--mode list` 列出所有项目

### [P2] 同步历史日志

记录每次同步的时间、项目、文件数、commit hash，方便回溯。
