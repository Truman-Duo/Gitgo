# 管理安装

关联 [Issue 21](https://github.com/Truman-Duo/Gitgo/issues/21)。这是明确选择的新安装模式；普通旧安装和已发布预览包不会自动获得更新能力。

## 适用范围

| 平台 | 安装格式 | 更新方式 |
|---|---|---|
| Windows x64、本地 NTFS | API 8 / schema 6 | 手动确认或启用自动安装后正常退出，完整清理后交接 |
| Linux x86_64 / aarch64 | API 9 / schema 5 | 手动确认或启用自动安装后正常退出，完整清理后交接 |
| macOS ARM64 | API 9 / schema 5 | 手动确认或自动准备交接后等待电脑重启；跨重启升级/回滚尚待验收 |

管理安装要求完整构建清单和经实际冻结 Host 核对的数据兼容证据。缺少证据的旧包、旧实验格式、已有非空目录均不接管。默认不带正式发行公钥或渠道地址；缺少策略时不联网。

## Unix 安装

构建时明确选择 `--update-evidence`；需要更新源时另传 `--update-release-policy <publisher-policy-file>`。从新的完整包执行：

```text
./install.sh --managed --no-path-update --install-root /absolute/new-installation --bin-dir /absolute/separate-bin
```

安装根和独立命令目录必须是新的、规范绝对路径。此模式不修改 shell profile/PATH，不覆盖已有同名命令。项目、配置、密钥和状态库放在程序目录之外。Windows 安装命令见 [Windows 管理更新](windows-managed-update.md)。

## 使用与恢复

通过安装器生成的固定入口启动；不要直接改写安装登记、锁文件或 bootstrap。启动器检查未完成事务，恢复完成后才启动当前程序。其他实例仍占用运行租约时更新返回 busy，不强制结束它们。准备操作繁忙时可继续普通启动，但本次会话不提供更新交接。

更新及回滚共用一个安装记录。回滚仅恢复最近一次成功更新保留的上一版程序，不恢复数据库或配置，不降低渠道防重放序号。操作和限制见 [更新设置](update-preparation.md)、[恢复与失败处理](update-transactions.md)。

## 已知限制

尚无管理模式卸载、旧安装自动迁移或在线信任根轮换。不要使用 legacy 卸载器删除管理安装。失败安装可能保留诊断内容，不能直接当成空目录重新接管。

支持范围内已有源码及真实包自动化；可见终端交互、生产发行源与 macOS 跨重启验收分别跟踪。平台证据见 [Windows](windows-managed-update.md) 和 [Linux](linux-managed-update.md)。模拟测试不能代替真实包验证，也不能证明物理掉电耐久性或恶意同用户进程隔离。
