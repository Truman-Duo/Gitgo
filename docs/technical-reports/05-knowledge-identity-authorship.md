# 报告五：Knowledge、Identity 与 Authorship 系统深度解析

> gitgo v0.35 | 2026-07-16 | 完全透底技术报告

---

## 概述

这三套系统共同构成 gitgo 的"项目免疫系统"：
- **Knowledge**：从历史中自动提取教训，防止重复犯错
- **Identity**：保护项目关键文件不被 AI Agent 意外破坏
- **Authorship**：清洗 AI 生成痕迹，保证代码的人类署名

**核心文件**：

| 文件 | 行数 | 职责 |
|------|------|------|
| `knowledge/models.py` | 65 | Lesson 数据模型（三层） |
| `knowledge/manager.py` | 222 | LessonManager CRUD + 搜索 |
| `knowledge/harvest.py` | 471 | 自动收割：4 数据源 → Lesson |
| `identity/guard.py` | 188 | 三条完整性规则 |
| `identity/snapshot.py` | 152 | Memory 快照保存/恢复 |
| `authorship.py` | 261 | AI 痕迹清洗 + 隐私扫描 |
| `template_manager.py` | 127 | Commit 消息模板系统 |

---

## 一、Knowledge 知识传承系统

### 1.1 Lesson 数据模型

```python
@dataclass
class Lesson:
    id: str                    # UUID
    tech_stack: str            # 技术栈标签
    category: str              # api_migration|architecture|dependency|process
    severity: str              # critical|high|medium|low
    trigger: str               # 触发条件描述（子字符串匹配变更文件路径）
    rule: str                  # 规则描述
    dangerous_tools: list[str] # 危险工具（需要前置工具）
    prerequisite_tools: list[str]  # 调用危险工具前必须先执行的前置工具
    required_tools: list[str]      # 任务完成前必须调用的工具
    resolution_history: list[dict] # 历史解决记录
    check: dict                # 正则检查配置 {"pattern": "..."}
    source: str                # 来源标记
    abstract: bool             # True=抽象层, False=实例层
    project_name: str          # 项目名（实例层）
    verified_at: str           # 验证时间
    created_at: str
    verified_count: int        # 被验证次数
    verified_in: list[str]     # 在哪些项目被验证
```

### 1.2 三层存储架构

```
.gitgo/knowledge/
├── abstract/              # 抽象层：跨项目共享的通用教训
│   ├── python/lessons.jsonl
│   ├── typescript/lessons.jsonl
│   └── ...
├── instances/             # 实例层：特定项目的具体教训
│   ├── gitgo/lessons.jsonl
│   ├── lexi/pending.jsonl    # 待确认：收割产生但尚未人工验证
│   └── ...
```

**Lesson 生命周期**：
```
harvest → pending (待确认)
              │ verify()
              ▼
         instance (已确认, 项目级)
              │ promote_to_abstract()
              ▼
         abstract (已推广, 跨项目)
```

### 1.3 LessonManager

全静态方法。关键操作：

```python
class LessonManager:
    KNOWLEDGE_DIR = ".gitgo/knowledge"

    @staticmethod
    def save(workspace_path, lesson):
        """根据 lesson.abstract 写入对应 .jsonl 文件（追加行）。"""
        if lesson.abstract:
            path = _abstract_path(workspace_path, lesson.tech_stack)
        else:
            path = _instance_path(workspace_path, lesson.project_name)
        with open(path, "a") as f:
            f.write(json.dumps(lesson.to_dict()) + "\n")

    @staticmethod
    def save_pending(workspace_path, lesson):
        """写入 pending.jsonl，按 lesson.id 去重。"""
        existing = load_pending(workspace_path, lesson.project_name)
        if any(e.id == lesson.id for e in existing):
            return  # 已存在，跳过
        path = _pending_path(workspace_path, lesson.project_name)
        with open(path, "a") as f:
            f.write(json.dumps(lesson.to_dict()) + "\n")

    @staticmethod
    def verify(workspace_path, lesson_id, project_name):
        """确认 pending lesson → 移到 instance（verified_count += 1）。"""
        ...

    @staticmethod
    def promote_to_abstract(workspace_path, lesson_id, project_name, tech_stack):
        """实例 → 抽象：在 N 个项目验证后推广。"""
        ...

    @staticmethod
    def search(workspace_path, query, project_name=None, tech_stack=None):
        """全文搜索（遍历 .jsonl 文件，子字符串匹配 trigger/rule/check）。"""
        ...
```

### 1.4 harvest_lessons() 四源收割（harvest.py 471 行）

```python
def harvest_lessons(workspace_path, project_name, tech_stack):
    lessons = []

    # 源 1: Git Log — 同一文件连续修改 ≥3 次
    lessons.extend(_harvest_from_git_log(workspace_path, project_name))

    # 源 2: CLAUDE.md — 解析章节（已知问题/注意事项/约束/禁止/避坑）
    lessons.extend(_harvest_from_claude_md(workspace_path))

    # 源 3: Scan History — 跨轮次反复修改
    lessons.extend(_harvest_from_scan_history(workspace_path, project_name))

    # 源 4: Governance Signals — 治理事件 → Lesson 桥接
    lessons.extend(_harvest_from_governance_signals(workspace_path, project_name))

    # 去重：已存在于 pending.jsonl 的 trigger 跳过
    existing_triggers = {l.trigger for l in LessonManager.load_pending(workspace_path, project_name)}
    new_lessons = [l for l in lessons if l.trigger not in existing_triggers]

    for lesson in new_lessons:
        LessonManager.save_pending(workspace_path, lesson)

    return new_lessons
```

#### 源 1: _harvest_from_git_log

```python
def _harvest_from_git_log(workspace_path, project_name):
    """检测同一文件在连续 ≥3 个 commit 中都被修改的模式。"""
    commits = get_git_log(workspace_path)
    file_commit_map = {}
    for c in commits:
        changed = get_changed_files(c.hash)  # 从 diff 获取
        for f in changed:
            file_commit_map.setdefault(f, []).append(c.hash)

    lessons = []
    for f, hashes in file_commit_map.items():
        if len(hashes) >= 3:
            lessons.append(Lesson(
                trigger=f,
                rule=f"文件 {f} 连续 {len(hashes)} 次被修改",
                category="process",
                severity="medium",
            ))
    return lessons
```

#### 源 2: _harvest_from_claude_md

解析 CLAUDE.md 中的已知问题/注意事项/约束/禁止/避坑章节。提取模式：
- 列表项：`- 描述`
- 表格行：`| 描述 | ... |`

#### 源 3: _harvest_from_scan_history

跨 session 的反复修改检测——从 HistoryManager 的 scan 事件中提取同一文件在多次 scan 中都出现在变更列表中的模式。

#### 源 4: _harvest_from_governance_signals（最复杂的源）

```python
def _harvest_from_governance_signals(workspace_path, project_name):
    entries = HistoryManager.load()
    lessons = []

    # integrity_warning → lesson
    for e in entries:
        if e.operation == "integrity_warning":
            lessons.append(Lesson(
                trigger="identity_integrity",
                rule=e.detail.get("message", ""),
                severity=e.detail.get("level", "high"),
            ))

    # governance_drift → lesson
    for e in entries:
        if e.operation == "governance_drift":
            lessons.append(Lesson(
                trigger="contract_drift",
                rule=e.detail.get("message", ""),
            ))

    # burst detection: 短时间内 ≥5 次 sync → 节奏过快
    syncs = [e for e in entries if e.operation == "governance_synced"]
    if len(syncs) >= 5:
        time_span = parse_time(syncs[-1].timestamp) - parse_time(syncs[0].timestamp)
        if time_span < timedelta(hours=1):
            lessons.append(Lesson(
                trigger="burst_sync",
                rule=f"1小时内 {len(syncs)} 次 sync，建议合并提交",
            ))

    # trend analysis: sync 文件数趋势
    ...

    # post-hoc 修正模式
    ...

    return lessons
```

---

## 二、Identity 身份保护系统

### 2.1 三条完整性规则（guard.py 188 行）

```python
_DEFAULT_IDENTITY_FILES = [
    "CLAUDE.md", ".claude/", ".codex/", ".codebuddy/",
    ".gitignore", "gitgo_config.json", "sync_config.json",
]

def _run_integrity_checks(entries, workspace_path, project):
    alerts = []

    # 规则 1: 全量覆盖检测
    alert = _detect_mass_override(entries, project)
    if alert: alerts.append(alert)

    # 规则 2: 身份文件删除检测
    alert = _detect_identity_file_deletion(workspace_path, project)
    if alert: alerts.append(alert)

    # 规则 3: 目录骨架崩塌检测
    alert = _detect_structure_collapse(entries, workspace_path)
    if alert: alerts.append(alert)

    return alerts
```

#### 规则 1: _detect_mass_override

```python
def _detect_mass_override(entries, project):
    threshold = project.integrity.get("mass_override_threshold", 0.80)
    changed = sum(1 for e in entries if e.status != "same")
    total = len(entries)
    if total == 0:
        return None

    ratio = changed / total
    if ratio >= threshold:
        return {
            "rule": "mass_override",
            "level": "error",
            "message": f"变更文件占比 {ratio:.0%} (≥{threshold:.0%})，疑似大规模覆盖",
        }
    return None
```

#### 规则 2: _detect_identity_file_deletion

检查 `_DEFAULT_IDENTITY_FILES` 中的每个文件在 workspace 中是否仍然存在。

#### 规则 3: _detect_structure_collapse

```python
def _detect_structure_collapse(entries, workspace_path):
    # 1. 加载目录骨架基线
    skeleton_path = Path(workspace_path) / ".gitgo" / "directory_skeleton.json"
    if not skeleton_path.exists():
        _save_directory_skeleton(workspace_path)  # 创建基线
        return None

    baseline = set(json.loads(skeleton_path.read_text()))
    current = set(get_top_level_dirs(workspace_path))

    # 2. 计算 Jaccard 相似度
    intersection = baseline & current
    union = baseline | current
    jaccard = len(intersection) / len(union) if union else 1.0

    if jaccard < 0.3:
        return {
            "rule": "structure_collapse",
            "level": "error",
            "message": f"目录骨架崩塌 (Jaccard={jaccard:.2f})",
        }
    return None
```

### 2.2 Memory Snapshot 系统（snapshot.py 152 行）

```python
MEMORY_SOURCES = [".claude", ".codex", ".codebuddy"]
_MAX_SNAPSHOTS = 5

def snapshot_tool_memories(workspace_path, backup_path, project):
    """增量复制工具记忆到 backup。最多保留 5 个快照。"""
    for source in MEMORY_SOURCES:
        src = Path(workspace_path) / source
        if not src.exists():
            continue
        dst = Path(backup_path) / ".gitgo" / "memories" / snapshot_ts / source
        copy_tree(src, dst)

    # 清理旧快照：最多保留 _MAX_SNAPSHOTS 个
    snapshots = sorted(list_snapshots(backup_path))
    for old in snapshots[:-_MAX_SNAPSHOTS]:
        shutil.rmtree(old)

def restore_tool_memories(backup_path, workspace_path, snapshot_timestamp):
    """从 backup 恢复指定快照到 workspace。"""
    snapshot_dir = Path(backup_path) / ".gitgo" / "memories" / snapshot_timestamp
    for source in MEMORY_SOURCES:
        src = snapshot_dir / source
        if src.exists():
            dst = Path(workspace_path) / source
            copy_tree(src, dst)

def list_memory_snapshots(backup_path):
    """列出所有可用快照（按时间倒序）。"""
    ...
```

---

## 三、Authorship 著作权清洗

### 3.1 Commit Message 清洗

```python
_COAUTHOR_PATTERNS = [
    re.compile(r'Co-authored-by:.*', re.I),
    re.compile(r'(Generated|Created|Written)\s+(with|by)\s+.*', re.I),
    re.compile(r'via\s+(Copilot|Cursor|Codex|Copilot Chat)', re.I),
    re.compile(r'Signed-off-by:\s*.*(?:bot|ai|assistant)', re.I),
]

def strip_commit_message(msg):
    for pattern in _COAUTHOR_PATTERNS:
        msg = pattern.sub('', msg)
    return msg.strip()
```

### 3.2 代码注释清洗

```python
_COMMENT_AI_PATTERNS = [
    re.compile(r'#\s*(?:Generated|Created|Written)\s+(?:with|by)\s+.*', re.I),
    re.compile(r'//\s*(?:Generated|Created|Written)\s+(?:with|by)\s+.*', re.I),
    re.compile(r'/\*\s*(?:Generated|Created|Written)\s+(?:with|by)\s+.*\*/', re.I | re.DOTALL),
    re.compile(r'#\s*AI\s*(?:generated|assisted|suggested)', re.I),
    re.compile(r'//\s*AI\s*(?:generated|assisted|suggested)', re.I),
]
```

### 3.3 AI 配置文件检测

```python
_DEFAULT_AI_CONFIG_FILES = [
    "CLAUDE.md", ".claude/", ".codex/", ".codebuddy/",
    ".cursor/", ".windsurf/", ".cursorrules",
    ".copilot-*", ".github/copilot-instructions.md",
]

def is_ai_config_file(rel_path, exclude_patterns):
    return any(fnmatch.fnmatch(rel_path, p) for p in exclude_patterns)
```

### 3.4 隐私扫描三级

```python
_DEFAULT_PRIVACY_PATTERNS = {
    "email": r'[\w\.-]+@[\w\.-]+\.\w+',
    "ip": r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
    "apikey": r'(?:sk-|ghp_|xox[bpras]-)[a-zA-Z0-9_-]{20,}',
    "private_key": r'-----BEGIN (?:RSA|DSA|EC|OPENSSH) PRIVATE KEY-----',
    "internal_path": r'(?:/home/|C:\\Users\\|/Users/)[\w\\/.-]+',
}

def scan_privacy(file_path, content, level=1, patterns=None, deep_scan=False):
    """level 1: 正则匹配。level 2: 结构特征。level 3: 深度扫描。"""
    if level >= 1:
        for name, pattern in (patterns or _DEFAULT_PRIVACY_PATTERNS).items():
            for match in re.finditer(pattern, content):
                alerts.append({"file": file_path, "type": name, "match": match.group()})

    if level >= 2:
        # 结构特征检测：表格列数 > 5、数值密度 > 0.3
        if count_table_columns(content) > _STRUCTURAL_THRESHOLDS["table_columns"]:
            alerts.append(...)
        if compute_numeric_density(content) > _STRUCTURAL_THRESHOLDS["numeric_density"]:
            alerts.append(...)

    if level >= 3 and deep_scan:
        # 深度扫描：上下文相关的敏感信息检测
        ...

    return alerts
```

---

## 四、Template 模板系统

```python
@dataclass
class CommitTemplate:
    name: str
    description: str
    header_format: str     # "[{prefix}-{number}] {type}{scope}: {subject}"
    body_format: str       # "项目: {project_name}\n\n变更:\n{commit_list}"
    prefix_override: str   # 覆盖默认 prefix

class TemplateManager:
    TEMPLATE_FILE = "commit-config.json"

    @staticmethod
    def get_template(name) -> CommitTemplate:
        templates = TemplateManager.load()
        for t in templates:
            if t.name == name:
                return t
        return _BUILTIN_DEFAULT  # 内置默认模板
```

`build_commit_template()` 执行变量替换：`{prefix}`, `{number}`, `{type}`, `{scope}`, `{subject}`, `{project_name}`, `{commit_list}`。

---

## 五、测试覆盖

| 测试文件 | 测试内容 | 测试方法 |
|----------|----------|----------|
| `test_lesson.py` | Lesson 模型、LessonManager CRUD、harvest_lessons | 单元（临时目录） |
| `test_identity_guard.py` | 三条规则检测、threshold 配置、directory_skeleton | 单元（FakeProject） |
| `test_authorship.py` | strip_commit_message、strip_code_comments、AI 配置检测、隐私扫描 | 单元（纯字符串） |
| `test_template_manager.py` | CommitTemplate CRUD、build_commit_template | 单元 |

---

## 六、已知限制与潜在问题

1. **harvest_lessons 需要触发**：知识收割不是自动运行的——需要在 sync 时或 daemon rejection 链中显式调用。如果从不 sync 也不 reject，lesson 永远不会被收割。

2. **CLAUDE.md 解析的脆弱性**：`_harvest_from_claude_md` 依赖特定 Markdown 章节标题格式。如果用户使用了变体标题（如"注意事项" vs "注意"），可能无法匹配。

3. **identity_file_deletion 只检查存在性**：不检查文件内容是否被篡改——只检查文件是否存在。CLAUDE.md 被替换为空白文件不会触发告警。

4. **structure_collapse 基线创建时机**：如果在首次初始化时目录结构已经不正常（如刚 clone 的空项目），基线就是异常的，后续正常化反而会触发告警。

5. **Memory Snapshot 的复制是目录级全量**：没有增量快照，每次 snapshot 都复制全部 MEMORY_SOURCES 目录。

6. **Authorship 的激进模式可能误伤**：`strip_code_comments` 的激进模式会删除任何包含 AI 关键字的注释——包括注释中引用 AI 工具的正常讨论。

7. **隐私扫描的正则可能漏报或误报**：`_DEFAULT_PRIVACY_PATTERNS` 的正则比较基础——`internal_path` 模式会匹配到文档中的示例路径。

---

## 七、设计审查总结

### ✅ 已实现
- 四源自动 Lesson 收割
- 三层 Lesson 存储（pending/instance/abstract）
- 三条 Identity 完整性规则
- Memory Snapshot 快照/恢复（最多 5 个）
- AI 痕迹清洗（commit message + 代码注释 + AI 配置文件）
- 三级隐私扫描
- 可自定义 Commit 模板

### ⚠️ 部分实现
- CLAUDE.md 解析脆弱
- Identity 只检查存在性不检查内容篡改
- 隐私扫描正则可能误报

### ❌ 未实现
- Lesson 的自动定期收割（当前依赖手动触发）
- Identity 的文件内容完整性校验（哈希对比）
- Memory Snapshot 的增量快照

---

## v0.35 更新补遗

**Knowledge 三期实施**:
- 收割: 硬规则5源信号捕获 + 多维调度算法 + LLM总结 + is_testable_proposition门禁 + pending三级消化
- 检索+注射: recall_grep(L0)/recall_semantic(L1)/recall_rag(L2), tool_result即注射
- 分离: per-agent scope实时embedding过滤(替代文件复制)
- 回收: round_complete锚定 + 热/温/冷分类 + sticky cap
- 联想: 架构预留,暂未设计

**Lesson数据模型**: 10+新字段 (trigger_count/applied_count/recent_retrievals/origin/harvest_retry_count)

**TestDataFactory**: 种子可复现的通用测试数据生成器, 覆盖5个子系统

---

## v0.36-v0.41 更新补遗

**v0.36 上下文管理接线（已落地）**:
- `harvest.py`（+70）: 收割信号接入九层 Context 的 Signal 层
- `recall.py`（+36）: 检索结果经 tool_result 注入 Transcript 层
