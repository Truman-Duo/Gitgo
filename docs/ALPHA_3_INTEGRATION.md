# Alpha 3 功能集成候选

本地集成记录日期：2026-10-22。关联 [Issue #34](https://github.com/Truman-Duo/Gitgo/issues/34)。本次完成两条功能分支与 Alpha2.1 的源码融合；仍为待验收候选，不构成 Alpha3 安装包发布或 Issue 全部验收完成。产品版本保留 `1.0.0-alpha.2.1`，尚未分配新的发布版本或 Windows 文件版本。

## 来源

集成分支 `codex/alpha3-integration` 从 `release/alpha2.1` 创建，核对并获取了以下上游提交：

| 来源 | 提交 |
|---|---|
| Alpha2.1 基底 | `4d9e9488f90a850a0333e8d5b7024b341dfbbbb6` |
| `feat/managed-update-v1` | `a8e24d922310b7a97120893880fdeeb5547b8fb9` |
| `feat/history-resize-v1` | `5c87d07836db10dea42f98078aa88667013dc681` |

先合入管理更新，再合入历史缩放，保留两条分支的提交关系。`fix/managed-update-owned-cleanup` 的 `c37dcceb40f4da26f24dcfe9acc5e331f57418ea` 与管理更新 head 文件树完全一致，未重复合并。

## 融合方式

- 应用服务保留 Alpha2.1 的经验候选逐项审核，增加管理更新操作及程序目录与项目目录隔离检查。更新候选、授权、取消与持久化由原功能分支的更新服务处理；Native Host 只负责请求关联、路由及关闭。
- 进程控制同时支持管理更新的 Windows owned Job scope 和沙箱原有 Native Job handle；保留 Linux cgroup 清理。安装租约沿既有子进程入口传递，不把 Job 进程所有权视作沙箱授权。
- Windows 监管程序的启动诊断沿父运行时既有 stderr 通道保留，命令 stderr 使用独立的显式继承管道；避免监管诊断混入工具结果。Job 分配、命令释放、共享租约及清理报告的权威边界保持一致。
- 存储 facade 同时保留受保护 checkpoint 的可信内存 schema catalogue 和更新健康检查的内存迁移验证；不以更新探测打开用户存储或放宽原生存储要求。
- 搜索保留私有 stdin 管道并立即关闭提供 EOF，避免 AppContainer 访问 NUL 的问题，同时加入安装租约传递。文档转换保留原 stdin 处理并传递租约。
- 首页保留 Alpha2.1 的项目分组；进程列表保留所有权树、依赖、手动滚动与底部详情，将选中项绑定到实际 DOM 行以适应缩放。Context 保留按 process ID 的选择状态和响应式布局，在切换进程时重置内容滚动。Stats 的详情入口与新增 `/update` 共存。
- Windows 打包保留既有参数位置与规范化发行目录，同时接入管理安装、更新契约及发行信任策略参数；移除自动合并产生的重复参数。

共处理管理更新 8 个、历史缩放 3 个文本冲突，并检查自动合并的 Host 入口、退出确认、进程输出、安装租约、前端入口、Ink 滚动实现与打包调用链。

## 源码验证

执行环境为 Windows x64；最终回归使用官方 CPython 3.14.7 / SQLite 3.53.4，SQLite 下载核对仓库固定的 SHA3-256；早期环境诊断另使用 Conda CPython 3.14.7 / SQLite 3.53.2。Dashboard 使用 CI 固定的 Bun 1.4.2。测试业务数据使用隔离临时项目，未使用开发仓库的产品 `.gitgo` 数据目录。

| 范围 | 本组合结果 |
|---|---|
| 更新、安装、Job、Host、文档、搜索及沙箱相关回归（含新增合并契约） | 409 passed / 386 skipped / 0 failed |
| 新增合并边界契约 | 5 passed / 0 failed |
| Windows Job 与原生 runner 的诊断通道修复回归 | 26 passed / 0 failed |
| 候选工作区模块，短路径私有临时目录补测 | 24 passed / 0 failed |
| Dashboard 全量 | 322 passed / 0 failed |
| Dashboard 类型检查与生产构建 | 通过 |
| Windows 打包脚本语法、发行版本一致性 | 通过 |
| tracked 文件发布隐私检查 | 1496 项文件通过 |
| 后端全量单次运行 | 3864 passed / 855 skipped / 1 failed；长路径环境限制，见下文 |

代码回归对应提交 `376accc`；后续文档提交仅更新验证记录。专项之间存在覆盖重叠，不能相加作为全量成绩。

新增契约覆盖内存更新健康检查与受保护 checkpoint 共存、搜索 stdin EOF 与安装租约共存，以及两类 Windows 进程所有权对象的关闭路径。缩放用例使用正式输入路由，并在窄屏中通过进程 ID 核对选中行，避免将底部详情误认为列表中的选中行。

初次受限运行无法正常清理临时文件；普通 AppData 的既有 ACL 又不能满足管理更新对祖先目录的要求。最终回归使用新建的私有测试目录，并保留既有文件系统权限检查，没有修改已有目录 ACL 或放宽产品检查。

首次全量尝试还发现独立测试环境无法从临时项目导入受信任的 `sandbox_pytest` 适配器；将源码注册到本地隔离 Python 环境后，该注册测试用例单独复测通过。此项为开发测试环境准备，不修改产品或临时项目的导入策略。

随后完整回归在 1652 passed / 49 skipped 后发现原生 PowerShell 用例失败：监管 Python 的启动诊断混入了命令 stderr。官方与 Conda 运行时均可复现，并非只更换解释器即可解决。修复以独立管道区分命令输出和父运行时诊断，没有过滤告警文字、吞掉错误或扩大沙箱权限；补充真实非零退出、命令 stderr、父诊断可见性和第三条管道分配失败的回归。受影响 Windows Job 与原生 runner 26 项复测通过。

修复后的全量 4720 项单次运行完成，耗时约 20 分钟；唯一失败为 `test_real_native_command_with_deep_state_location`。本机 `LongPathsEnabled=0`，初次深层调用记录目录达到 Win32 路径限制，返回 `INVOCATION_JOURNAL_UNAVAILABLE`，命令未启动；缩短部分临时路径后，又在更长的 CAS 临时文件路径触及同一系统限制，命令结果保持 withheld。调用记录、CAS 及该用例的代码均未因本次合并改变。

未修改系统长路径策略，也未跳过该用例或放宽产品检查。另建满足 NTFS 所有权要求的短路径私有临时目录后，原候选工作区模块 24 项全部通过，包含深层状态目录中的实际原生命令与正式文件提交。此补测不改写全量单次运行的 1 failed 记录；本机任意长状态路径仍受 OS 前提限制，不能据此宣称单次全量零失败或远端 CI 已通过。

## 后续验收与兼容

源码测试中的跳过项不是通过。相关回归的跳过主要来自 POSIX/macOS 专用用例、未提供实际冻结包与双版本产物，以及本机不满足的 symlink/native tar 前提。新组合尚未完成冻结包双版本实际切换、真实模型 API、普通安装、特权原生存储、Linux/macOS 实机与正式可见彩色终端试用。此次组件渲染测试不替代 Issue #34 要求的正式终端验收。未执行上游合入、远端 CI 或安装包发布。

管理安装为显式选择的新安装，不接管现有 Inno 安装；后台自动更新仍关闭，缺少发行信任策略时不联网。程序回滚不回滚数据库、配置或项目数据，沿用[管理安装](managed-installation.md)、[更新操作](update-preparation.md)及[事务恢复](update-transactions.md)的兼容限制。

正式发布前须分配一致的新产品版本并提高 Windows 文件版本，对最终提交重新打包验证；PR 审核、合入和发布分别确认。若任何来源或目标基线变化，应重新验证组合，不能沿用此前分支或 Alpha2.1 包的成绩。
