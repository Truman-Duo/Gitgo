# 配置

Gitgo 的用户级配置使用 `~/.gitgo/config.json`。Dashboard 保存设置与运行入口使用同一配置模型；项目身份和历史数据不因终端选择改变。

## General

首次启动在 Terminal 行选择检测到的可用终端：左右切换、Enter 保存、R 重新检测。初次保存可以转到所选终端；日后在 `/config → General` 修改时保留当前窗口，下次启动采用新选择。

发现或验证失败会提示原因。不要通过手填未经验证的 Bash 路径绕过检查。旧配置兼容保留 `auto/current/windows_terminal/custom`；可选项取决于当前系统发现结果。

Verbose 控制思考、工具等内容的默认展开程度，不改变任务或历史事实。

Diff Layout 选择 `Split`（默认，左右对照）或 `Unified`（逐行增删）。保存后立即更新当前对话中的 diff，重启后保留；窄屏下 Split 自动回退为 Unified。普通模式使用同一个 diff 框展示有界预览，省略内容会明示。

## 绑键

`/config → 绑键`（或 `/config keybindings`）查看当前动作与按键，并切换方案：

- 标准方案：Shift+Enter 换行，Shift+Tab 向前切换焦点。
- 兼容方案：Alt+Enter 换行，Ctrl+P 向前切换焦点，供终端不能区分部分 Shift 组合键时使用。

选中方案后按 Enter，左右选择，再按 Enter 保存；Esc 取消。恢复默认会回到标准方案。保存后立即生效并在重启后保留，帮助和按键提示使用同一绑定。每个动作的具体作用取决于当前界面；本页不提供运行模式切换。

终端、系统或终端自身的快捷键可能拦截输入。更换 JavaScript 运行时不能恢复未送达的按键信息；应在实际终端验证，必要时使用兼容方案。

## Providers

在 `/config → Providers` 配置名称、协议、Base URL、模型及真实容量。凭据单独保存；Windows 使用当前用户 DPAPI 加密，不写入项目文件。

支持 OpenAI Responses、OpenAI Chat Completions 与 Anthropic Messages 风格协议。模型原生搜索、独立 Provider 搜索和外部检索是不同能力，取决于配置和实际服务支持。

主 Provider 与备用设置分别保存。开启自动 failover 并设置备用顺序后，失败请求才允许转到所选节点。自动路由不改写配置主模型。详见 [备用切换](provider-routing.md)。

本地端点有部分接口和兼容服务测试，但实际本地模型验收延期，不能按正式支持承诺。

## 项目与数据

项目配置包含工作区、提交格式及发布偏好，不应保存凭据。不同终端使用相同项目身份和状态根目录时共享历史。显式指定 `GITGO_STATE_HOME` 会产生独立数据环境，测试隔离环境不能冒充正常安装历史。

修改运行中的 Provider 配置可能被拒绝，应等待任务结束或明确取消。终端选择仅影响下次启动，不应为切换终端同时启动另一个写入同一数据环境的窗口。
