# 架构概览

Gitgo 的公开运行边界由四层组成：

1. **Dashboard**：Bun/React/Ink 终端界面，负责场景、输入、流式时间线、Diff、决策卡和状态展示。
2. **Native Host**：版本化 stdio 协议入口，负责应用服务、Daemon 生命周期和 Dashboard 数据投影。
3. **Agent runtime**：Provider 适配、Main Process / Subprocess 协作、工具、权限、上下文、完成判断和恢复。
4. **State and evidence**：SQLite、CAS、知识、依赖证据、治理信号和发布前隐私边界。

```text
User
  -> Terminal Dashboard
  -> Native Host protocol
  -> Daemon / Agent runtime
  -> Provider + tools
  -> SQLite / CAS / project metadata
```

## 关键约束

- Dashboard 到后端走原生协议；MCP 仅用于兼容和自动化控制。
- 用户意图高于普通治理软门；敏感操作仍需要可审计、带作用域和时效的批准。
- Main Process 负责与用户对齐、路由、审查与交付；Subprocess 是可长期复用的责任单元，不是每轮一次性的临时调用。
- Subprocess 之间不私聊。依赖变化通过版本化接口更新、协调事件与 Main Process 转发。
- 完成状态必须来自结构化证据，不以一段非空模型文本替代。
- 上下文采用稳定前缀、追加式事件、动态尾和低频压缩；过期事实由版本/有效性控制，不靠模型猜测。
- SQLite 是状态存储，不是逐 token Trace sink。高频事件先聚合或保留在内存，持久化受写入预算与保留策略约束。

更细的内部技术报告保留在本地维护区，不属于公开发行内容。
