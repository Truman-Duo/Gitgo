# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 工作流背景

我的开发工作流有两个目录：

- **Workspace（工程版）** — 本地开发目录，放着很多项目。每个项目有自己的 git 仓库做本地版本管理，**从不 push**
- **Backup repo（正式版）** — 另一个文件夹，每个项目对应一个正式仓库，是真正会 commit 并 push 到 GitHub 的

sync_tool 的作用：当 workspace 里的项目开发到"差不多了"的时候，把它同步到 backup repo，生成一个规范的正式 commit，后续手动 push 到 GitHub。

## Commands

```bash
# GUI 模式（默认）
python -m sync_tool

# 终端 CUI 模式
python -m sync_tool --mode cui

# 配置管理
python -m sync_tool --mode config

# 安装依赖
pip install -r requirements.txt

# 打包为独立 exe
python build.py
```

产物：`dist/sync_tool.exe`（单文件，双击运行，无需 Python 环境）

## Architecture

### Module layout

| Module | Responsibility |
|---|---|
| `__main__.py` | CLI 入口，`--mode gui\|cui\|config` 参数分发，crash 日志 |
| `config.py` | `Config` dataclass + `ConfigManager` 读写 JSON（`sync_config.json`，搜索 exe 同目录或 `~/.vernier/`） |
| `core.py` | 核心逻辑：文件扫描（os.walk）、SHA256 对比、重命名检测、git log 解析、commit 模板生成、git add+commit 执行、`.gitignore` + `force_exclude` 规则合并 |
| `gui_main.py` | PySide6 桌面 GUI，`QThread` + `QObject` worker 模式避免界面卡顿 |
| `cui_main.py` | Rich 终端界面，交互式文件选择、commit 范围选择、系统编辑器编辑 commit message |
| `build.py` | PyInstaller 打包脚本，`--onefile --noconsole` 生成单文件 exe |

### Key design decisions

- **文件对比**：基于 SHA256 哈希，支持大文件流式读取（8MB chunk），额外通过 hash 映射检测重命名
- **排除规则**：合并 `.gitignore` + `config.force_exclude`，支持 `**/xxx`、`xxx/**`、`/xxx` 等 glob 模式
- **Commit 整合**：解析 workspace 的 git log（自上次 sync 基点之后），剥离 `[PREFIX-N]` 前缀后按 Conventional Commits 分类（feat/fix/docs/...），聚合多个本地 commit 为一个正式 commit 模板，打开系统编辑器供用户编辑确认
- **同步编号**：从 backup repo 的 git log 中正则匹配 `[PREFIX-N]` 找到最大编号后自增
- **线程模型**（GUI）：`ScanWorker` + `SyncWorker` 通过 Qt Signal/Slot 汇报进度，避免 UI 冻结
- **首次运行**：CUI 走配置向导，GUI 弹出 `QDialog` 目录选择器
- **同步基点**：成功后记录 workspace 的 HEAD hash 到 `config.sync_base`，下次只读取此 hash 之后的 commit

### Configuration (`sync_config.json`)

每个项目一份配置，放在 exe 同目录或 `~/.vernier/` 下：

```json
{
  "backup_path": "D:/Projects/MyApp",          // 正式版目录（git 仓库，最终 push 到 GitHub）
  "project_name": "MyApp",
  "commit_format": { "prefix": "MYAPP", "number_start": 0, "padding": false },
  "force_exclude": ["CLAUDE.md", ".claude/", ".git/", "__pycache__/", "*.pyc", ...],
  "sync_base": "abc123def..."                   // 上次同步时 workspace 的 HEAD hash
}
```
