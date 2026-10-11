# 安装与启动

## Windows 预览包

从 [GitHub Releases](https://github.com/Truman-Duo/Gitgo/releases) 下载 Windows x64 ZIP 和对应校验文件。校验后完整解压，再运行 `gitgo.exe`；主 EXE 需要包内的内部组件，不能单独复制。

已有预览资产与当前源码候选可能不同。当前候选没有为本次全部修改重新打包或安装，见 [版本](VERSION.md)。

首次启动选择检测到的终端并保存，然后在 `/config` 添加 Provider。Windows 凭据按当前用户加密保存。之后修改终端在下次启动生效，历史仍使用相同项目数据。

## 源码启动

在仓库根目录运行：

```text
run_dashboard_native.bat
```

入口使用 Gitgo Python、Bun 和完整 Dashboard。正式终端需要 ANSI 颜色；`NO_COLOR=1` 或 `TERM=dumb` 会影响外观。浏览器、简化 renderer 和隐藏 PTY 不能替代可见终端验收。

环境和验证要求见 [贡献指南](../CONTRIBUTING.md)。Windows 构建与可选安装器见 [打包说明](../packaging/README.md)。

## 退出与卸载

正常退出保留项目历史。卸载器负责产品及用户配置/凭据，不应遍历或删除用户仓库、项目文件、worktree 或运行历史。

## 平台范围

本次完整原生交互验收在 Windows Git Bash 完成，不表示所有 Windows 终端尺寸、旧系统或 macOS/Linux 均已通过。其他平台和沙箱适配见 [已知限制](KNOWN_ISSUES.md)。
