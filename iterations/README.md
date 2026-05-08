# 迭代计划

> 本文件记录 sync_tool 后续待实现的功能迭代。按优先级排列，每个迭代独立，可择一实现。

---

## 待办迭代

### [当前] Commit 工作流重构 — Box + 选择合并 + 分离 Push

**优化 commit 整合体验。** 当前 commit 列表是纯文本、合并靠自动模板、同步与 push 捆绑。改为可视化 box 交互，用户手动选择哪些 workspace commit 合并为一个正式 commit，并将 sync（commit）与 push 分离为两步。

**核心改动：** gui_main.py commit 区域重构（QListWidget → 自定义 box 布局 + 多选 + 合并按钮）、新增 formal commit box 管理、新增 push 按钮（仅 formal commit 存在时可点）、core.py 新增 push 操作。

**详见：** `iterations/commit工作流重构.md`

---

### [P0] 多项目管理

当前工具只能管理一个项目（A 工程版 → A 正式版）。需支持在同一个界面中添加/切换多个项目，各自独立配置。

**核心改动：** config.py 配置模型重构（单项目 → 多项目列表）、GUI/CUI 增加项目列表首页、现有操作界面绑定到当前选中项目。

**详见：** `iterations/多项目管理.md`

---

### [P1] 同步前差异预览

当前只能看到文件列表（新增/修改/相同/重命名），看不到具体内容变化。需增加文件内容 diff 预览功能。

**思路：** 选中某个文件后，显示 workspace vs backup 的文本差异对比（类似 git diff）。

---

### [P2] CLI 模式增强

当前 `--mode config` 只能看/重置配置，缺少无交互的静默模式。可增加：

- `--mode sync --project A` 直接执行 A 的同步（跳过 UI）
- `--mode list` 列出所有项目

---

### [P2] 同步历史日志

记录每次同步的时间、项目、文件数、commit hash，方便回溯。
