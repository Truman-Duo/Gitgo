# 实现状态

更新时间：2026-09-28

## 已落地并自动验证

- Native Host 版本化协议与 Daemon 生命周期。
- SQLite/CAS 状态层、迁移、恢复与 WAL 安全检查。
- OpenAI Responses、OpenAI Chat Completions、Anthropic Messages 适配边界。
- Provider 配置、能力探针、加密凭据存储与手动切换。
- 通用工具、文档读取、网络检索、spill、动态工具注册与权限入口。
- Main Process / Subprocess 路由、任务 DAG、协调事件、完成证据与恢复流程。
- 上下文装配、手动/自动压缩和运行时投影。
- 知识、依赖、治理、隐私扫描与发布前出站策略。
- Bun/Ink Dashboard、时间线、Markdown、Diff、输入路由、配置、运行时和项目视图。
- Windows staging 构建与打包后 Native Host 协议 smoke test。

“自动验证”表示已有测试或构建检查，不等同于所有真实 Provider、终端尺寸和项目组合都已覆盖。

## 本地保留、不公开

- 技术报告、迭代过程、完整交接记录；
- 真实 API 验收记录、Trace 和运行时数据库；
- 项目状态、知识实例和本地 Agent 指令；
- Windows staging/installer 二进制与旧版 Qt 产物。

## 后续版本

1. 多 Provider failover/circuit breaker 与真实故障演练；
2. 更多正式终端尺寸、输入法、长文本与长会话验收；
3. 自定义工具和知识生命周期的复杂项目回归；
4. macOS/Linux 原生包与系统密钥存储；
5. 桌面产品形态和自动更新另行设计。
