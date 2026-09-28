# Security Policy

## 报告安全问题

请不要用公开 Issue 报告可利用漏洞、真实凭据、项目数据泄漏或能够绕过权限/隐私边界的细节。优先使用 GitHub 的 Private Vulnerability Reporting；如果仓库尚未启用该入口，请先通过仓库维护者的私有渠道联系，再提供复现材料。

报告中请包含：

- 受影响版本或 commit；
- 最小化复现步骤；
- 影响范围和前置条件；
- 已脱敏的错误码、日志或截图；
- 建议的缓解方式（如有）。

不要提交 API key、密码、OAuth token、完整 SQLite/WAL、原始 Trace、个人路径或第三方项目源码。

## 当前支持范围

当前公开版本是 Windows Terminal Preview。安全修复优先覆盖最新 prerelease 分支；早期 Qt 产品和本地 legacy 归档不再作为受支持产品。

## 安全边界

Gitgo 将 Provider 凭据、项目状态、发布隐私扫描、工具权限和进程隔离作为不同边界处理。用户授权可以覆盖相应作用域内的普通治理软门，但不能伪造执行收据、改变工具版本/参数摘要、越过路径边界或把未知副作用声明为已回滚。

公开设计说明见 [安全与隐私](docs/SECURITY_AND_PRIVACY.md)。
