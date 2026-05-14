# Gitgo Plugin API v1.0

> 基于 gitgo v0.20 | `backend/core/plugin.py` | `backend/core/plugin_loader.py`

---

## 概述

Gitgo 插件系统允许第三方扩展工作流行为，无需修改 Gitgo 核心代码。

每个插件是一个 Python 模块或包，包含一个继承 `SyncPlugin` 的类，通过 `plugin_class` 全局变量暴露。

---

## 插件发现与加载

### 搜索路径（2 级，按优先级）

| 优先级 | 路径 | 说明 |
|--------|------|------|
| 1 | `{exe}/plugins/` | 内置插件（与可执行文件同目录） |
| 2 | `~/.vernier/plugins/` | 用户全局插件 |

搜索非递归：直接子目录中的 `.py` 文件（排除 `__init__.py`）和含 `__init__.py` 的包。

### 启用控制

插件发现后**默认不启用**。必须在项目配置的 `commit_format.plugins` 列表中显式启用：

```json
{
  "commit_format": {
    "prefix": "MYAPP",
    "number_start": 1,
    "padding": false,
    "plugins": ["auto-merge", "slack-notify"]
  }
}
```

CLI 启用（一次性）：
```bash
gitgo --plugin slack-notify --mode scan --project X
```

### 加载约定

- 每个插件文件/包必须暴露全局变量 `plugin_class`，指向 `SyncPlugin` 的子类
- `plugin_class.name` 作为插件标识符，必须唯一，与 `commit_format.plugins` 中的名称匹配
- 插件抛异常时不影响主流程（`PluginOrchestrator` 捕获并记录）

---

## 8 个 Hook 参考

所有 hook 接收的数据均为 JSON 兼容的 `dict`/`list` 格式。

### 扫描阶段

#### `on_scan_complete(entries: list[dict]) -> list[dict] | None`

**调用时机：** 文件扫描对比完成后。

**参数：**
```
entries: [
  {
    "rel_path": str,        # 相对路径
    "status": str,          # "new" | "modified" | "deleted" | "same"
    "old_path": str,        # 旧路径（重命名时）
    "workspace_hash": str,  # workspace 文件 SHA256
    "backup_hash": str,     # backup 文件 SHA256
    "selected": bool        # 默认选中状态
  }
]
```

**返回值：** 替换整个 entries 列表，或 `None`（使用原始 entries）。可修改 `selected` 默认值。

---

### Commit 整合阶段

#### `on_commit_select(commits: list[dict]) -> list[int] | None`

**调用时机：** commit 选择界面打开前。

**参数：**
```
commits: [
  {
    "hash": str,      # commit SHA
    "subject": str,   # commit 标题
    "type": str,      # "feat" | "fix" | "docs" | ...
    "scope": str,     # 作用域
    "body": str       # commit body
  }
]
```

**返回值：** 建议选中的 commit 索引列表；`None` 或 `[]` 表示不干预。

#### `on_commit_message(selected: list[dict], project_config: dict) -> str | None`

**调用时机：** 生成正式 commit message 前。

**参数：**
```
selected: [CommitInfo dict, ...]    # 已选中的 commits
project_config: {                   # 完整的 ProjectConfig dict
  "name": str,
  "workspace": {...},
  "release": {...},
  ...
}
```

**返回值：** 建议的 commit message 字符串；`None` 走默认生成流程。

---

### Sync 阶段

#### `on_sync_start(entries: list[dict], message: str) -> str | None`

**调用时机：** 复制文件到 backup **之前**。

**返回值：** 非空字符串 → **中断** sync 并以该消息提示用户。`None` → 放行。

#### `on_sync_complete(result: dict) -> None`

**调用时机：** sync 完成后（不论成功或失败）。

**参数：**
```
result: {
  "success": bool,
  "commit_hash": str,
  "files_count": int
}
```

**返回值：** 无。

---

### Push 阶段

#### `on_push_start() -> str | None`

**调用时机：** push 到远程 **之前**。

**返回值：** 非空字符串 → 中断 push；`None` → 放行。

#### `on_push_complete(result: dict) -> None`

**调用时机：** push 完成后。

**参数：**
```
result: {
  "success": bool,
  "remote": str       # 远程 URL 或目标标识
}
```

**返回值：** 无。

---

### Trial / Triage 阶段

#### `on_triage_recommend(incoming_changes: list[dict], project_config: dict) -> list[dict] | None`

**调用时机：** 展示 trial incoming changes 前。

**参数：**
```
incoming_changes: [
  {
    "index": int,
    "hash": str,
    "message": str,
    "author": str,
    "date": str,       # ISO date
    "body": str
  }
]
project_config: dict   # 完整的 ProjectConfig dict
```

**返回值：**
```json
[
  {"index": 0, "action": "accept", "reason": "安全补丁，高优先级"},
  {"index": 1, "action": "discard", "reason": "实验性提交，暂不合并"}
]
```

`None` → 不干预，由人决策。

---

## 开发约定

1. **数据类型**：所有 hook 传参和返回值为 JSON 兼容类型（`dict`/`list`/`str`/`int`/`float`/`bool`/`None`），不使用 dataclass 实例
2. **不阻塞**：hook 执行时间应 < 100ms。如需耗时操作（如网络请求），使用后台线程
3. **异常安全**：插件内未捕获异常会被 `PluginOrchestrator` 捕获，记录到日志，不中断主流程
4. **纯 Python**：不引入编译依赖，保证跨平台可用
5. **版本声明**：`name` 和 `version` 为必需类属性，`name` 作为唯一标识符

---

## 参考插件

| 插件 | 文件 | 覆盖 Hook | 说明 |
|------|------|----------|------|
| auto-merge | `plugins/auto_merge.py` | `on_commit_select` | 按连续类型分组，推荐每组第一个 |
| slack-notify | `plugins/slack_notify.py` | `on_sync_complete`, `on_push_complete` | Sync/Push 完成后发通知 |
| jira-link | `plugins/jira_link.py` | `on_commit_select` | 从 commit message 提取 Jira key，推荐同 key 合并 |

---

## 插件模板

```python
"""my_plugin.py — 自定插件描述"""
from backend.core.plugin import SyncPlugin


class MyPlugin(SyncPlugin):
    name = "my-plugin"
    version = "0.1.0"

    def on_scan_complete(self, entries: list[dict]) -> list[dict] | None:
        # 自定义逻辑
        return None


plugin_class = MyPlugin
```
