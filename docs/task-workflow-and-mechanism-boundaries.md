# 任务计划、执行证据与机制职责

## 工作事项与工程证据

任务计划的步骤 `id` 是当前任务内的工作事项标识。工程工作流的节点用
`work_item_id` 引用这个标识。同一事项可以关联多个证据节点。事项的目标、
业务依赖和验证意图写在计划里，工程节点描述具体要收集的证据，例如注册测试、
用户决定或文件变更。同一事项内仍可指定证据节点的先后关系。

例如，计划中的 `deliver` 依赖 `inspect`，两个工程节点都关联 `inspect`。
关联 `deliver` 的检查会自动等待这两个证据节点。工程节点不需要再填写这两条
依赖。旧节点即使碰巧同名也保持未映射，状态会列出可供确认的节点。
通过 `declare_task_contract` 补充明确的关联可保留原有证据规则。简单任务仍可省略工程工作流。

采纳的新要求可以用 `resolve_changes[].work_item_ids` 指明需重新验证的事项，
即使事项文字保持原样。缺少影响范围时，运行时保守地复核相关工作流的已绑定事项。
事项及其前置事项的变化会传播到关联证据。契约、依赖、当前证据和归档记录在同一个
checkpoint 中提交，提交失败时一并回滚。旧调用即使在提交后才返回，其回执仍绑定
原契约版本。已声明的来源变化后，保留计划的语义仍可要求受影响的证据重新生成。
移除或改写已经接纳的工程证据条件沿用
`engineering_workflow.propose_amendment` 的明确用户决定。

## 两套机制的职责

| 责任 | 固定任务机制 | 通用机制 |
| --- | --- | --- |
| 定义权威 | `loop/mechanism_catalog.py` 校验固定任务组件及其约束。任务契约保存已接纳定义 | `mechanism_runtime/registry.py` 提供组件描述，`capability_declaration.lower` 展开简化声明，`compiler.compile_program` 校验图与显式依赖 |
| 实例状态 | `task_maintenance.state_for` 读取变更、计划和维护状态。`mechanisms.snapshot` 读取所有者会话中的实际推进模式 | `Repository.instance` 读取绑定任务的有效版本、组件状态、在途调用和激活阶段 |
| 激活 | `task_contract.publish_contract` 在任务围栏内同时提交契约、工作流、维护状态和适用的推进模式 | `Coordinator.request` 排队，`settle` 在安全边界选择有效版本，已经在途的调用保留原版本 |
| 失效 | `task_maintenance.synchronize` 检查已声明依赖。`EngineeringWorkflow._refresh` 复核任务证据及其前置条件 | `Coordinator.maintain` 检查组件版本和描述摘要，计算受影响调用。未知执行效果由协调器和 Runner 的恢复链处理 |
| 持久化与恢复 | `mechanism_lifecycle.commit_activation` 使用现有 SessionStore checkpoint。`restore_incomplete_processes` 恢复原任务上下文 | `Repository` 通过项目 CAS 保存定义、实例和回调记录。`Coordinator.reconcile` 保留未知调用，等待核对 |
| 继承 | `task_maintenance.guard` 检查上级任务义务。`mechanism_lifecycle.owner` 定位推进模式所有者 | `Application.requirements` 和 `Coordinator.check_constraints` 检查继承的组件要求与能力约束 |
| 完成消费 | `HostCompletionEvaluator` 消费任务维护门禁与工作流证据 | `DeliveryRuntime` 消费已注册的完成阶段和实际控制结果，最终交付仍经过现有 Host 门禁 |

BTW 的直接采纳和通用机制的收件箱都经 `task_maintenance.add_change` 移交任务范围。
收件箱记录候选的收集和移交，任务维护记录已接纳后的义务。推进模式的消费端读取
会话实例，契约里的定义提供编译输入和来源。PR-C 复用这些入口，保持两套生命周期的职责分工。

## 简化声明的字段归属

Agent 用 `harness_mechanism.catalog` 查可调用组件，填写声明的意图、事件、
调用参数以及必要的条件和数据连线。预览由 Runtime 绑定当前任务的用户来源、
版本和编译图。语义审阅消费相同的编译输入与来源，发布使用原有 CAS 和激活边界。
事件到达后，由 Runner 创建调用记录并保存实际回调结果。命名修订只提交发生改变的调用。
暂停、退出和恢复沿用原实例状态与回执。

对应的源码回归包括 `test_work_item_bindings.py` 中的旧节点对齐、依赖投影、
范围提交回滚和恢复，以及 `test_mechanism_capability.py` 中注册新组件、
简化声明编译、真实 Runner 回调和命名修订。测试使用独立项目与受控组件，
真实模型的字段填写成本需要另行取样。
