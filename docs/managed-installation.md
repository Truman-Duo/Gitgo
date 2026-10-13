# 明确选择的管理安装与运行锁

关联 [安全自动更新与回滚 #21](https://github.com/Truman-Duo/Gitgo/issues/21)。此文描述功能分支新增的 **POSIX 安装/启动层**，不是现有公开预览包已经支持自动更新。

## 范围

默认安装行为不变。新构建的 Unix 包可以显式选择 `--managed`，但必须有通过实际冻结 Host 核对的 `update-data-contract.json` 与完整 build inventory，并支持 managed bootstrap API 1。构建时需要选择 `--update-evidence`。旧包、缺少契约的包、Windows、未知或已有非空安装目录不被接管。

该模式的更新总开关仍关闭。`/update` 可识别已经登记的管理布局，但仍不提供下载/应用/回滚操作。正式发行信任、应用更新交接与完整健康/平台验收尚未开放；不能把管理安装成功当作更新验收通过。

## 布局与唯一权威

```text
installation/
  gitgo                           # 本地生成、固定位置的启动脚本
  bootstrap/gitgo-host/           # 独立冻结启动/恢复程序及组件
  control/registration.json       # schema 2，simulation=false
  control/ledger.json             # 复用原事务引擎的唯一持久记录
  control/operation.lock
  control/runtime.lock
  active/                         # 实际 Dashboard、Host、脚本和组件
  transactions/                   # 已记录候选/旧版/失败版本
```

模拟模式 schema 1 与管理模式 schema 2 不能互相接管；不是修改一个布尔值就转换安装。两种模式复用同一事务状态机、文件检查、锁、持久化和恢复，不再建立一套平行 ledger。

管理登记固定安装 ID、根与控制目录设备/inode、两把锁身份、保护路径，以及 bootstrap 完整文件树和本地生成启动脚本摘要。更换锁 inode、修改稳定程序/脚本或手工开启未支持的开关会被拒绝。初始安装只复制进入新的私有目录；完整包与实际 frozen runtime 的版本、目标、数据证据及 API 都要匹配。

## 启动与子进程

稳定启动程序先检查持久记录，未完成事务必须恢复后才能启动 `active/gitgo`。运行期间持有共享安装锁；恢复或切换需要独占锁，运行中的实例令操作返回 busy，不强杀项目进程。

安装使用锁以已验证的真实文件描述符传给拥有的 Unix supervisor 和它的 worker；启动进程关闭自己的描述符后，仍在使用程序的子进程继续持有内核锁。Native Host、daemon、工具运行器和 Python 子角色在普通应用初始化前，也从实际可执行文件的物理位置核对管理安装、重新取得共享锁。调用方的环境根目录或数字不能替代登记和真实描述符。

现有 daemon、工具运行、搜索与文档转换子进程保留安装租约；进程范围仍采用原有 owned Unix supervisor。macOS owned-session-only 的清理不完整事实保持不变，不能因此提交生产更新。

直接打开 `active/gitgo` 或其 Node companion 时，Dashboard 也在 launch-session/profile 初始化前请求固定 bootstrap 的租约。bootstrap 持有真实共享锁，以有界 nonce/版本报告确认，直到前端 stdin 管道关闭；租约异常退出时前端停止。`--version` 是不初始化用户资料的只读例外。该早期入口不接受环境变量作为管理安装凭据。

候选数据探针是窄例外：只在 CHECK_NEW 状态且继承了与登记 inode 一致、能够证明独占的真实安装锁描述符时进入。否则包括手工设置环境变量的探针都被拒绝。普通应用、聊天和任意 Python 角色不能利用该例外运行未提交的新版本。

## 数据保护

安装入口只读取有界的现有配置中登记的 workspace 路径，不加载/迁移配置、不读取 Provider 密钥或初始化凭据。配置、秘密父目录、runtime state 与登记项目均保护，程序目录和 command 目录不得与它们重叠。

运行时同样要求当前配置/state 与项目位于程序目录之外。项目服务不允许在管理安装目录内部或其祖先范围创建/登记项目，以免后续程序替换误伤项目资料。

## 明确选择的使用方式

仅对本功能分支新构建的完整 Unix 包，在全新的、规范绝对路径及独立 command 目录中演练：

```text
./install.sh --managed --no-path-update --install-root /absolute/new-installation --bin-dir /absolute/separate-bin
```

管理模式不改 shell profile/PATH，要求 `--no-path-update`。已有同名命令或安装文件会拒绝，不覆盖旧安装、不删除项目或资料。新目录可能在失败时留下未登记诊断内容，后续不会自动接管。不要用日常安装目录做首次演练。

这一阶段尚没有管理模式卸载入口。包内原卸载脚本针对 legacy 所有权格式，不应作为管理安装的卸载方案。测试安装生命周期与清理由隔离测试目录负责，不能将旧脚本绕过检查使用。

## 验证与未完成项

`tests/test_managed_installation.py` 覆盖新登记、模式隔离、已有目标/命令拒绝、bootstrap/锁变化、项目和配置保护、候选探针租约、共享 FD 跨拥有子进程存活，以及未完成记录恢复。部分 fixture 的 bootstrap probe 为 mock，不能当作实际产品验收。

可显式提供 `GITGO_MANAGED_TEST_STAGE` 指向新构建的 managed-capable staging，运行实际冻结安装与启动测试。未提供工件时明确跳过。测试只使用新建的私有目录、保护哨兵和隔离资料，不安装到日常目录，不调用付费模型。

尚需完成正式更新请求/确认/退出交接与稳定 helper 应用、签名发行配置、两个真实版本切换/失败恢复、Windows ACL/运行锁适配、Linux 原生验收，以及正式终端、断电和清理完整性验收。默认关闭保持到这些条件满足；已有[模拟恢复与真实整包准备验证](update-transactions.md)不能代替它们。
