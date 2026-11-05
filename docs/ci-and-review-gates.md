# CI 与合并门槛

主仓库的 Gitgo CI 对所有目标分支的 PR 代码更新、master push 和手动运行触发；功能接纳分支也使用同一套检查。PR 标题、正文等元数据编辑只重验 PR metadata contract，不重启打包，也不替代已有 CI gate。预留 merge_group 支持，不表示个人仓库已启用合并队列。workflow 不按 paths 跳过，统一必需状态为 **CI gate**。gate 使用 always 汇总：失败、取消或意外跳过不能成为成功。功能接纳与版本基线更新分别验证，见 [分支生命周期](branch-lifecycle.md)。

## 检查范围

- PR 标题遵守目标基线的 commit-config.json；贡献者无需申请全局编号。正文中的候选 head、验证 base 和源/目标身份必须与本次 PR 一致，包含问题、设计、验证、兼容/回滚、上下游和风险说明。允许同义标题，不机械要求复制模板；这只能检查说明存在，不能证明说明真实或设计合理。
- 开发分支必须包含本次观察到的实际目标。目标变化时同步并更新正文后重新验证；启用 strict required-status 规则可阻止过时组合合入，未启用远端规则时维护者仍须核对，不得省略。CI 保存实际测试的 merge commit/tree、head/base 和 run/attempt，旧绿灯不是新候选证据。
- tracked-file 隐私边界、Windows 完整 Python suite、锁定 Bun 依赖的 dashboard suite 和 production build。基础源码验收不冒充跨平台覆盖；另有 macOS/Linux 包和原生凭据测试。
- Bun/Node 三平台 runtime matrix、Windows 最终 ZIP 与 frozen 连续性、macOS ARM64/Linux x86_64/Linux aarch64 原生 build 和独立 worker 的最终包验收均进入 CI gate；完整来源、依赖固定和人工验收边界见 [原生包自动化](native-package-automation.md)。
- 管理更新另调用 Windows x64 与 Linux x86_64/ARM64 的双版本真实包验收，并纳入同一 CI gate；任一失败或取消都会阻断。复用测试仅上传结果报告，临时更新包不作为正式 Release。Windows 源码测试使用用户 profile 下的规范私有临时目录，不通过修改共享 scratch 盘 ACL 绕过安装权限检查。
- 候选或目标上游包含原生 sandbox 后，必须保留真实验收入口，启用 Windows/Linux 的源码与实际 frozen Host 验收，Windows 先跑精确 ACL 恢复检查。未引入 sandbox 的主线跳过此项，gate 明确判断这是预期跳过，不将其宣称为沙箱通过。原生实现进入主线后，不能删掉 backend/验收入口让检查消失。

## 调度与重复执行

昂贵任务在 PR 契约、调度回归和公开文件检查通过后启动。Windows 完整源码回归分到四台独立 runner，每片完整收集当前套件，按整文件分配并保留文件内次序；汇总核对同轮提交、树、依赖环境、完整集合及每个用例的报告，漏片、重复、过滤、失败或不一致均拒绝。跳过用例仍在 JUnit 中标为跳过，不统计为通过；仅在全部分片及完整性检查成功后输出回归结果。Linux x86_64/ARM64 各自在真实委托环境执行完整源码回归。原生任务另验本环境的沙箱、ACL、存储原语和真实 frozen Host，不再次运行与它无关的整套源码测试。没有移除完整源码用例，也不以单元测试替代最终包验证。

用例身份必须稳定，随机测试数据应使用明确的参数 ID。每片在执行前比对两次独立收集结果，不一致立即停止；跨 runner 的最终完整性检查仍保留。新增功能应增加相应用例，旧行为保留回归，当前协议检查复用协议权威，历史兼容用例则明确绑定其旧版本。

Windows 更新任务只接收同一轮成功前置任务输出的精确 commit，核对它与自己的 checkout 一致后，复用源码及 Dashboard 回归结论，继续独立构建两个版本并验证安装、升级、回退和恢复。缺少前置输出时自行运行完整套件；身份错误直接失败。这里没有跨提交的通过缓存。测试包与正式发行包仍分别记录身份。

原生 Unix 按目标分别执行“构建→独立 worker 验收”，某一目标排队不会阻止其他目标验收；总 gate 仍等待全部目标。macOS 三类能力审计纳入总 gate，独立 Native sandbox security 保留手动及旧安全分支入口，不再对相同 PR 自动启动另一套重复测试。所有测试或工作流变动都须在本次候选重新验证，不能复用旧候选绿灯。

scripts/provision_ci_sqlite.py 与 provision_ci_ripgrep.py 单独复用 PR #25 的固定下载/校验辅助脚本（参考候选 1a2da14fc6e9879a7ead6fd471f53b409659ddcc，贡献者 jgeted），仅在可丢弃的 hosted CI 中准备依赖，不表示接受该 PR 的生产隔离实现。SQLite 3.53.4 官方归档使用固定 SHA3-256；ripgrep 15.2.0 官方发行归档使用固定 SHA-256；原有 SQLite 安全校验保留，不设环境绕过。依赖升级应单独审查并更新版本和证据。

## CI 自身的权限边界

第二轮发现测试终端 keeper 在交接时提前退出。WindowsProcess 的进程退出竞态修复参考 PR #25：先检查 OS handle 是否已 signaled；身份查询失败后仅在该 handle 已退出时按退出处理，活进程身份读取错误继续拒绝。新增真实已退出进程回归，交接测试显式输出 keeper 的错误报告；不隐藏失败、不放宽 PID/镜像/出生时间检查。

Windows 测试使用 runner 自有临时目录的规范绝对路径，并启用 Python UTF-8 模式，避免 hosted 环境的 RUNNER~1 别名和 cp1252 默认编码影响测试身份。删除与终端清理仍拒绝重定向路径；这不表示产品已经支持所有 Windows 短路径别名。首次全量运行也暴露了 LocalFileAdapter.is_symlink 先 resolve 后检查的真实缺陷，本 PR 仅修复目录项判断，并用现有测试验证正常文件和悬空链接；不接入 PR #25 的其余生产实现。

只运行 GitHub-hosted 临时 runner，顶层 token 为 contents:read；checkout 不保存凭据，不注入项目 provider/API key，不缓存 PR 执行后可污染的依赖产物，不上传整个 .gitgo 或 trace。新增原生打包 job 仅上传安装归档和脱敏验收记录，不自动发布正式 Release。第三方 Actions 固定完整 commit SHA，并注明版本；原有 sandbox 下载工具仍是验证用，不冒充正式 release staging。

使用 pull_request 执行候选组合，不使用带高权限的 pull_request_target 执行 fork 代码。不可信标题/正文从 JSON 读取，不直接插入 shell。fork 首次/外部贡献运行所需的 GitHub 人工批准保留；批准 CI 执行不等于批准 PR 或沙箱越权。

required status 限定 GitHub Actions 来源，减少其他身份伪造同名状态的机会。PR 仍能修改自己的 workflow/测试，这个来源约束不能证明检查逻辑未被篡改；所有 CI、验收脚本、依赖和安全策略变更必须由维护者逐项审核。严格的独立可信 attestations/required workflows 是后续可选加强措施，不能把当前普通 Actions 说成不可绕过的安全审计。

## 远端 master 规则

master 是版本基线，功能 PR 不直接合入。维护者先在独立功能分支接纳，再在版本整合分支完成组合验证，最后通过单独授权的基线 PR 更新 master，见 [分支与接纳生命周期](branch-lifecycle.md)。CI 对功能目标的覆盖不表示这些分支已自动获得 master 的远端保护规则；维护者仍须核对检查与合并条件。

CI 配置首次在独立 PR 运行并修好后，再接纳并启用下面的仓库规则，避免凭空指定尚未产生过的状态名称：

1. master 的更新必须走 PR，禁止 force push 和删除。
2. 必需状态为 CI gate，来源为 GitHub Actions；必须与目标分支保持最新，检查通过再合并。
3. 所有讨论须解决；新的提交使旧 review 失效。设计审查不能被绿灯代替。
4. 维护者默认 squash，为最终提交分配最新唯一 [GITGO-N] 和规范 subject，保留贡献作者。当前 push CI 会校验新集成提交的格式，不能把事后 push 检查误称为提交编号的服务端预阻断；合并前编号与最终 squash message 仍由维护者核验。
5. 不启用签名强制、线性历史等与既有历史/工具未经协调的额外门槛。规则的 review 数量需匹配实际可用的独立 reviewer 身份；本仓库的 AI 若与 PR 作者共用同一 GitHub 身份，不能自批算作独立审批。

不可用或未完成的 sandbox 平台必须明确阻断，不能为了 CI 绿灯无隔离重试、全局 mock OS、跳过失败或放宽 ACL 比较。检查 failure/skip 数需准确解释。CI 不证明“无逃逸”，运行时必需原语失效也不能依赖 CI 报告兜底。

## 正式终端与发布

Dashboard 测试/build 不等于正式 UI 验收。用户可见变更依然需要从 run_dashboard_native.bat 在真实颜色终端检查完整 Bun/Ink UI、输入、状态栏与 Host 同步。CI 保存的证据明确 formal_terminal_verified=false；维护者另行记录人工验收。

Native security 的 frozen Host 是一次性安全测试构建，不是签名 Installer/Release。实际发布仍按既有打包、隐私、第三方通知、安装和正式终端流程验证。
