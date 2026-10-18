# 原生沙箱：配置、限制与验证

更新日期：2026-10-18。能力概览见[沙箱摘要](SANDBOX_ARCHITECTURE.md)。
本页针对源码与实际 frozen Host 的受限能力，不代表已发布安装包自动启用全部功能。

## 执行范围

Shell、exec、测试、自定义可执行工具及 Git/搜索/patch 等外部进程通过原生 runner 进入 OS 边界。
注册测试使用同一入口；审批不能关闭隔离，不支持的能力明确拒绝，不回退为普通 Host 子进程。
普通文件工具和匿名公共检索仍按各自合同判断，不能由某个原生入口的通过推断全部工具已具备同等隔离。

工具执行继续使用[既有执行与审批链](EFFECTIVE_EXECUTION_PLAN.md)。
`read_file` / `file_read` 的[对象读取](STABLE_FILE_READ.md)及[显式审批](APPROVED_FILE_READ.md)另有更窄的接口限制。

## Windows：AppContainer 与 Job Object

文件访问受 AppContainer SID 与 ACL 共同约束，默认没有网络 capability。
Job 在用户代码开始前绑定，限制整棵进程树，并禁止 breakaway。
当前策略默认内存 512 MiB、进程数 32；累计 CPU 预算由 Host 根据调用超时确定，原始策略默认 120 秒。
CPU 带宽不超过系统一颗逻辑 CPU 的份额，外层 Job 可能进一步收紧。
内存、进程数及带宽硬限制与累计 CPU 监督分别生效；50 ms 检查间隔和调度会产生终止延迟。
必需的 Job 限制无法建立时拒绝启动，包括部分 DFSS 环境。

正常返回、取消、超时和 Host 消失均受整树生命周期管理。
终止请求不等于清理完成：必须观察 Job 活动进程归零并成功关闭句柄，才能确认清理。

只给专用运行时读取/执行权限，给本次工作区修改权限。
Windows 向 ALL APPLICATION PACKAGES 开放的部分系统资源仍可访问，不能承诺系统文件一律不可读。

### 显式配置

采用专用、可丢弃工作区与专用 Python/安装运行时。
不要授予用户主目录、磁盘根目录或包含凭据的通用 Python 环境。
Source 模式还需要独立 Gitgo source root；venv 需要其专用 base Python；frozen 模式需要完整安装目录及依赖。

可写工作区与可信源码、Python、冻结运行时不得互为父子，链接/目录联接别名也会检查。
开发 Gitgo 自身时从工作区外的可信安装启动 Host。
路径分离不等于完整供应链、硬链接或运行时内容审计。

在受信任操作员终端先预览：

```text
python scripts/provision_sandbox.py --workspace PROJECT --runtime DEDICATED_RUNTIME --runtime GITGO_SOURCE
```

预览不创建 profile、修改 ACL 或建立恢复记录。
应用前停止所有相关进程，并在现有的外部 Host 私有目录指定一个新恢复文件；
它不得位于工作区或暴露的运行时树，已有记录不得覆盖：

```text
python scripts/provision_sandbox.py --workspace PROJECT --runtime DEDICATED_RUNTIME --runtime GITGO_SOURCE --record PRIVATE_HOST_DIR/project-acl.json --apply
```

应用需要独立授权的操作员令牌已有 SeSecurityPrivilege、SeBackupPrivilege 和 SeRestorePrivilege。
脚本不请求 UAC，Agent 工具也不自动配置权限。
它保留并检查对象身份、owner/group、DACL、审计、MIC 和继承状态；不能无损处理或存在冲突时拒绝。
最多处理 100000 个对象，恢复记录最多 64 MiB。记录含私有路径/SID，仅保存在 Host 私有目录。

### 恢复和配置失败

先预览，再显式恢复：

```text
python scripts/provision_sandbox.py --restore PRIVATE_HOST_DIR/project-acl.json
python scripts/provision_sandbox.py --restore PRIVATE_HOST_DIR/project-acl.json --apply
```

恢复只支持记录中的原对象与预期权限状态。新增、删除、原子替换、链接/重解析、多硬链接或外部权限冲突会拒绝；
不能给新对象套用旧 ACL。版本 1/2 记录不支持自动恢复，需保留后人工核验。已完成记录不能重复应用。
失败保留明确状态和恢复材料，不把部分回滚报告为成功。

这支持配置失败回滚和未变化树的恢复，允许保留同一对象的内容编辑；
它不是产生新文件后工作区的通用卸载器。
配置和恢复期间必须停止所有相关使用者，现有互斥与对象检查不能冻结其他程序修改 ACL。
profile 保留以免影响共享使用者；撤销全部相关 ACE 并停止所有使用者后，由操作员单独审查删除。
不要手动丢弃恢复记录，或递归修改权限来绕过拒绝。

### 工具兼容

Windows `shell_script` 使用系统 PowerShell，禁用用户 profile，固定系统模块并使用 UTF-8。
调用方须迁移 Bash 语法并重新审批。Git Bash/MSYS 的共享全局对象与该 AppContainer 不兼容。
`exec_command` 接受明确 argv；运行时不可访问或程序不兼容时返回错误。

Home、AppData、Temp 和模块缓存重定向到工作区内的沙箱目录。
凭据等环境变量不在继承白名单中；但磁盘上的运行时内容仍需单独检查。
旧版 DOC 的外部转换器也必须位于已配置的可读运行时范围，未验收任意转换器发行包。

## Linux：系统前提

需要可信系统 bubblewrap、当前 ABI 的 libseccomp、用户 namespace，以及已委派的 cgroup v2 scope/service。
缺少 cpu/memory/pids 控制器、`cgroup.kill` 或必要过滤能力时拒绝执行，不降级。

后端提供私有文件系统/PID/网络/IPC namespace、只读运行时、可写工作区和私有临时目录。
每次调用有独立 cgroup 的内存、进程和 CPU 限制，后代受同组监督。
只允许受限 socket domain/socketpair 组合；Unix socket 服务、io_uring 和兼容 32 位 ABI 当前不支持。
取消需终止进程树；清理必须确认 cgroup 为空并完成移除。

在已支持用户委派的系统中，可使用现有包装器：

```text
systemd-run --user --scope -p Delegate=yes python scripts/run_linux_sandbox_scope.py -- python -m backend.core.native_host_entry
```

包装器不申请 root 或修改祖先委派策略。系统管理员仍需预先提供授权的委派范围，
Host 使用 `GITGO_SANDBOX_CGROUP_ROOT` 指向该范围。
独立子树配置可用 `scripts/provision_linux_sandbox.py` 预览；仅导出变量不能跨越委派边界。
Linux source/frozen 使用同一原生边界，Windows 结果不能代替 Linux CI。

## macOS

生产入口返回 `SANDBOX_UNAVAILABLE`。
CI capability audit 只记录系统能力和当前缺口，不是原生后端验收。
进程组清理、单进程 rlimit 或空 VM 创建成功不能证明整树生命周期及聚合资源限制。

## 结果、错误与恢复

调用证据写到项目绑定的外部 Host 状态目录，不能落入可写工作区或暴露的运行时树。
旧工作区调用日志只供人工核验，不自动升级为可信证据。
业务成功、启动/退出、效果、证据提交和清理分别报告；退出码零不独立证明任意写入效果。
证据更新失败保留已有业务结果及恢复依据，不为补日志重跑调用。

已启动后取消、超时或异常退出仍可能有副作用，不能宣称自动回滚。
未知清理保留原生所有权和已持有的读取资源，阻止继续原生执行；Host 重启不复活旧 grant 或重放未知调用。
每路输出最多保留 2 MB，超过预算终止进程树；这不是磁盘写入配额。

| 错误码 | 含义 |
| --- | --- |
| GITGO-E3801 | 原生沙箱不可用 |
| GITGO-E3802 | 启动被拒绝 |
| GITGO-E3803 | 策略无效 |
| GITGO-E3804 | 输出超限 |
| GITGO-E3805 | 执行失败，效果可能未知 |
| GITGO-E3806 | 先前清理未确认，阻止新执行 |

遇到清理未知先检查原生进程与保留债务；新的用户授权不能替代清理证明。
不要通过关闭隔离、放宽运行时目录或裸 Host 重试处理错误。

## 验证入口

源码测试：

```text
python -B -m pytest tests/test_native_sandbox.py tests/test_runtime_cleanup_evidence.py -q
```

实际 Windows frozen Host 可通过环境变量 `GITGO_SANDBOX_TEST_HOST` 指向已构建的 `gitgo-host.exe`，再运行：

```text
python -B -m pytest tests/test_native_sandbox.py tests/test_windows_sandbox_minimum.py -k packaged -q
```

上述原生测试会使用自有临时工作区/运行时和临时 ACL，必须在授权的测试环境运行。
缺少实际 Host 的 packaged 测试会跳过，不能把跳过视为通过。
既有 `scripts/run_sandbox_acceptance.py` 先做源码回归，再构建真实 onedir Host 并运行打包测试；
Windows 增量已接入该入口。该脚本会生成测试产物，需要现有构建依赖及搜索引擎，不是只读命令或安装器。

真实 OS 验证涵盖文件/网络正向对照与拒绝、环境过滤、资源限制、正常/取消/超时及 Host 强制退出后的生命周期。
协议故障注入单独验证未知清理、证据失败和伪造结果，不能冒充 OS 故障实测。
准确版本、已完成结果与未覆盖范围见[验证摘要](SANDBOX_ARCHITECTURE.md#验证版本与结果)。

未提供通用硬磁盘配额、完整开发项目闭环、指定 API 或完整签名/安装/升级/卸载验收。
受保护存储另受[checkpoint 接口限制](CAS_LIFECYCLE.md)，不能作为这些能力已经开放的证据。
