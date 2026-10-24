# Gitgo 版本

## Alpha 3 基线（2026-10-24）

产品、Dashboard 与 Native Host：`1.0.0-alpha.3`；Windows 文件版本：`1.0.0.4`。独立分支 `release/alpha3`，发布标签 `v1.0.0-alpha.3`，属于 preview 渠道。owner 已确认正式终端试用版本为本次基线；所有必需 CI 通过后发布。[Release](https://github.com/Truman-Duo/Gitgo/releases/tag/v1.0.0-alpha.3) 的验证摘要和包内 `build-manifest.json` 记录确切源码身份及校验值。

本期有机整合沙箱、管理更新和历史缩放，并修复状态一致性、主子及子子协作、问题转交、Grill、侧讨论、统计和终端交互。Windows 更新固定发行公钥；自动检查、下载和干净退出时安装沿用同一更新事务。范围、平台限制、基线与回退见 [Alpha 3](ALPHA_3.md)。下列 Alpha 2.1 与 Alpha 2 条目保留各自历史证据。

## Alpha 2.1 已发布预览（2026-10-22）

产品、Dashboard 与 Native Host：`1.0.0-alpha.2.1`；Windows 文件版本：`1.0.0.3`。独立分支 `release/alpha2.1`，发布标签 `v1.0.0-alpha.2.1`。本期包含沙箱与知识、提示词、工作台及任务状态整合；`managed-update-v1`、`history-resize-v1` 留给完整 Alpha 3。

- 构建源提交：`55d6800067830fae7e963418d1a306f5c71759ca`，工作树干净；发行后的文档记录不改变该标签或包内容。
- ZIP：`gitgo-1.0.0-alpha.2.1-win-x64-55d68000.zip`，包内 387 项清单核对通过。
- SHA256：`4b1874b4a5014026a86d005a9e1dbfe38146defdf242bb7bf503d58e88ff00cb`；资产上传后重新下载核对字节，通过。
- 发布：[Alpha 2.1 Release](https://github.com/Truman-Duo/Gitgo/releases/tag/v1.0.0-alpha.2.1)，包含 ZIP、校验文件与验证摘要；没有 Installer，不覆盖此前 Terminal Preview。

普通安装、特权存储、Linux/macOS 实际运行和最终可见终端仍未完成本期验收。范围与接续要求见 [Alpha 2.1](ALPHA_2_1.md)，测试口径见 [验证](VALIDATION.md)。

下文为 2026-10-11 Alpha 2 历史记录；其中“当前”“尚未发布”均仅指当时版本。

记录日期：2026-10-11。Gitgo 分别记录源码候选、组件版本、通信协议、数据库迁移和发布资产。

## Alpha 2 源码候选

| 标识 | 当前值 | 含义 |
|---|---|---|
| Dashboard package | `1.0.0-alpha.2` | 前端包声明版本 |
| Native Host | `1.0.0-alpha.2` | 原生宿主声明版本 |
| 原生协议 | `1` | Dashboard 与 Native Host 的通信协议 |
| 关系状态 migration | 最高 `13` | 当前源码的状态数据库迁移序列 |
| 可观测状态 migration | 最高 `2` | 当前源码的可观测数据库迁移序列 |
| 集成状态 | `feat/alpha2` 功能分支 | 已包含上游 CI 基线；PR 与发布流程分别跟踪 |
| Alpha 2 ZIP | 已构建，主流程验收通过 | 冻结运行时、连续性、真实 API 及人工终端主流程通过；尚未发布 |

组件版本、协议与 migration 各自表达不同兼容边界。数据库迁移按实现中的追加序列执行；组件声明版本不能单独确定某个构建包含哪些功能。

候选包含 Provider 故障转移、需求问题与答案采用、权限及恢复、状态与事件、交付证据、工具/Shell 改进和终端交互修复。详细范围见 [当前功能](IMPLEMENTATION_STATUS.md)。

已有验证包含后端 1395 passed / 2 skipped、Dashboard 173 passed / 0 failed、类型检查、真实 API 及正式 Git Bash 人工验收。各项覆盖与未通过案例见 [测试结果](VALIDATION.md)。源码回归与冻结产物分别验收；正式终端视觉结果单独记录。

## Alpha 2 构建产物

- 产品：`1.0.0-alpha.2`；Windows 文件版本：`1.0.0.2`。
- 构建源码：`56e6b1039e7b1fe6fac6be5549fe3201b8d5958b`；工作树干净。
- ZIP：`gitgo-1.0.0-alpha.2-win-x64-56e6b103.zip`。
- SHA256：`7ad6fa8812e504c76cfa236ce7d280ef82370614a626b0b6585bf42484de706a`。
- 包内 `build-manifest.json` 记录 commit/tree 与 174 个文件的摘要；解压后逐文件核对通过。
- `gitgo.exe --version` 显示产品版本，两个 EXE 的 Windows 文件版本一致。
- 该 ZIP 正式终端主流程已由用户确认，扩展 Trace 另行核对；尚未生成 Installer，构建机缺少 Inno Setup。
- 集成入口：[Alpha 2 PR](https://github.com/Truman-Duo/Gitgo/pull/28)。源码合入与公开 Release 分别确认。

## 已发布预览

Windows Terminal Preview 的公开入口为 [terminal-preview-2026-09-28](https://github.com/Truman-Duo/Gitgo/releases/tag/terminal-preview-2026-09-28)。下载时核对发布资产及对应校验文件，以该构建的版本说明判断能力范围。

Alpha 2 使用产品版本 `1.0.0-alpha.2`，Windows 文件版本为 `1.0.0.2`，协议和数据库迁移编号保持各自兼容边界。当前候选与已有预览资产分别维护；正式发布以 PR、CI 和产物验收结果为准。

## 后续版本记录

源码合入后记录实际提交 SHA；生成安装包后记录标签、构建来源和校验信息；完成安装与正式终端验证后更新对应结果。功能实现、主线集成与安装包发布分别标记，保持记录可追溯。

[安装](INSTALL.md) · [已知限制](KNOWN_ISSUES.md) · [贡献与发布要求](../CONTRIBUTING.md)
