# Windows 管理更新与回滚

关联 [Issue 21](https://github.com/Truman-Duo/Gitgo/issues/21)。支持 Windows x64、本地 NTFS 的全新管理安装，格式为 `managed-windows-v1`、API 7/schema 6。不会接管现有 Inno 安装；后台自动检查继续关闭。

## 构建与安装

`packaging/build_windows.ps1 -ManagedUpdate` 要求新的私有输出目录，并核对实际冻结 Host 数据契约。可另传 `-UpdateReleasePolicy <publisher-policy-file>`，只包含公钥与 HTTPS 渠道地址。缺少策略时不联网。

从完整解压包在 PowerShell 执行：

```powershell
./install-managed.ps1 -Root <新的规范绝对安装路径> -BinDir <独立的规范绝对命令目录> -ProtectedPath <项目路径>
```

安装不提升权限、不改注册表、全局执行策略或 PATH，不覆盖非空目录或已有命令。通过独立命令目录的 `gitgo.ps1` 或安装根的 `gitgo.cmd` 启动；支持中文和空格路径。配置、密钥、数据库及项目须位于程序目录之外。没有管理卸载入口，不要使用旧 Inno 卸载器删除新安装。

## 更新与保护

首页 `/update` 提供检查、确认下载、确认退出更新及保留版本回滚，见[操作说明](update-preparation.md)。签名、数据兼容、包内容、安装目录/锁身份和运行占用均重新检查；损坏登记、外部可写目录、网络路径、reparse/junction、硬链接和数据流被拒绝。

正常程序和子进程持有共享租约；更新需要独占，其他实例存在时返回 busy，不强制结束它们。目标命令在可信等待程序加入独立 Job 后才启动。启动报错、取消和正常结束均需终止并确认整树归零，再等待租约守护进程退出；清理未知时保留资源并明确阻止继续启动命令，允许重新核验清理。该 Job 只管理拥有的进程树，不是 OS 沙箱。

坏候选恢复旧程序；回滚只支持最近保留版本，不回滚数据库/配置。恢复与失败处理见[说明](update-transactions.md)。健康检查使用真实 Host 和 Dashboard 的有界启动检查，不代表完整用户交互已验收。

## 验证摘要与限制

[基线自动化 38728012455](https://github.com/Truman-Duo/Gitgo/actions/runs/38728012455) 对 PR #32 的 `000d80f` 与目标 `0b7c2ab` 的合并候选完成原生 NTFS/Job 测试、源码回归、Dashboard 测试/构建及真实双版本 PE 包升级、回滚和恢复。包安装测试包括 PowerShell 5.1/7、中文空格路径和公开启动入口。基线通过不覆盖后续修复提交，须单独验证。

真实包测试使用一次性签名与注入传输，不配置生产更新源，也不发布安装包。正式可见终端确认流程、生产签名/CDN/代理尚待验收。Windows ARM64、UNC/共享盘、旧安装迁移、管理卸载及物理掉电耐久性不在当前承诺范围。
