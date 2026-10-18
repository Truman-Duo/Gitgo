# 存储与受保护 checkpoint：能力和限制

更新日期：2026-10-18。范围：现有 StorageRuntime 与 Windows 显式受保护 checkpoint。
本页描述可用接口和恢复边界，不表示完整工具持久化准入或通用 CAS v2 已启用。

## 默认存储

默认 StorageRuntime 保留现有调用入口和迁移 checksum，不自动迁移到受保护路径。
未知格式、无法验证的迁移历史或缺少必要状态时明确拒绝，不能当成空库重建。
受影响写连接在提交结果未知时被隔离，不返回虚假的成功，也不自动重试；
观测数据降级与权威状态失败分别处理。CAS 收集在根引用发现不完整时拒绝。

运行时关闭失败会保留所有权和占用，不能通过缓存淘汰抹去债务。
已有锁与容量描述原语不等于所有 writer/collector 已接入统一协议，也不构成通用磁盘硬配额。

## 显式受保护入口

`StorageRuntime.create_protected_project(registry, processes, workspace_hint=...)` 在已安装并固定身份的 Windows Host/运行时中创建项目。
保留返回的项目和 slot，使用 `StorageRuntime.open_protected_project(...)` 重开同一注册；
工作区提示只是注册元数据，不授予文件访问权。

返回接口提供 `save_agent_checkpoint(snapshot)`、`load_agent_session_state(process_id)`、`recover()` 和确认式 `close()`。
保存必须明确提供 `expected_revision`，新 process 从零开始。
它不自动安装依赖、接受工作区选择的 DLL 或回落到旧 I/O；不支持的通用操作明确拒绝。

| 边界 | 当前受保护接口限制 |
| --- | --- |
| 准备 | 一次串行准备 |
| 编码操作输入 | 最多 16 KiB，另受节点、深度及注册共享预算约束 |
| 每次 checkpoint | 最多 8 个 CAS 对象，每对象最多 64 KiB |
| 项目物理代际 | 最多 32，包含待处理和已被替代的副本 |

达到上限时拒绝，不截断历史或以空数据替代。
这些边界只支持该有限 checkpoint 接口，不代表日常开发任务容量、任意进程硬磁盘配额或默认存储全局容量保障。
当前已限定的 SQLite 构建为 3.53.2/3.53.4，并绑定实际运行时文件身份；版本号相同不自动获得资格。

## 结果与恢复

已提交 checkpoint、独立验证与资源清理分别判断。
工作进程输出仅作诊断；确认其原生生命周期结束后，再由独立读取验证提交事实和内容。
`StorageCleanupIncomplete.committed_reference` 可表达“checkpoint 已验证，但清理未完成”；
无法确认提交验证时返回 `StorageCommitUnknown`，不为找回响应再次发布。

未知启动、提交、对象创建或清理保留债务，不自动接管同名对象、退款或重放。
仍有未结算周期时阻止运行时切换和退役。
数据库 fencing 和持久记录不能替代真实进程退出及原生资源关闭确认。

## 验证与未覆盖范围

验证包含真实 Windows 原生工作进程、独立临时账户、父进程退出、缺失输出、提交/清理故障、版本冲突和容量拒绝。
源码→实际 frozen Host→源码重开的受保护 checkpoint 连续性已有通过记录。
部分故障采用注入，不能据此声称硬件掉电、所有文件系统或全平台恢复已经认证；对应候选见[验证摘要](SANDBOX_ARCHITECTURE.md#验证版本与结果)。

该入口没有迁移默认 daemon/SessionStore，也没有开放 Shell/Git/网络的完整持久化准入。
通用 CAS v2、任意开发工作负载、复杂并发和其他平台的受保护后端不在本轮启用范围。
