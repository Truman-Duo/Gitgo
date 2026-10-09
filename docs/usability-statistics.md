# 后台可用性统计

目的：在正常使用中积累事实，供未来评估治理与交互的可用性。当前不进行 A/B 分流、不打“好用/不好用”分数、不调整模型提示词或权限，也不自动产生优化建议。

源码独立放在 `backend/core/usability/`。复用既有 TraceJournal、`observability.sqlite3` 和 CAS；之前的 `audit_prompt_surface.py` 与新模块共用字符统计逻辑。

SQLite 连接由现有 storage/runtime 入口创建并验证运行时；collector 只负责派生统计的投影、schema、配额与生命周期。它不会构造第二个 StorageRuntime，不执行权威库迁移，读取源 trace 与离线摘要都使用只读连接。

## 生命周期与位置

Daemon 发出 `daemon_started` 后自动启动一个独立线程，正常运行不发消息。每五秒读取已提交的 trace，第一次会补采还在源 trace 中的历史。退出时在任务线程收尾后执行有界补采并关闭自己的数据库连接。既有 Daemon 不会被强制重启；下次启动采用新代码时自动启用。

派生数据跟随既有项目身份，放在外部项目状态目录，而不是源码工作区：

```text
<state-home>/projects/<project-id>/usability/
  metrics.sqlite3
  health.json
```

默认 Windows 位置为 `~/.gitgo/state/projects/<project-id>/usability/`；显式隔离测试沿用 `GITGO_STATE_HOME`。因此正常使用不同终端仍记录到同一项目。Daemon 的 `loop_status` 提供只读 `usability_statistics` 状态字段，未修改前端界面。

## 记录内容

| 类别 | 指标 |
| --- | --- |
| 上下文负担 | 系统说明、当轮信封、契约增量、用户消息、工具 schema 的字符数；工具/消息数量；指令与本轮用户消息的长度比 |
| 执行 | 任务和 Provider 请求次数、可关联的 Provider 耗时、任务耗时、步骤数、最终回答字符数 |
| 交互与治理 | 结构化提问类型、工具调用及错误、完成门禁接受/拒绝、恢复、压缩事件次数 |
| 用量 | Provider 实际给出的 token、推理与缓存统计；不把归一化时补出的零当作已知数据 |
| 分组与关联 | trace/sequence/process 身份、任务类型、角色、模型、协议、提示词/schema 哈希、工具名、任务来源终端（源记录有时才记录） |

不复制用户消息、提示词正文、工具参数/结果、回答、Provider reasoning 或凭据。只在后台读取既有内容来计算数值，新的统计库保留数值与受限元数据。稳定 trace 身份用于去重，不按文本或墙钟排序猜测“这是同一件事”。

字符数不是 token 数，快照字节数不是 HTTP 请求大小。Provider 耗时只在找到同一进程匹配的已持久化请求时记录；缺失值不补零。提问、工具调用或治理事件多，不自动意味着多余或不好用。主观满意度、用户意图是否满足仍需要后续人工标注/反馈。

## 边界、恢复与资源

- 这是既有已持久化 trace 的派生统计。上游 trace 原有的限流、清理和故障可能造成缺口；`coverage=persisted_trace_only`，上游损失数量为 unknown，不宣称完整捕获。
- 每轮后台工作有时间预算，每批最多 64 个事件。只读连接不持有写事务；处理内容和派生落盘都在独立线程。默认 catch-up 每周期约 250ms 的调度预算，单个文件读取/解码不能被强行抢占。前台调用不等待统计完成。
- CAS 内容校验哈希，单请求最多读取 8MiB、最多还原 128 层增量。明细缺失、损坏或超过边界时记录不完整样本，继续处理后面的事件，不报告假的完整数值。
- 样本、每日聚合和游标在同一事务提交。重启、重复读取、源表清理/VACUUM 导致 rowid 改变时，通过源身份检查和样本主键恢复，避免正常补采的重复计数。
- 关联样本保留 90 天，每日聚合保留 730 天，按小时清理。派生数据库主文件上限 64MiB，独立使用 DELETE journal；低于 128MiB 可用空间时暂停采集。数据库满、锁冲突等故障会重试、留诊断，任务继续执行。
- 采集故障通过既有 Daemon 错误事件通道通知，并在 stderr 与 `health.json` 留受限诊断；正常成功不通知。错误通知至多每分钟一次，健康文件常态至多每分钟更新一次，退出强制更新。缺失样本会保留不完整计数，未修复前不会宣称数据完整。
- `backlog_pending` 表示尚有补采积压。强制结束进程时已提交统计仍保留，未完成采集只能依赖下次启动时仍存在的源 trace；不能保证无限期追回已被上游清理的数据。

## 离线读取

在项目根目录，使用 Gitgo Python 运行：

```powershell
& "$env:USERPROFILE/.gitgo/runtime/python/python.exe" -m backend.core.usability --project-root "$env:USERPROFILE/.gitgo/state/projects/<project-id>" --days 30
```

输出 JSON，包含健康状态、每日分组、已知样本数、合计、最小值与最大值。平均值可用 total / known_samples 计算。读取命令使用只读模式，不启动 Daemon/Provider、不创建缺失数据库。后续分析可以直接读取该派生库并按 trace 关联现有证据。

## 验证

覆盖了后台启动、增量快照还原、重启去重、源 rowid 重用、缺失明细后续采集、Provider 未报告字段与真实零的区别、采集失败重试及数据库连接释放。

`scripts/check_terminal_continuity.py --source --host <Gitgo Python>` 使用临时项目与本地 HTTP Provider，经真实 Host/Daemon 完成三轮对话和重启，再断言任务、Provider 请求、用量、完成事件各有三次统计，且无不完整样本。不会使用真实用户项目数据或付费 API。

已重新构建 `dist-terminal/20261008-continuity/` 完整测试包，并通过冻结 Host/Daemon 的同一项三轮 HTTP Provider 验证，自动统计四类事件各三次且无不完整样本。Windows 构建门禁现在也检查该统计结果。源码与打包后的后端都无需额外命令启用采集；已经运行的旧 Daemon 需要正常关闭后使用新版本启动。

2026-10-09 上传准备阶段将 SQLite 连接纳入 storage/runtime，随后后端全量 1186 项测试通过、2 项跳过；源码 API 检查与重新构建的 `dist-terminal/20261009-collaboration-baseline/` 冻结后端检查再次通过。统计线程仍独立，只读源数据库，不增加模型交互。

这是后端验收，不作为可见终端渲染证明。该目录是可执行测试包，本次没有生成或安装新的安装器。
