# Gitgo Runtime Constitution

本文声明当前 Terminal Preview 的运行时不变量。它不是路线图；违反这些规则的实现应视为缺陷。组件拓扑见 [ARCHITECTURE.md](ARCHITECTURE.md)，具体表结构见 [RuntimeStateModel.md](RuntimeStateModel.md)。

## 1. 单一事实源

同一事实只能有一个权威写入者：

- Native Host 负责协议、项目注册和应用服务边界；
- 每项目 Daemon 负责该项目的任务生命周期与运行事件；
- SQLite 保存关系状态，CAS 保存较大的不可变内容；
- Git 与 linked worktree 保存代码事实；
- Dashboard 只消费投影，不反向创造运行时真相。

缓存、状态栏、项目概览和 Runtime 页面都必须从权威状态派生。读取失败应显示 `Unavailable/Error`，不能伪装成空项目、零用量或已完成。

## 2. 原生协议优先

Dashboard 通过版本化原生协议连接 Native Host。MCP 是给外部 harness 和自动化使用的受限兼容面，不是产品前后端的默认数据面，也不能成为原生能力缺失时的隐式降级路径。

协议操作必须具备稳定的名称、结构化结果、结构化错误和取消语义。界面文字不是协议。

## 3. Main Process / Subprocess 责任

- Main Process 直接面对用户，负责意图对齐、动态路由、协调、审查和最终交付；
- Subprocess 负责一个可持续迭代的任务域或 DAG 节点；
- 简短、单责任人的工作允许 Main Process 自执行；
- 任务中途变复杂时允许将已有事实和责任交接给 Subprocess；
- 同一部件的连续工作优先回到原 Subprocess；
- Subprocess 不绕过 Main Process 私聊其他 Subprocess，依赖变化通过合同、事件和 Main Process 转发。

创建 Subprocess 不是质量证明；测试、收据、审查和用户确认才是证据。

## 4. Provider-neutral Agent Loop

OpenAI Responses、OpenAI Chat Completions 和 Anthropic Messages 必须映射到统一的消息、Reasoning、工具调用和流事件模型。Provider 特有字段留在 adapter 边界，不扩散到 Dashboard、治理或存储层。

会话中途切换 Provider 后，以 Host 保存的当前配置和能力探针为准。旧上下文中的模型身份或能力描述不能覆盖当前事实。

## 5. 工具与权限

所有内置工具、组合工具和自定义工具都经过同一条工具管线：

```text
schema → capability/policy → approval → isolation → execution → receipt → projection
```

- 模型不能自行扩大权限；
- 用户的明确授权在其作用域和有效期内优先于普通治理软门；
- 授权不能伪造工具版本、参数摘要、执行收据或回滚事实；
- 低风险、已由用户目标明确表达的操作不应制造重复审批；
- 等待审批是可恢复状态，不应丢失尚未执行的工具调用。

## 6. 完成与恢复

自然语言声称“已完成”不是完成证据。完成判断应读取任务合同、工具收据、测试证据、Subprocess outcome、未决问题和用户决策。

超时、断线、关闭终端或 Daemon 重启不得被伪装成完成。恢复时：

- 可以重建已持久化的任务树、消息、上下文 epoch 和安全检查点；
- 不自动重放无法证明副作用状态的操作；
- 取消 Main Process 时取消其仍活动的任务树；
- 关闭终端应触发有界关闭，随后由 Host 做强制终止兜底。

## 7. 上下文与缓存

Provider 可见内容按稳定性组装：

```text
稳定 ROM / 能力合同
→ Task Contract
→ append-only 会话事件
→ 本轮动态 envelope
→ 当前输入
```

动态信息只在相关时进入模型上下文；Host-only 元数据留在运行轨迹。大对象进入 CAS，通过 locator 按需物化。相同引用由 session memo 去重；压缩创建新的 `context_epoch`，但不能改写历史事实。

## 8. 持久化与写入预算

- `state.sqlite3` 保存权威关系；
- `observability.sqlite3` 保存有界事件、Trace 和聚合指标；
- CAS 保存 Prompt、Reasoning、工具结果等较大对象；
- token delta、动画帧、成功心跳和原始网络帧不得逐条同步落盘；
- migration 只追加并校验 checksum；
- 数据库大小、WAL、checkpoint、保留期和物理写入速率必须可观测；
- 损坏或版本不安全时失败关闭，并提供备份、恢复或迁移路径。

## 9. 事件与投影

任务、工具、权限、问题、Diff、用量和治理事件首先进入统一的有序时间线，再投影到 Dashboard、Stats 和 Runtime。最终回答是时间线中的后续独立节点，不能覆盖工作轨迹。

Verbose 只改变折叠内容的详细程度：关闭时仍保留有意义的阶段摘要、工具名、简要结果、决策和 Diff；开启时展示更完整的 Reasoning 与工具细节。

## 10. 隐私与发布

密钥、SQLite/WAL、Trace、项目状态和本地技术报告不得进入源码发布。Provider 凭据与元数据分离保存；Windows 凭据使用当前用户 DPAPI。所有 commit/push/publish 路径必须复用统一的内容级隐私策略，不能只依赖文件名或 `.gitignore`。

详细边界见 [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md)。
