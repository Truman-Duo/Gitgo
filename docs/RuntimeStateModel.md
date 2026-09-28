# Gitgo Runtime State Model

本文描述当前 Terminal Preview 的状态分层、权威位置和恢复边界。它是面向维护者的结构说明，不替代表结构 migration 本身。

## 状态分层

| 层 | 典型内容 | 权威位置 | 更新方式 |
|---|---|---|---|
| 配置 | Launcher、Provider 元数据、项目注册、UI 偏好 | 用户级配置与 secret store | 应用服务受控修改；外部编辑按类别热重载或重启生效 |
| 项目标识 | 稳定 `project_id` | Git common dir 的 `gitgo/project-id`；非 Git 项目为 `.gitgo/project-id` | 首次创建后不自动替换 |
| 关系状态 | session、task、process、message、receipt、worktree、依赖、lesson | `state.sqlite3` | 事务 + append-only migration |
| 大对象 | Prompt、Reasoning、工具输入/结果、快照、证据 | CAS | 内容寻址、不可变 |
| 可观测性 | 时间线事件、Trace、rollup、存储指标 | `observability.sqlite3` | 批量、有界写入与保留策略 |
| 代码事实 | 文件、提交、分支、linked worktree | Workspace / Git | 工具管线和 Git 操作 |
| UI 投影 | 项目概览、状态栏、Stats、Runtime、时间线 | 内存 read model | 从以上事实派生，不是权威状态 |

## 稳定身份与物理位置

Git 项目的身份写入 Git common dir，因此主工作区与 linked worktree 指向同一个项目状态。非 Git 工作区使用项目内 `.gitgo/project-id`。

运行数据库不与用户交付物混放。Windows 默认状态根目录为：

```text
~/.gitgo/state/projects/<project_id>/
├─ state.sqlite3
├─ observability.sqlite3
├─ cas/
└─ storage-health.json
```

可以通过 `GITGO_STATE_HOME` 显式迁移状态根目录；不得只移动主数据库而遗留 WAL/SHM。存在两个候选状态目录时，必须依赖迁移收据判定，不能静默选择。

## `state.sqlite3`

当前关系域包括：

- 项目与会话：`projects`、`sessions`；
- 任务与进程：`tasks`、`session_processes`、`process_dependencies`、`process_presentation`；
- 消息与事件：`messages`、`session_events`、`mailbox_messages`；
- 工具与证据：`tool_calls`、`receipts`、`test_evidence`、`tool_result_objects`、`tool_result_owners`；
- Provider 与用量：`provider_attempts`、`task_usage`；
- 治理与知识：`governance_signals`、`history_events`、`lessons`；
- Worktree 与依赖：`worktrees`、`dependency_nodes`、`dependency_edges`；
- 上下文与撤销：`session_lineage`、`session_lineage_heads`；
- 自定义工具：`custom_tools`、`custom_tool_versions`；
- 删除生命周期：`deletion_plans`、`deleted_processes`；
- CAS 目录索引：`objects`、`object_refs`、`storage_kv`。

表只保存适合查询和约束的关系字段；大块正文通过 `*_ref` 指向 CAS。

## `observability.sqlite3`

可观测数据库与权威关系库分离，避免高频诊断写入阻塞任务状态。它包含：

- `events`：有界、用户或维护者可消费的运行事件；
- `event_rollups`：按时间桶聚合的计数和样本；
- `storage_metrics`：state/observability/CAS 大小、剩余空间和健康级别；
- `trace_events`：按 Trace 顺序保存的诊断事件；
- `observability_maintenance`：压缩、保留和维护收据。

Trace 不是会话真相。删除或截断有界 Trace 不得破坏任务恢复、消息历史或工具收据。

## CAS 引用规则

- 内容以 digest 寻址；相同内容复用对象；
- 关系行保存引用，不复制大块正文；
- locator 必须能验证对象存在和媒体类型；
- 垃圾回收只能删除已经证明不可达的对象；
- 缺失引用是显式存储错误，不能返回空字符串掩盖。

## 生命周期状态

进程状态至少区分运行、等待、等待用户、取消中、恢复中、可恢复、需要恢复审查以及各类终态。父任务完成判断必须检查活动子树和必需 outcome；Subprocess 的完成、归档和删除是不同概念。

项目列表读取 read model 或最后已知状态，并携带新鲜度。Daemon 暂时失联时可以展示带时间戳的最后状态，但不能把它改写成 `New`、`Finished` 或空项目。

## 上下文与会话 lineage

每个 session 记录 `context_epoch`。普通轮次向事件历史追加；手动/自动压缩建立新 epoch。`session_lineage` 保存可撤销检查点和父子关系，`session_lineage_heads` 指向当前头部。

`/undo` 或 `/rewind` 只移动到可证明的检查点；它不自动撤销外部网络、Shell 或文件系统副作用。存在未知副作用时必须进入审查或用户决策。

## 删除与归档

- 归档只改变展示状态，不删除任务证据；
- 延迟删除以 `deletion_plans` 表达，包含目标、最早执行时间、清单和错误；
- 删除单个 Subprocess 不等于删除项目；
- 删除项目状态与删除工作区文件是不同模式，必须二次确认并留收据；
- 卸载 Gitgo 不遍历或删除用户项目。

## 写入与恢复约束

1. migration 只追加；已发布 migration 的 SQL checksum 不得修改。
2. SQLite 主文件、WAL、SHM 和 journal 必须位于同一稳定目录。
3. 高频 token、动画帧和成功心跳先在内存聚合，再按边界批量提交。
4. 维护操作使用租约，避免运行任务与 checkpoint/GC 并发破坏状态。
5. 数据库损坏时先复制可恢复文件族与 CAS，再重建 shadow store；不得直接清空原库。
6. Daemon 恢复只重放确定性状态变换，不重放副作用不明的工具调用。

## 维护入口

- schema：`backend/core/storage/migrations.py`
- 路径与项目身份：`backend/core/storage/paths.py`
- SQLite/CAS runtime：`backend/core/storage/runtime.py`
- 存储维护：`backend/core/storage/maintenance.py`
- 恢复工具：`scripts/recover_state_sqlite.py`
- 状态迁移：`scripts/relocate_state_storage.py`
- 发布隐私检查：`scripts/verify_release_privacy.py`

新增持久状态前，先回答：谁是唯一写入者、是否应进入 SQLite 或 CAS、如何迁移、如何恢复、如何限流，以及 Dashboard 应读取哪一个投影。
