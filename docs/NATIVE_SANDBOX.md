# 原生工具执行隔离（#17）

Shell、exec_command、run_command、run_test、pure/privileged authored 工具在
ProcessToolRunner 启动时进入 OS 沙箱。Git、搜索、patch、formalize 与旧版组合 runner 也进入此边界，防止 hooks/子步骤绕过。注册测试使用同一入口；授权不能关闭隔离。
普通文件工具和匿名公共网络检索仍沿用各自的权限边界。

## Windows 10+：AppContainer 与 Job Object

CreateProcessW 的 SECURITY_CAPABILITIES 创建无网络 capability 的 AppContainer。
HANDLE_LIST 只继承三个标准输入输出管道；JOB_LIST 在用户代码执行前绑定 Job，
防止“先运行、后绑定”窗口。Job 限制整棵树的进程数（32）、提交内存（512 MiB）
及累计 CPU 时间，并启用 kill-on-close，禁止 breakaway。终端或 Host 消失、
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

安装系统 bubblewrap 并启用其所需的用户 namespace。空根目录中只挂载只读
系统运行时、专用 Python/Gitgo source，以及可写项目工作区；/tmp 为私有
tmpfs，/proc 对应私有 PID namespace。网络、IPC、用户等 namespace 独立，
删除所有 capabilities，使用 die-with-parent/new-session。
每次调用使用独立 cgroup v2：整棵树的 memory.max/memory.swap.max、pids.max、cpu.max；Host 根据 cpu.stat 汇总累计 CPU 预算。执行前加入 cgroup，再设置地址空间、每进程 CPU 和 core dump 的补充限制。取消使用 cgroup.kill；原生 PID namespace 负责 Host 崩溃时的后代清理。不使用共享 UID 的 RLIMIT_NPROC 作为调用级限额。

受信任的 Host 必须运行在已授权委派的 cgroup scope 内，并设置 GITGO_SANDBOX_CGROUP_ROOT。缺失 cpu/memory/pids 委派或 cgroup.kill 时明确拒绝，不降低隔离。

例如，在支持用户 cgroup 委派的系统中：

```text
systemd-run --user --scope -p Delegate=yes python scripts/run_linux_sandbox_scope.py -- python -m backend.core.native_host_entry
```

包装器仅将自身移入 scope 的 host 子组、启用已委派控制器，并启动指定 Host；不会申请 root 或修改祖先 cgroup。若系统管理员未委派控制器，需要管理员先配置。scripts/provision_linux_sandbox.py 可预览显式独立子树配置；独立子树还需管理员将受信任 Host 置于该子树内，不能仅导出变量后跨委派边界迁移。

cgroup namespace 保留 Host 所在的命名空间以便加入委派的兄弟组；沙箱不挂载 /sys，只暴露本次 cgroup.procs，可将可见进程移入本次组，不能修改限制或迁出。

Linux source/frozen 使用同一私有 Python role 启动协议。macOS 后端尚未完成，保持 SANDBOX_UNAVAILABLE。
Linux 集成验收由专用 CI 执行；Windows 本地验证不能替代该验收。

## 授权与收据

新 grant 同时绑定 process/task、工具契约摘要（含 authored 版本和源摘要）、
参数摘要、资源和 30 分钟期限。预检与执行使用同一匹配函数；一次性授权消费后
不能复用，敏感工具不继承旧的无版本授权。权限只能进一步收窄或允许调用，
不能绕过文件 ACL、网络 namespace 或资源限额。对外部资源的审批也不会自动
修改 OS ACL；当前原生执行配置仅支持已配置的本项目工作区，额外访问保持拒绝。

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
- [Microsoft process attributes: SECURITY_CAPABILITIES, HANDLE_LIST, JOB_LIST](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)
- [bubblewrap security model and namespace options](https://github.com/containers/bubblewrap)

新增验收使用真实 Host 强制退出，以及实际 PyInstaller onedir 产物；打包测试不通过伪造 sys.frozen 冒充。三平台完整原生发行验收尚需补齐 macOS 及签名/安装环境。
