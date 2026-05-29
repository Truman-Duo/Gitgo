# Gitgo Identity Guard — 项目环境完整性保护

> 设计日期：2026-05-16 | 基于 v0.21 源码 | 事故驱动设计

---

## 一、问题

CC 覆盖了一个项目文件夹，因为它不知道"这个文件夹的身份是什么"。
对它来说，`D:/Projects/MyApp` 和空目录没有区别——都是文件系统路径。

Gitgo 已经在管 git 状态。但项目除了代码，还有一层**身份性文件**：

```
project/
├── .git/              ← gitgo 管了
├── .claude/           ← CC 记忆、项目关联 —— 不在保护范围
├── .codex/            ← Codex 记忆 —— 不在保护范围
├── .codebuddy/        ← 其他 AI 工具记忆
├── CLAUDE.md          ← 项目手册 —— 不在保护范围
├── .gitignore
├── pyproject.toml     ← 项目配置
├── sync_config.json   ← gitgo 自己的配置
└── src/               ← 代码
```

一次事故暴露的是 LLM 的内生性问题：**LLM 不具备"这个实体有不可侵犯的身份"的直觉。**
它看到路径，看不到身份。这不是 prompt engineering 能解决的——需要在 runtime 层面加约束。

---

## 二、Gitgo 已有的能力可以复用

| 已有能力 | 位置 | 可用于 |
|---------|------|--------|
| SHA256 文件对比 | `compare_files()` / ops/scan.py:41 | 检测全量覆盖 |
| 排除规则引擎 | `get_exclude_patterns()` | 定义身份文件列表 |
| Sync 流程 | `step_sync()` / sync_session.py:692 | 记忆快照时机 |
| State Bundle | `collect_state_bundle()` / governance/state_bundle.py:13 | 身份包导出 |
| Operation History | `HistoryManager.add_operation()` | 完整性告警记录 |

---

## 三、架构：三层防御

```
┌─────────────────────────────────────────────┐
│             Identity Guard                   │
│                                               │
│  Layer 1: Integrity Detection (scan 阶段)     │
│  ┌─────────────────────────────────────────┐ │
│  │ _detect_mass_override(entries)           │ │
│  │   → 80%+ files changed → WARNING         │ │
│  │ _detect_identity_file_deletion(entries)   │ │
│  │   → .claude/ CLAUDE.md missing → ALERT   │ │
│  │ _detect_structure_collapse(files)         │ │
│  │   → 目录骨架突变 → WARNING               │ │
│  └─────────────────────────────────────────┘ │
│                                               │
│  Layer 2: Memory Snapshot (sync 阶段)         │
│  ┌─────────────────────────────────────────┐ │
│  │ _snapshot_tool_memories(ws, bk)          │ │
│  │   → .claude/ → .gitgo/memories/          │ │
│  │   → .codex/  → .gitgo/memories/          │ │
│  └─────────────────────────────────────────┘ │
│                                               │
│  Layer 3: Identity Bundle (export 阶段)       │
│  ┌─────────────────────────────────────────┐ │
│  │ collect_identity_bundle(session)          │ │
│  │   → project_structure + tool_memories    │ │
│  │   → integrity_checks + warnings          │ │
│  └─────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

---

## 四、Layer 1: Integrity Detection

### 4.1 插入位置

`step_scan()`（sync_session.py:407）的最后，在 `compare_files()` 返回 entries 之后、
写入 history 之前。新增：

```python
# sync_session.py — step_scan() 末尾新增

entries = compare_files(...)
entries = self.on_file_selection(entries)

# ── Integrity Detection ──
warnings = _run_integrity_checks(entries, self.workspace_path, self.project)
for w in warnings:
    self.on_log(f"[INTEGRITY] {w['level'].upper()}: {w['message']}")
    HistoryManager.add_operation(
        self.project.name, "integrity_warning", "warning",
        w, correlation_id=self._correlation_id,
    )
```

### 4.2 三条检测规则

```python
# backend/core/identity/guard.py — 新文件

import os
from pathlib import Path
from backend.core.operations.models import FileEntry

# 身份性文件清单（可配置）
IDENTITY_FILES = [
    "CLAUDE.md", ".claude/", ".codex/", ".codebuddy/",
    ".gitignore", "pyproject.toml", "Cargo.toml", "package.json",
    "gitgo_config.json", "sync_config.json",
]

# 身份性目录（含子文件）
IDENTITY_DIRS = [".claude", ".codex", ".codebuddy"]

def _run_integrity_checks(
    entries: list[FileEntry],
    workspace_path: str | Path,
    project,  # ProjectConfig
) -> list[dict]:
    """运行全部完整性检测，返回警告列表。"""
    warnings = []
    ws = Path(workspace_path)

    # Rule 1: 全量覆盖检测
    result = _detect_mass_override(entries)
    if result:
        result["rule"] = "mass_override"
        warnings.append(result)

    # Rule 2: 身份文件删除检测
    result = _detect_identity_file_deletion(entries, ws)
    if result:
        result["rule"] = "identity_file_deleted"
        warnings.append(result)

    # Rule 3: 目录结构突变
    result = _detect_structure_collapse(entries, ws)
    if result:
        result["rule"] = "structure_collapse"
        warnings.append(result)

    return warnings


def _detect_mass_override(entries: list[FileEntry]) -> dict | None:
    """检测全量覆盖：entries 中 'new' 或 'modified' 的占比超过阈值。

    正常开发：5-30% 的文件变更。全量覆盖：80%+ 的文件变更。
    """
    if not entries:
        return None
    changed = sum(1 for e in entries if e.status in ("new", "modified"))
    ratio = changed / len(entries)
    threshold = getattr(getattr(getattr(
        entries, '__class__', None), '__module__', None), 'IGNORE', 0.80
    )
    # 简化：直接用常量 0.80
    MASS_OVERRIDE_THRESHOLD = 0.80

    if ratio >= MASS_OVERRIDE_THRESHOLD:
        return {
            "level": "warning",
            "message": f"Mass override detected: {changed}/{len(entries)} files changed ({ratio:.0%}). "
                       f"This may indicate the project directory was replaced by another project.",
            "changed_ratio": ratio,
            "changed_count": changed,
            "total_count": len(entries),
        }
    return None


def _detect_identity_file_deletion(
    entries: list[FileEntry], workspace_path: Path
) -> dict | None:
    """检测身份性文件是否被删除。

    检查 IDENTITY_FILES 和 IDENTITY_DIRS 中的条目是否存在。
    """
    missing_files = []
    for rel_path in IDENTITY_FILES:
        full = workspace_path / rel_path
        if not full.exists():
            missing_files.append(rel_path)

    missing_dirs = []
    for rel_dir in IDENTITY_DIRS:
        full = workspace_path / rel_dir
        if not full.is_dir():
            missing_dirs.append(rel_dir)

    if missing_files or missing_dirs:
        msg_parts = []
        if missing_files:
            msg_parts.append(f"missing files: {', '.join(missing_files)}")
        if missing_dirs:
            msg_parts.append(f"missing dirs: {', '.join(missing_dirs)}")
        return {
            "level": "alert",
            "message": f"Identity files missing: {'; '.join(msg_parts)}. "
                       f"Project identity may be compromised.",
            "missing_files": missing_files,
            "missing_dirs": missing_dirs,
        }
    return None


def _detect_structure_collapse(
    entries: list[FileEntry], workspace_path: Path
) -> dict | None:
    """检测目录骨架突变：顶级目录数量跟上次 scan 差异过大。

    从 .gitgo/directory_skeleton.json 读取上次的骨架记录（由 sync 阶段写入）。
    """
    skeleton_path = workspace_path / ".gitgo" / "directory_skeleton.json"
    if not skeleton_path.exists():
        return None  # 首次运行，无基线

    import json
    try:
        old_dirs = set(json.loads(skeleton_path.read_text()))
    except (json.JSONDecodeError, OSError):
        return None

    # 当前顶级目录
    current_dirs = set()
    for entry in entries:
        parts = entry.rel_path.replace("\\", "/").split("/")
        if len(parts) > 1:
            current_dirs.add(parts[0])

    if not old_dirs:
        return None

    jaccard = len(old_dirs & current_dirs) / max(len(old_dirs | current_dirs), 1)
    if jaccard < 0.3:
        return {
            "level": "warning",
            "message": f"Directory structure collapsed: Jaccard similarity {jaccard:.2f} "
                       f"(old: {sorted(old_dirs)}, new: {sorted(current_dirs)}). "
                       f"Project may have been replaced.",
            "jaccard": jaccard,
            "old_dirs": sorted(old_dirs),
            "new_dirs": sorted(current_dirs),
        }
    return None
```

### 4.3 配置化

在 `ProjectConfig` 或 `sync_config.json` 中增加可选字段：

```json
{
  "integrity": {
    "enabled": true,
    "mass_override_threshold": 0.80,
    "identity_files": ["CLAUDE.md", ".claude/", ".codex/"],
    "identity_dirs": [".claude", ".codex"]
  }
}
```

### 4.4 认证标准

- [ ] 正常开发（5-20% 文件变更）不触发警告
- [ ] 全量覆盖（80%+ 文件变更）触发 `mass_override` 警告
- [ ] `.claude/` 目录被删除后触发 `identity_file_deleted` 告警
- [ ] 目录骨架 Jaccard < 0.3 触发 `structure_collapse` 警告
- [ ] 警告写入 HistoryManager，可通过 `gitgo history --op integrity_warning --json` 查询

---

## 五、Layer 2: Memory Snapshot

### 5.1 插入位置

`step_sync()`（sync_session.py:692）成功后，在写入 `HistoryManager.add_operation("sync")` 之前：

```python
# sync_session.py — step_sync() 中，同步成功后

if success:
    fc.synced = True
    # ...
    
    # ── Memory Snapshot ──
    _snapshot_tool_memories(self.workspace_path, self.backup_path, self.project)
    
    HistoryManager.add_operation(...)
```

### 5.2 实现

```python
# backend/core/identity/snapshot.py — 新文件

import shutil
from pathlib import Path

MEMORY_SOURCES = [".claude", ".codex", ".codebuddy"]


def _snapshot_tool_memories(
    workspace_path: Path,
    backup_path: Path,
    project,  # ProjectConfig
) -> dict:
    """将 workspace 的工具记忆目录拷贝到 backup 的 .gitgo/memories/。

    不做格式解析——文件级快照。工具记忆格式可能随版本变化，
    Gitgo 只负责保存和恢复原始文件。
    """
    dest_root = backup_path / ".gitgo" / "memories"
    dest_root.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    snapped = []
    for src_name in MEMORY_SOURCES:
        src = workspace_path / src_name
        if not src.exists():
            continue
        dest = dest_root / f"{src_name}_{ts}"
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
        snapped.append(src_name)

    return {"snapped": snapped, "timestamp": ts, "dest": str(dest_root)}


def restore_tool_memories(
    backup_path: Path,
    workspace_path: Path,
    snapshot_timestamp: str | None = None,
) -> dict:
    """从 .gitgo/memories/ 恢复工具记忆到 workspace。

    snapshot_timestamp=None 时使用最新快照。
    """
    src_root = backup_path / ".gitgo" / "memories"
    if not src_root.exists():
        return {"restored": [], "error": "no_snapshots"}

    # 找到最新的快照
    snapshots = {}
    for item in src_root.iterdir():
        name = item.name
        for src_name in MEMORY_SOURCES:
            if name.startswith(f"{src_name}_"):
                ts = name[len(src_name) + 1:]
                snapshots.setdefault(src_name, []).append((ts, item))

    if not snapshots:
        return {"restored": []}

    restored = []
    for src_name, versions in snapshots.items():
        # 选最新或指定时间戳
        if snapshot_timestamp:
            target = next((v for t, v in versions if t == snapshot_timestamp), None)
        else:
            versions.sort(key=lambda x: x[0], reverse=True)
            target = versions[0][1]

        if target is None:
            continue

        dest = workspace_path / src_name
        if target.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(target, dest)
        else:
            shutil.copy2(target, dest)
        restored.append(src_name)

    return {"restored": restored}
```

### 5.3 CLI

```bash
gitgo memory snapshot --project X          # 手动触发快照
gitgo memory restore --project X           # 从最新快照恢复
gitgo memory restore --project X --ts 20260516_100000  # 从指定快照恢复
gitgo memory list --project X --json       # 列出可用快照
```

### 5.4 认证标准

- [ ] `step_sync()` 成功后自动 snapshot .claude/ .codex/ 到 backup
- [ ] `gitgo memory restore --project X` 将最新快照恢复到 workspace
- [ ] 多次 snapshot 不互相覆盖（按时间戳区分）
- [ ] 恢复后 CC 可以读取 `.claude/` 下的记忆

---

## 六、Layer 3: Identity Bundle

### 6.1 插入位置

`collect_state_bundle()`（governance/state_bundle.py:13）的扩展。

### 6.2 实现

```python
# backend/core/governance/state_bundle.py — 扩展

def collect_state_bundle(session, minimal=False, include_identity=False):
    bundle = { /* 现有逻辑 */ ... }
    
    if include_identity:
        bundle["identity"] = _collect_identity_snapshot(session)
    
    return bundle


def _collect_identity_snapshot(session) -> dict:
    """收集项目身份快照。"""
    ws = session.workspace_path
    return {
        "project_structure": _capture_directory_skeleton(ws),
        "identity_files": {
            f: _file_status(ws / f)
            for f in IDENTITY_FILES
            if (ws / f).exists()
        },
        "tool_memories": {
            name: _dir_summary(ws / name)
            for name in MEMORY_SOURCES
            if (ws / name).exists()
        },
        "last_integrity_checks": _load_recent_integrity_warnings(session.project.name),
    }


def _capture_directory_skeleton(ws: Path) -> dict:
    """捕获项目顶级目录骨架（不递归）。"""
    skeleton = {"dirs": [], "files": []}
    try:
        for entry in sorted(ws.iterdir()):
            if entry.name.startswith('.') and entry.name not in ('.git', '.claude', '.codex', '.codebuddy'):
                continue
            if entry.is_dir():
                skeleton["dirs"].append(entry.name)
            else:
                skeleton["files"].append(entry.name)
    except PermissionError:
        pass
    return skeleton
```

### 6.3 CLI

```bash
gitgo export state-bundle --project X --json --include-identity
```

### 6.4 认证标准

- [ ] `--include-identity` 输出含 `identity` 块
- [ ] `project_structure.dirs` 和 `project_structure.files` 各至少一个条目
- [ ] `identity_files` 含至少 CLAUDE.md（如果存在）

---

## 七、文件清单

| 文件 | 内容 | 预估行数 |
|------|------|---------|
| `backend/core/identity/__init__.py` | 门面 re-export | 10 |
| `backend/core/identity/guard.py` | 三条检测规则 + `_run_integrity_checks` | 100 |
| `backend/core/identity/snapshot.py` | `_snapshot_tool_memories` + `restore_tool_memories` | 60 |
| `backend/core/sync_session.py` | step_scan 加 integrity checks + step_sync 加 snapshot | +15 |
| `backend/core/governance/state_bundle.py` | 扩展 `--include-identity` | +40 |
| `cli/commands.py` | `--mode memory` + snapshot/restore/list | +60 |
| `__main__.py` | `--mode memory` + `--memory-action` | +10 |

总计约 300 行新代码。零新依赖。

---

## 八、跟 Phase Gate 的关系

Identity Guard 和 Phase Gate 是同一个 defensive layer 的两个维度：

| 维度 | Identity Guard | Phase Gate |
|------|---------------|------------|
| 保护什么 | 项目环境的物理完整性 | agent 推理的逻辑完整性 |
| 预防什么 | 文件夹被覆盖、身份文件被删、目录骨架崩溃 | scope creep、phase skip、hallucinated continuity |
| 在哪个阶段干预 | scan（发现）、sync（备份）、export（查询） | 每次 tool dispatch 之前 |
| 实现位置 | gitgo backend | agent harness 内部 import |

两者可以独立实现、独立验证。Identity Guard 更容易出成果——scan 层改动约 20 行。
