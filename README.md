# Gitgo

Gitgo 是一个面向真实项目工作的终端 Agent harness。它把对话、工具执行、Main Process / Subprocess 协作、权限确认、运行时治理、知识沉淀和发布前隐私检查放在同一条可审计链路中。

当前仓库是首个终端预览版的源码。正式界面由 Bun、React 与 Ink 构成；Python Native Host 负责模型协议、工具、持久化和治理。Native Host 是内部服务进程，不是第二个用户产品。MCP 只作为兼容与自动化入口，不承载 Dashboard 的主通信链路。

## 当前能力

- OpenAI Responses、OpenAI Chat Completions 与 Anthropic Messages 协议适配层。
- 可配置 Provider、模型、上下文窗口和加密的本地凭据存储。
- Main Process / Subprocess 动态协作、任务 DAG、进程恢复和可审计完成判断。
- 文件、搜索、编辑、Shell、文档读取、网络检索和可注册自定义工具。
- 上下文压缩、spill 取回、知识收割、依赖证据、治理信号与发布前隐私扫描。
- 正式终端 Dashboard：流式时间线、Reasoning、工具活动、Diff、决策卡、运行时视图和项目管理。
- SQLite 持久化、写入预算、WAL 安全检查和跨 Daemon 恢复。

## 从源码运行

Windows 开发环境可运行：

```text
run_dashboard_native.bat
```

它启动完整 Bun/Ink Dashboard，并连接内部 Native Host。浏览器页面、mock 页面与 trace renderer 只用于诊断，不代表正式产品界面。

运行前至少需要：

- Bun；
- 通过 Gitgo SQLite 安全检查的 Python 运行时；
- 一个已配置且协议匹配的模型 Provider。

配置和安装说明见 [docs/INSTALL.md](docs/INSTALL.md) 与 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)。

## 发布边界

- `dist-terminal/`、`dist-installer/`、SQLite/WAL、API 密钥、Trace、项目状态和本地技术报告均不会提交到 Git。
- Windows Provider 密钥使用当前用户的 DPAPI 加密，Provider 元数据与密钥分离保存。
- 项目内 Gitgo 元数据应位于项目的 `.gitgo/` 或 Git 管理区，不应与用户交付物混放。
- 卸载器只删除 Gitgo 安装与用户选择删除的配置/凭据；不删除项目、worktree 或运行时数据库。

安全边界详见 [docs/SECURITY_AND_PRIVACY.md](docs/SECURITY_AND_PRIVACY.md)。已知限制见 [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md)。

## 状态

这是 Windows 终端首版预览。核心后端、正式 Dashboard、构建流水线和 Native Host 协议已有自动化验证；多 Provider 自动故障转移、完整 macOS/Linux 安装包和自动更新仍不在本版范围内。
