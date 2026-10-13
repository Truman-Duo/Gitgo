# 以打开对象为边界的文件读取

状态：只读对象原语及 `read_file` / `file_read` 接入；属于 Issue #17 的阶段实现。它与[有效执行计划](EFFECTIVE_EXECUTION_PLAN.md)之间的审批对象绑定尚未接通，不代表阶段 A 或 B1–B6 完成。

## 合同与调用链

两个读取 handler 继续声明为 `NATIVE_PROCESS`，通过现有 `ProcessToolRunner` 和 `sandbox_popen` 执行。读取模块不修改 ACL、不启用管理权限、不产生授权，也不提供普通 Host 进程回退。现有 Host 授权摘要包含读取模块的实现代码，源码和 frozen 代码改变都使旧摘要失效。

```text
Host 选定 workspace / 现有授权范围
  → 原生隔离 runner
  → 仅按名称选择资源根（不 resolve 链接）
  → ReadRoot 获取目录对象
  → 内核相对打开、核验文件对象
  → 同一描述符读取、计算内容 SHA-256
  → 关闭文件及目录句柄
```

`ReadRoot.identity` 和 `PinnedRead.identity` 是打开对象的不可变身份描述，不能用工具 JSON 或相同路径字符串制造授权。此实现获取的是**执行时对象**；现有 grant 仍绑定工具/参数/路径范围，未持有审批时文件对象。批准后、对象获取前替换为另一个普通文件，仍是待解决的审批绑定缺口。调用后的内容 hash 只描述实际读到的字节，不是预先批准的对象证明。

读取只接受已存在、单硬链接的普通文件。不进行创建、写入、删除、枚举或路径修改。路径中的 `..`、链接、重解析和平台不支持的能力返回可见拒绝；没有退回 `Path.read_bytes` 的路径。精确外部文件授权只选择该名字，不将父目录范围暴露成新的通用能力；原生 OS 策略仍可以拒绝现有路径授权。

## Windows

使用 `NtCreateFile`，`RootDirectory` 指向已打开目录，每次只打开一个名称。根目录获取和相对打开使用 `OBJ_DONT_REPARSE`、`FILE_OPEN_REPARSE_POINT`；返回后核验磁盘对象、目录类型、重解析属性、链接数量及 128 位文件 ID/卷 ID。

目录只请求 traverse/read-attributes/synchronize，文件只请求 read-data/read-attributes/synchronize。目录及文件共享读取和写入，**不共享删除**；持有中间目录到读取结束。未请求 WRITE_DAC/WRITE_OWNER、备份语义或继承句柄，不复用 ACL operator 的管理句柄。读取通过拥有文件句柄的 CRT 描述符进行；失败路径及时关闭已经取得的句柄。

这一合同仅支持普通本地驱动器路径。UNC、设备命名空间、drive-relative、ADS、DOS 保留名称、尾部点/空格等别名不支持。文件 ID 获取失败时拒绝，不降级成路径比较。相同进程内的普通内容修改可以发生；不承诺冻结文件内容或阻止外部权限管理者改变 ACL。

API 合同见 [Microsoft NtCreateFile 文档](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)。实际共享模式、父目录替换、junction 拒绝和 AppContainer 正向操作需要真实 Windows 测试支持，不能仅由 API 名称推断。

## Linux

需要支持 `openat2` 的内核及 x86-64/AArch64 ABI。根目录以 `O_PATH|O_DIRECTORY|O_CLOEXEC` 打开，拒绝链接和 magic link。根本身可以处在一个挂载文件系统上；文件获取以该根描述符为起点，使用 `RESOLVE_BENEATH|RESOLVE_NO_XDEV|RESOLVE_NO_SYMLINKS|RESOLVE_NO_MAGICLINKS`，对整个查找实施内核限制。缺失 syscall、被拒绝的 syscall、未知 ABI 或无法落实的解析约束均拒绝，不以多次 stat/resolve 模拟。

文件先用 `O_PATH` 获取，核验单链接普通文件后，才通过 `/proc/self/fd/<已持有 fd>` 转换为可读描述符并复核对象身份。该路径由可信代码及活跃的私有描述符产生，不来自请求；原生后端提供当前 PID 命名空间的 procfs。这样避免在类型核验前打开 FIFO/设备执行 I/O，也避免重新打开用户提供的名字。缺少 procfs 时拒绝。

Linux 可以重命名已打开对象。目录 fd 保持原来的根对象，文件 fd 保持原文件对象，新的同名目录或文件不能重定向读取。文件被 unlink 后会拒绝读取；父目录移动后可继续读取已经获取的原文件，不能因名字被替换读取新文件。本合同保护对象获取后的使用，不冻结整个目录树。

解析语义见 [Linux openat2 文档](https://man7.org/linux/man-pages/man2/openat2.2.html)。工作区内的额外挂载点，包括 bind mount，属于拒绝范围；根挂载身份在审批/启动之间的绑定仍需后续计划实现。

## 内容与资源边界

每次读取最多 16 MiB；先检查描述符 size，读取过程中再限制实际累计字节，防止文件增长绕过初始大小检查。前后描述符元数据检查发现 size/mtime/ctime 或链接数量变化时返回拒绝，禁止输出部分内容。

这是有界读取及常见并发修改检测，不是事务快照证明。返回的 SHA-256 绑定此次实际读取字节；写入 CAS、磁盘硬配额、任务聚合预算另行验收。现有原生协议输出限制继续生效，文件大小上限不扩大传输上限。

## 验证与后续接入

真实对象测试涵盖正常文本、缺失/目录目标、外部精确名称范围、文件替换、父目录替换、根目录替换、junction/链接、硬链接、特殊文件、名称别名、超限/增长、失败释放及独立并发读取。原生 source runner 和实际 frozen Host 另外检查正常读取、硬链接/链接拒绝以及外部 Host 文件不可读取。平台专属测试的 skip 原因必须保留。

尚需完成：

1. Host 纯编译阶段获取文件/根对象，将身份及动作加入统一计划和审批摘要；审批恢复重新获取并核验，不能自动将旧路径 grant 升级为对象授权。
2. 由受限 helper 消费已批准计划，在实际使用前核验对象；若通过句柄传递接入，协议由 Host 控制，避免工具数据取得任意 Host 句柄权限。
3. 写入/创建/删除的稳定父目录及缺失名称合同，Git 元数据/关联目录保护和所有其他文件入口覆盖。
4. 与 StorageRuntime 持久化准入、可信结果、租约及原生能力编译共同落实。

目录/文件替换的局部测试通过不能据此宣称“审批到执行”完整身份闭合。macOS 生产后端继续不可用。
