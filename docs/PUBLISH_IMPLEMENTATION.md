# Publish 实现与验收交接

## 基线与当前状态

本实现对应 [Issue 40](https://github.com/Truman-Duo/Gitgo/issues/40) 和固定版本的[需求与验收清单](https://github.com/Truman-Duo/Gitgo/blob/7bcd35e62b8f69865704a11b70ac1d90ad75e5fa/examples/publish-reference/REQUIREMENTS.md)。产品基线为 `03a573c396ce65676b07c7ca142dbfdfae65a583`，包含 Alpha3 `76a78969b529a2b274da8312fc59e1e9c75b3144`。实现沿用该主线的 Dashboard、应用服务、Runtime、配置、权限和 SQLite/CAS；参考包没有进入运行链，旧支线源码没有用于替代产品。

2026-10-27：M1–M3 的下述业务路径已经实现，真实 GitHub 单账户协作和发行验收已通过。本机回归、可见终端、性能及独立源码打包的证据入口见文末和桌面 `delivery.json`。**整体 Issue 尚不能标记完成**：真实第二账户和自建 GitLab 仍无可用环境；协议 fixture 不计作这些平台验收。按用户分工，Linux/macOS 原生运行验收交由原作者执行，本轮继续完善现有 Windows 环境。当前产品提交及最终包来源以桌面交接清单为准。

| 阶段 | 已实现 | 退出条件现状 |
| --- | --- | --- |
| M0 | 差距、权威、共享/API、迁移和恢复方案；正式终端原型；有界接口及测量工具 | 平台限制明确；真实规模性能结果和构建来源记录已交接 |
| M1 | 三仓/单仓多角色、固定候选接手、Formal、检查矩阵、队列、暂停/恢复、配置、迁移、插件与环境快照 | Windows 原生业务路径已运行；可见终端证据独立于协议测试 |
| M2 | 平台协作、主子任务、共享事实、多候选/多方法集成、分支保留与清理、真实历史与diff | GitHub 单账户通过；第二账户协作仍待验收 |
| M3 | 原生构建、固定版本包、导入导出、tag、Release上传/下载/恢复、多渠道 | GitHub 实际字节校验通过；自建 GitLab 待验收；本地构建详情见交接清单 |

## 架构与权威

Dashboard 与 Agent 调用同一应用服务和 PublishService。Runtime 负责排队、原生执行、取消和恢复；Host 负责接入。项目测试、构建、插件不在 Host 内直接执行。真实 task/process/parent 身份来自调用上下文，正文不能指定可信身份。

| 事实 | 权威 | 约束 |
| --- | --- | --- |
| commit、tree、parent、ref | 对应 Git 仓库 | 完整 OID、明确仓库/ref；禁止隐式 origin/master、FETCH_HEAD |
| Issue、PR/MR、评论、审查、平台Release | 明确实例、仓库和账户的平台对象 | 固定观察、发送前身份/内容核对、发送后结果核对 |
| 事项、计划、草稿、证据、请求和操作 | 既有项目 SQLite/CAS | 稳定 item_id、单调 revision、请求参数摘要、先记录意图 |
| 专有共享事实 | 指定远端 `refs/heads/gitgo/publish-metadata-v1` | schema、前一OID、条件推进；不强推覆盖他人修改 |
| UI 焦点、页面、滚动 | 项目内前端状态 | 不改变业务身份、权限、平台状态 |

请求 ID 绑定调用者、项目与规范参数；同 ID 不同参数拒绝，同请求读取原收据。外部效果分别记录 `intent/observed/uncertain/partial`；未知结果不能标记成功或自动重发。每次写入重新验证当前配置、资源权限和实现摘要。Git ref 写入用预期旧 OID 条件更新。GitHub/GitLab 编辑端点没有跨对象原子 CAS 保证：本实现核对发送前对象和回执，不能承诺平台原子事务。

## 终端与应用接口

2026-10-27 交互补齐：`/task [序号]` 从本地事项打开已关联任务的正式公开对话，`P` 切换关联时记录的父任务，`D` 查看既有进程详情，Esc 返回原事项、节点与滚动位置。显示和跳转使用 `linked_task_id`，不误用关联操作发起者的 `task_id`。`publish.task` 在既有项目 SQLite 的只读快照中核对 process/session/task 关系，复用正式公开对话投影；较早的主任务、同一主进程的较早任务也可读取，不切换当前对话、不创建 writer、不启动 Daemon。读取每个 session 最多2000条消息、1000个任务，CAS读取总预算16MiB；超限或身份不符明确失败。

页面选择、节点、列表分页、焦点、滚动、历史图分页/选择/父提交diff位置，以及未保存的操作/评论/审阅/配置表单按配置档案、项目名与工作目录隔离保存。私有前端状态位于配置文件同目录的 `ui/publish`，使用内容受限的JSON和同目录原子替换，每项目上限1MiB，不进入业务账本、共享元数据或发行包。输入逐次落盘，保留中文/emoji、当前字段和光标；账户令牌不进入草稿文件。Esc保留未保存表单，重新打开原入口继续；重启后打开 `/publish` 恢复未完成页面。事项revision/binding或平台观察变化后，旧草稿不能直接提交，Ctrl+R明确按当前对象重新填写；配置字段同样核对保存时的原值。恢复只读取，不重放操作或自动发送。

`/history` 现在显示横向Git分支/合并轨道：左新右旧，`O`为选中提交、`*`为其他提交，边只来自真实parent；相邻轨道保留分叉和合并，跨页延续未到达的parent及其轨道编号，不把Publish业务流程当Git边。左右选择提交并自动平移，窄屏围绕选中轨道裁剪，Enter查看diff，`/parent N`选择merge父提交。图使用现有Ink和ASCII连线，没有新增依赖。

正式入口为 `/publish`。事项页上下选事项、左右选节点、Enter 看详情、Esc 逐层返回；设置页保留提交格式、隐私、远端和 Trial/Formal。绿色 CommandBar 输入命令，蓝色 NormalBar 编辑草稿。刷新不抢焦点，异步回复绑定项目和请求代次。长内容和输入字段随终端尺寸裁剪/滚动；仓库文本控制字符按纯文字显示。真实经典Windows控制台发现新宋体把部分箭头/中点当双格而Unicode默认单格，导致增量重绘残字。Host现通过不激活的私有控制台buffer实测已分配BMP符号和ambiguous字符，布局、输入字宽与Ink绘制在首帧前安装相同映射；不改用户字体、活动画面或配置。ConPTY隐藏伪窗口继续使用终端默认Unicode规则。

| 终端入口 | 应用服务/Agent工具职责 |
| --- | --- |
| 默认列表、详情 | `publish.items/item/operation`：纯查询；不开writer、不fetch、不测试、不迁移 |
| `/task [N]`、P、D、Esc | `publish.task`：读取精确的已有任务及公开对话；跳转父任务/进程详情并返回原位置 |
| `/prepare`、`/formal`、`/combine`、`/revise` | `publish.prepare`：固定 Workspace/Trial OID、范围、方法和最终树 |
| `/plan`、`/adopt` | 调整接手目标/负责人/剩余工作；条件创建本事项工作分支 |
| `/verify`、`/execute`、`/decide` | 检查固定最终树；正式写入；记录接受/返工/暂缓及理由 |
| `/run verify execute`、`/pause`、`/resume`、`/reconcile` | 指定连续步骤；边界暂停；明确恢复与效果观察 |
| `/history [candidate|formal|contribution:N]` | 真正 Git parent 分页；Enter看diff；`/parent N`选择merge父节点 |
| `/account`、`/remote`、`/search`、`/platform-more` | 明确账户绑定与对象读取/作用域搜索/分页 |
| `/issue`、`/pull`、`/comment`、`/review`、`/edit`、`/state`、`/comment-edit`、`/reply` | 保存固定平台观察上的草稿；不自动发送 |
| `/send 草稿ID`、`/platform-reconcile 草稿ID` | 明确发送与只观察未知结果 |
| `/followup`、`/link` | 评论/锚点待回复与本地处理记录；真实主子任务引用 |
| `/share 远端`、`/share-send`、`/share-refresh 远端`、`/share-reconcile` | 共享预览、显式推送、显式读取与未知结果核对 |
| `/shared`、`/shared-more` | 读取已保存共享观察；不联网、不追加业务状态 |
| `/branch`、`/branch-apply`、`/branch-reconcile` | 保留或准备归档/删除/发布；明确执行与核对 |
| `/package`、`/export`、`/import`、`/tag`、`/release`、`/download`、`/distribution-reconcile` | 固定资产构建、分发、下载验证与恢复 |
| `/migrate` | 明确备份、迁移选中的旧记录；不相信旧synced/pushed标志 |

Agent 工具在 `backend/core/tools/publish_service.py` 注册相同契约，使用 `publish_` 前缀命名。操作 schema 见该文件及 `backend/core/application/services.py`；原生构建、插件、项目检查分别通过封闭注册 Runtime 工具进入已有 ToolPipeline。账户令牌只在本地账户入口保存到操作系统加密存储，不传给项目脚本或Agent工具。

`config.terminals` 默认继续返回完整验证目录；启动时可传布尔值 `startup` 和 `attached`。Windows 已附加启动、保存为 current、或首次未配置的启动仅观察当前终端和字宽；首屏不枚举其他终端。首次设置和 General 页仍明确加载完整目录，新的已配置跨终端启动仍完成签名、来源和行为验证。前端启动已取消结果不再参与决策的父进程 PowerShell 查询。非 Windows 路径继续使用既有目录发现逻辑。

## 候选、检查与集成

每个候选固定仓库、原ref、解析后的实际分支、完整OID、base和tree。更改候选、组合、策略、测试或插件使旧证据失效；历史仍保留。接手可以保留失败检查和返工责任，不把接手等同Formal成功。Formal写入前核对当前目标、最终树、有效检查、环境与策略。

支持 ff、squash、merge、线性rebase、选定提交cherry-pick、选定完整路径partial，以及最多8个跨Workspace/Trial贡献组合。冲突保存路径和预览，停止正式写入；可接手到工作分支处理。所有最终检查在实际组合树执行；ff/merge保留历史和改写提交中间树同样执行出站检查。partial目前以完整文件路径选择，不提供任意hunk编辑；rebase不重写merge历史。自定义Git hook、merge driver、textconv和replace/graft不能参与这些执行。

项目必需pytest检查不能被请求省略；最多8项、每项4个种子、单种子120秒、声明总时限180秒。测试运行于私有最终树副本，逐项/逐种子记录退出码和有界日志；改写输入拒绝通过。检查通过不代表完整语义审阅。依赖/锁文件保存hash；运行环境保存平台、架构、解释器、库及元数据内容摘要，不保存凭据或绝对安装路径，也不自动安装依赖。

五个提交插件钩子接入既有 SyncPlugin：扫描、选择、消息、开始、完成。最多8个插件、每包64个条目、单文件512KiB、合计1MiB、总时限30秒；固定源码及资源，拒绝链接。插件在原生隔离中运行；选择变化重新绑定并要求复评，完成钩子未知/失败保留已发生Git事实，不伪造回滚或自动重放。源码注释清理沿用现有配置，对实际变换树检查，不修改原工作区文件。

Runtime 队列最多16个请求、单工作线程；仅推进明确授权的步骤，保留每一步观察。失败、漂移、授权失效、暂停或未知效果停止后续步骤。暂停请求不宣称撤销已发生Git/平台效果；原生资源退出和访问撤销确认后才结束。重启后需要明确核对，不自动重跑未确认动作。

## 协作、共享与分支

GitHub和自建GitLab适配器均有列表、搜索、创建、编辑、关闭/重开、评论、本人评论编辑、引用回复与固定锚点线程。搜索限制在指定仓库，不接受改变作用域的查询语法；分页有上限。目标绑定平台、HTTPS实例、仓库和账户；发送前核对平台账户ID、仓库ID和固定对象。评审保存固定head/base与文件锚点；旧锚点变化时明确拒绝或按原线程回复，不猜测新行号。未知新建结果只通过标记和已观察对象核对，不能自动补发。

待回复记录包含固定snapshot/reference、owner、note和状态；`resolved`仅代表本地跟进已处理，不冒充平台线程已解决。主子任务必须引用已有进程/任务；记录关联不自动创建或委派任务。

共享分支只保存明确允许的来源映射、责任、审阅决定、证据摘要、结果OID、任务和分支保留引用，不上传私有草稿、原日志、密钥或本地数据库。发送前重新核对公开投影digest，防止计划未变但负责人或决定已变时发送旧预览。共享只读页面读取固定CAS观察并分页，不把收到的数据变成当前本地授权或执行证明。

分支清理先准备明确计划，保存原OID并归档，再核对真实依赖、worktree占用、目标变化及远端旧值。其他事项候选、组合源、Formal目标、未关闭PR/MR和未知发送草稿均阻止删除；删除意图后新依赖也被拒绝。PR/MR状态以显式平台刷新为准，清理不隐式联网。归档与保留有独立事实记录；实际删除只作用于本事项拥有的分支。

Git历史与流程图分别建模。历史查询读取真实commit bytes并验证hash及parent，以固定head/snapshot分页；不凭业务节点生成Git边。diff两端须属于该固定范围，根空树只对真正无parent提交开放。提交、页面、输出均有硬上限，巨大diff标记截断，不先无界读取再裁剪。已验证不可变结果缓存TTL为30秒、最多64项/8MiB；每次查询仍先读取当前授权范围、配置和binding，缓存不包含权限。冷读与缓存命中的温读分别报告。

## 资产、发行与恢复

`package`只使用已观察Formal结果OID，不包含工作区未提交文件。确定性ZIP包含源码、manifest、逐文件blob/模式/SHA-256、检查、环境和依赖摘要；版本一经绑定不能换指其他来源。可选Python构建入口必须存在固定源码中，输出声明为dist/下的明确文件；时限1–120秒，原生隔离无凭据无自动安装。原生进程与访问授权退出后，父端固定句柄读取资产封入CAS，核对输入与环境未改写。

源码上限4096文件、单文件16MiB、总计128MiB；构建资产最多8个、各16MiB、合计128MiB；整包上限272MiB。构建输出通过文件捕获，不经JSON/base64扩大原有协议限制。构建日志及环境摘要持久保存。取消/崩溃保留原生运行记录，核对只恢复已保存结果，不重跑脚本。

导出仅在明确已有目录新建内容寻址文件，拒绝覆盖；使用平台原生相对目录句柄和无覆盖改名。导入要求预期SHA-256并验证每个成员、路径、容量、Git blob/tree及固定Formal来源；只保存已验证包，不执行或安装。摘要不是数字签名。

GitHub先创建草稿，逐资产上传并真正下载验证后再发布；按标签找不到时有界读取Release列表寻找草稿，避免重复创建。GitLab先上传版本固定generic package，核对字节后创建含准确链接的Release。每次外部写入先记录checkpoint；`distribution_reconcile`仅观察tag/Release和下载字节，缺失步骤需新的明确release动作。标签变化、不同同名内容、额外资产或账户/仓库身份变化拒绝成功。GitHub资产CDN跳转移除凭据且限制主机；bare Git通过固定标签和独立包/digest交接，不伪造平台Release。

实际GitHub验收修复了两处协议差异：草稿不由tag查询返回；上传响应必须请求JSON而非下载用octet-stream。另修复评论创建误用父Issue ETag引发HTTP400。原失败/未知回执保留，先观察再发起新恢复请求，未盲重放。

## 迁移与故障处理

页面打开只读旧状态；旧行使用快照观察身份，不能直接写。显式迁移先将旧状态备份到既有CAS，再同一次CAS保存稳定事项与映射。新旧路径共用交接锁，旧同步/推送在Git前拒绝已交接记录。无新变化的旧记录可保留为未验证观察；旧成功标志不能生成新证据。迁移不撤销外部效果，回退通过新的明确操作完成。

故障交接应保留事项ID/revision、operation/request ID、固定OID、运行记录和原因。读取 `publish.operation`，按所属领域使用reconcile；确认未发生的动作才可新建请求。令牌过期由账户入口更新；不要在日志、Issue或交接包中写令牌。

维护修正：目标写入前的确定拒绝进入失败终态，旧检查失效并允许重新准备；超时和丢失应答继续只观察，不重复写入。rebase/cherry-pick 在计划中绑定原 Author/AuthorDate，提交人使用当前集成人身份。

账本超过原有 4,000,000 字节内联阈值时，正文以不可变页面保存在同一项目 CAS，页面引用与新账本索引在既有 SQLite 事务内同时提交；旧版本 1 内联账本仍可读取。项目/CAS/磁盘配额没有取消，真正写入失败保留旧意图并要求恢复。生成分页账本后，旧程序不能读取新版索引；回退前需保存完整状态及页面，不能把旧快照强行恢复到已发生外部效果之前。

## 验收证据与限制

用户授权的测试目录为 `C:/Users/jgeted/Desktop/Gitgo-Issue40-Acceptance`，所有生成目标、日志与产物放在该目录。已有一个真实GitHub账户jgeted；本地alice/bob clone仅是Git身份，不是两个平台账户。主机没有可用Docker/Podman或WSL发行版，未伪造自建GitLab。

| 验证 | 证据及结果 |
| --- | --- |
| GitHub单账户原生应用API | `remote-acceptance/result.json`：Issue #1、PR #2、评论、审查、共享推送、Release、下载和导入通过 |
| GitHub扩展协作 | `remote-acceptance/collaboration-extended.json`：搜索、编辑、close/reopen、本人评论编辑、引用回复、跟进通过 |
| Release实际下载 | `gitgo-v0.0.40-alpha.1-763d38f24524.zip`，SHA-256 `763d38f24524f1ad865cebaf46f8e793257d4d6bfe16c55e4a24920abe9a5703` |
| 前端最终组件回归 | `frontend-final-layout.log`：59项、354断言通过（含实测宽度、换行、省略号、固定头尾、输入、启动和重绘）；TypeScript与生产bundle通过 |
| Release协议故障注入 | `release-protocol.xml`：11项通过，明确不计作真实GitLab验收 |
| Publish完整后端 | `publish-final.xml`覆盖280项：278通过；检查矩阵在源码漂移后单独复核通过，并发测试更新为验证事项独占锁后通过（`check-failure-rerun.xml`、`concurrency-verified.xml`） |
| 受影响边界 | `boundary-final.xml`：196通过、38平台/能力跳过；原失败的Git接收hook在修后完整50项边界组通过，Host崩溃回收在正确导入路径的独立运行时复核通过（`native-crash-verified.xml`） |
| 终端配置 | `terminal-config-final-width.xml`：32通过、1跳过；真实NativeHost IPC已返回新宋体540个宽度修正，测量前后活动画面、尺寸及字体一致 |
| 真实原生终端业务 | `terminal-m2-m3-9deslpx5/result.json`：中文/emoji项目名和输入、Workspace+Trial组合、原生双种子检查、Formal写入、原生构建及导出通过；原HEAD/main与未提交文件保留。Formal `c1721dd73b424a645939d9141b1a69077fc1a7db`；导出SHA-256 `5576e3dde7b29fd141bc77a4740f6b8d288bfb7ad2154db7d4f91a4666a511cb` |
| 可见终端布局 | 上述业务运行发现头尾压缩及字体宽度问题，已修正固定头尾与正文剩余高度分配。修后正式bundle的120×32、80×24、40×20截图及人工复核在 `terminal-m2-m3-9deslpx5/visual-review-final.json`；同目录 `render-ab-34s1ntiy` 保留有/无宽度修正的真实窗口对照。业务与视觉结果独立记录 |
| 1000事项/10000提交 | `history-performance-final-20261026/performance-results.json`：1000事项温读p95 25.3ms；10000提交首/中/尾页冷读426/444/437ms，温读p95 16.9/19.9/16.1ms；diff冷读545.9ms、温读p95 15.4ms（15样本） |
| 独立干净源码打包 | `scripts/publish_acceptance_build.py snapshot`重建本次差异并生成来源记录；`build-delivery.log`记录门禁与打包过程，`packages-delivery`保存正式Windows包、manifest和SHA-256，最终来源和摘要以 `delivery.json` 为准 |

性能目标：120×32、80×24、40×20；事项单页50/上限100，1000项与10000提交固定快照。温读取p95 300ms、按键100ms、首屏1s；未达到的测量需明确报告，不把Mock计时算作真实平台速度。远端操作有独立等待状态，不能用网络等待阻断输入。

后续 Windows 验收增加 `scripts/publish_terminal_benchmark.py`：在最终 EXE 的真实可见控制台，三个尺寸各采样20次节点切换、文字输入、滚动及历史页面打开；程序新进程启动、Publish 首次打开、历史首次打开与重复读取分别记录。控制台观察在启动前附加并重新打开当前活动缓冲区，不修改终端模式。统计使用最近秩 p95，保留全部原始样本；程序启动不混同于已打开应用中的页面首屏。该业务样例为1事项/短历史，规模查询仍参照上表独立的1000事项/10000提交数据。

打包版冷读发现 Windows 进程监管角色重复加载整个业务模块图。`backend` 和 `backend.core` 的公开导出改为按需解析，保持原对象身份；`windows-scope` 与 `windows-job-lease` 在加载应用/更新模块前分派，沿用原本仅接受继承句柄的监管协议与原有准入豁免。Windows 打包钩子仅为这两个固定角色跳过 setuptools/traitlets 的预加载，其他角色保留原有 distutils 与冻结源码兼容行为，依赖仍随包提供。Python、Daemon、tool-runner 等业务角色仍经过安装准入，Git 配置固定、输出上限和 Job 清理没有改变。对应回归与重新打包的实测结果以桌面交接清单为准。

## 构建与交接

使用现有 `packaging/build_windows.ps1`，从独立干净源码副本编译正式Dashboard和PyInstaller Host，执行输入完整性、Host/Daemon/tool-runner与会话连续性门禁，生成ZIP/清单/摘要。快照记录基线SHA、diff摘要、新增文件和本地快照提交，不提交或推送产品工作树。工具仅使用Python 3.14.7、SQLite 3.53.4、Bun 1.4.2及锁定PyInstaller依赖；ripgrep 15.2.0按官方归档SHA验证，携带原始许可证。

本功能没有新增运行时依赖：网络复用httpx，终端复用既有React/Ink，存储和摘要复用现有实现与标准库；`package.json`及运行依赖锁不因Publish新增。构建依赖固定在 `packaging/requirements-build.lock`，前端固定在 `cli/dashboard/bun.lock`。项目许可证沿用 `LICENSE.md`；vendor许可证及运行时组件声明随交接保留，不能把项目许可替代第三方许可。桌面 `dependency-notices/inventory.json` 已登记128个Python/JavaScript运行或构建依赖的版本、许可元数据和原始声明文件，均找到声明文件，并附项目、Python、ripgrep及Bun 1.4.2官方许可/静态依赖说明原文。既有vendored Ink的首次引入提交、tree/blob及官方参考已核实在 `cli/dashboard/vendor/ink/PROVENANCE.md` 和 `provenance.json`；原始上游 revision 与授权依据仍未取得，状态为 unverified。本次没有给它补写未经证实的MIT/Apache许可。用户项目产物是否允许分发由其来源许可决定，本功能不改写许可。

启动产品继续使用仓库正式启动器/构建后的Gitgo入口。桌面测试使用独立config/state和合成仓库，未上传产品源码。验收脚本只在已登记测试目标运行；恢复脚本保留未知结果，不自动重新创建外部对象。
