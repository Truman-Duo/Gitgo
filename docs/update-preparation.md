# 手动更新准备与确认交接

关联 [Issue 21](https://github.com/Truman-Duo/Gitgo/issues/21)。这是功能分支的准备及确认交接实现，不是已公开发行的完整自动更新。检查和下载不执行候选、不替换当前版本。只有另行确认且通过平台、稳定启动器及运行锁门禁后，才可交接到事务引擎；不执行包内安装/卸载脚本，不支持主动回滚。

## 使用与状态

`/update` 仅属于项目列表首页，不出现在项目聊天或进程列表。页面打开与 `R` 只读本地状态，不访问发行服务器；General 继续显示执行 Host 的真实只读版本。

- `C`：明确选择 stable / preview，只刷新本地该渠道状态，不自动切换或下载。
- `K`：手动请求签名清单和独立签名，显示最新兼容版本；检查不会下载整包。
- `D`：在已有已验证候选时进入下载确认，显示 release ID 和整包大小。只有确认后才下载该清单对应的精确包，不在下载时偷偷改选刚发布的另一个版本。
- `A`：仅受支持的管理启动器、平台及已验证下载可进入应用确认。
- `Enter`：只在下载或应用确认中生效；应用确认绑定显示的精确候选，不选择其他版本。
- `X`：检查/下载过程中请求取消；`Esc` 取消并关闭页面，确认框内的 `Esc` 只取消确认。

粘贴和修饰键不触发上述单键操作。取消是协作式的，阻塞读取仍受网络超时限制；取消确认与下载完成可能交错，但均不会安装任何软件。

状态区分未配置、尚未检查、有新版本、已是当前版本、下载并验证完成、缓存过期/被修改。失败仅显示安全错误，不打印远程响应、代理凭据或本机资料路径。缺少发行信任时，页面明确显示未配置，不从任意输入链接下载。

## 本地权威与并发

应用请求支持 `update.status(channel)`、`update.check(channel, operation_id)`、`update.download(channel, candidate_id, operation_id)`、`update.cancel(operation_id)`，以及 `update.request_apply(channel, candidate_id, operation_id)`。最后一项只登记当前启动会话的确认请求，不在 Host 内替换程序。不接受调用方给定的安装根、暂存目录、URL、公钥、启用标记或健康回调。没有 `update.apply`/`update.enable` 操作。

真实安装由正在执行的冻结 Host 的物理位置与 schema 3 登记核对，不能通过环境变量冒充。普通安装/源码入口仍显示兼容的 schema 1 禁用状态；schema 2 是此前未公开的管理实验登记，不自动迁移或接管。Windows 还没有管理所有权/ACL 适配，不能使用真实下载暂存入口。

每次签名检查成功后，渠道 high water 先持久化到既有安装 ledger，再保存签名缓存。取消、断网、错误包和重启不降低它。缓存中的原始清单/签名每次使用都重新验证，不作为第二套 release 权威。有效期在收到清单后、传输后与解压后重新核对。

独立 preparation lock 的 inode 固定在安装登记中。同一安装只允许一个准备操作；检查/下载期间持有共享运行锁，不在网络等待期间持有独占 runtime/operation 锁，其他正常项目子进程仍可启动。Native Host 请求超时、页面关闭和 Host 正常关闭会向当前准备任务发出取消。

下载目录由本地登记固定在 `control/preparation/` 下，每次随机新建。不扫描采用残片，不执行已下载文件，不允许客户端指定路径。归档大小/摘要、安全解压、完整 product/build/source/inventory/data contract 全部通过后，才写入已准备 receipt。本地 receipt 路径、目录 inode、归档与 payload 字节变化会使它失效。应用更新阶段仍须重新核验，不得凭该 receipt 直接替换。

仅清理本次明确创建、身份未变且未发布 receipt 的失败目录；断电或发布持久化故障可能留下诊断内容，不会成为 active。保留条目到达上限时明确拒绝，维护清理是独立事项，不删除未知目录、项目、配置或数据库。

## 发行策略配置

默认构建不带公钥或发行地址。发行维护者准备仅包含公开信息的 `release-trust.json`，在新 Unix 构建时明确传入 `--update-evidence --update-release-policy <publisher-policy-file>`。打包器验证目标和策略格式后，在完整 inventory 生成前加入该文件。初始化管理安装将它复制到完整摘要固定的 bootstrap；远程候选中的同名文件不能覆盖它。更改信任/轮换密钥的生产流程不属于此阶段。

策略 schema 1 固定字段为：

| 字段 | 要求 |
|---|---|
| `schema_version` / `product_id` | `1` / `gitgo-terminal` |
| `platform` / `architecture` | 精确匹配已验证安装目标 |
| `allowed_hosts` | 1–16 个明确 DNS 名，无 IP、任意端口或通配符 |
| `public_keys` | 1–16 项，唯一 `key_id` 与规范 Base64 的 32 字节 `ed25519_base64` 公钥 |
| `channels` | stable/preview 中至少一项，各含不同的 `manifest_url`、`signature_url`，均为允许域名的 HTTPS URL |

策略不包含私钥或 API key。正式域名、公钥与签名发行流程必须由维护者确认；测试内存密钥或 MockTransport 不能成为正式发行信任。不能在用户 Provider 配置中混入策略，或者手工修改已登记的 bootstrap 来开启更新。

## 验证边界

回归覆盖明确检查/确认、渠道隔离、持久 high water、防重放、过期、下载取消/中断、错误签名/摘要、包契约错误、缓存/锁替换、并发项目启动，以及 Host 取消/关闭。UI 有真实输入路由和渲染测试，但它们不等于正式终端视觉人工验收。

可选真实冻结测试使用 `GITGO_MANAGED_TEST_STAGE` 与 `GITGO_UPDATE_TEST_ARCHIVE` 指定新构建的完整包。测试在副本中固定一次性公钥，使用注入的 HTTPX 流运行真实冻结 Host 协议，并要求签名清单宣称的版本与实际整包不符时拒绝。它不执行候选，不是两个真实版本升级成功，也不是正式 CDN/HTTPS/代理验收。

## 第二阶段交接边界

管理状态协议 schema 3 通过 `can_request_apply` 单独表达可请求交接；`can_apply` 仍为 false，因为运行中的 Host 不直接替换自身。旧 schema 2 状态仍可展示检查/下载，但不提供应用确认。

稳定 bootstrap 在 active 之外创建有界会话票据。收到与精确候选匹配的确认回复后，前端停止界面并要求 Host 正常以 0 退出，才返回专用退出码 75。取消后的迟到回复、普通退出、崩溃和强制关闭 Host 不触发应用。bootstrap 必须确认自己拥有的进程范围清理完整，并释放自身全部共享租约，才重新验签、核对下载并取得独占运行锁；其他实例仍运行则拒绝切换，不杀它们。

切换使用既有单一 ledger。候选在独立临时资料目录中执行真实 Host 初始化/协议检查及 Dashboard `--version` 加载检查；不调用模型、不读取用户秘密、不迁移用户数据库。失败按既有事务恢复旧版，启动前再次恢复检查；重启不重放旧命令行任务。此检查不等于完整可见终端或真实业务验收。

当前仅 Linux 可进入交接，尚无 Linux 实机验收。macOS owned-session-only 无法证明完整清理，因此明确拒绝应用，不能通过测试替身或用户确认绕过。Windows 管理适配仍缺失。默认包没有正式发行信任。真实双版本升级/故障恢复、主动回滚、正式发行与跨平台验收仍待完成。`tests/test_update_handoff.py` 的模拟平台和健康驱动只验证交接状态机，不作为这些验收证据。
