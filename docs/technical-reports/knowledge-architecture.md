# Gitgo Knowledge System Architecture（修订版 v2）

> 设计日期：2026-07-16 | 修订：2026-07-16
> 状态：架构设计完成，待实施
> 6 个环节：收割 / 检索+注射 / 分离 / 回收 / 联想（暂未设计）

---

## 零、设计原则

1. **硬规则负责确定性操作，LLM 负责语义判断。** 硬规则是 Agent 的工具，不是主角。
2. **系统自己做，做到极限才求助人。** 不假定用户会主动 verify。人有最高权限覆盖任何系统决策。
3. **三层结构（pending → instance → abstract）是所有环节共享的基础设施。**
4. **知识管理 = 发现 → 理解 → 存储 → 检索 → 使用 → 撤出。** 每个环节单一职责，互不耦合。

---

## 一、整体架构

```
                         ┌──────────────────┐
                         │   Lesson Store   │
                         │  (三层 .jsonl)   │
                         └────────┬─────────┘
                                  │
    ┌───────────┬───────────┬─────┴─────┬───────────┬───────────┐
    ▼           ▼           ▼           ▼           ▼           ▼
 Harvest    Retrieval   Injection    Isolation   Recycle    Association
  (收割)     (检索)      (注射)       (分离)      (回收)     (联想)
    │           │           │           │           │           │
    │           └───────────┤           │           │           │
    │          recall 工具  │           │           │           │
    │        tool_result    │           │           │           │
    │           即注射       │           │           │           │
    │                        │           │           │           │
    └────────────────────────┴───────────┴───────────┴───────────┘
                    通过三层结构共享数据
```

**Retrieval + Injection 合并为一个环节。** Agent 通过 `recall` 工具主动检索知识，tool_result 本身即注射——Agent 带着检索结果工作 = 被"记住"了。不再有独立的 injection 步骤。

**环节职责边界：**

| 环节 | 做什么 | 不做什么 |
|------|--------|----------|
| 收割 | 发现 pattern → LLM 理解 → 写入 lesson | 不管 Agent 是否知道这些 lesson |
| 检索+注射 | 给 Agent 工具自己查；tool_result 即记入上下文 | 不替 Agent 决定查什么 |
| 分离 | 控制什么 lesson 对哪个 Agent 可见 | 不复制数据 |
| 回收 | 任务完成后从上下文撤出知识 | 不删 lesson |
| 联想 | 从多条 lesson 中提炼元 pattern | 暂未设计 |

---

## 二、Lesson 三层结构与数据模型

### 2.1 存储布局

```
.gitgo/knowledge/
├── abstract/                          ← 跨项目共享的通用教训
│   ├── {tech_stack}/lessons.jsonl     ← 自动 promote 或人 confirm
├── instances/
│   ├── {project}/lessons.jsonl        ← 项目级，verified = True
│   └── {project}/pending.jsonl        ← 待验证，verified = False
```

**没有 per-agent 独立副本。** 分离通过检索时的实时过滤实现（见 §5.2），不复制文件。

### 2.2 Lesson 数据模型

```python
@dataclass
class Lesson:
    id: str                          # UUID
    tech_stack: str                  # 技术栈标签
    category: str                    # api_migration|architecture|dependency|process
    severity: str                    # critical|high|medium|low

    # ── 收割产生的字段 ──
    trigger: str                     # 触发条件（子字符串匹配文件路径）
    rule: str                        # 可行动的约束（testable proposition）
    source: str                      # 信号来源标记
    resolution_history: list[dict]   # 历史解决记录

    # ── 验证与成熟度 ──
    verified: bool = False           # pending → instance 的开关
    verified_at: str = ""
    verified_count: int = 0
    verified_in: list[str] = field(default_factory=list)
    abstract: bool = False           # True = 已提升到 abstract 层
    origin: str = ""                 # "manual" | "auto_verify" | "harvest"
                                     # auto_verify 可被人工一键 revert 到 pending

    # ── 工具约束（Harness 层消费）──
    dangerous_tools: list[str] = field(default_factory=list)
    prerequisite_tools: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    check: dict = field(default_factory=dict)  # {"pattern": "正则"}

    # ── 有效性追踪（回收 + 联想消费）──
    trigger_count: int = 0            # 总触发次数
    applied_count: int = 0            # Agent 遵循了的次数
    violated_after_count: int = 0    # 已有 lesson 但仍然违反

    # ── 检索追踪（热/温/冷分层，滑动窗口）──
    recent_retrievals: list[str] = field(default_factory=list)
    # 近 N 次检索的 ISO timestamp，最多保留 MAX_RETRIEVAL_LOG 条

    project_name: str = ""
    created_at: str = ""

    # ── 收割重试追踪 ──
    harvest_retry_count: int = 0     # LLM 总结此信号失败次数，≥5 自动 discard
```

### 2.3 热/温/冷分类（基于近期活跃度，非累计计数）

```python
MAX_RETRIEVAL_LOG = 10       # 最多保留 10 条检索时间戳
RECENT_ROUND_WINDOW = 5      # 滑动窗口：近 N 个 round
HOT_THRESHOLD = 3            # 近 N 轮里 ≥3 轮被检索 = hot
STICKY_CAP = 10              # 最多 sticky K 条 hot lesson

def record_retrieval(lesson: Lesson):
    """检索时追加时间戳。"""
    lesson.recent_retrievals.append(datetime.now().isoformat())
    if len(lesson.recent_retrievals) > MAX_RETRIEVAL_LOG:
        lesson.recent_retrievals = lesson.recent_retrievals[-MAX_RETRIEVAL_LOG:]

def classify_lesson_heat(lesson: Lesson, current_round_id: str) -> str:
    """基于近 RECENT_ROUND_WINDOW 轮的检索频率分类。"""
    recent = [t for t in lesson.recent_retrievals
              if within_recent_rounds(t, current_round_id, RECENT_ROUND_WINDOW)]
    if len(recent) >= HOT_THRESHOLD:
        return "hot"
    elif len(recent) >= 1:
        return "warm"
    else:
        return "cold"

def get_sticky_lessons(all_lessons: list[Lesson], current_round_id: str) -> list[Lesson]:
    """热 lesson 中取 top-K sticky。按 severity + 最近活跃度排序。"""
    hot = [l for l in all_lessons if classify_lesson_heat(l, current_round_id) == "hot"]
    hot.sort(key=lambda l: (
        -severity_rank(l.severity),
        -len(l.recent_retrievals),
    ))
    return hot[:STICKY_CAP]
```

---

## 三、收割（Harvest）—— 发现 + 理解 + 写入

**职责：发现 pattern → 理解 → 写成 lesson。只写不管记。**

### 3.1 硬规则检测（确定性判据，零 token 成本）

daemon 每次 `workspace_dirty` → PolicyEngine 产出 results。以下信号被自动捕获为"未处理信号"：

| 信号类型 | 检测方式 | 存储 |
|----------|------|------|
| 同一文件被连续修改 ≥3 次 | git log 分析 | HistoryManager (operation="unprocessed_signal") |
| 同类型 policy_check_result 连续 warning | PolicyEngine 产出 | 同上 |
| contract_drift 重复出现 | drift 告警累积 | 同上 |
| rejection chain ≥3 次 | daemon reject handler | 同上 |
| 工具调用失败 | daemon dispatch 后 | 同上 |

> **同步更新**：`unprocessed_signal` 是 HistoryManager 的第 13 种 operation 类型。详见 docs/technical-reports/03-policy-governance.md §4.2。

### 3.2 硬规则去重

```python
def _is_exact_duplicate(signal: dict, existing_lessons: list[Lesson]) -> bool:
    """硬规则：trigger + rule 完全相同 = 重复。"""
    key = (signal.get("trigger", ""), signal.get("rule", ""))
    return any((l.trigger, l.rule) == key for l in existing_lessons)
```

**允许相似模式重复存在。** 不做事先的语义去重——这是为"联想"环节留数据。

### 3.3 调度算法（事件驱动 + 多维参数）

```python
def should_trigger_harvest(signal_type: str) -> bool:
    signals = get_unprocessed_signals(signal_type)
    baseline = get_signal_baseline(signal_type)  # 最近 100 个事件的滚动均值
    return (
        len(signals) >= MIN_BATCH_SIZE
        and density(signals, window=50) > baseline * DENSITY_THRESHOLD
        and source_diversity(signals) >= MIN_SOURCES
        and time_since_last_harvest(signal_type) >= COOLDOWN
    )
```

```yaml
# contract.yaml
knowledge:
  harvest:
    min_batch_size: 5
    density_window: 50
    density_threshold: 2.0
    min_sources: 2
    cooldown_minutes: 5
```

### 3.4 LLM 总结

**批量策略**：每次最多 30 条信号。超过 → 按来源类型分批。

**Prompt 硬约束**：
1. `rule` 必须是 **"if X, then must/should/should not Y"** 格式
2. 不接受纯描述性 lesson
3. 每条 lesson 必须能对应到未来的 PolicyEngine 检测条件
4. 信号不足以形成 actionable lesson → 返回空
5. 默认写入 pending（verified=False）

### 3.5 LLM 总结失败降级链

```python
def harvest_llm_summary(signals: list[dict]) -> list[Lesson]:
    """降级链：API失败→重试→退避→废弃 / 格式不合规→门禁丢弃"""
    try:
        result = call_llm_with_retry(prompt, timeout=30, max_retries=2, backoff=[1, 2, 4])
    except MaxRetriesExceeded:
        # 标记重试计数，超 5 次自动 discard
        for s in signals:
            s["harvest_retry_count"] = s.get("harvest_retry_count", 0) + 1
        still_viable = [s for s in signals if s["harvest_retry_count"] < 5]
        mark_unprocessed(still_viable)  # 下次再试
        return []

    lessons = parse_response(result)
    validated = [l for l in lessons if is_testable_proposition(l.rule)]
    return validated

def is_testable_proposition(rule: str) -> bool:
    """LLM 输出的门禁。不合规的直接丢弃，不写入 pending。"""
    if len(rule) < 20:
        return False
    keywords = ["if", "when", "must", "should", "禁止", "必须", "需要先", "不能直接"]
    if not any(kw in rule.lower() for kw in keywords):
        return False
    return True
```

### 3.6 Pending 消化调度（三级自动 + 独立触发）

```yaml
knowledge:
  harvest:
    pending_soft_threshold: 50     # L1
    pending_medium_threshold: 100  # L2
    pending_hard_threshold: 200    # L3
  pending_digest:
    check_interval_seconds: 3600   # 独立定时检查（不依赖 harvest 事件）
    trigger_conditions:
      - "pending_size >= soft_threshold"
      - "time_since_last_digest >= 24h AND pending_size > 20"
```

- **L1 (Soft, ≥50)**：LLM 扫描 pending，自动 discard 明显无效的
- **L2 (Medium, ≥100)**：LLM 自动 verify 高置信度 → instance（origin="auto_verify"）。人可一键 revert 到 pending
- **L3 (Hard, ≥200)**：阻塞新 harvest + Dashboard 告警。人始终有最高权限覆盖

**关键**：L1/L2 消化由 daemon 主循环的独立定时任务驱动，不依赖 harvest 事件——即使没有新信号进来，pending 也会被消化。

---

## 四、检索 + 注射（Retrieval + Injection）—— 合并为一个 recall 工具

**职责：Agent 主动检索知识，tool_result 即注射。**

### 4.1 核心决策：Tool Result 模式

Agent 通过 `recall` 工具检索。检索结果作为 tool result 返回——这是唯一正确的注射方式。

**为什么不是 system prompt 追加？**
- 破坏 Anthropic prompt cache
- Tool result 是 LLM 训练的 native 模式
- 天然可回收（tool_call/tool_result 对精确定位）

### 4.2 分级检索工具

| 工具 | 级别 | 方式 | 成本 |
|------|------|------|------|
| `recall_grep(query, project, top_k, agent_context)` | L0 | 硬规则子字符串 + 轻量排序 | 毫秒级，零 token |
| `recall_semantic(query, project, top_k, agent_context)` | L1 | 多向量语义搜索 | API embedding 调用 |
| `recall_rag(query, project, agent_context)` | L2 | LLM 带检索结果综合 | LLM 推理成本 |

所有 recall 工具共享 `agent_context` 参数（task_description 等），用于分离过滤（§5.2）。

#### L0: grep + 轻量排序

```python
def recall_grep(query: str, project: str, top_k: int = 10,
                agent_context: dict = None) -> list[Lesson]:
    lessons = load_all_lessons(project)
    if agent_context:
        lessons = filter_by_relevance(lessons, agent_context["task_description"])
    matches = [l for l in lessons
               if query.lower() in l.trigger.lower()
               or query.lower() in l.rule.lower()]
    matches.sort(key=sort_key)
    result = matches[:top_k]
    for l in result:
        record_retrieval(l)  # 更新 recent_retrievals
    return result

def sort_key(lesson: Lesson) -> tuple:
    return (
        -lesson.verified_count,
        -severity_rank(lesson.severity),
        -is_current_project(lesson),
        -recent_verification(lesson),
    )

def is_current_project(lesson: Lesson, current_project: str) -> int:
    """当前项目的 lesson（包括 abstract 层已验证过的）靠前。"""
    if lesson.project_name == current_project:
        return 1
    if current_project in lesson.verified_in:
        return 1
    return 0
```

输出带 `noise_signal`：top-1 和 top-2 分差 < 阈值 → 建议升级到 L1。

#### L1: 多向量语义搜索

每条 lesson 生成 2 个 embedding：
- `trigger` embedding：匹配"我在改 X 文件"
- `rule` embedding：匹配"我要做 Y 事情"

检索时两个向量分别查，结果合并。

```yaml
knowledge:
  recall:
    l1:
      enabled: false
      embedding_provider: "openai"      # provider-agnostic，复用 LLMProvider 抽象
      embedding_model: "text-embedding-3-small"
```

#### L2: RAG

LLM 带着 L0/L1 结果做综合思考。

```yaml
knowledge:
  recall:
    l2:
      inner_tool_call_budget: 0   # L2 内部 LLM 最多调几轮工具
      # 0 = 完全不能调，只读 L0/L1 输入做综合（默认安全）
      # 1-3 = 允许调 1-3 轮，但禁止 rag→rag（代码层强制）
      # 硬上限 3，配置不可绕过
      model_override: ""          # 空 = 用主 Agent 模型
      count_toward_max_steps: true
```

**关键约束**：
- `inner_tool_call_budget` 语义明确：L2 内部 LLM 的工具调用轮数
- 即使 budget > 0，`recall_rag` 内部禁止再调 `recall_rag`（同类递归禁止。允许 rag→grep, rag→semantic）
- 这个约束在 ToolDispatcher.dispatch() 中强制

### 4.3 注射 = tool_result 返回

```python
def format_recall_result(lessons: list[Lesson], top_k: int = 10) -> dict:
    """结构化 + 文本摘要双轨。"""
    displayed = lessons[:top_k]
    lines = []
    for i, l in enumerate(displayed):
        lines.append(
            f"## Lesson {i+1} [{l.severity.upper()}] {l.rule[:80]}\n"
            f"  trigger: {l.trigger}\n"
            f"  tools: prerequisite={l.prerequisite_tools} required={l.required_tools}\n"
            f"  verified: {l.verified_count}x in {l.verified_in}\n"
        )
    if len(lessons) > top_k:
        lines.append(f"\n还有 {len(lessons) - top_k} 条匹配。使用 top_k 参数增加返回数。")
    return {
        "lessons": [asdict(l) for l in displayed],
        "total_matches": len(lessons),
        "text": "\n".join(lines),
    }
```

---

## 五、分离（Isolation）—— 三层 + 检索时实时过滤

**职责：控制什么 lesson 对哪个 Agent 可见。不复制文件。**

### 5.1 三层天然分离

| 层 | 可见范围 | 写权限 |
|----|---------|--------|
| pending | 当前项目 | Agent harvest 写入；B Agent 不可写 |
| instance | 当前项目 | A Agent verify 后写入 |
| abstract | 所有同 tech_stack 项目 | 跨项目自动 promote |

### 5.2 Per-agent scope（检索时实时过滤，不复制文件）

```python
def filter_by_relevance(lessons: list[Lesson], task_description: str,
                        threshold: float = 0.5) -> list[Lesson]:
    """用 rule embedding 与 task_description embedding 的相似度过滤。

    替代之前的字符串匹配方案（任何包含常见词如 'the'/'文件' 的 lesson 都会被误匹配）。
    复用 L1 的 embedding 基础设施。L1 未启用时 fallback 到子字符串匹配。
    """
    if EMBEDDING_AVAILABLE:
        task_emb = embed(task_description)
        relevant = []
        for l in lessons:
            sim = cosine_similarity(task_emb, l._rule_embedding)
            if sim >= threshold:
                relevant.append((l, sim))
        relevant.sort(key=lambda x: -x[1])
        return [l for l, _ in relevant]
    else:
        # Fallback: 子字符串匹配（轻量但不精确）
        keywords = set(task_description.lower().split()) - COMMON_WORDS
        return [l for l in lessons
                if any(kw in l.rule.lower() or kw in l.trigger.lower()
                       for kw in keywords)]
```

**为什么不用 agent worktree 副本？**
- B Agent 拿到的是 fork 时的快照——主项目更新 lesson 后 B Agent 看不到
- 需要维护额外的存储层和同步机制
- 检索时实时过滤永远看到最新 lesson，零维护成本
- 复用 L1 embedding 基础设施，无新依赖

### 5.3 RingGate 权限

- `verify_lesson` / `promote_lesson` → RING_0 only
- `recall_*` → 所有 ring 可用
- B Agent 可读 instance，不可写 instance

---

## 六、回收（Recycle）—— 任务完成，知识从上下文撤出

**职责：释放 context window。不是清理知识库。**

### 6.1 双锚定事件

| Agent 类型 | 回收锚定 | 策略 |
|-----------|---------|------|
| B Agent | kill / reap | 整个 session 销毁，context 天然清理 |
| A Agent | round_complete | 只回收该 round 新增的、且非 hot 的 recall 结果 |
| A Agent | 无 round 事件 | 靠热/温/冷自然分类 + context prune |

### 6.2 回收机制

```python
def recycle_after_round(session, context_window, current_round_id):
    """round_complete: 降优先级 + 主动 prune。"""
    for msg in session.messages:
        if msg.get("type") != "tool_result":
            continue
        if msg.get("tool_name") not in ("recall_grep", "recall_semantic", "recall_rag"):
            continue

        # 查 tool_result 里注入的 lesson 什么分类（不是查 message flag）
        lesson_ids = msg.get("lesson_ids", [])
        all_cold_or_warm = all(
            classify_lesson_heat(load_lesson(lid), current_round_id) != "hot"
            for lid in lesson_ids
        )
        if all_cold_or_warm:
            msg["_retention_override"] = 0.1  # 可被 ContextWindow.prune 裁剪

    # 主动 prune（不等自然触发）
    context_window.prune(session, force=True)
```

**关键设计**：
- 判断依据是 lesson 的热/温/冷分类，不依赖 message 上的 session_scope flag
- 热 lesson 天然不被回收（分类为 hot → 不降优先级）
- 不物理删除 tool_call/tool_result 对（保护 Anthropic API 消息配对完整性）

### 6.3 Sticky lesson 上限

热 lesson 被标记为 sticky（不参与回收）时，最多 `STICKY_CAP` 条。超过时按 severity + 最近活跃度取 top-K。防止 sticky 集合无限增长挤爆 system prompt slot。

---

## 七、联想（Association）—— 暂未设计

**职责：从多条 lesson 中发现跨文件的元 pattern，从"事实"到"规律"。**

### 7.1 设计预留

- 硬规则去重只做精确匹配，不做语义去重——保留相似但独立的 lesson
- Lesson 的 `resolution_history`、`verified_in`、`recent_retrievals` 可供联想分析
- Abstract 层天然适合存储联想产出的"元 lesson"

### 7.2 未来方向（TODO）

```
Lesson A: "auth.py 反复改" (verified 5x)
Lesson B: "session.py 反复改" (verified 3x)
Lesson C: "token_manager.py 反复改" (verified 4x)
         │
         ▼ 联想
元 Lesson: "认证子系统整体不稳定，考虑重构而非逐个 patch"
          → abstract 层
          → 可归纳或替换原有 instance lesson
```

机制方向（待设计）：
1. 基于 rule embedding 聚类 lesson（聚类阈值 K 待定）
2. 聚类触发时机待定（harvest 事件驱动？独立定时？）
3. 聚类 ≥ 阈值条数时触发 LLM 生成元 lesson
4. 元 lesson 进入 abstract 层

**TODO**：聚类阈值 K、触发时机、聚类算法的选择。

---

## 八、配置汇总（contract.yaml 完整示例）

```yaml
knowledge:
  # ── 收割 ──
  harvest:
    min_batch_size: 5
    density_window: 50
    density_threshold: 2.0
    min_sources: 2
    cooldown_minutes: 5
    llm_batch_max: 30
    llm_timeout: 30
    llm_max_retries: 2
    max_harvest_retry: 5              # 同一批信号最多重试次数
    pending_soft_threshold: 50
    pending_medium_threshold: 100
    pending_hard_threshold: 200

  # ── Pending 消化（独立触发）──
  pending_digest:
    check_interval_seconds: 3600      # 每小时检查
    trigger_conditions:
      - "pending_size >= soft_threshold"
      - "time_since_last_digest >= 24h AND pending_size > 20"

  # ── 检索+注射 ──
  recall:
    l0:
      default_top_k: 10
      noise_warning_threshold: 0.3
      per_agent_filter_threshold: 0.5  # 实时过滤的相似度阈值
    l1:
      enabled: false
      embedding_provider: "openai"
      embedding_model: "text-embedding-3-small"
    l2:
      inner_tool_call_budget: 0       # 默认 0 = 完全不能调 tool
      model_override: ""
      count_toward_max_steps: true

  # ── 回收 ──
  recycle:
    hot_threshold: 3                   # 近 5 轮里 ≥3 轮检索 = hot
    recent_round_window: 5             # 滑动窗口大小
    sticky_cap: 10                     # hot lesson 最多 sticky 条数
    max_retrieval_log: 10              # recent_retrievals 最多保留条数
    a_agent_anchor: "round_complete"
    b_agent_anchor: "kill"

  # ── 联想（暂未实现）──
  association:
    enabled: false
    # cluster_threshold: TODO
    # trigger_timing: TODO
```

---

## 九、HistoryManager Operation 类型更新

在原有 12 种 operation 类型基础上新增第 13 种：

| # | Operation | 说明 |
|---|-----------|------|
| 13 | `unprocessed_signal` | 收割检测到的未处理信号，等待 LLM 总结 |
