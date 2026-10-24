# Windows 一键打包

在源码根目录双击 `build_windows.bat`，或在 PowerShell 中运行 `.\build_windows.bat`。它沿用原有 Windows 构建器，生成 Alpha3 管理安装 ZIP、SHA256 和文件清单，内置正式公开更新策略，并运行冻结 Host/Daemon/工具冒烟及会话连续性检查。输出显示在控制台，默认位于 `dev-artifacts/windows-packaging/` 下的独立目录。

日常打包使用显式的 `-BuildOnly`，不重复运行整套源码测试，也不代表正式发行验收。它不发布、安装或覆盖现有 Release。正式源码验收及签名准备仍使用同一个入口的完整模式。

## 本机工具路径

首次把本机路径写入已忽略的 `dev-artifacts/release-settings.local.json`；显式命令参数优先。仅保存路径，不保存私钥内容或 Provider 凭据。

```json
{
  "schema_version": 1,
  "Python": "C:/tools/python/python.exe",
  "PyInstallerPackages": "dev-artifacts/build-packages",
  "Ripgrep": "C:/tools/ripgrep/rg.exe",
  "RipgrepNotices": "C:/tools/ripgrep/ripgrep-notices.txt"
}
```

Python 必须通过现有 SQLite 自检，并具备匹配的构建依赖；构建依赖使用 `packaging/requirements-build.lock`，Dashboard 依赖使用冻结的 Bun lock。Bun 默认取用户目录的 `.bun/bun.exe`，也可配置 `Bun`。脚本创建私有的开发临时目录并恢复原环境；可用 `TemporaryRoot` 指定已知的开发目录。材料不写入项目的 `.gitgo`。

未提交的本地试验可显式加 `-AllowDirty`，产物记录 dirty 状态，不能签名。需要改变输出位置时使用 `-Output` 和 `-ArtifactOutput`；已有产物不会被静默覆盖。

## 完整验收与签名准备

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File packaging/release_windows.ps1 -ManagedUpdate
```

完整模式先执行源码与 Dashboard 测试，再构建并验证实际冻结包的原生边界。`-VerifyOnly` 只做源码验收，不构建安装包。

正式签名准备再显式加 `-Sign -SigningKey <仓库外的私钥文件> -KeyId <已固定的公钥ID> -Channel preview -Sequence <渠道序号>`。私钥不进入源码或包；签名不能与快速打包、只验证、dirty 构建或普通安装混用。本地设置文件不能开启这些模式。

签名文件位于归档目录的 `signed-release/`；工具只准备本地文件。正式发布还要核对同一候选的 CI、版本与渠道、实际 API/终端验收及上传后下载结果。已经发布的同版本归档保持不变；续签沿用原归档并递增渠道序号，不重新打包覆盖。参见[更新验证](update-discovery.md)。
