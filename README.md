# sync_tool — 工作区 ↔ 备份仓库同步工具

通用 git 工作区同步工具，将开发工作区的变更整合后同步到发布用备份仓库。

## 功能

- **文件智能对比**: 自动对比工作区与备份仓库，区分新增/修改/相同/重命名
- **Commit 整合**: 读取工作区 git 日志，多 commit 合并为一个正式 commit
- **双界面**: 默认 Windows 桌面 GUI（PySide6），支持切换到终端 CUI（rich）
- **可配置**: 所有项目特化配置（commit 格式、排除规则）外部化，任意项目可复用

## 使用

### 直接运行（开发期）

```bash
# GUI 模式（默认）
python -m scripts.sync_tool

# 终端 CUI 模式
python -m scripts.sync_tool --mode cui

# 查看/修改配置
python -m scripts.sync_tool --mode config
```

### 打包为独立 exe

```bash
cd scripts/sync_tool
pip install -r requirements.txt
python build.py
```

产物: `dist/sync_tool.exe`（单文件，无 Python 环境也可运行）

## 配置

配置文件搜索顺序:
1. `.exe` 同目录下的 `sync_config.json`
2. `%USERPROFILE%\.vernier\sync_config.json`

首次运行会自动进入配置向导，设置备份仓库路径等信息。

## 独立部署

本工具当前位于 `scripts/sync_tool/`，但逻辑上完全独立于宿主项目。
如需搬走成为独立项目，复制整个目录到新仓库即可:

```bash
cp -r scripts/sync_tool/ /path/to/new-repo/
cd /path/to/new-repo
pip install -r requirements.txt
python build.py
```

修改 `sync_config.json` 中的 `commit_format.prefix` 等字段适配新项目。
