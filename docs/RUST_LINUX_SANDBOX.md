# Linux 原生资源边界：Rust 迁移

本阶段依赖前两阶段的原生模块身份和安全读取实现。保持 Host 的工具准入、权限审批、执行合同和既有持久恢复记录；资源 token 是进程内所有权，不是访问许可，也不保存成另一套审批或恢复账本。

## 各层职责

- Host 继续选择工作区、只读运行时挂载、命令、环境和预算，使用标准 Popen 管理传输。不能由模型选择运行时路径、继承描述符或无隔离入口。
- 固定且受信的系统 bubblewrap 继续实施 user/PID/network/IPC/UTS 与文件系统隔离。不是用不完整的 Rust unshare 或单个进程组替代原有边界。
- Rust 持有实际 cgroup v2 根、调用组和成员文件对象，配置整树限制、监督聚合 CPU、终止整树并确认退休。Linux 资源扩展版本为 `linux_cgroup_version=2`。
- Rust 使用受信系统 libseccomp 构造原有 socket 策略，输出不可修改的匿名 BPF 文件。`linux_filter_version=1`。过滤器只交给 bubblewrap，绝不安装到 Host 自身。
- C ABI 仍为1。`linux_execution=false` 继续表示模块不提供独立的通用执行接口；它不是应用层 Linux 工具执行被关闭。应用仍经上述既有隔离入口运行，不存在原生调用失败后直接 Popen 运行工具的后备路径。

## cgroup 所有权与资源限制

操作员须先配置 `/sys/fs/cgroup` 下独立的委托子树，并启用 cpu、memory、pids 控制器。程序不会自动提升权限、创建系统委托或修改祖先控制器。

Rust 检查实际文件系统为 cgroup2，使用目录相对且禁止链接/逃逸的对象打开。每个调用独占创建 `gitgo-<恢复标识>`，设置 memory.max、禁止 swap、整组 OOM、pids.max 及既有 cpu.max。内核能力缺失或任一配置失败时拒绝启动；已产生的资源保留所有权直到清理得到确认。

成员文件由 Rust 固定，Host 显式将其描述符交给 bubblewrap 的 `--bind-fd`，避免按可替换的路径重新选择对象。载荷只能向自己的调用组加入私有 PID 命名空间内可见的进程，不能借此改预算或离开调用组。bubblewrap 消费并关闭两个显式传入的原生描述符（成员文件及 BPF），工具不继承 Host 的其他描述符。

聚合 CPU 预算来自 cgroup 的 user+kernel 记账，不是某个 PID 的 rlimit。Rust 保留启动器的 pidfd，每约50ms观察退出和 CPU；记账失效或超过预算时终止调用组。最终记账查询也检查预算，避免 leader 恰好在采样间隔内退出而掩盖超限。每进程 rlimit 仍作为补充，不冒充整树限额或严格实时的零延迟 CPU 截止。

退休先使用 cgroup.kill，再观察整个后代树的 populated=0，停止并回收原生监控，最后核对目录身份并移除实际调用组。仍有成员、对象被替换、记账/退休失败或监控回收未知，都不能报告成功。token 单调递增且不复用；活动调用组上限1024，过滤器上限128。

受保护 Git 的目录 pin 也复用 Rust 目录对象，通过 `linux_directory_version=1` 借用 O_PATH 描述符做原有挂载。描述符不转移所有权，Root 在整个 Git 调用期间由 Host 保留；文件 token 或退休的 Root 不能取得目录描述符。目录替换不改变已固定对象。配置快照内容生成、既有密封 memfd 和 Git 语义编排继续沿用原链路。

## seccomp 生命周期

从固定系统目录选择 root 拥有且不可被 group/world 写入的 libseccomp；拒绝工作区库、PATH/LD_LIBRARY_PATH 选择及未解析的必需符号/系统调用。上下文分别使用 seccomp_release 和 dlclose 管理，匿名文件使用标准 File 所有权。

保留原来的 AF_INET/AF_INET6 私有网络命名空间策略、仅 AF_UNIX stream socketpair 的本地 IPC、其他域/类型拒绝、io_uring 拒绝及错误架构终止策略。BPF 文件有大小边界，并密封写入、增长、缩短及后续 seal 修改。构造失败不返回可启动描述符；关闭结果未知保留 Python 所有者并拒绝自动重放。

## 兼容与恢复

需要支持 openat2、pidfd、cgroup.kill 的 Linux 内核和支持描述符挂载的受信 bubblewrap（与既有受保护 Git 的0.11.1要求一致）。支持目标为 x86_64/aarch64；操作员委托、发行版 user namespace/AppArmor 配置等前提继续适用，不自动解除系统策略。

Publish 的既有独占 lease、恢复标识与 location 核对仍是冷恢复入口。恢复只退休精确命名的组，未知执行不重放；已经完成的业务结果与未完成的资源清理分别保留。Host 崩溃、强制终止或断电后的恢复依赖既有记录和内核边界，不能仅用 RAII 或监控线程作证明。

无业务 schema 迁移。原生模块变化继续进入已有运行实现摘要，使旧实现授权失效；回退必须整体恢复匹配的 Python 与原生模块，并先处理未确认的调用和清理责任。

## 验证边界

专项工作流分别在实际 x86_64/aarch64 委托环境运行原生核心、工具正常/攻击/拒绝、累计预算、取消和恢复测试。随后独立构建 frozen Host，直接在真实可执行程序中验证原生模块及累计 CPU 超限不能伪装成功；最终发行包和完整平台 CI 仍分别验收。

平台条件跳过、模拟故障和真正的 OS 执行分别统计。源码或交叉编译通过不等于最终包通过；本说明也不代表 macOS 通用执行/受保护 Git 已实现，或 Issue #45 全部完成。

依据：[Linux cgroup v2](https://cdn.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)、[bubblewrap](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)、[libseccomp API](https://github.com/seccomp/libseccomp/blob/main/src/api.c)。
