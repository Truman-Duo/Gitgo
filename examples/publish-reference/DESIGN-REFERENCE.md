# /publish 具体设计建议

此文提供实施起点，**不锁定模块名称、数据库表、API 名称、页面布局或所选开源库**。负责人可提出更好的方案；要保持 [需求与验收](REQUIREMENTS.md) 中的业务语义、事实一致性和正式终端体验。

## 1. 总体结构：流程工作入口与共享业务服务

建议把 /publish 看作协作与版本工作的入口：默认展示需要推进的事项和当前上下文，按需进入节点、历史、材料或讨论。它既不是独立通用 Todo 产品，也不是新的 Agent 主循环。不要为了此功能先实现一个任意可编程流程引擎。

```text
正式 Dashboard / 用户明确命令 ── 应用适配 ──┐
                                          ├─ 同一发布/集成业务服务
Agent（主进程或受委派子进程）── 工具适配 ──┘        │
                                    规则/授权 + 固定候选/证据
                                                   │
                                  Git / 协作平台 / 构建 / 产物操作
                                                   │
                                      真实结果 → 状态 → 流程投影

Runtime：任务调度、暂停、恢复、执行上下文
Host：正式接入、装配和进程宿主
```

逻辑职责不要求各起一个进程：

| 责任 | 推荐落点与边界 |
| --- | --- |
| 需求理解、审阅质量、是否接手/委派 | Agent 或真实用户；预检查不能证明“代码一定值得合入” |
| 已声明步骤的推进、取消、恢复、负责人关联 | 既有 Runtime；不另建独立 Publish Agent 调度器 |
| 固定候选、diff、测试、预演、ref 操作、平台/包操作 | 业务服务与工具能力；既可由 UI 用例调用，也可由 Agent 工具调用 |
| 权限、批准对象与版本、允许转换 | 既有 Policy/Authorization；模型意见与页面状态均不产生授权 |
| 候选/操作结果/证据与事务 | 既有 State/Evidence，Git 保有其对象/refs 事实，平台保有其外部对象事实 |
| 请求、已提交事件、投影更新 | 既有通信/事件契约，统一身份和因果；通知丢失后重新读取，不重复效果 |
| 焦点、草稿、导航、显示 | 正式 Dashboard；UI 状态不替代已提交决定 |
| 生命周期与连接 | Native Host 负责装配和宿主边界，不成为“所有非模型业务”的总管 |

主进程可自行审阅或继续原子进程的返工；用户不应为查看一次进度被迫切回主对话。节点中显示同一任务的状态和必要材料，需要语义讨论时跳到正式会话/负责子进程并保留返回位置。不要复制一套会话历史、审批卡和确认状态。

## 2. 对象与流程投影

建议用一个“工作事项”将已有对象关联起来，而不是把所有状态压成 `todo/done`：

- 长期事项/Issue：目标、负责人、讨论、完成条件。
- 提案与不可变候选：源仓、OID/范围、base、目标、依赖、来源身份。
- 计划：去向、目标完整 ref、集成方法、自动推进边界、配置版本。
- 操作：请求 ID、事项版本、任务/进程、调用者、预期 tip、批准/证据引用、结果和恢复状态。
- 审阅与证据：判断针对哪个候选；检查针对哪些输入和策略；保留旧记录与失效原因。
- 集成结果与后续工作：采用了哪些内容、结果 OID、进入哪个角色/分支、剩余责任。
- 分支、版本、构建、产物、分发：保留独立身份与生命周期，再关联到事项。

一个事项可以同时处于“审阅需完善、已接到加工分支、尚未正式接纳、未发布”。列表摘要和小流程由这些事实派生，不能反向修改事实来让图变绿。

参考节点：准备 → 检查 → 去向 → 交接/正式草稿 → 执行 → 结果；可选推送/构建/分发作为相关后续阶段。不同类型可使用不同流程模板，节点 ID 稳定，不靠列表序号寻址。节点状态建议区分未开始、可执行、进行中、待输入、受阻、失败、完成、不适用、结果待核对；由真实状态推导。

计划修改只作用于未执行部分；候选、目标、依赖或策略变动时重新计算适用证据。执行中的步骤在明确边界响应暂停，不宣称中止已发出的远端效果。为已发生步骤记录补偿/新计划，不改写过去。

## 3. 后端契约建议

优先扩展既有应用服务和工具适配。以下是**待设计的语义接口**，不是已存在产品 API 名称：

| 用例 | 输入/输出应包含 |
| --- | --- |
| 列表与节点查询 | 项目/目标/权限范围、快照/分页、稳定事项 ID、当前版本、观察时间、流程投影、允许动作及不可执行原因 |
| 准备材料 | 固定源、base、目标 tip、diff/变更路径、检查配方、策略版本、依赖和证据身份；只读页面本身不触发 |
| 修改计划 | 对象版本、目标、方法、自动步骤/停止边界；返回失效证据、需重做部分和新的计划版本 |
| 请求审阅/返工 | 同一候选与材料引用、实际 task/process/parent、原会话上下文；复用任务接纳/反馈路径 |
| 执行/恢复 | request ID、固定候选、计划版本、预期目标 tip、授权上下文；返回操作 ID 与真实状态，长任务异步 |
| 评论和决定 | 平台实例/账户/对象/修订/线程、正文或决定；普通评论不等于批准；草稿和已发送分开 |
| 读取操作结果 | 结果 OID/远端对象/产物 digest、证据、错误类别、允许的恢复动作、核对状态 |

命令示意（仅说明字段关系，正式实现自行映射）：

```json
{
  "request_id": "client-generated-unique-id",
  "item_id": "stable-item-id",
  "expected_revision": 12,
  "candidate_id": "immutable-candidate-id",
  "plan_revision": 3,
  "target": {"repository_id": "stable-repository-id", "ref": "refs/heads/integration/x", "expected_oid": "full-git-oid"},
  "action": "adopt_to_work_branch"
}
```

调用者身份由经过认证的调用上下文确定，不能信任 JSON 里的 `actor=maintainer`。能力查询用于提示，执行时重验。请求 ID 绑定规范化参数与作用域；复用 ID 但不同参数应冲突，不返回另一操作的“成功”。

流程投影需要同一对象版本、当前节点、各节点事实状态、前置条件/证据引用和允许动作；前端不维护第二套状态机。事件包含项目、事项、操作、修订和因果标识；初次快照、增量与断线重读要衔接，旧项目响应不能覆盖新场景。

### 确定性前置工作

建议先为审阅者准备：固定源与目标、变更摘要/diff、作者/关联事项、政策允许的检查、依赖、冲突预演、来源映射与检查日志。检查运行需复用执行隔离；作者提供的测试也是待执行代码，不能因其叫“检查”就绕过规则。

这些材料回答“改了什么、检查了什么、目标是否适用”。是否值得采用、如何拆分、需要怎样沟通由用户/Agent 判断，不能用测试全部通过代替审阅。

### 写入与恢复

1. 记录操作意图，固定候选/目标与有效授权，验证期望版本。
2. 在隔离上下文计算组合结果；将检查绑定到最终树与配方。避免提前移动正式 ref。
3. 写入前重新核对目标 tip/必要证据；本地 ref 用条件更新。远端按平台实际支持的条件操作执行，不泛化为分布式事务。
4. 记录可核验结果与来源映射，提交事实后更新投影。
5. 故障区分“未执行”“已执行”“结果未知”。Git/平台效果已发生而数据库未写完时，恢复先核对 refs/远端操作身份，不能简单重跑。

保存草稿、审查、工作分支、Formal 写入、push、tag、包分发是不同效果边界。多目标失败要逐目标报告和恢复。可编辑评论与不可编辑的操作事实分开，旧批准不能因改评论文字自动扩大范围。

## 4. 前端 HTML 参考的用法

建议默认一个事项列表，选中项内展开小流程，展示当前/已完成/受阻节点与三角色去向；进入节点后主内容区专注于一项处理。历史图、diff、讨论、设置通过动作菜单进入，再回到原选择。

原型采用上下选事项、左右选节点、Enter 进入、Esc 返回；页头进入前后使用不同焦点色，底部固定动作与填写分开。这些是**可试验的方案**，负责人可调整行密度、节点数量、键位、栏目、图示和场景划分，只需遵守正式终端的既有输入/视觉体系和业务验收。不要把截图尺寸、CSS 像素、SVG、所有命令名称列为硬性实现条件。

三类边明确分开：

- 流程关系：准备、检查、决定、执行的前置条件。
- 三角色流转：哪个固定候选计划/实际进入哪个目标，附实际结果 OID。
- Git parent：真实提交 ancestry；squash/cherry-pick 的来源映射另列，不画成不存在的父边。

详细 Git 图可参考 [Serie](https://github.com/lusingander/serie) 的稳定线道和简洁节点并横向组织。TypeScript/Ink 的图布局项目可作候选，负责人重新核对维护状态、许可、拓扑/分页和适配成本。Bun/Node 可用不代表终端有 SVG 通道；先提交真实终端中的可行样例，复杂图不应阻塞基础流程操作。

## 5. 当前公开源码定位

以下链接固定为原型核对基线，说明复用入口；**不表示远端最新主线毫无变化**。开工时与当前 master 比较。

| 现有入口 | 参考方式 |
| --- | --- |
| [PublishPanel](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/cli/dashboard/src/components/PublishPanel.tsx) | 三项配置的字段、编辑与 Trial/Formal 入口，迁移时保留原含义 |
| [TrialPanel](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/cli/dashboard/src/components/TrialPanel.tsx)、[FormalPanel](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/cli/dashboard/src/components/FormalPanel.tsx) | 当前业务操作与缺口，不把旧数据结构当作新需求已经完成 |
| [tools.ts](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/cli/dashboard/src/backend/tools.ts)、[应用服务](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/backend/core/application/services.py) | `publish.get/set`、`trial.list/triage`、`formal.list/edit_message/delete/dissolve` 等已有调用，逐步扩展稳定身份与业务结果 |
| [SyncSession](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/backend/core/sync_session/session.py)、[syncpush](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/backend/core/sync_session/syncpush.py) | 当前同步/推送链；核对实际内容选择、出站检查和恢复边界 |
| [remote](https://github.com/Truman-Duo/Gitgo/tree/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/backend/remote) | GitHub/GitLab 连接器起点，补平台实例、账户、评论、分页、错误与条件操作 |
| [页头](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/cli/dashboard/src/components/HorizontalHeaderStrip.tsx)、[CommandBar](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/cli/dashboard/src/components/CommandBar.tsx)、[theme](https://github.com/Truman-Duo/Gitgo/tree/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/cli/dashboard/src/theme)、[input](https://github.com/Truman-Duo/Gitgo/tree/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/cli/dashboard/src/input) | 正式颜色、焦点、字宽、输入与 footer，优先复用而非移植 HTML 样式 |
| [主会话接纳](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/cli/dashboard/src/chat/sendChat.ts)、[AgentDetail](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/cli/dashboard/src/components/AgentDetail.tsx)、[delegation](https://github.com/Truman-Duo/Gitgo/blob/c8e7906e6ed5e23af29ebdbc51e4bfafe696926b/backend/core/loop/protocol_tools/delegation.py) | 关联正式主/子进程、反馈和继续原任务，不能以本地演示 notes 代替 |
| [贡献说明](https://github.com/Truman-Duo/Gitgo/blob/master/CONTRIBUTING.md) | 分支、提交、PR 与正式验收规范；产品 Formal 目标不能硬编码成本仓库的约定 |

## 6. 演示后端与产品后端的差距

示例实际暴露 `GET /api/state`、`GET /api/evidence`、`POST /api/command`。命令使用 `request_id/action/item/version/args`；`workflow` 是结果投影。可阅读 [service.py](demo/service.py) 的 `workflow_view`、`step`、`apply_result`、`command` 与 [集成检查](demo/test_flow.py) 理解闭环。

值得参考：固定候选、预期 tip、组合预演与检查、失败候选接手仍保留失败、重复请求、修订失效、页面不驱动成功状态。

不能直接复制：可选择的示例身份、固定三仓/master、单进程全局锁/SQLite JSON、本地 notes、AST helper 装载、有限故障恢复。示例没有真实 Agent 与平台权限，幂等回执也没有覆盖 Git/数据库之间所有崩溃窗口。正式实现应结合现有任务/证据存储做恢复核对，不能用“示例通过”代替生产完整性论证。

推荐按纵向场景实现并验收：先一条失败贡献接手与一条本地产出正式整理，随后补真实评论/返工、两个候选组合、清理、包和第二平台。每阶段同时交付前后端，避免直到最后才发现对象身份、焦点和执行边界对不上。
