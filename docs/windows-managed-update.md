# Windows 管理更新与回滚

关联 [Issue 21](https://github.com/Truman-Duo/Gitgo/issues/21)。这是明确选择的新管理安装，不是把现有 Windows Inno 安装原地接管，也不是后台无人确认更新。

## 安装与构建

当前目标是 Windows x64、本地 NTFS。完整新包由 `packaging/build_windows.ps1 -ManagedUpdate` 构建，要求新的私有输出目录，并自动核对实际 frozen Host 的数据契约。可显式传入 `-UpdateReleasePolicy`，仅含发布者公钥与 HTTPS 渠道地址；私钥不得打包。未配置策略时不联网、不检查更新。

从完整解压包在 PowerShell 执行：

```powershell
./install-managed.ps1 -Root <新的规范绝对安装路径> -BinDir <独立的规范绝对命令目录> -ProtectedPath <项目路径>
```

安装不提升权限，不改注册表、全局执行策略或 PATH，不覆盖已有命令/非空目录。生成的公开入口是独立命令目录的 `gitgo.ps1`；安装根的 `gitgo.cmd` 使用固定相对 bootstrap 路径。PowerShell 入口使用 UTF-8 BOM，路径作为字面量转义；不靠修改控制台编码解释用户路径。安装和公开 PowerShell 命令使用显式进程等待与 `ExitCode`，不把尚未设置的 `$LASTEXITCODE` 当作成功或失败；本机参数按 Windows 原生规则逐个引用，保持中文、空格与尾部反斜杠。旧 Windows 安装仍可保留，配置、密钥、数据库和项目必须在新程序目录之外。

产品格式明确标记 `managed-windows-v1`，使用 API 7/schema 6。没有提供管理模式卸载入口，不携带或宣传 legacy Inno 卸载器；不要用旧卸载器删除这个新目录或用户资料。

## 唯一状态与运行保护

复用 Unix 的唯一 ledger、事务状态机、签名/缓存复核、失败恢复及保留上一版回滚；没有第二套 Windows 更新记录。稳定 bootstrap 不随候选替换。登记固定完整卷 GUID、128 位文件 ID、目录/锁身份、bootstrap 文件树、启动脚本和保护路径。损坏登记、未知格式、外部可写目录、网络路径、reparse/junction、硬链接和备用数据流被拒绝，不自动修复权限或猜测归属。

普通 Host、daemon、工具和前端在应用资料读写前取得共享运行租约。Windows 使用内核共享访问保留以及 LockFileEx，而不是伪装成 POSIX flock。切换需要独占；仍有实例使用程序时返回 busy，不强制终止其他实例或无关进程。

拥有的子进程先启动可信等待程序、加入无 breakaway 的独立 Job，随后才收到目标命令。父进程核对 Job 实际成员归零，并由专门的租约守护进程保持共享访问直到清理完成；父进程意外退出也不提前释放。无法证明清理完成则不能提交更新。Job 管理不是 OS 沙箱，不能据此声称已隔离恶意代码或清点由系统服务/WMI 另外启动的进程。

## 确认、健康检查与回滚

项目列表的 `/update` 使用现有 UI/Host 通路：选择渠道、检查、确认下载、静态核对、确认退出更新。稳定启动器在本次会话请求有效、进程清理完成、全部共享租约关闭后才重新验证签名和包并切换；普通退出、崩溃或环境变量不能代替确认。

Windows 不把继承的 LockFileEx 当成候选探针权限。健康检查复制完整已核对程序树到独立私有快照，执行实际 Host 内存迁移/启动与 Dashboard `--version`，前后复核快照及原树，并要求拥有的 Job 清理完成。该检查不运行用户任务、不迁移真实数据库、不调用付费模型；它也不等于可见终端交互验收。

坏候选恢复旧程序；显式回滚只允许最近已提交更新保留的那个上一版，不任意降级、不回滚数据库/配置，并保持渠道防重放序号。程序文件写入刷新，控制记录/目录切换使用写穿透移动并保留恢复证据；不声称已经证明各种硬件的物理掉电持久性。

## 自动化范围与尚需验收

`windows-update.yml` 在独立测试分支运行真正的 Windows runner。原生测试涵盖 ACL/身份/锁、升级与回滚状态、中断恢复、实际 Job 与父进程退出后的租约。模拟健康/占位文件的通过不计为真实产品升级。

双版本任务分别从干净提交构建真实 Windows PE/DLL/Host/Dashboard，第二个版本只改变三个版本声明。真实包测试覆盖升级、回滚、坏 Dashboard 恢复、冻结 bootstrap 恢复、实际 Host 租约，以及中文路径的新安装/公开入口/重复安装拒绝。HTTP 流使用临时签名与注入传输，不冒充生产 CDN；协调器源码调用不冒充实际编译 UI 的确认操作。运行成功与失败按最新确切 head 分别记录。

仍需 Windows 实机可见终端确认流程、终端切换/重启行为与现有配置的人工验收，以及维护者配置真实签名发行源。Windows ARM64、UNC/共享盘、旧安装自动迁移、管理卸载与物理断电不在已实现范围。测试包仅存在于临时 runner，不发布安装包或创建 PR。

## 本轮候选的验证记录

2026-10-14，[Windows Actions 38562218580](https://github.com/hanyujason/Gitgo/actions/runs/38562218580) 整轮通过。测试源码 head 为 `bf1b2d03593c9f1991770472aeae1ad2828864c2`，固定上游基线为 `0b7c2ab6d9b0e4f2650fedff35fcad6fcc7d47c8`。后续仅修改说明文档的提交不冒充重新编译的包证据。

- Windows Server 2022 x64、本地 NTFS 原生专项：48 通过。
- Windows 全量源码：1772 通过、486 按平台/工件条件跳过；Dashboard 265 通过，生产构建通过。
- 从干净提交构建两个实际 Windows 包：`1.0.0-alpha.2` 与 `1.0.0-alpha.3.windows.1`，两包的 Host/Daemon/tool-runner smoke 均通过。
- PowerShell 7 与 Windows PowerShell 5.1 的公开安装 smoke：2 通过。完整实际包验收：7 通过，覆盖真实升级及回滚、坏 Dashboard 恢复、中断后的冻结 bootstrap 恢复、公开 CMD 入口、实际 Host 占用租约，以及两种 PowerShell 的中文空格路径安装、公开命令版本启动和重复安装拒绝。
- 同一运行代码的 macOS 源码回归：2165 通过、93 按平台/工件条件跳过。不是新的 macOS 安装包跨启动验收，也不继承为 Linux API 6 包验收。

此前公开 PowerShell 安装脚本在 Host 尚未完成时读取了空 `$LASTEXITCODE`，造成误报失败及临时文件仍占用；修复为显式等待实际进程并读取 `ExitCode` 后，两种 PowerShell 的原测试均通过。没有删除失败用例、放宽 ACL、将空退出码视为成功或绕过公开安装入口。

上述证据仍不等于正式 Windows 可见终端 UI 验收、生产签名/CDN 验收或已发布安装包；后续候选、上游基线或运行环境改变时须重新验证。
