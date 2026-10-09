# Contributing to Gitgo

感谢你改进 Gitgo。这个项目同时包含终端 UI、Native Host、Agent runtime、治理、工具和持久化系统；变更应保持单一权威边界，避免为修一个界面或 Provider 再造第二套状态通路。

## 开始之前

1. 阅读 [README](README.md)、[架构说明](docs/ARCHITECTURE.md) 和相关 Open Issue。
2. 对行为变化先明确事实源、调用链、持久化和恢复影响。
3. 不要提交 API key、SQLite/WAL、Trace、项目状态、构建产物或本地 Agent 指令。
4. UI 修改应延续现有视觉语言；涉及重新设计时先讨论。

## 仓库、分支与上下游

Gitgo 主仓库是 `Truman-Duo/Gitgo`，共同正式目标为它的 `refs/heads/master`。只有这一条长期开发主线；功能分支服务于具体任务，完成后清理。workspace、trial、formal 是工作角色，不限定仓库数量，也不要求创建同名永久分支。多人、多仓库的设计边界见 [协作模型](docs/collaboration-workflow.md)；该文档中的自动治理仍是设计，当前按下面的贡献流程执行。

- 新工作从最新主仓库 `master` 创建短期分支，建议 `feat/<issue>-<topic>`、`fix/<issue>-<topic>` 或 `docs/<topic>`。不要复用已经合入的日期分支继续承担新任务。
- 外部贡献者在自己的 fork 创建功能分支并推送，再向 `Truman-Duo/Gitgo:master` 开 PR。上游无需为每个 fork 再复制一条持久分支。
- 一个 PR 尽量解决一个可审查的责任范围。存在依赖时写出前置 PR 和基线 SHA；必要时使用明确标注的堆叠 PR，不能把未合入依赖伪装成自己的新增工作。
- 上游更新后先 fetch。纯同步用途的 fork 默认分支采用 fast-forward；若有下游独有提交，保留它们，明确合并策略，不能强制覆盖。
- 正在工作的功能分支也需要带入上游更新并验证。共享、已公开分支默认 merge；只有作者独占且确认无其他使用者的分支才考虑 rebase，不替别人重写历史。先保存未提交工作，再执行会改变工作目录的操作。
- PR 要求完善时保留分支和 PR，修改后重新评估；明确拒绝或撤回时记录原因并关闭。审查意见、实际合入、资源清理分别确认。
- 合入后核对正式结果，再清理已完成的功能分支。清理前检查新增提交、其他 PR/分支依赖、活动 worktree 与未保存内容。只清理有权管理的资源；fork 和本地分支由对应所有者处理，不能假定随上游合并一起消失。
- 主仓库 `master` 的正式历史不 force-push；回滚使用新的修复或 revert 变更。发布用 Tag/Release 标识，合入不等于已完成安装包发布。

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

正式集成提交使用 `commit-config.json` 和 `.gitmessage` 的格式：

```text
[GITGO-N] type(scope): subject
```

要求：

- `N` 由维护者在正式集成时协调分配，依据最新上游历史核对唯一性；并行贡献者不能各自用“当前最大值加一”争用全局编号；
- `type` 只能是 `feat`、`fix`、`docs`、`style`、`refactor`、`perf`、`test`、`chore`；
- `scope` 必填，合法值以 `commit-config.json` 为准；
- subject 最长 60 个字符，不以句号结尾；
- 一个提交只表达一个可审查的责任边界；不要把大批不相关文件压成一次提交。

贡献者的功能分支提交及 PR 标题可以先使用 `type(scope): subject`，并在正文关联 Issue；不要求外部作者预占全局 `GITGO-N`。维护者默认 squash 合入这样的 PR，并为正式提交填写编号，保留作者与必要的共同作者信息。若候选已经包含维护者协调过编号的完整提交，可以在核对编号和格式后使用 merge commit 保留它们。不要为了编号修改其他贡献者的公开分支，或重写已有正式历史。

当前部分 Gitgo formalize 工具仍要求带编号；上述贡献分支可以使用普通 Git 创建。新的协作模型尚未实现自动编号协调或自动选择合并策略。

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

- 明确的源仓库/分支、候选 head SHA、目标仓库/分支与验证时的目标 base SHA；
- 问题与长期解决方向；
- 修改的 authority/数据流；
- 测试与正式终端验证证据；
- 数据迁移、兼容性和回滚说明；
- 未解决风险。

使用 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md)，写清测试的实际执行平台、终端、源码或 frozen 包、验证版本、跳过项和失败项。只有进程/API 证据时，不勾选可见终端验收。上传供协作者对齐的候选可以先开 Draft PR；Draft 的存在不代表已批准或已进入共同正式基线。

检查结果应随候选版本更新。合并冲突处理后重新验证受影响行为；审批不能扩大工具权限，检查失败也不能被“已批准”覆盖。上游目标变化时重新确认集成结果，不能把旧 base 的通过结果当作新组合已经通过。上传内容不包含真实用户配置、原始 trace、数据库、生成的 exe 或其他构建产物。

不要在 PR、Issue 或测试日志中粘贴真实 API key、用户路径、数据库、原始 Trace 或私有项目内容。
