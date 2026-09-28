# 已知限制

以下内容属于首个终端预览版的已知边界，不应被描述为已经完成：

- 多 Provider 自动 failover 与 circuit breaker 尚未达到生产级；Provider 可以配置、探测和手动切换。
- macOS/Linux 的安装包、系统密钥适配器和安装器尚未发布。
- 自动更新暂不提供。
- 云端/远程调度不属于本版范围。
- Windows prerelease 已提供完整 staging ZIP；Installer 仍只有在构建机已有 Inno Setup 时生成。二进制作为 Release asset 发布，不提交进 Git history。
- 自定义工具、知识收割与长循环虽已有结构化存储和测试，仍需要更多真实 Provider/复杂项目回归。
- Provider 原生联网能力取决于模型、账号和服务配置；不可用时可配置外部检索后备。能力差异应作为可恢复配置提示，不应伪装成模型错误。
- 正式 UI 验收必须在真实彩色终端中进行；组件测试、浏览器或 mock renderer 不能替代终端交互验收。

问题报告应包含可公开的错误码、版本、复现步骤和脱敏环境信息；不要附带 API key、数据库、原始 Trace 或项目私有内容。

当前路线图和公开缺陷见 [GitHub Issues](https://github.com/Truman-Duo/Gitgo/issues)。
