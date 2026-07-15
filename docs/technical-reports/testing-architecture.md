# Gitgo Testing Subsystem Architecture

> 设计日期：2026-07-16 | 状态：架构设计

---

## 零、设计原则

1. **Agent 负责"测什么"——写场景描述。工厂负责"怎么测"——执行描述、扩规模、收反馈。**
2. **测试反馈一五一十摊开给 Agent 看。Agent 判断是否符合精神，拿不准问用户。用户确认的不符合 → 信号进入 PolicyEngine。**
3. **场景描述是纯数据，与语言无关。Python class / TypeScript object / JSON 文件都可以承载。**
4. **Agent 动手前登记、loop 交付时强制检查、工厂扩规模跑多种子。登记过的不许跳过。**

---

## 一、Test Manifest —— 测试注册表

**位置**：`.gitgo/test_manifest.json`

**职责**：记录哪些模块边界需要集成测试、用哪个场景模板、最近一次跑的结果。

```json
{
  "boundaries": {
    "harvest_to_recall": {
      "modules": ["knowledge/harvest", "knowledge/recall", "knowledge/manager"],
      "registered_by": "agent_42c8",
      "registered_at": "2026-07-16T10:00:00",
      "scenario_template": "harvest_chain",
      "last_run": {
        "seed": 42,
        "passed": true,
        "timestamp": "2026-07-16T10:05:00"
      },
      "run_count": 5
    },
    "policy_to_gate": {
      "modules": ["policy/contract", "policy/gates", "sync_session"],
      "registered_by": "agent_42c8",
      "registered_at": "2026-07-16T10:00:00",
      "scenario_template": "policy_to_gate_chain",
      "last_run": null,
      "run_count": 0
    }
  },
  "module_registry": {
    "knowledge/harvest":  {"requires_test": true,  "boundaries": ["harvest_to_recall"]},
    "knowledge/recall":   {"requires_test": true,  "boundaries": ["harvest_to_recall"]},
    "knowledge/manager":  {"requires_test": true,  "boundaries": ["harvest_to_recall"]},
    "operations/scan":    {"requires_test": false, "boundaries": []},
    "policy/contract":    {"requires_test": true,  "boundaries": ["policy_to_gate"]},
    "policy/gates":       {"requires_test": true,  "boundaries": ["policy_to_gate"]},
    "sync_session.py":    {"requires_test": true,  "boundaries": ["policy_to_gate"]}
  }
}
```

**Agent 的判断指引**（何时登记——在 task 开始时判断）：

| 条件 | 动作 |
|------|------|
| 纯函数、零外部依赖 | `requires_test: false`，不登记 |
| 有外部依赖但依赖稳定 | `requires_test: false`，不登记 |
| 跨 2+ 模块边界、近期修改 | `requires_test: true`，查已有 boundary → 加入 |
| 跨 2+ 模块边界、无已有 boundary | `requires_test: true`，新建 boundary → 写场景模板 |

---

## 二、IntegrationScenario —— 场景描述（纯数据）

**职责**：描述一个跨模块集成测试的结构。不是代码——是 Agent 写的数据。

**核心抽象**（与语言无关）：

```
Scenario {
  name: str
  description: str
  steps: [
    {
      name: str              # 步骤名
      module: str             # 被测模块
      action: str             # 调用的函数名
      input_factory: str      # 工厂生成器名 (e.g. "signals", "lessons")
      input_from: str | null  # 前置步骤名（null = 工厂直接生成新数据）
      output_key: str         # 本步输出存入上下文，供后续步骤引用
    },
    ...
  ]
}
```

**具体示例**——harvest→recall 链路：

```json
{
  "name": "harvest_to_recall",
  "description": "验证 harvest 产出的 lesson 能被 recall 正确检索",
  "steps": [
    {
      "name": "capture_signals",
      "module": "harvest",
      "action": "capture_signal",
      "input_factory": "signals",
      "input_from": null,
      "output_key": "captured_count"
    },
    {
      "name": "llm_summary",
      "module": "harvest",
      "action": "harvest_llm_summary",
      "input_from": "capture_signals",
      "output_key": "lessons"
    },
    {
      "name": "save_pending",
      "module": "knowledge/manager",
      "action": "save_pending",
      "input_from": "llm_summary",
      "output_key": "saved_count"
    },
    {
      "name": "recall",
      "module": "recall",
      "action": "recall_grep",
      "input_from": "save_pending",
      "output_key": "recall_result"
    },
    {
      "name": "verify_recall",
      "module": "recall",
      "action": "check_recall_matches_lessons",
      "input_from": "recall",
      "output_key": null
    }
  ]
}
```

**Agent 的工作**：写这份 JSON。不是写测试代码——是描述要测什么。
**工厂的工作**：读这份描述 → `run(scenario, factory)` → 用不同种子跑多遍 → 收集每步输入输出。

---

## 三、TestReport —— 结构化反馈（摊开给 Agent 看）

**职责**：把每一步的输入、输出摊开。不自动判定 pass/fail——Agent 自己判断。

```python
@dataclass
class TestReport:
    scenario: str              # 场景名
    seed: int                  # 可复现种子
    steps: list[StepTrace]     # 每步完整轨迹
    failure_step: str | None   # 从哪步开始不对

@dataclass  
class StepTrace:
    step_name: str             # 步骤名
    module: str                # 被测模块
    input_summary: str         # 输入摘要（截断到 500 字符）
    input_full: dict           # 完整输入
    output_summary: str        # 输出摘要
    output_full: dict          # 完整输出
    duration_ms: float
    error: str                 # 异常信息（无异常为空）
```

**Agent 看到的反馈格式**——事实摊开，不判断对错：

```
场景: harvest_to_recall  (seed=42)
──────────────────────────────────────
Step 1: capture_signals [harvest]
  输入: signal_count=5, signals=[{trigger:"auth.py",...}, ...]
  输出: captured_count=5

Step 2: llm_summary [harvest]
  输入: signals (from step 1, 5条)
  输出: lessons=[Lesson(trigger="auth.py", rule="if modifying auth..."), ...]

Step 3: save_pending [knowledge/manager]
  输入: lessons (from step 2, 2条)
  输出: saved_count=2

Step 4: recall [recall]
  输入: query="auth"
  输出: total_matches=1, lessons=[...]
  ⚠ harvest产出2条包含"auth"的lesson, recall只命中1条

Step 5: verify_recall [recall]
  ✗ 匹配失败
```

**Agent 拿到这份报告后**：
- 知道 Step 4 不对 → 不用全局猜
- 知道 seed=42 的数据 → 可复现
- 前置步骤都正常 → 不是上游数据的问题
- 拿不准 → 请求用户确认

---

## 四、Loop 集成 —— 登记 → 强制检查

### Agent 工作流

```
1. Agent 接收 task
2. Agent 判断：涉及哪些模块？→ 查 test_manifest.json
   ├─ 已登记的 boundary → 记录下来，交付时必须跑
   ├─ 新模块、跨边界 → Agent 判断要不要登记
   │   ├─ 登记 → 写 test_manifest + 写场景 JSON
   │   └─ 不登记 → 跳过
   └─ 纯内部修改 → 不需要
3. Agent 执行 task
4. task 完成 → loop 检查
   ├─ 本轮修改涉及已登记 boundary？
   │   ├─ 是 → 工厂.run(场景, seed=N) → 生成 TestReport
   │   │   ├─ Agent 检查报告 → 确认通过 → round_complete
   │   │   └─ Agent 发现问题 → 修 / 问用户
   │   └─ 否 → 跳过
```

### Loop 强制检查

`round_complete` 时检查 manifest：

```python
def _check_test_manifest(session, project):
    manifest = load_test_manifest(project)
    changed = get_modules_changed(session.entries)
    
    for name, boundary in manifest["boundaries"].items():
        if any(m in changed for m in boundary["modules"]):
            if not boundary.get("last_run"):
                return False, f"Boundary '{name}' 登记了但还没跑过测试"
    
    return True, ""
```

---

## 五、用户确认 → PolicyEngine 偏移信号

**流程**：Agent 看报告拿不准 → 问用户 → 用户确认"这个输出不对" → 信号进入系统。

复用现有 rejection 模式：

```
Agent → 用户: "harvest_to_recall Step 4 输出不对，请确认"
用户 → Agent: "recall 应该能检索到 pending 中的 lesson"
Agent → round_complete (with rejection note)
  → HistoryManager: operation="test_boundary_rejected"
     detail={boundary:"harvest_to_recall", step:4, user_note:"recall应该检索pending"}
  → 同 boundary 连续 3 次 rejection → Fact 推导 → PolicyEngine 告警
```

**关键**：不是系统自动判断"测试失败"——是人确认后才成为信号。

---

## 六、规模化 —— Agent 写 1 个样例，工厂跑 N 个种子

```python
# Agent 写的场景模板（只写一次）
template = load_scenario("harvest_to_recall")

# 工厂扩规模
for seed in [42, 77, 123, 456, 789]:
    report = run_scenario(template, TestDataFactory(seed=seed))
    if not agent_review(report):
        # Agent 发现问题 → 记录 → 可能触发用户确认
```

以后加了 DEFG 模块：只要旧场景还在 manifest 里、工厂还能生成数据 → 老边界不会悄悄断掉。新模块的场景 Agent 自己写——模板已经在那里。

---

## 七、文件结构

```
.gitgo/test_manifest.json              # 测试注册表
tests/
├── factory/                           # 数据工厂（已有）
│   ├── __init__.py                    # TestDataFactory 主类
│   ├── pools.py                       # 数据池
│   ├── knowledge.py / policy.py / agent.py / history.py / sync.py
│   ├── chains.py                      # 链路生成器
│   ├── scenarios.py                   # NEW: IntegrationScenario + 加载 manifest
│   └── report.py                      # NEW: TestReport + StepTrace
├── test_data_factory.py               # 工厂自测
├── test_knowledge_system.py           # 知识系统单元+链路
├── test_knowledge_seed2.py            # 种子 2 验证
├── test_integration.py                # NEW: 集成测试入口（读 manifest 跑全部场景）
└── conftest.py                        # factory fixture
```
