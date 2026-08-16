# gitgo — 项目治理操作系统

AI 协作开发过程中的项目状态治理系统。监测变更、匹配规则、拦截错误、记录决策。

## 工作流

- **工作区(workspace)** — Agent 本地开发目录，有 git 但从不 push。Daemon 持续监控。
- **发布区(release)** — 正式仓库，推送到 GitHub。Gate A/B 在此生效。
- **试验区(trial)** — 外部仓库，三叉决策处理其新 commit（accept/promote/discard）

## 功能

- **多项目管理** — 同时管理多个项目的配对
- **文件智能对比** — SHA256 哈希对比 + EOL 归一化
- **Sync/Push 分离** — 同步到 release 仓库和推送到 GitHub 分为两步
- **Push 安全检查** — 推送前自动扫描敏感信息（密钥/密码/token）
- **Trial 三叉工作流** — accept（cherry-pick 到 release）/ promote（fetch 到 workspace）/ discard
- **Policy Engine** — 可插拔策略引擎：lesson trigger + contract drift + identity + dep-chain
- **Agent Loop** — A→B Agent 通路：fork + dispatch + LLM call（v0.30）
- **LLM Provider 配置** — Ink 终端面板管理多 Provider + 一键切换（v0.30）
- **Ink Dashboard** — TypeScript + Bun + @anthropic/ink 终端 UI
- **SSH 远程支持** — 通过 SSH 管理远程仓库
- **主题系统** — 浅色/深色/跟随系统（Qt GUI）
- **国际化** — 中文/English 界面

## 使用

```bash
# MCP Server（Claude Code 连接用）
python mcp_server.py

# 一键同步
python -m gitgo --mode sync --project <name>

# CLI Dashboard（Ink 终端 UI）
cd cli/dashboard && bun run src/main.tsx

# 运行测试
pytest tests/ -q
```

## 版本

详见 `docs/VERSION.md`。最新版本 v0.30。
