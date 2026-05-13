# gitgo — 工作区 ↔ 备份仓库同步工具

通用 git 工作区同步工具，将开发工作区的变更整合后同步到发布用备份仓库。

## 工作流

- **工作区(workspace node)** — 本地开发目录，每个项目有自己的 git（从不 push）
- **发布备份区(release backup node)** — 另一个文件夹，是真正会 push 到 GitHub 的
- **试验区(trial node)** — 第三方只读仓库，三叉决策处理其新 commit

## 功能

- **多项目管理** — 同时管理多个项目的配对，Last sync 列 + 30s 定时刷新
- **文件智能对比** — SHA256 哈希对比，区分新增/修改/相同/重命名
- **Box 选择合并** — 可视化卡片选择 workspace commit，手动合并为正式 commit
- **Action Bar** — 每个 Tab 独立操作按钮（Undo/Save/Export/Refresh + 快捷键）
- **Sync/Push 分离** — 同步到备份仓库和推送到 GitHub 分为两步
- **Push 安全检查** — 推送前自动扫描敏感信息（密钥/密码/token），命中则告警确认
- **Trial 三叉工作流** — accept（cherry-pick 到 release）/ promote（fetch 到 workspace）/ discard
- **SSH 远程支持** — 通过 SSH 管理远程仓库
- **双界面** — 默认 Windows 桌面 GUI（PySide6），支持切换到终端 CUI（rich）
- **主题系统** — 浅色/深色/跟随系统，QSS 动态插值实时刷新
- **国际化** — 中文/English 界面，SettingsDialog 切换
- **键盘快捷键** — Ctrl+Shift+S/M/S/P、Ctrl+Return、Escape

## 使用

### 直接运行（开发期）

```bash
# GUI 模式（默认）
python -m gitgo

# 终端 CUI 模式
python -m gitgo --mode cui

# 查看项目配置
python -m gitgo --mode config
```

### 打包为独立 exe（两阶段）

```bash
pip install -r requirements.txt

# 1. 调试版（带控制台，可见 stderr 输出）
python build.py --debug

# 2. 双击测试 gitgo_debug.exe，确认功能正常

# 3. 正式版（无控制台）
python build.py
```

产物: `dist/gitgo.exe`（单文件，双击运行，无需 Python 环境）
调试产物: `dist/gitgo_debug.exe`（带控制台窗口）

## 配置

配置文件 `sync_config.json`（搜索 exe 同目录或 `~/.vernier/`）。

多项目格式：

```json
{
  "projects": [
    {
      "name": "MyProject",
      "workspace": { "file_access": { "kind": "local", "path": "D:/Workspace/MyProject" } },
      "release": { "file_access": { "kind": "local", "path": "D:/Backup/MyProject" } },
      "trial": { "file_access": { "kind": "local", "path": "D:/Trial/MyProject" } },
      "commit_format": { "prefix": "MYAPP", "number_start": 0, "padding": false },
      "force_exclude": ["CLAUDE.md", ".git/", "__pycache__/", "*.pyc"]
    }
  ]
}
```

旧格式（单项目顶层字段）会自动迁移。

## 架构

```
三角色 × RepoNode（file_access）→ 统一同步框架
```

- **三角色**：工作区(workspace node)、发布备份区(release backup node)、试验区(trial node)
- **Trial 三叉**：accept → release / promote → workspace / discard
- **适配器体系**：FileAdapter + GitRunner 支持本地/SSH
- **插件系统**：Hook 模式，第三方可挂载自定义逻辑
- **共享状态机**：SyncSession 驱动 GUI / CUI / Daemon
- **Mixin 组合**：WorkspacePanel 聚合 7 个 Mixin，职责单一分离
- **动态 QSS**：`_build_qss(t)` 运行时插值，主题切换即时刷新
- **两阶段构建**：调试版 → 测试 → 正式版

## 版本

详见 `VERSION.md`。最新版本 v0.6。
