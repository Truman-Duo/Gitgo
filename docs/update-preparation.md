# 手动更新与回滚

关联 [Issue 21](https://github.com/Truman-Duo/Gitgo/issues/21)。更新只用于明确选择的[管理安装](managed-installation.md)，默认不启用后台检查或自动下载。

## 使用

在项目列表首页输入 `/update`。项目聊天、Agent 详情和进程列表不提供此命令，也不支持 `/update apply` 等额外参数。打开页面或按 `R` 只刷新本地状态。

| 按键 | 操作 |
|---|---|
| `C` | 选择 stable / preview；不自动检查或切换版本 |
| `K` | 手动检查签名清单；不下载整包 |
| `D` | 确认下载当前显示的精确候选 |
| `A` | 对支持的平台和已验证下载，确认退出并申请更新 |
| `B` | 确认恢复可核对的上一版程序；无需联网 |
| `Enter` | 确认当前显示的操作 |
| `X` | 取消检查/下载；有待重启请求时进入独立撤销确认 |
| `Esc` | 取消当前确认，或取消操作并关闭页面 |

粘贴和修饰键不触发单键操作。取消为协作式，阻塞网络读取仍受超时约束。下载完成不构成替换授权；迟到回复、普通退出或崩溃不能代替确认。

未配置发行策略时明确显示未配置，不从用户输入的任意链接下载。错误信息不输出代理凭据或本机资料路径。General 中的版本来自实际执行 Host；版本读取失败显示不可用。

## 发行策略

发行维护者在初始构建/安装时固定公钥和渠道地址。Unix 使用 `--update-evidence --update-release-policy <publisher-policy-file>`；Windows 使用 `-ManagedUpdate -UpdateReleasePolicy <publisher-policy-file>`。策略包含公开信息，不能包含私钥、API key 或 Provider 配置。

`release-trust.json` schema 1：

| 字段 | 要求 |
|---|---|
| `schema_version` / `product_id` | `1` / `gitgo-terminal` |
| `platform` / `architecture` | 精确匹配安装目标 |
| `allowed_hosts` | 1–16 个明确 DNS 名；无 IP、任意端口或通配符 |
| `public_keys` | 1–16 项；唯一 `key_id` 和规范 Base64 的 32 字节 `ed25519_base64` 公钥 |
| `channels` | stable/preview 至少一项；各含不同的 `manifest_url`、`signature_url`，均为允许域名的 HTTPS URL |

候选包不能更换初始信任根。在线密钥轮换和正式发布源尚待维护者配置，不能通过修改已登记文件开启更新。

## 平台行为

Windows 和 Linux 仅在正常确认退出、自己的进程树清理得到证明、其他实例释放租约后切换。检查/下载期间不阻塞其他正常任务启动。忙、清理未知、签名/兼容性失败均停止交接。

macOS 显示待重启状态，不即时替换，也不主动重启电脑。重新打开 Gitgo或睡眠不能代替电脑重启。重启后只尝试已确认的精确选择，重新验证有效期、包和占用情况；中断请求不自动重放。真实跨重启升级/回滚尚待验收。受限候选健康检查依赖已弃用的系统 `sandbox-exec` 接口，缺失或失败时拒绝，不授予普通工具隔离能力。

坏候选由启动器恢复旧程序；显式回滚只支持最近保留版本，不还原用户数据。缓存过期/被修改、保留记录达到上限时明确拒绝，需要重新准备或独立维护。验证与限制见 [更新检查](update-discovery.md)、[恢复处理](update-transactions.md)、[Windows](windows-managed-update.md)、[Linux](linux-managed-update.md)。
