# gitgo

**本地运行的 Git 工作流管理工具** — 为开发者在 workspace/trial/release 三个仓库角色之间编排同步、审核和发布流程。同时提供 GUI 和命令行界面。

## 核心理念

Gitgo 不是另一个 Git GUI。它是一个 **workflow runtime** — 在 workspace（工程区）、trial（试用区）和 release（正式区）三个 Git 仓库之间建立结构化的变更通道。所有操作都可脱离 GUI，通过 CLI 完成，为 agent 和自动化预留接口。

```
workspace ──→ trial ──→ release
  (高熵)     (审核层)    (正式线)
```

## 安装

```bash
pip install -r requirements.txt
```

依赖：Python 3.12+, PySide6, Rich, Paramiko, HTTPX, Watchdog

## 启动方式

```bash
# GUI 模式（默认）
python -m gitgo

# 终端交互界面（CUI）
python -m gitgo --mode cui

# 命令行操作
python -m gitgo --mode list            # 列出所有项目
python -m gitgo --mode scan            # 扫描工作区变更
python -m gitgo --mode formalize       # 创建正式 commit
python -m gitgo --mode sync            # 同步到备份仓库
python -m gitgo --mode push            # 推送到远程
python -m gitgo --mode trial           # 查看 trial 新提交
python -m gitgo --mode status --json   # 机器可读状态
```

## 架构

```
gitgo/
├── backend/          # 纯 Python 核心 — 零 Qt 依赖
│   ├── core/         # SyncSession 状态机、operations、config
│   ├── adapters/     # 文件/Git 适配器（local, SSH）
│   ├── models/       # RepoNode、FileAccess 数据模型
│   └── remote/       # GitHub/GitLab 连接器
├── frontend/         # PySide6 GUI
│   └── workspace/    # WorkspacePanel + 各功能 Mixin
├── cui/              # 终端交互界面（Rich）
├── cli/              # 无头命令行（JSON 输出）
├── themes/           # 主题系统（QSS token 驱动）
└── tests/            # pytest 测试
```

**关键分离：** `backend/` 不包含任何 Qt import。GUI 和 CUI 共用同一个 `SyncSession` 状态机，通过工作时 `step_*()` 方法驱动。

## 三节点模型

| 节点 | 角色 | 特点 |
|---|---|---|
| **Workspace** | 工程开发区 | 高熵、频繁变更、允许多个未整理 commit |
| **Trial** | 审核暂存层 | incoming commit 在此等待人工三叉决策 |
| **Release** | 正式发布线 | 只接受 formal commit，历史保持 curated |

## 核心工作流

1. **Scan** — 对比 workspace 和 release 的文件差异
2. **Select** — 选择要同步的文件
3. **Formalize** — 将选中的 workspace commits 合并为 formal commit（带编号前缀和语义化 message）
4. **Sync** — 将文件同步到 release 仓库
5. **Push** — 推送到远程（含安全检查：敏感信息扫描）

## Trial 三叉决策

Trial 仓库的 incoming commit 提供三种处置：

- **Accept** — 接受并合并到 trial
- **Promote** — 提升为 formal commit 纳入 release
- **Discard** — 丢弃

## 开发

```bash
# 运行测试
pytest

# 查看可用模式
python -m gitgo --help

# 构建可执行文件
python build.py
```

## 路线图

| 阶段 | 目标 | 状态 |
|---|---|---|
| **Stage 1** — Runtime 抽离 | backend 完全独立于 GUI，数据结构化 | 🚧 进行中 |
| **Stage 2** — CUI/CLI 成熟 | 全 workflow 可脱离 GUI 脚本化运行 | 基础完成 |
| **Stage 3** — Agent Integration | agent 可理解 workflow，提出 formal commit 建议 | 规划中 |
| **Stage 4** — Governance Layer | AI 工程语义治理 | 远期 |
| **Stage 5** — Protocol / Ecosystem | Agent SDK，远程编排，第三方集成 | 远期 |

## 许可

Apache-2.0
