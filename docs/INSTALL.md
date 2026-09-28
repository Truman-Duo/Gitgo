# 安装与启动

## Windows Terminal Preview

从 [GitHub Releases](https://github.com/Truman-Duo/Gitgo/releases) 下载 Windows x64 ZIP 和同名 `.sha256` 文件。校验 SHA-256 后完整解压，运行目录中的 `gitgo.exe`。

当前发布物是 prerelease staging，不是安装器。`gitgo.exe` 需要包内 `internal/gitgo-host/`，不要只复制主 EXE。首次使用时在 `/config` 中添加 Provider；API key 由 Windows 当前用户 DPAPI 加密保存。

## Windows 源码运行

在仓库根目录执行 `run_dashboard_native.bat`。该入口会启动正式 Bun/Ink Dashboard 与内部 Native Host。

不要用以下内容代替产品验收：

- 浏览器 smoke 页面；
- `liveTestServer`；
- trace-only renderer；
- mock Dashboard；
- 隐藏 PTY。

正式终端需要支持 ANSI 颜色。`NO_COLOR=1`、`TERM=dumb` 等测试环境可能禁用颜色，这不代表正式主题失效。

## Windows 发布构建

发布脚本位于 `packaging/build_windows.ps1`。构建脚本会：

1. 选择 Gitgo 专用或显式指定的 Python；
2. 拒绝未通过 SQLite WAL 安全检查的运行时；
3. 编译 Dashboard 可执行文件；
4. 编译内部 Native Host；
5. 通过版本化 stdio 协议执行 Host smoke test；
6. 将 staging 结果放到被 Git 忽略的 `dist-terminal/`。

只有本机存在 Inno Setup 编译器并显式请求时，才生成安装包。构建过程不会自动安装打包工具，也不会把产物加入 Git。

贡献者优先使用统一入口：

```text
packaging/release_windows.ps1 -VerifyOnly
packaging/release_windows.ps1
packaging/release_windows.ps1 -BuildInstaller
```

分别对应完整验证、验证后生成 staging、验证后生成 staging 与 Installer。详细约束见 [贡献指南](../CONTRIBUTING.md) 和 `packaging/README.md`。

安装后的主命令为 `gitgo`。安装器会为当前用户配置 PATH/App Paths。双击启动时可按用户配置选择 Windows Terminal、当前控制台或自定义终端；SSH/已附着终端始终复用当前 TTY。

## 卸载边界

卸载器可以删除：

- Gitgo 安装目录；
- 用户配置；
- 用户加密凭据。

卸载器不会删除：

- 用户项目和 Git 仓库；
- 独立 worktree；
- 项目 `.gitgo` 元数据；
- 运行时/session 数据库。

## 其他平台

协议和配置层不依赖 Windows 安装器，但本版没有发布 macOS/Linux 安装包。未来平台包应继续使用同一 Host 协议，并实现对应系统的密钥存储与终端启动适配器。
