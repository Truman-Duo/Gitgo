# sync_tool — 工作区 ↔ 备份仓库同步工具

通用 git 工作区同步工具，将开发工作区的变更整合后同步到发布用备份仓库。

## 工作流

- **Workspace（工程版）** — 本地开发目录，每个项目有自己的 git（从不 push）
- **Backup repo（正式版）** — 另一个文件夹，是真正会 push 到 GitHub 的

sync_tool 的作用：当 workspace 里的项目开发到差不多了的时候，把它同步到 backup repo，生成规范的正式 commit，后续手动 push 到 GitHub。

## 功能

- **多项目管理** — 同时管理多个项目的工程版↔正式版配对
- **文件智能对比** — SHA256 哈希对比，区分新增/修改/相同/重命名
- **Box 选择合并** — 可视化卡片选择 workspace commit，手动合并为正式 commit
- **Sync/Push 分离** — 同步到备份仓库和推送到 GitHub 分为两步
- **双界面** — 默认 Windows 桌面 GUI（PySide6），支持切换到终端 CUI（rich）

## 使用

### 直接运行（开发期）

```bash
# GUI 模式（默认）
python -m sync_tool

# 终端 CUI 模式
python -m sync_tool --mode cui

# 查看项目配置
python -m sync_tool --mode config
```

### 打包为独立 exe

```bash
pip install -r requirements.txt
python build.py
```

产物: `dist/sync_tool.exe`（单文件，双击运行，无需 Python 环境）

## 配置

配置文件 `sync_config.json`（搜索 exe 同目录或 `~/.vernier/`）。

多项目格式：

```json
{
  "projects": [
    {
      "name": "MyApp",
      "workspace_path": "D:/Workspace/MyApp",
      "backup_path": "D:/Backup/MyApp",
      "commit_format": { "prefix": "MYAPP", "number_start": 0, "padding": false },
      "force_exclude": ["CLAUDE.md", ".git/", "__pycache__/", "*.pyc"],
      "sync_base": "abc123def..."
    }
  ]
}
```

旧格式（单项目顶层字段）会自动迁移。

## 版本

详见 `VERSION.md`。
