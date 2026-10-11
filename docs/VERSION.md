# Gitgo 版本

记录日期：2026-10-11。Gitgo 分别记录源码候选、组件版本、通信协议、数据库迁移和发布资产。

## Alpha 2 源码候选

| 标识 | 当前值 | 含义 |
|---|---|---|
| Dashboard package | `1.0.0-alpha.2` | 前端包声明版本 |
| Native Host | `1.0.0-alpha.2` | 原生宿主声明版本 |
| 原生协议 | `1` | Dashboard 与 Native Host 的通信协议 |
| 关系状态 migration | 最高 `13` | 当前源码的状态数据库迁移序列 |
| 可观测状态 migration | 最高 `2` | 当前源码的可观测数据库迁移序列 |
| 集成状态 | `feat/alpha2` 功能分支 | 已包含上游 CI 基线；PR 与发布验收进行中 |
| 最新安装包 | 待构建与安装验收 | 当前源码全部改动尚未形成已验收的新包 |

组件版本、协议与 migration 各自表达不同兼容边界。数据库迁移按实现中的追加序列执行；组件声明版本不能单独确定某个构建包含哪些功能。

候选包含 Provider 故障转移、需求问题与答案采用、权限及恢复、状态与事件、交付证据、工具/Shell 改进和终端交互修复。详细范围见 [当前功能](IMPLEMENTATION_STATUS.md)。

已有验证包含后端 1382 passed / 2 skipped、Dashboard 173 passed / 0 failed、类型检查、真实 API 及正式 Git Bash 人工验收。各项覆盖与未通过案例见 [测试结果](VALIDATION.md)。这些记录对应源码候选，安装包另行验收。

## 已发布预览

Windows Terminal Preview 的公开入口为 [terminal-preview-2026-09-28](https://github.com/Truman-Duo/Gitgo/releases/tag/terminal-preview-2026-09-28)。下载时核对发布资产及对应校验文件，以该构建的版本说明判断能力范围。

Alpha 2 使用产品版本 `1.0.0-alpha.2`，Windows 文件版本为 `1.0.0.2`，协议和数据库迁移编号保持各自兼容边界。当前候选与已有预览资产分别维护；正式发布以 PR、CI 和产物验收结果为准。

## 后续版本记录

源码合入后记录实际提交 SHA；生成安装包后记录标签、构建来源和校验信息；完成安装与正式终端验证后更新对应结果。功能实现、主线集成与安装包发布分别标记，保持记录可追溯。

[安装](INSTALL.md) · [已知限制](KNOWN_ISSUES.md) · [贡献与发布要求](../CONTRIBUTING.md)
