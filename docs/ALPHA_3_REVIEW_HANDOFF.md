# Alpha3 源码复审与能力交接

本记录回应 [PR #35 的修改要求](https://github.com/Truman-Duo/Gitgo/pull/35#pullrequestreview-5612390591)。日期为 2026-10-22；保留已完成的融合及来源历史，不合入 master，不发布安装包，也不关闭 #34。此前 Windows 源码测试记录见 [Alpha3 集成记录](ALPHA_3_INTEGRATION.md)，其成绩不转记为本轮或跨平台通过。

## 来源重新对齐

复审前重新 fetch 并查询了主仓库实际分支。以下四个 head 均未变化，没有额外来源增量需要接入；它们均为候选的祖先。

| 主仓库来源／目标 | 本轮重新观察的完整 SHA | 与候选的共同祖先 |
| --- | --- | --- |
| `master`（目标） | `0b7c2ab6d9b0e4f2650fedff35fcad6fcc7d47c8` | 同一 SHA |
| `release/alpha2.1` | `4d9e9488f90a850a0333e8d5b7024b341dfbbbb6` | 同一 SHA |
| `feat/managed-update-v1` | `a8e24d922310b7a97120893880fdeeb5547b8fb9` | 同一 SHA |
| `feat/history-resize-v1` | `5c87d07836db10dea42f98078aa88667013dc681` | 同一 SHA |

Alpha2.1 分别与更新、缩放来源的共同祖先也都是上述 master。被审查的旧候选为 `2fea85cad4e499079429b7a663461b701398da24`；本轮整改在它之后追加提交，最终候选完整 SHA 由 PR 正文和对应 CI evidence 记录，不能把旧 head 当作新候选。相对 master 的大量差异包含 Alpha2/Alpha2.1 及两个来源的既有工作。

## 失败归因与本轮处置

下表区分产品能力缺口、历史缺陷、集成环境契约和未完成验收。代码相同只是证据之一；同时检查调用方、构建入口与拒绝链。下述平台分派探针仅用于定位，不能充当真实 macOS/Linux 内核或冻结包验收。

| 现象、平台与受影响操作 | 来源与对照证据 | 分类 | 本轮处置与后续归属 |
| --- | --- | --- | --- |
| macOS 新工作区 daemon 返回 `DAEMON_START_FAILED`，内部为 `authoritative_session_storage_failed / GOVERNANCE_STATE_UNAVAILABLE`；不是只有某个文件工具不可用 | Alpha2.1 的 `sync_session/state.py` 在会话不存在时仍先构造 `ReadRoot`；`sandbox_read.py` 只有 Linux/Windows 分派。两个文件及该调用链原样继承。Darwin 分派探针确认安全读取先拒绝，再由治理服务拒绝初始化；真实 macOS 失败来自下列 CI | 原有未实现平台能力，阻断普通 daemon 初始化及依赖它的工作流 | 保留 fail-closed，不以普通文件读取、空状态或吞错兜底。补公开支持矩阵；维护者在 #34 后续平台任务中接续安全读取、治理初始化及 daemon 实机验收，本轮不实现 macOS 后端 |
| 仅由构建锁安装依赖的 Linux x64/ARM64 环境出现 `ModuleNotFoundError: nbformat`；工作台创建、续写、材料/来源测试及间接工具调用失败 | Alpha2.1 已有工作台 `_validate()` 的延迟导入和 portable 依赖声明，但构建锁没有 `nbformat` 及其完整闭包。该锁在 master、Alpha2.1、更新来源和旧候选的 Git blob 均为 `d314837422a60ba76654663b9501d618df9ebd07`；旧打包/锁定入口也已存在，非冲突删除。新组合的 Linux 更新全量源码任务进一步暴露了它 | 原有锁定构建依赖缺陷；新 CI 暴露历史缺口，不能归为工作台业务回归 | 按本轮审查范围移交，未声称修好依赖或冻结工作台。维护者需另行审查规范依赖与完整哈希闭包，并对真实包创建、读取、关闭后重开做验证。实际冻结包受影响是风险推断，目前仅 CI 源码失败被证实；启动冒烟通过不能证明工作台可用 |
| Linux 受保护工作树、晋升与正式提交报 `GIT_BOUNDARY_UNAVAILABLE`，有时外层只呈现子进程 failed、sealed/terminal 状态断言失败 | Alpha2.1 `loop/git_execution.py` 在 `os.name != 'nt'` 时明确拒绝；更新、缩放来源没有替代后端。候选保留同一实现及 worktree 调用链。对五项间接 DAG 失败，分别用 Alpha2.1 和候选、相同 Windows 环境下仅在 protected Git 入口切换的非 Windows 分派探针，均复现原断言失败并记录相同拒绝码。正式 Linux 证据仍来自 CI | 原有 Windows 专用能力与平台测试成功预期不一致，不表示 Linux 全平台不可用 | 新增明确的无执行能力拒绝契约；保留 Windows 正向测试及 Linux 独立更新/存储/清理验证，不批量 skip，不增加无隔离 Git 回退。维护者接续 Linux 安全后端与历史门槛协调 |
| Linux 更新任务在原生测试 fixture 处报 `bubblewrap must be installed`，尚未验证实际原生进程边界 | 更新来源只有 `patchelf`，当时没有 Alpha2.1 原生全量用例；Alpha2.1 原生 CI 已准备 bubblewrap/编译工具/libseccomp2 并使用 systemd 委托服务。组合接入该全量 suite 后，更新 job 没有同步准备这些前提 | 本次新增集成/CI 环境契约遗漏 | 本轮修复 Linux 更新 job 的工具安装和委托执行，复用既有 `run_linux_sandbox_scope.py`；不改宿主祖先权限或全局隔离策略，不关闭测试。Linux x64/ARM64 修复后的实际结果以新 CI 为准，本机 Windows 检查不冒充通过 |
| POSIX 搜索选包测试回退到 PATH 或返回 None，未选测试“包内引擎” | Alpha2.1 测试以 `write_bytes` 创建假 rg，未置执行位；生产 resolver 正确要求 `os.access(..., X_OK)`。候选未更改选择优先级，更新 CI 只是多运行了该继承用例 | 原有跨平台测试 fixture 缺陷 | 本轮仅为测试引擎设置 `0700`，继续断言选包内引擎，不放宽生产可执行检查。Windows 完整搜索 suite 通过；POSIX 结果由新 CI 记录 |
| Windows 六项注册测试失败，子进程无法导入 `backend.core.tools.sandbox_pytest`，外围呈现 `TEST_FAILED` | Alpha2.1 与候选的 `registered_test.py`、`child_process.py` 相同。另建同一未注册源码的隔离解释器，对两版分别运行 CI 的六项用例，均为 6 failed，观测到所有七次 seed 的 `No module named 'backend'`。再将各自可信源码注册为该私有开发解释器的导入根，最小项目测试两版均 1 passed | 原有源码开发运行时／注册测试环境契约缺陷；不是仅 parent pytest 能导入源码就足够，也不证明冻结运行时失败 | 保留六项 CI 历史失败。维护者接续受信源码适配器的正式解析及开发环境准备；本轮支持环境明确要求子解释器可导入所选可信 Gitgo 源码，不能把 PYTHONPATH（会被清理）或项目内伪造 backend 当作修复。本地准备仅是对照和回归前提，不宣称修改了产品或修好了原 CI |
| Windows 深层状态路径触及 Win32 限制；审查者另有 `UPDATE_ACL_INVALID` | 旧本机 `LongPathsEnabled=0`；短路径私有临时目录补测已有通过记录。维护者的十项 ACL 失败发生在健康检查断言之前，审查使用 CPython 3.12.10，与提交者 3.14.7 分开记录 | 已证实的环境前提，不是组合回归，也不计作通过 | 保留原单次全量 1 failed；不修改系统长路径策略或已有 ACL。短路径/权限前提及未到达的断言单独交接 |
| Windows/Linux 双包脚本只变版本声明，不能证明真实业务与数据契约变化的升级兼容 | 两个 CI build 脚本的 Git blob 与管理更新来源相同；均先构建候选，再修改产品/前后端版本并提交构建第二包 | 原模块验收覆盖不足，不是本次重写的更新实现 | 实际走到该阶段时只证明两个真实产物的安装/切换/事务机制。真实业务差异、旧数据迁移及真实发行版本兼容交给维护者后续整体验收；未到该阶段的任务不能计为双包通过 |

### 历史 CI 数字的拆分

以下只描述旧 head 的 [ready 后运行](https://github.com/Truman-Duo/Gitgo/actions/runs/39755427894)，不改写其 required `CI gate` 失败：

- Linux 更新 x64/ARM64 各为 **90 failed / 3953 passed / 605 skipped / 72 errors**。其中受保护 Git 拒绝链为 60 failed / 11 errors（含 5 个间接 DAG 断言及 formalize 错误回执）；缺 `nbformat` 为 29 failed / 14 errors（含 4 个间接工作台工具断言）；搜索 fixture 为 1 failed；缺 bubblewrap 为 47 errors。上述分组分别相加还原原计数，不能把它们全归为 Git。
- 原生 Linux 为 **61 failed / 4029 passed / 619 skipped / 11 errors**：Git 拒绝链为 60 failed / 11 errors，搜索 fixture 为 1 failed。该 job 使用 portable 依赖且已准备原生工具，与更新 job 的依赖前提不同。
- Windows 更新 frozen job 实际止于源码回归：**6 failed / 3922 passed / 792 skipped**；未到构建两包和实际冻结更新。普通 Windows source/native 的记录还含取消及部分运行成绩，不能拼成全量通过。
- macOS 包完成编译后在普通 daemon 冒烟失败，独立 accept job 随之跳过。`macOS capability audit (not backend acceptance)` 的通过只说明所测能力探针与安全拒绝，没有证明 daemon 可用。
- 首轮 [run 39753903570](https://github.com/Truman-Duo/Gitgo/actions/runs/39753903570) 和上述第二轮同为旧候选；保留两轮红灯、取消、跳过及已通过检查。必需门槛不撤销，历史失败如何进入最终合入决定由维护者处理。

## 本轮验证和可复现入口

本轮 Windows x64 源码环境为 CPython 3.14.7 / SQLite 3.53.4；可信 Gitgo 源码可被所选私有开发解释器的子进程导入，临时数据位于满足所有权要求的私有目录。没有修改产品权限、宿主长路径策略或已有目录 ACL。

- 平台拒绝、原集成边界及完整搜索回归：**39 passed / 1 skipped**；唯一跳过原因是本机不能创建 symlink。新增三个拒绝探针不作真实 Darwin/Linux 验收。
- 全部管理更新、Windows Job/受保护 Git、真实 Windows 源码沙箱、版本证据、工作台及注册测试组合回归：**418 passed / 361 skipped / 0 failed**，共 779 项，约 163 秒。包含全部 `test_update*.py`、三个新平台拒绝契约、原集成契约、搜索、Windows owned Job、受保护 Git、真实源码 sandbox、版本证据、工作台稿纸、注册测试单元与最小项目用例；不与前项或旧全量相加。跳过含 POSIX/macOS 专用路径及未提供实际 frozen 产物的用例，因此不证明 Linux 交接/事务或双包通过。
- Alpha2.1／候选的未注册源码对照各 **6 failed / 0 skipped**，是预期暴露历史环境缺陷的诊断记录，不能列为通过。可信源码注册后的最小对照各 **1 passed**，也不等同原 CI 已修复。
- 五项 DAG 狭窄平台分派对照各 **5 failed**，均在相同 `GIT_BOUNDARY_UNAVAILABLE` 链后到达原失败断言；仅为归因探针，非真实 Linux 验收。

仓库公开入口（使用通过 SQLite 校验且正确准备的 Gitgo Python）：

```text
python -B -m pytest tests/test_alpha3_platform_boundaries.py tests/test_alpha3_update_integration.py tests/test_workspace_search.py -q
python -B -m pytest tests/test_update_runtime_contract.py tests/test_update_handoff.py tests/test_update_transaction.py tests/test_update_recovery_helper.py tests/test_update_manual_rollback.py -q
python -B -m pytest tests/test_windows_owned_job.py tests/test_update_windows_storage.py tests/test_update_windows_transaction.py tests/test_update_windows_managed.py tests/test_worktree_git_boundary.py tests/test_alpha3_native_source.py -q
python -B -m pytest tests/test_agent_lifecycle_closeout.py::test_registered_test_imports_real_project_package_without_test_path_hacks tests/test_agent_runtime.py::TestManifestTests tests/test_validation_versions.py tests/test_workbench_sheets.py -q
```

Linux 原生实际执行需 bubblewrap、gcc、libseccomp2、匹配校验的 SQLite/ripgrep，以及操作员明确委托的 systemd cgroup；按 `.github/workflows/linux-update.yml`、`ci.yml` 的参数运行 `scripts/run_linux_sandbox_scope.py -- <python> -B -m pytest ...`。包装器仅管理自己的委托子树，环境缺失仍拒绝。远端运行的真实 head/base/tested merge tree 与结果由该轮 CI evidence 记录；修改后 CI 结果不从旧绿灯继承。

## 更新组合边界与维护者交接

管理更新的候选、授权、取消与持久化仍由原服务负责；Host 只负责请求关联/路由，存储保留 checkpoint catalogue 与内存迁移检查。租约与两类 Job 的关闭权威不合并，Linux cgroup 清理保留。针对这些组合边界继续运行更新健康、共享租约、请求交接、取消、恢复、回滚及已存在的关键失败路径测试；不另造授权或恢复流程。

源码组合完成、原模块未完成、待维护者试用、待发行验收是四种不同状态：

| 后续事项 | 责任与边界 |
| --- | --- |
| macOS 安全读取、治理初始化、普通 daemon，以及 Linux 受保护 Git | 维护者接续平台能力；按 #34 审查约定，本轮记录限制，不补写整个平台后端 |
| 锁定构建依赖闭包与真实冻结工作台；未注册源码时的可信测试适配器解析 | 已有缺陷交给对应构建/运行时维护工作；未做二进制工作台实测，不宣称已经修复 |
| 最终 Alpha3 组合、真实 API、正式可见终端与用户试用 | 维护者继续完成；组件渲染、隐藏进程或 API 证据不替代正式终端验收 |
| 真正不同业务/数据契约的双版本、代表性旧数据、最终版本与资产 | 维护者安排发行验收；双包机制测试和版本字符串变化不等于迁移兼容。源码复审通过不等于已发布 |

程序回滚不回滚数据库、配置或项目数据；管理安装不接管既有 Inno 安装；后台更新仍关闭，缺发行信任策略不联网。本 PR 不改这些约束。

保留本 PR 和来源历史，新增修正使用 `type(scope): subject`；正式 `[GITGO-N]` 由维护者分配。旧自动化提交的贡献归属在正式集成时核对，本轮新增提交使用贡献者账号的隐私邮箱，不为署名重写已公开来源。复审、合入、分支清理和发布分别确认。
