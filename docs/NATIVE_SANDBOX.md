# 原生工具执行隔离（#17）

Shell、exec_command、run_command、run_test、pure/privileged authored 工具在
ProcessToolRunner 启动时进入 OS 沙箱。Git、搜索、patch、formalize 与旧版组合 runner 也进入此边界，防止 hooks/子步骤绕过。注册测试使用同一入口；授权不能关闭隔离。
普通文件工具和匿名公共网络检索仍沿用各自的权限边界。

## Windows 10+：AppContainer 与 Job Object

CreateProcessW 的 SECURITY_CAPABILITIES 创建无网络 capability 的 AppContainer。
HANDLE_LIST 只继承三个标准输入输出管道；JOB_LIST 在用户代码执行前绑定 Job，
防止“先运行、后绑定”窗口。Job 限制整棵树的进程数（32）、提交内存（512 MiB）
及累计用户态 CPU 时间，并将 CPU 带宽硬上限设为不超过系统一颗逻辑 CPU 的份额；
嵌套的父 Job 若已限流，实际份额可能更小。启用 kill-on-close，禁止 breakaway。
Host 根据原生 Job accounting 的用户态与内核态计数检查整组总 CPU 预算；
超额或查询失败关闭 Job，不能只依赖用户态计时。终端或 Host 消失、
取消、超时和正常退出都会关闭 Job；沙箱及输出预算失败没有无隔离回退。

文件访问由 AppContainer SID 与 DACL 双重检查。只给专用运行时 RX，
只给本项目工作区 M；目录联接和符号链接的目标仍需通过 OS ACL。
Windows 本身向 ALL APPLICATION PACKAGES 开放的系统资源仍可访问，
因此这不是“系统文件一律不可读”的容器。

### 显式配置

使用专用、可丢弃的工作区和专用 Python/安装运行时，不要把用户主目录、
磁盘根目录或包含密钥的通用 Python 环境授予沙箱。Source 模式还需对独立
Gitgo source root 授予 RX；运行时必须包含标准库、DLL 与项目依赖。
venv 模式还需其专用 base Python 运行时；冻结版需要安装目录。
Host 在构造策略和实际启动前拒绝工作区与可信源码、Python 或冻结运行时目录的父子重叠，按真实路径检查链接/目录联接别名。配置脚本同样在创建 profile 或修改 ACL 前检查显式运行时路径。开发 Gitgo 自身也必须从工作区外的可信安装启动 Host，不能让正在编辑的检出副本同时充当受信任运行时。此路径检查不等于完整的安装供应链或硬链接审计。

先在受信任的操作员终端预览配置：

```text
python scripts/provision_sandbox.py --workspace PROJECT --runtime DEDICATED_RUNTIME --runtime GITGO_SOURCE
```

检查路径后添加 --apply。预览只建立 AppContainer profile，不修改 ACL。
应用会为该 profile 增加继承 ACE，并把工作区设为 Low integrity。
这会改变工作区安全描述符，必须使用专用工作区；Agent 工具不会自动执行配置。
不继承 ACL 的已有文件需要操作员另行审查配置，失败时工具保持拒绝。

清理时先停止所有项目进程，再删除专用工作区和运行时；若需保留目录，
使用配置输出的 SID 撤销其 ACE，并恢复原始 integrity label。
不要未经记录地覆盖已有 ACL；配置脚本不承担通用 ACL 备份/恢复。

### Shell 迁移

Windows shell_script 使用系统 PowerShell（NoProfile、NonInteractive），
Linux 使用 Bash（noprofile、norc）。Windows 调用方必须把 Bash 脚本迁移到
PowerShell 语法并重新获得参数绑定的授权。Git Bash/MSYS 的共享全局对象
命名空间与 AppContainer 不兼容，不能通过退出沙箱来兼容。
PowerShell 模块搜索固定到系统引擎的 Modules 目录，并显式加载系统 Utility/Management 模块；避免用户模块路径、PowerShell 7 模块及 Windows Server 自动加载差异。
基础模块加载后建立仅以本次工作目录为根的 Gitgo: provider drive 并进入该目录；无需读取磁盘根目录或项目祖先。输出使用 UTF-8，初始化失败会中止脚本。
exec_command 仍接受明确的 argv；不兼容的外部程序返回执行错误。

## Linux：bubblewrap

安装系统 bubblewrap 并启用其所需的用户 namespace。Host 固定查找 /usr/bin/bwrap 或 /bin/bwrap，解析真实路径并要求可执行常规文件、文件及所有父目录由 root 所有且不可被组/其他用户写入；不从 PATH、工作区或模型参数选择启动器。此检查建立文件系统来源边界，不声称验证软件包签名。空根目录中只挂载只读
系统运行时、专用 Python/Gitgo source，以及可写项目工作区；/tmp 为私有
tmpfs，/proc 对应私有 PID namespace。网络、IPC、用户等 namespace 独立，
删除所有 capabilities，使用 die-with-parent/new-session。
每次调用使用独立 cgroup v2：整棵树的 memory.max/memory.swap.max、pids.max、cpu.max；Host 根据 cpu.stat 汇总累计 CPU 预算。执行前加入 cgroup，再设置地址空间、每进程 CPU 和 core dump 的补充限制。取消使用 cgroup.kill；原生 PID namespace 负责 Host 崩溃时的后代清理。不使用共享 UID 的 RLIMIT_NPROC 作为调用级限额。

受信任的 Host 必须运行在已授权委派的 cgroup scope 或 service 内，并设置 GITGO_SANDBOX_CGROUP_ROOT。缺失 cpu/memory/pids 委派或 cgroup.kill 时明确拒绝，不降低隔离。

例如，在支持用户 cgroup 委派的系统中：

```text
systemd-run --user --scope -p Delegate=yes python scripts/run_linux_sandbox_scope.py -- python -m backend.core.native_host_entry
```

包装器仅将自身移入委派单位的 host 子组、启用已委派控制器，并启动指定 Host；不会申请 root 或修改祖先 cgroup。若系统管理员未委派控制器，需要管理员先配置。scripts/provision_linux_sandbox.py 可预览显式独立子树配置；独立子树还需管理员将受信任 Host 置于该子树内，不能仅导出变量后跨委派边界迁移。

cgroup namespace 保留 Host 所在的命名空间以便加入委派的兄弟组；沙箱不挂载 /sys，只暴露本次 cgroup.procs，可将可见进程移入本次组，不能修改限制或迁出。

Linux source/frozen 使用同一私有 Python role 启动协议。macOS 后端尚未完成，保持 SANDBOX_UNAVAILABLE。
Linux 集成验收由专用 CI 执行；Windows 本地验证不能替代该验收。

系统级 systemd 服务必须设置 User= 为运行用户并启用 Delegate=yes；仅对系统级 scope 指定 --uid 并不保证目录所有权委派。CI 使用委派服务启动非 root 验收。

整组累计总 CPU 的 Host 检查间隔为 50 ms（Windows Job accounting、Linux cpu.stat）；调度和检查间隔可能带来超额，不能宣称逐 CPU tick 精确终止。内存、进程数和 CPU 带宽等内核硬限制与累计 CPU 检查分别验证。

Windows 的 CPU rate control 在部分启用 DFSS 的远程桌面服务环境中不可用；无法应用所需 Job 限额时拒绝启动，不放宽策略。

## 授权与收据

新 grant 同时绑定 process/task、工具契约摘要（含 authored 版本和源摘要）、
参数摘要、资源和 30 分钟期限。预检与执行使用同一匹配函数；一次性授权消费后
不能复用，敏感工具不继承旧的无版本授权。权限只能进一步收窄或允许调用，
不能绕过文件 ACL、网络 namespace 或资源限额。对外部资源的审批也不会自动
修改 OS ACL；当前原生执行配置仅支持已配置的本项目工作区，额外访问保持拒绝。

调用证据写入绑定项目的外部 Host 状态目录，按工作区真实路径区分 worktree；不再把工作区 .gitgo/tool_invocations 当成权威日志。生产写入和恢复均使用已绑定的 StorageRuntime 路径；若状态目录落入可写工作区或被挂载的可信运行时树则拒绝建立日志，工具不得启动。旧工作区日志保留供人工核对，不自动搬迁为可信证据；恢复遇到旧日志会标记 LEGACY_INVOCATION_JOURNAL_UNTRUSTED、unknown/ambiguous 并要求人工核验。这只解决调用日志真实性边界，Git hooks/config、项目身份及其他工作区元数据仍需独立 OS 保护。

授权期限是调用准入期限；已启动工具仍受调用超时和任务取消约束。
尚未启动的拒绝记录 not_committed；已执行后被取消、输出超限或异常退出，
收据保留 ambiguous（未知），不能宣称副作用已回滚。

进入 OS 沙箱后，runner 将 Home、AppData、Temp 与 PowerShell 模块缓存重定向到工作区 .gitgo/sandbox，避免启动时尝试写入真实用户目录。原始 LOCALAPPDATA 仅用于 Windows 建立 AppContainer profile。

Host 持续排空 stdout/stderr，每路最多保留 2 MB；超过预算终止进程树，
避免子进程无限输出耗尽 Host 内存。输入写入和输出读取均可被取消。

稳定错误码：GITGO-E3601..E3605，分别对应不可用、启动拒绝、策略无效、
输出超限和执行失败。结果包含配置建议与 request_permission 恢复入口。
网络被拒绝不意味着可授权关闭沙箱。

## 验证

```text
python -B -m pytest tests/test_native_sandbox.py -q
```

Windows 测试使用临时独立 Python 与项目 ACL，覆盖正常执行、读写越界、
目录联接、网络可达性对照、内存/进程限制、breakaway、后代进程清理，
以及真实 JSON runner 的 exec、PowerShell 和 privileged authored 路径。
现有目录/版本/授权语义测试可显式 mock OS 启动；这些不作为隔离证据。

## 原语参考

- [Microsoft AppContainer launch contract](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)
- [Microsoft Job accounting](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information) 与 [CPU bandwidth control](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_cpu_rate_control_information)
- [Microsoft process attributes: SECURITY_CAPABILITIES, HANDLE_LIST, JOB_LIST](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)
- [bubblewrap security model and namespace options](https://github.com/containers/bubblewrap)

新增验收使用真实 Host 强制退出，以及实际 PyInstaller onedir 产物；打包测试不通过伪造 sys.frozen 冒充。三平台完整原生发行验收尚需补齐 macOS 及签名/安装环境。

## 验收矩阵与 macOS 边界

| 验收边界 | Windows | Linux | macOS |
| --- | --- | --- | --- |
| 工作区执行、文件越界、链接、网络 | 原生安全测试 | 原生安全测试 | 独立系统能力探针；生产入口未开放 |
| 整组内存、进程数、CPU 预算 | AppContainer/Job 原生测试 | namespace/cgroup 原生测试 | 尚未实现 |
| 取消、超时、输入堵塞、输出超限 | 真实 ProcessToolRunner 测试 | 真实 ProcessToolRunner 测试 | 尚未实现 |
| Host 强制退出与脱离会话的后代 | 原生安全测试 | 原生安全测试 | 尚未实现 |
| 实际 onedir Host 执行、越界读取、网络 | 实际构建后验收 | 实际构建后验收 | 尚未实现 |
| 完整安装、签名、公证、升级卸载、终端矩阵 | 本 PR 未覆盖 | 本 PR 未覆盖 | 本 PR 未覆盖 |

Host 已观察到取消时不会启动工具；此时收据为 not_committed。工具启动后再取消或超时，副作用仍为 ambiguous。回归包含流水线准入后、实际启动前收到取消的场景，不能只检查流水线最初的任务状态。

macOS CI 的 capability audit 在 Intel、Apple Silicon 与不同系统版本上运行 scripts/probe_macos_sandbox.py，输出独立报告。探针验证执行正向对照、文件/符号链接与网络拒绝，观察脱离会话的后代能否在仅 killpg 后存活，以及地址空间限额在当前内核和解释器上的行为。它还确认生产入口保持 SANDBOX_UNAVAILABLE。该任务通过只表示审计成功，不表示 macOS 后端通过验收。

当前 macOS 15 Intel、macOS 15 Apple Silicon、macOS 26 Apple Silicon 的实测均观察到：仅 killpg 后脱离会话的后代仍存活，当前 Python 运行时将 RLIMIT_AS 设置为 512 MiB 被系统拒绝。这些结果说明当前方案不能直接提供生产所需的边界，不代表所有系统版本和运行时都无法设置地址空间限额。

Seatbelt 的文件/网络限制不能单独证明完整进程树清理或整组资源限制。不能用进程组清理、轮询累计内存或只设置每进程 rlimit 冒充 Windows Job/Linux cgroup 的全部语义。探针的加载器规则只允许读取根目录本身，不开放其全部后代；文件与网络负向测试必须在真实解释器成功运行后执行。

若后续采用 macOS 虚拟化来提供这些强边界，还需配置可用的 Mac 测试环境与原生 macOS guest，验证原生工具兼容、仅工作区共享、无外部网络、Host 崩溃时整个 guest 的退出，以及进程数、内存、CPU 的实际限额。报告中的 hypervisor_available 只是系统能力信号，不能代替实际启动和安全验收。

能力审计还会编译并临时 ad-hoc 签名 scripts/probe_macos_virtualization.swift，直接调用 Hypervisor 的 hv_vm_create/hv_vm_destroy，记录 Virtualization.framework 的 isSupported。编译、签名或清理失败必须使审计失败，不能伪装成硬件不支持；创建被拒绝则记录原生状态码。该探针不启动 guest、不添加网络或共享目录，也不改变系统配置。成功创建空 VM 仍不等于 macOS guest 启动或生产后端验收。Apple Virtualization 的原生 macOS guest API 面向 Apple Silicon；Intel 的 Hypervisor 创建成功不能证明其具备该 macOS guest 后端。


### 主进程退出

调用主进程退出后，Windows 原生监控会关闭唯一的 kill-on-close Job 句柄，
Linux 原生监控会终止剩余 cgroup 成员，不依赖调用方开始读取 stdout/stderr
或执行最终清理。这补齐正常返回、取消、超时和 Host 崩溃的生命周期保护；
持有输出管道的后代不能无限延长调用。Host 退出检测与资源核算均使用 50 ms
轮询间隔，因此不能保证主进程退出后没有任何调度延迟。


安全 CI 的 Windows/Linux 恢复验收使用固定 SQLite 3.53.4；下载来自 SQLite
官方站点并校验发布 SHA3-256。scripts/provision_ci_sqlite.py 仅在可丢弃的
GitHub-hosted runner 中配置本次 Python，不修改系统 SQLite，也不禁用项目的
SQLite 安全检查。Linux 为本次 Python 扩展设置私有库路径，不给模型注入
LD_LIBRARY_PATH 的权限。实际构建的 frozen Host 还必须验证打包后的 SQLite
版本及安全检查，不能以源码解释器的结果代替。
