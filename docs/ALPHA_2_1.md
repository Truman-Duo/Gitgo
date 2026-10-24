# Alpha 2.1 发布与 Alpha 3 接续

记录日期：2026-10-22。产品版本 `1.0.0-alpha.2.1`，独立发布分支 `release/alpha2.1`，标签 `v1.0.0-alpha.2.1`。本期承接 Alpha 2 及已完成的知识、提示词、工作台、任务状态与沙箱整合；完整 Alpha 3 后续集成另行验证。

## 本期范围

- 经验收集、分类、确认、召回与动作前提示；建议召回允许明确记录失败后的有限降级，必需检查失败或完整性未知时不提交。
- 提示词分层、实例切换与工具接口提示；版本化稿纸及计算能力与知识收割入口衔接。稿纸是任务工作数据，不新增记忆层级。
- 文件、文档、命令、注册测试与自定义工具使用候选、检查及精确提交；子进程工作树和晋升复用相同文件与状态边界。
- Windows PowerShell 原生隔离、生命周期及回收证据；Git 本地操作与标准远程传输使用受控执行边界，仓库级可执行 hook、自定义传输和 SSH/凭据命令不被当作普通仓库配置直接执行。
- 项目生命周期、运行信息、上下文、逐轮统计、历史与工具展示等现有 Dashboard 改进。

## 验证与限制

本期已发布 [Windows x64 ZIP](https://github.com/Truman-Duo/Gitgo/releases/tag/v1.0.0-alpha.2.1)，构建源提交 `55d6800067830fae7e963418d1a306f5c71759ca`、工作树干净。后端全量单次 3542 passed / 456 skipped，Dashboard 221 passed；本次冻结 Host 原生组另为 16 passed / 1 skipped，冒烟、连续性、387 项清单与上传后下载校验通过。此前真实 API 覆盖候选写入、读取、稿纸计算与冷恢复，本次包未重跑付费 API；源码、旧候选和新包证据分别报告，详见[验证](VALIDATION.md)。

普通安装、特权原生存储、Linux/macOS 实际运行和最终可见终端尚未完成本期验收。Windows 本期使用 PowerShell；Git Bash 隔离支持另行跟踪。跳过项不算通过，经验的长期收益也尚无充分对照证明。配置或必需隔离前提缺失时明确失败，不退回无隔离执行。具体平台前提见[原生沙箱](NATIVE_SANDBOX.md)。

## 留给完整 Alpha 3 的集成

下表是本次核对的远端快照，后续合并前需重新获取实际 head。

| 分支 | 核对的 head | 目的与接续注意事项 |
|---|---|---|
| `feat/managed-update-v1` | `a8e24d922310b7a97120893880fdeeb5547b8fb9` | 签名更新、管理安装、回滚与清理；协调安装租约、进程回收、Native Host/服务入口及构建兼容信息，继续使用已有授权与状态权威 |
| `feat/history-resize-v1` | `5c87d07836db10dea42f98078aa88667013dc681` | 缩放阅读锚点及跨页面选中项可见性；核对 History/ScrollBox、Chat/Diff、Runtime 与现有 Dashboard 改动，使用正式终端复测连续缩放与滚动 |

两条分支均从当前共同上游派生，尚未进入本期。Alpha 2.1 保留已有 Alpha 2 与沙箱提交关系，不将未来分支的能力提前列为本期完成。 后续请从 `release/alpha2.1` 接续，其已发布功能提交为 `55d6800067830fae7e963418d1a306f5c71759ca`；之后的文档提交不改变包内容。

分别以本发布提交做三方合并预检：管理更新有 8 个文本冲突，涉及 `application/services.py`、`process_control.py`、`storage/runtime.py`、`tools/document_tools.py`、`tools/workspace_search.py`、前端 `backend/client.test.ts`、`components/overlays.tsx` 和 `packaging/build_windows.ps1`；历史缩放有 3 个，涉及 `Overview.tsx`、`ProcessList.tsx`、`RuntimeContextPanel.tsx`。本期未实际合并；后续须同时核对自动合并的重叠代码、职责与生命周期，并运行组合测试，不能把解决文本冲突视为功能验收。

必要关联工作发现与 Grill 对齐仍为后续 P0；完成检查已有硬性证据要求，尚不能保证模型找全用户想法涉及的所有联动修改。

产品 SemVer、Windows 文件版本与数据库迁移各有职责。Windows `1.0.0.3` 是本期文件版本序列，后续发布必须提高该数值，不能根据 Alpha 3 名称重新使用相同或更低的文件版本。完整 Alpha 3 的具体产品版本、安装兼容及发布验收由后续集成确定。

[版本](VERSION.md) · [测试结果](VALIDATION.md) · [已知限制](KNOWN_ISSUES.md)

后续两条功能分支的源码融合记录见 [Alpha3 集成候选](ALPHA_3_INTEGRATION.md)；该记录不改变上述 Alpha2.1 已发布包的范围或验收结果。
