# Contributing to Gitgo

感谢你改进 Gitgo。这个项目同时包含终端 UI、Native Host、Agent runtime、治理、工具和持久化系统；变更应保持单一权威边界，避免为修一个界面或 Provider 再造第二套状态通路。

## 开始之前

1. 阅读 [README](README.md)、[架构说明](docs/ARCHITECTURE.md) 和相关 Open Issue。
2. 对行为变化先明确事实源、调用链、持久化和恢复影响。
3. 不要提交 API key、SQLite/WAL、Trace、项目状态、构建产物或本地 Agent 指令。
4. UI 修改应延续现有视觉语言；涉及重新设计时先讨论。

## 开发入口

正式 Dashboard 从仓库根目录启动：

```text
run_dashboard_native.bat
```

浏览器、mock、`liveTestServer`、trace-only renderer 和隐藏 PTY 可以帮助诊断，但不能代替正式终端验收。

Dashboard 子项目：

```text
cd cli/dashboard
bun install
bun test
bun run build
```

Python 测试应使用通过 Gitgo SQLite 安全检查的运行时：

```text
<gitgo-python> -B -m pytest tests -q
```

## Commit 格式

提交信息必须符合 `commit-config.json` 和 `.gitmessage`：

```text
[GITGO-N] type(scope): subject
```

要求：

- `N` 使用项目连续编号；提交前检查远端和当前分支，避免重复；
- `type` 只能是 `feat`、`fix`、`docs`、`style`、`refactor`、`perf`、`test`、`chore`；
- `scope` 必填，合法值以 `commit-config.json` 为准；
- subject 最长 60 个字符，不以句号结尾；
- 一个提交只表达一个可审查的责任边界；不要把大批不相关文件压成一次提交。

示例：

```text
[GITGO-67] docs(docs): 重构项目说明与贡献指南
[GITGO-68] fix(frontend): 保持终端缩放后的滚动锚点
```

## 一键验证与打包

Windows 维护入口为：

```text
powershell -ExecutionPolicy Bypass -File packaging/release_windows.ps1
```

默认流程依次检查：

1. 工作树与提交格式；
2. SQLite runtime 安全性；
3. tracked 文件隐私边界；
4. Python 全量测试；
5. Dashboard 全量测试与生产 build；
6. Windows staging 构建和打包后 Native Host smoke test。

常用选项：

```text
# 只验证，不生成 staging
packaging/release_windows.ps1 -VerifyOnly

# 在已安装 Inno Setup 的机器上同时生成 Installer
packaging/release_windows.ps1 -BuildInstaller
```

`-AllowDirty` 只允许本地演练，不能作为正式发布证据。脚本不会推送、安装、下载构建工具、改写 Git 历史或删除项目数据。

## UI 变更验收

涉及 Dashboard 的变更至少检查：

- 真实彩色终端中的颜色、聚焦块、间距和场景专用状态栏；
- NormalBar/CommandBar、IME、粘贴、组合键和 Escape；
- 流式 Reasoning/工具/Diff/问题/权限/最终回答顺序；
- 调整终端宽高后的滚动锚点、表格、代码块和 Diff；
- Main Process/Subprocess、上下文比例、缓存和 Runtime 数据与后端同步。

## Pull Request

PR 应包含：

- 问题与长期解决方向；
- 修改的 authority/数据流；
- 测试与正式终端验证证据；
- 数据迁移、兼容性和回滚说明；
- 未解决风险。

不要在 PR、Issue 或测试日志中粘贴真实 API key、用户路径、数据库、原始 Trace 或私有项目内容。
