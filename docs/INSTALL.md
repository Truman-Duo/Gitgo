# 安装与启动

## Windows 预览包

从 [GitHub Releases](https://github.com/Truman-Duo/Gitgo/releases) 下载 Windows x64 ZIP 和对应校验文件。校验后完整解压，再运行 `gitgo.exe`；主 EXE 需要包内的内部组件，不能单独复制。

已有预览资产与当前源码候选可能不同。Alpha 2 已构建 ZIP 并完成冻结与真实 API 验证，正式终端及发布状态见 [版本](VERSION.md)。

首次启动选择检测到的终端并保存，然后在 `/config` 添加 Provider。Windows 凭据按当前用户加密保存。之后修改终端在下次启动生效，历史仍使用相同项目数据。

Alpha 3 正在进行 Windows、macOS 与 Linux 的原生验收，尚未公开发布；通过后先由 owner 实际试用。执行前需要满足[原生沙箱配置](NATIVE_SANDBOX.md)，缺少隔离或操作员前提时会明确拒绝。建议候选包解压到新的独立目录，保留旧安装及数据备份。具体能力与限制见 [Alpha 3](ALPHA_3.md)。

Linux 受保护 Git 需要 root 拥有、普通用户不能修改的 bubblewrap 0.11.1 或更新版本，并支持描述符挂载及无特权用户命名空间。请由操作员通过可信系统管理方式准备；程序不会自行提权安装，也不会退回普通 Git 执行。CI 的临时 worker 使用校验摘要固定的官方源码构建助手，不等于替用户安装。

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

旧 Alpha 2 完整原生交互验收在 Windows Git Bash 完成。Alpha 2.1 沙箱本期使用 PowerShell，普通安装与最终可见终端未完成本期验收；旧终端结果不能替代当前包验证。其他平台和沙箱适配见 [已知限制](KNOWN_ISSUES.md)。
