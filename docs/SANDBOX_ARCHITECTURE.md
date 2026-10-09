# 沙箱作为 Harness 基础边界：修订设计

状态：设计候选，待实现与复评，不是安全验收通过证明。2026-10-10。
适用 PR #25 / Issue #17；按维护者 2026-10-09 的架构审查修订。
当前公开代码快照 `1a2da14fc6e9879a7ead6fd471f53b409659ddcc`；
观察目标为 `Truman-Duo/Gitgo:refs/heads/master`，
`fbeab2c5280c4eb1380f98f359e8f36536454af7`。
版本只是此次观察，后续交付重新读取，不冻结主线。
本文描述目标合同；[现有原生实现](NATIVE_SANDBOX.md)中的测试与限制按其候选解释。

## 1. 目标、威胁模型与信任

沙箱是 Harness 的执行边界。安全性必须覆盖工具诱导的 Host 行为、恢复和并发，
不能从 AppContainer/bubblewrap 存在或进程退出 0 推导整体安全。
工具代码、工作区、动态源码、stdout/stderr、JSON 和依赖脚本均不可信。
Host 策略、审批、监督、身份、凭据、存储及安装服务是可信计算基；内核和管理员可信。
内核漏洞不在此证明范围；Host 所解释的可执行元数据必须不能由工具普通写入。

必须始终成立：

1. 每个入口先声明执行合同；未知或无法落实的合同在启动前拒绝，不普通用户重试。
2. OS、文件/Git/网络 broker 和审批使用同一有效计划；deny 不被更宽 allow 覆盖。
3. 工具不能写出 Host 自动执行的配置、项目身份、授权或恢复事实。
4. 成功退出、业务声称成功、副作用验证、证据提交、清理确认、回滚确认分别记录。
5. admission 未可靠持久化不得启动；启动后证据/监督失效终止并保留 ambiguous。
6. 授权、计划、工具/运行时版本、任务和树身份由 Host 绑定；共享工作区不共享授权。
7. 可启用能力必须有真实限额与并发预算；不支持的能力保持 BLOCKED。

## 2. 本地 Codex 参考及取舍

用户提供的归档包含嵌套源码树；本次阅读内层 `codex-main/codex-rs`。
未发现可用 `.git`，不宣称官方 commit/tag 或当前发行版本；下列 SHA-256 固定实际材料。
只研究给定本地快照，没有执行其源码或修改参考目录。

| 文件（相对 codex-rs） | 阅读位置 / 借鉴内容 | SHA-256 |
| --- | --- | --- |
| protocol/src/permissions.rs | 40–78、1589 起、2360–2395、2586 起；元数据 deny、gitdir 指针及真实别名 | ba815705a004571cb1772d9ae97c30075fc6e3ed16c1a1b151ad64c0d459e0dd |
| linux-sandbox/src/bwrap.rs | 722–752、1274–1365；可写 symlink 拒绝、缺失保护目标及只读挂载 | 4249718b4b50fa385ce27c539961d68c46a59f63fce4428a6380e43aa02c7b89 |
| exec-server/src/sandboxed_file_open.rs | open/open_platform；受限 helper 内实际打开，Unix FD / Windows handle 交接 | f98e6d84a82455b8c5d065645099e9c7175c8440c8feaa9efac372134112daa6 |
| windows-sandbox-rs/src/resolved_permissions.rs | 60–116；解析权限与可落实能力检查 | 189906392c8d45fe8d5e55b823e84e9396ac598270ca848e5518d1b5f0d63d5c |
| windows-sandbox-rs/src/spawn_prep.rs | token/根能力/ACL 与启动准备分离 | a5e4b45995e0d676d8adeb0354c5cb9206f45abd40ed75dd53a5855e9146bfb1 |
| network-proxy/src/credential_broker.rs | 凭据所有者、替代值、目标绑定与版本；同时阅读 environment.rs 的 Host 私有路由上下文 | a78c91bf7b78c540d6b1ddfddd72181d55446cb46c8e84f8f0f621b86528635d |
| sandboxing/src/manager.rs | 87–135；权限转换后形成启动快照，原路径类型保留到执行边界 | 8abe2a5f66f8101a669dd0c1571fb7c5edbab9eff2e80c8d13d9064bc5bc175d |

借鉴统一权限、对象身份、受限打开、编译后的启动快照和具名凭据代理。
Codex 此快照的 Windows elevated 后端要求 root 可读；Gitgo 不采用广读默认。
其平台选择也存在关闭 Windows sandbox 的分支；Gitgo 不采用无隔离回退。
缺失元数据的 synthetic mount 可能影响 Git 发现，不能不加验证直接复制。
不从参考代码推断整树资源、掉电恢复或不可逃逸已得到证明。
保留 Gitgo AppContainer/Job、bubblewrap/cgroup/seccomp；Landlock 是需探测 ABI 的可选纵深层。

## 3. 单一权威与模块职责

复用现有 ToolPipeline、AgentTool/catalog、permission_broker、StorageRuntime、事件与恢复入口。
以下是模块职责，不新增平行的权限、恢复或消息状态机。

| 边界 | 负责决策的现有层及新增职责 | 不接受的权威来源 |
| --- | --- | --- |
| 注册与能力 | AgentTool + catalog：强制执行类型、资源动作与受评审 broker 标识 | 模型传入类型、handler 名称猜测、effect 推导权限 |
| 策略编译 | Host 策略编译器：声明 + 审批 + 身份 + OS 能力 → 不可变有效 manifest | 工具 JSON、工作区配置自行放宽 |
| 准入 | ToolPipeline + permission_broker + StorageRuntime：原子授权消费/预算 lease/intent | 旧 grant、未持久化或跨任务共享的批准 |
| 启动与监督 | ProcessToolRunner + OS 后端：预先归属整树、专用通信、监督 envelope | stdout 自报 PID、成功、清理或回滚 |
| 文件/Git/服务 | 受限 helper / 具名 broker：同一 manifest 的动作级能力 | 通用 Host Shell、任意凭据或自报 execution ID |
| 证据与恢复 | StorageRuntime 权威事务；CAS 诊断附属；现有恢复扫描 | 工具 receipt、工作区 JSON、旧日志路径 |
| 事件与展示 | 现有 event bus / native protocol / CLI / Dashboard 的一致投影 | 独立绿色 sandbox 开关、遥测代替持久状态 |

执行类型为 HOST_COMPUTE、DATA_BROKER、NATIVE_PROCESS、EXTERNAL_SERVICE；未知类型拒绝。
HOST_COMPUTE 只允许受评审内部计算/调度，不读取工具指定任意文件或创建外部进程。
文件工具优先全程在受限 helper 操作，避免向通用 Host 转移任意句柄。
EXTERNAL_SERVICE 只接受注册的服务动作；未经审核的 plugin/MCP 不自动获得 Host 权限。
组合步骤在 Host 内逐步调度并建立 lineage，所有步骤重新准入；不在不可信 child 里建立可信回执。
动态工具、测试注册、兼容别名、恢复和 frozen private role 都必须经过相同合同。

## 4. 有效计划与授权合同

manifest 至少包含以下内容，schema 版本与执行协议版本分别演进：

- Host 签发的 execution/task/process/parent ID，委派关系和 workspace 稳定身份；
- 逻辑/真实路径、关联 gitdir/common-dir/worktree/submodule 身份及缺失保护名称；
- 安装 manifest 摘要、可信 helper/运行时对象身份、工具版本/源码摘要；
- 规范化参数摘要、文件动作/受保护 deny、具名服务 scope、网络/IPC/设备规则；
- 本次与任务/项目聚合限额、预算 lease、墙钟与有效期；
- required primitives、策略版本、编译器/后端版本和有效计划摘要。

审批绑定有效计划摘要，而非只扩大若干 tools 文件 hash。任何放宽使旧 grant 失效并重新审批。
收紧后的授权迁移必须有明确、可证明的子集规则；初版不自动迁移旧 grant。
实际启动前验证原生对象/代码身份并保持必要句柄或 immutable 安装保护；一次 resolve/hash 不够。
逻辑额外根若 OS 不能落实，返回 CAPABILITY_UNSUPPORTED；不建议再次批准同一无效能力。
backend 编译返回逐能力 READY/BLOCKED/DEGRADED 和原因。DEGRADED 不允许缺失必需边界。

## 5. 数据、元数据、执行与发布

长期默认使用按任务隔离、硬容量受限的 staging 执行数据；Host 用户树不直接整树 M/RW。
Windows 可研究独立受限卷、Linux 可研究独立配额文件系统；实现前不声称已有硬磁盘限制。
每任务 scratch/profile/cache 单独租约，共享数据是显式可审计资源，不能复用路径 profile 充当任务身份。
不受控任意持久写入在配额、identity 和 publication 未完成前 BLOCKED。
只读操作也需限额 scratch、完整监督及可靠证据；没有满足条件的执行也不得标 READY。

受保护资源来自 Host 解释器/加载器清单，不仅三个目录名：真实 Git 元数据；
治理/权限与 runtime 路由；项目/任务身份；自动加载 tools/skills/plugins/hooks；证据和凭据。
普通业务源码可编辑，运行/测试构建始终在同等级隔离中进行。
缺失保护路径不得在调用期间创建；不存在路径绑定稳定父目录身份与保留名称。
大小写/短名/ADS、junction/symlink/hardlink、父目录删除/重命名、关联 gitdir/common-dir、bare repo 均纳入测试。
无法保持对象身份或保护别名时拒绝整个计划，不能运行后 scrub。

发布是专门 Host 数据操作：白名单普通文件、拒绝保护对象/危险链接/特殊文件，
在稳定目录句柄下验证来源和最终目标，版本冲突检查与权限检查分别完成。
预先准备并持久化操作清单/备份；崩溃后由权威事务恢复。多个替换不等于原子整树发布。
无法证实完成或回滚时记录部分应用/ambiguous，阻断依赖其成功的下一步。
需保留文件类型、权限和配额；暂不支持的 sparse/device/共享网络树明确拒绝。

Git broker 分本地受限 Git 与具名远程代理。固定二进制/版本、仓库身份和隔离环境；
按子命令审计配置 include、attributes、filter、fsmonitor、hook、pager、helper、维护与 external diff/textconv。
不能只列若干 -c 然后证明全体命令安全。普通 Host LocalGitRunner 不能解释工具可写仓库配置。
remote 动作绑定 repository/remote/ref/method/destination；复用已有审批，通用工具拿不到 token。
没有可靠 broker 的命令先 BLOCKED，不能改为普通 Host 执行以恢复可用性。

## 6. Host envelope、证据与恢复

HostExecutionEnvelope 和 ToolPayload 分离。Host 记录 launch admission、是否启动、实际树身份、
退出/终止原因、计量、监督/清理状态、真实 lineage、policy/execution ID、证据持久化事实。
payload 的 authority 字段丢弃或列为不可信自述，不驱动恢复/授权。
任意代码已启动后失败且无 Host 可验证事务回滚证据，effect 为 ambiguous。
退出 0 只确认进程结束；业务成功另记，不等于文件副作用已验证。

使用绑定项目的 StorageRuntime 权威 state 事务（已有 WAL + synchronous FULL），
不再用另一个 JSON 状态机。grant 消费、预算 lease 与 intent/admission 尽量同事务；
内存授权先迁移为该事务内的绑定记录，不能假装现有两条路径已经原子。
COMMIT 返回是准入前提；intent 写入失败不启动。launch 之后持久化失败触发终止、
恢复锁定和独立有界本地错误输出，不声称故障日志保存成功。
CAS 诊断只在内容和引用成功持久化后宣称可读；不能以 artifact 路径表示成功。

支持介质先限定本地可信磁盘及平台确认过的同步语义，网络盘/不确定介质拒绝。
FULL 提供的保证依赖 OS/设备；Host 强杀测试不代替掉电/flush 故障验证。
对父目录身份、state ACL、单写入者/跨进程 lease、迁移及 quota 逐项验证。
未启动的 durable intent 可由 Host 确认 not_started；消费但未启动不得无证据重用 grant。
Host 崩溃后 running/启动间隙进入待核验状态，由 OS 生命周期资源及恢复扫描确认整树清理，
不能凭工具自述自动重试，不能凭 lease 到期就复用预算或删掉仍在运行的树。

## 7. Windows 安装与恢复

配置由显式 operator/安装路径完成，工具启动不更改 Host ACL。
资源清单绑定 volume/file ID、原始 owner/group/DACL/MIC/继承/保护状态、配置版本和应用后期望值。
跨 Host/升级/卸载使用 OS 可验证排他锁、lease/refcount；冻结身份和 ACL 变更的范围需说明。
普通句柄阻止 rename 不等于阻止其他句柄 WRITE_DAC/WRITE_OWNER，不能假称已独占 ACL。
检测合法外部权限变更时恢复冲突并停下，不能覆盖旧快照；改到一半保留逐对象结果。

当前未提交的 legacy inheritance_model 方案仅为研究候选：允许 ID 0→1
不能由当前访问等价推导未来父 ACL 修改行为等价。不得作为满足维护者 B5 的正式实现。
需覆盖父 ACL 后续变更的继承行为及现代/legacy、protected/unprotected、OWNER_RIGHTS、
默认/Medium MIC；无法保持审阅要求时在任何修改前拒绝该配置，或采用不修改用户树的隔离卷设计。
不得删除 ID 比较、跳过 hosted 失败或扩大 ACL。原生 token 验证 WRITE_DAC/WRITE_OWNER、
父 DELETE_CHILD、显式 deny、身份及后代权限，不能只测文件内容不可写。

## 8. 限额、IPC、监督和可见性

现有 memory/pids/CPU 内核限制保留；50 ms 累计 CPU 监督是采样预算，不是逐 tick 硬截止。
新增任务/项目原子总预算、硬磁盘/文件数、FD/handle、输入/输出/协议长度、墙钟和临时空间。
同时满足单调用和并发聚合限制才允许准入，跨 Host 租约由 StorageRuntime 管理。
磁盘容量不是 io.max，轮询文件大小不能替代硬容量限额。

Windows 验证 named pipe/object/shared memory、RPC/COM、loopback、registry/device、WSL；
Linux 保留 seccomp socket/socketpair/io_uring/ABI 与无 Host FD；可共享端点逐项声明。
将来网络只经具名 broker，目标/action/数据范围校验；验证重定向、DNS 重绑定、IPv6/IP literal、
内网及代理故障。当前没有此代理，不宣传已实现网络授权。
运行时 immutable 安装 manifest 排除凭据、配置和状态；只读广根不等于可安全公开。

监督解析异常、线程退出、cleanup 超时/失败和失联必须到达 Host 状态；确认整树退出后才释放预算。
错误包含 code/stage/started/exit-or-termination/effect/policy/execution ID，
有界 stderr 或可信 artifact 和可实施下一步。凭据与终端控制字符脱敏不吞普通错误。
证据失败提供独立错误输出并锁定恢复。模型、CLI、正式 Dashboard 读同一事件/状态。
真实彩色终端运行 run_dashboard_native.bat，验证授权、拒绝、诊断、取消、子代理、reasoning/tool、
上下文与底部状态；不重新设计视觉，不把 build/mock/隐藏 PTY 算正式 UI 验收。
macOS 在整树生命周期/资源与原生工具验收前始终 BLOCKED；Linux guest 不算原生 macOS。

## 9. 分阶段交付和证据失效

| 阶段 | 交付 | 必须通过的门槛 |
| --- | --- | --- |
| A | 上游对齐、统一执行合同/manifest、grant 绑定、入口清单 | 新入口/别名/动态/恢复未知类型拒绝；同资源权限一致；放宽不复用旧 grant |
| B | 元数据保护、受限文件 helper、Git broker、受限 staging/发布 | 正常源码编辑对照；普通/gitdir/common-dir/worktree/submodule/bare；缺失/别名/竞态/父删除；完整 OS 投毒不出界 |
| C | Host envelope、真实 lineage、StorageRuntime durable admission/结果 | 伪造 authority 无效；所有已启动失败保守；满盘/权限/replace/COMMIT/Host 强杀与启动间隙可见 |
| D | 安装/恢复锁及租约、冲突与原生权限验证 | hosted ACL suite → Windows 全量源码 → 实际 frozen；升级/卸载/并发不撤销他人资源 |
| E | 聚合限额、IPC/运行时矩阵、可靠清理、正式 UI | 所有启用能力满足预算；成功正向对照与整树失败注入；真实终端一致 |

A–D 与 E 中启用能力所需边界均是合并门槛，不能先发布默认安全再补漏洞。
未实现矩阵项保持 BLOCKED；不能把 skip 算通过，也不能“全部都启动失败”冒充安全。
mock 仅用于协议/故障注入，真实 OS/后代 exec/实际 frozen 单独给证据。

每次候选记录 head、target、观察时间/upstream、merge-base、组合树、策略/协议版本、
接口对齐、源码/frozen/OS/helper 版本、正向/拒绝/故障/跳过、run/artifact 和剩余风险。
审计脚本只生成版本/格式事实，不证明运行过测试：

```text
python -B scripts/audit_sandbox_candidate.py --observed-upstream FULL_SHA --observed-at ISO_TIME_WITH_ZONE
```

先从真实目标读取并获取该 SHA；脚本不联网，也不声称用户提供的观察值是实时事实。
输出 integration_candidate_tree 只代表已包含目标的候选树；tested_integration_tree 保持 null，
不得将它当作测试证明。非零退出表示版本/格式阻断；零退出仍不表示安全通过。
工作树未提交、目标未带入、checkout 与指定候选不一致、新 head 格式非法均阻断。
历史格式债务和编号仅在维护者 squash 路线下披露保留，不能用于直接 merge。

独立工作树针对已标识组合测试；
上游相关变更重新 merge 对齐并重跑受影响验收；无关文档说明沿用范围。
最终接纳由维护者使用 expected head 和合并队列/序列化机制约束最新组合。
作者不修改上游，不预占正式编号、不替他人 rebase。

## 10. PR 与提交规范

遵守当时 CONTRIBUTING.md、commit-config.json 和 PR 模板。
贡献提交/PR 标题使用 type(scope): subject；type/scope 合法，subject ≤60 字符，不以句号结尾。
当前最近提交符合此格式；贡献历史中非法 tools scope 及自行分配 GITGO-70..100
作为历史格式/编号债务明确披露。按维护者要求最终 squash，以接纳时最新历史分配编号，
保留作者/共同作者，不为了编号 force-push 公开分支。正式编号不能现在假定为 75。

PR 正文使用模板所有章节，写“需要修改/设计候选”，不引用 bbc169f 旧 CI 作为最新通过证据。
当前 public head 的 Windows 生产修复和完整验收没有成功证明；本地未提交改动也不能充当候选证据。
设计/审计提交的测试仅覆盖设计交付工具，不提升 OS 安全结论。
当前已有的未提交 ACL 实验保留供研究，避免混入本设计责任提交。
