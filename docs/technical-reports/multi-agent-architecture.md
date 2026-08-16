# 多 Agent 执行架构：Actor Model + 结构化通信 + 契约驱动协作

> 设计日期：2026-07-22，修订：2026-07-24
> v2.0：吸纳二审 + 元分析的全部修正
> 基于对 Claude Code / Kimi Code / OpenCode / Reasonix 子 Agent 机制的完整调研
> 结合 gitgo v0.36-0.40 已就位的 ExecutionContext / Resource Lock / assemble_context / manage_context

---

## 一、设计目标

gitgo 的多 Agent 系统**不是为了并行加速，而是为了保能力**。

LLM 本质是 `f(context)` 的函数。给同一个 LLM 两次但每次精炼的上下文，产出质量高于给一次混杂的上下文——不是因为硬件变了，而是因为**注意力没被分散**。

但这里还有一个更深层的、同等重要的动机：**推理隔离（Reasoning Isolation）。** 即使四个模块（Parser/Lexer/AST/Optimizer）的代码能塞进同一个 context window，LLM 在改 Parser 到一半时思维会被 Lexer 的细节拉扯、再切换到 Optimizer、再回来 Parser——不是 token 预算不够，是**推理轨迹互相污染**。独立的 context window 让每个子任务在自己的推理空间里完整展开，不被其他子任务的中间状态干扰。

**Context Isolation + Reasoning Isolation = gitgo 的多 Agent 分工。**

---

## 二、LLM 架构约束与 Actor Model 选型

### 2.1 为什么 LLM agent 之间无法实现传统并发

这不是各家没做好，是 LLM 架构本身的物理约束：

1. **单个 LLM 实例是自回归单流生成器。** 一次调用只能生成一条线性 token 序列。模型内部不存在"并发"——没有 OS 线程的时钟中断、上下文切换、运行时信号注入。不能被中断注入。

2. **context window 是唯一的感知入口，且只能在两次调用之间的 turn boundary 被改写。** 任何"通信"物理上只能发生在 turn boundary——不存在别的通道。

3. **非确定性 + 无法运行时打断 → 冲突检测必须交给编排层机械执行，不能指望模型"记得去做"。** 任何基于"B2 去看看 B1 留的笔记"的设计——无论文件约定多精致——本质上都是 advisory soft gate，和 gitgo 主线"只有 structural hard gate 才可信"的核心信条矛盾。

4. **LLM 内部注意力没有 memory barrier。** 即使两个 agent 各自跑，它们无法建立起真正意义上的 happens-before 关系——因为 LLM 不知道时间流逝，只知道上下文里有什么。分布式系统里的 Lamport clock、vector clock 一类工具在 agent 系统里完全失效。

### 2.2 Actor Model 是唯一理论上站得住的模型

基于以上约束，能实现的、且理论上站得住的模型是 **actor model**：独立的、内部顺序执行的单元，靠异步消息交互，而不是共享可变状态的同时访问。

每个 TaskSlot 是一个 Actor：
- 独立上下文窗口（不共享）
- 内部顺序执行 agent_step() 循环
- 与其他 Actor 的交互通过 Scheduler 路由的结构化消息
- 不存在"共享内存 + 锁"——LLM 不支持真正的"同时性"

注意：这个 actor model 比 Erlang 的 actor model 更弱——Erlang actor 有真时钟、真消息接收顺序，LLM actor 没有。在 LLM 场景下，版本/状态信息只能通过 context 注入，不能依赖时间戳。

---

## 三、核心设计原则

### 3.1 逻辑父子抽象，执行不绑定物理

A Agent 只知道"我把任务拆给了子单元，它们各自有独立上下文和工具权限，完成后回报我"。B1 可能在同进程内、B2 可能在远端 SSH 上、B3 可能用不同 LLM 提供商——A 不关心，Scheduler 负责。

```
A Agent（逻辑父）——只做：拆任务、发 TaskSlot、收结果、决策
  ├─ B1 → 可能同进程 LocalBackend
  ├─ B2 → 可能 ForkBackend
  └─ B3 → 可能未来 SSHBackend / RemoteLLMBackend
```

### 3.2 上下文分区，不是进程 fork

物理 fork 是手段。核心抽象是：

```
给定: TaskSpec + ContextSnapshot + Capability
     → 独立 AgentSession + agent_step() 循环
     → 产出: 结构化 result + transcript
```

### 3.3 与 gitgo 现状的关系

已有组件就位，不推翻重来：
- `AgentProcess` / `AgentSession` / `ToolRegistry` / `ExecutionContext` —— 提供执行基础
- `manage_context` —— 上下文压缩已接线
- `Resource Lock` —— 冲突检测已实现
- `assemble_context / assemble_return_context` —— 上下文装配已注册为 AgentTool

需要新建的是"分块决策 + 分块间协调 + 多后端执行"这一层。

---

## 四、核心抽象：四组件拆分

TaskSlot 不是一个 God Object——它是四个独立 dataclass 的薄包装：

```python
@dataclass
class TaskSpec:
    """任务定义：不可变声明。retry 时复用同一个 spec。"""
    spec_id: str
    task_description: str
    expected_output: str
    input_interfaces: list[str]    # 依赖的上游接口符号 ["auth.py:authenticate"]
    output_interfaces: list[str]   # 承诺产出的接口符号

@dataclass
class ContextSnapshot:
    """上下文精选：Scheduler 组装，不来自 TaskSlot 自身。"""
    needed: list[dict]        # 治理信号 + L1 强制上游注入 + L2 冻结契约
    relevant: list[dict]      # 知识库 lesson
    dependency: dict          # 依赖图子图
    status: dict              # workspace 状态
    rejections: list[dict]    # 最近的 rejection 历史
    phase_brief: str          # 同级其他 Slot 做了什么

@dataclass
class Capability:
    """权限四元组：精确控制每个 Slot 的能力边界。"""
    tool_allowlist: list[str]
    ring_level: RingLevel
    resources: list[str]       # Resource Lock 据此调度
    max_steps: int

@dataclass
class TaskRuntime:
    """运行时状态：retry/resume/cost/latency/cache 只动此类。"""
    status: str                # PENDING | RUNNING | COMPLETED | FAILED | KILLED
    backend: "SlotBackend"
    result: dict | None = None
    transcript: Any = None

@dataclass
class TaskSlot:
    """逻辑执行单元 —— 薄包装。"""
    slot_id: str
    parent_slot_id: str | None  # 树关系：谁创建的
    depends_on: list[str]        # DAG 关系：执行约束——等待谁完成
    depth: int                   # 嵌套深度（默认 max 2，可配置）
    spec: TaskSpec
    context: ContextSnapshot
    capability: Capability
    runtime: TaskRuntime
```

---

## 五、Scheduler：编排层统一入口

Scheduler 独立于 Backend——Backend 只回答 How，Scheduler 决定 Where / When / In What Order。

```
Scheduler
  │
  ├── 1. Partition
  │     收到任务 → 双层决策 → 产出 TaskSpec[] 列表
  │
  ├── 2. Build DAG
  │     分析 spec.input_interfaces / output_interfaces
  │     → 构建 depends_on 边 → 验证无环
  │
  ├── 3. Assemble Context
  │     为每个 TaskSpec 组装 ContextSnapshot
  │     → L1 强制注入上游产出 → L2 接口契约冻结
  │
  ├── 4. Create Slots
  │     组装 TaskSlot（spec + context + capability + runtime）
  │     → 选择 Backend
  │
  └── 5. Execute & Collect
        按 DAG 顺序逐批提交 Backend
        → 收集结果 → Escalation 处理 → verify_contract
```

---

## 六、结构化通信：三级强制原语

### 6.1 核心信条

advisory soft gate 不可信，只有 structural hard gate 才可信。这是 gitgo 主线 Policy Engine 的核心教训——contract drift 从"警告然后人决定"升级成"工具调用层面硬拦截"——在 agent 间通信这个新领域同样适用。任何依赖 LLM "记得去读"的机制都是 advisory，不可信。通信原语是编排层强制执行的，不留给模型的自由意志。

### 6.2 三级通信

**Level 0 —— 无依赖**

B1 和 B2 互不依赖。当前 fork-join 就够，不需要任何通信。

**Level 1 —— 单向依赖（B2 依赖 B1 的产出）**

不是"文件放那，B2 看不看随意"。Scheduler 在组装 B2 的 ContextSnapshot 时，机械地强制拉取声明的上游产出摘要，注入 `needed` 层：

```python
def assemble_context(slot, upstream_slots) -> ContextSnapshot:
    needed = collect_governance_signals(slot)
    relevant = collect_lessons(slot)
    dependency = collect_dependency_subgraph(slot)

    if slot.depends_on:
        for upstream_id in slot.depends_on:
            upstream = find_slot(upstream_slots, upstream_id)
            if upstream and upstream.runtime.status == "COMPLETED":
                needed.append({
                    "source": "upstream_slot",
                    "rule": f"上游 {upstream_id} 已完成。"
                            f"产出接口: {upstream.spec.output_interfaces}",
                    "output_files": upstream.runtime.result.get("files", []),
                })

    return ContextSnapshot(needed=needed, relevant=relevant, ...)
```

**Level 2 —— 真互相依赖（共享接口）**

采用**方案 B——冻结当前 dep_graph 签名**，不做前瞻性协商。理由：前瞻性协商（"先出接口草案给消费方确认"）本质上退化为多轮 LLM 往返，不划算且不可靠。冻结当前事实——dep_graph 说当前签名是什么就是什么——最符合 gitgo 的风格。

```python
@dataclass
class InterfaceSpec:
    file: str
    symbol: str
    signature: str
    owner_slot: str
    consumers: list[str]

@dataclass
class InterfaceContract:
    contract_id: str
    frozen_at: str
    interfaces: list[InterfaceSpec]

def freeze_contract(slots, dep_graph) -> InterfaceContract:
    """从 dep_graph 提取当前签名作为冻结契约。不涉及未来预测。"""

def verify_contract(contract, workspace_state) -> list[ContractViolation]:
    """检测实际产出是否偏离冻结契约。机制等同 contract.py 的 detect_drift。"""
```

### 6.3 Escalate Recovery Policy（显式定义）

B2 执行中发现契约有缺陷（如 B1 漏了参数）→ EscalateToParent。**A 收到后做什么必须显式定义，不是 print(error)：**

1. **默认：中止全部子 slot，单一上下文重跑。** 把 B1 的产出和 B2 的发现合并回 A 的上下文，A 自己完成剩余工作。这是最保守、最可靠的选择。L2 契约的真实价值在这一刻体现：早期发现分解失败，及时回退，而不是继续在错误基础上构建。
2. **重试一次（可选）：** A 修正契约后重新 fork B1/B2。只在 A 确认契约错误是可修复的且范围有限时使用。最多 1 次重试。
3. **人工介入（可选）：** 如果单一上下文也装不下且重试也失败，升级给用户。

如果某个任务分解频繁触发 escalation（连续 3 次以上），说明这个任务不适合 L2 分解——A 下次不应再拆它。

---

## 七、三层正交解耦

传统多 Agent 架构的绑定：`上下文隔离 = 进程隔离 = 文件系统隔离 = 无法协作`

gitgo 的拆解：

```
上下文隔离层  → TaskSlot 独立 AgentSession + ContextSnapshot
                LLM 注意力不被分散（context isolation + reasoning isolation）
                
文件系统层    → 所有 TaskSlot 共享 workspace（data plane）
                文件系统承载内容同步
                
编排层        → Scheduler 强制执行三级通信（control plane）
                L0: fork-join
                L1: assemble_context 强制注入上游产出
                L2: InterfaceContract 冻结 + verify
                冲突: EscalateToParent（默认重跑，最多1次重试）
```

control plane 和 data plane 分离——文件系统解决了内容同步，编排层解决了意图/协商同步。

---

## 八、分区决策：双层门控

### 层 1（系统规则 —— hard gate）

| 条件 | 动作 |
|------|------|
| `manage_context()` need_compact 连续 2 轮 | 强制触发分区评估 |
| max_steps 消耗 ≥ 80% 且未完成 | 强制触发分区评估 |
| target_files ≥ 5 且有交叉依赖 | 建议触发分区评估 |
| doom_loop 检测触发 | 强制终止，分区重试 |
| nudge_counter 任一 ≥ MAX_NUDGE_REPEAT | 强制 upgrade 父级 |

### 层 2（LLM 自主分解 —— 软建议 + structural 验证）

LLM 可主动调用 `decompose_task` 工具建议拆分。Scheduler 进行 structural 验证：检查 input_interfaces 和 output_interfaces 是否能形成完整依赖链。验证失败 → 退回重新分解或退化单层。

**过度分解防护：**
- 分解后每个子 slot 消耗独立的 max_steps，总和从父预算扣 —— LLM 感知分解成本
- 连续 3 次分解被 structural 验证驳回 → 暂时禁用 decompose_task 工具（冷却期）
- 工具 description 声明成本

---

## 九、Runtime Constitution

以下约束是 gitgo Runtime 的宪法级设计原则。任何未来功能不得违反：

1. **advisory soft gate 不可信，只有 structural hard gate 才可信。** 任何依赖 LLM "记得去做"的设计都是设计错误。
2. **冲突升级给父级，不是子级之间私聊。** 改变任务边界的权限只在父级。
3. **上下文隔离和文件系统共享是正交的。** 不要把进程隔离和文件系统隔离绑定。
4. **LLM 没有 happens-before 关系。** 不要依赖时间戳、时钟、或"先发生"语义做状态决策。唯一可信的是 context 里有什么。
5. **权限即路由。** 子单元的能力通过 Capability 四元组定义，不通过预定义角色名。
6. **递归深度可配置。** 当前默认 depth ≤ 2，但不应写死为架构常量。
7. **Escalate 必须有 Recovery Policy。** 不能停在 print(error)。

---

## 十、执行模型：完整周期

```
═══════════════════════════════════════════════════════════════
A Agent 收到任务: "重构 auth.py + api.py + db.py 的认证逻辑"
═══════════════════════════════════════════════════════════════

1. Scheduler.Partition:
   → 系统规则：target_files=3，未触发硬门控
   → LLM 调 decompose_task:
      - assemble_context(files=["auth.py","api.py","db.py"]).dependency
      - 依赖图: {auth.py:[], api.py:[auth.py], db.py:[auth.py]}
      - 建议: B1(output=authenticate) → B2(input=authenticate)
   → Structural 验证: output ∩ input = {authenticate} ✓

2. Scheduler.Build DAG:
   → B2.depends_on = ["B1"]

3. Scheduler.Assemble Context:
   → 交叉依赖文件 auth.py → Level 2 触发
   → freeze_contract: 从 dep_graph 取 authenticate 当前签名
   → InterfaceContract 注入双方 ContextSnapshot.needed

4. Scheduler.Execute:

   ╔══════════════════════╗     ╔══════════════════════╗
   ║ B1                   ║     ║ B2                   ║
   ║ spec: 重构 auth.py   ║     ║ spec: 重构 api+db    ║
   ║   output: [auth.py:  ║     ║   input: [auth.py:   ║
   ║     authenticate]    ║     ║     authenticate]    ║
   ║ context.needed: [    ║     ║ context.needed: [    ║
   ║   governance_signals,║     ║   governance_signals,║
   ║   InterfaceContract   ║     ║   InterfaceContract   ║
   ║ ]                    ║     ║   L1: B1产出摘要     ║
   ║ depends_on: []       ║     ║ ]                    ║
   ║ backend: ForkBackend ║     ║ depends_on: ["B1"]   ║
   ╚══════════════════════╝     ║ backend: ForkBackend ║
            ↓                    ╚══════════════════════╝
   B1 执行完毕                           ↓
                              Scheduler 检测 B1 COMPLETED
                              → L1 注入上游产出
                              → B2 启动

   B2 执行中 workspace 读到 B1 的产出（共享 FS）
   → 按 InterfaceContract 签名调用 authenticate
   → 如果签名有偏差 → EscalateToParent

5. Scheduler.verify_contract → 无偏差 ✓

6. A 收集 B1.result + B2.result → 验证一致性 → 下一步
═══════════════════════════════════════════════════════════════
```

---

## 十一、Context Runtime 定位

gitgo 的以下组件服务于**Context 作为可管理运行时资源**这个目标，不依赖于多 Agent 协作：

| 组件 | 职能 | 单 A→B 是否有价值 |
|------|------|-----------------|
| Context Assembler | 三层精选（needed + relevant + dependency） | ✅ |
| TranscriptBuilder | 结构化执行记录 + 返回上下文 | ✅ |
| ContextWindow + Compact | 五级压缩优先级链 | ✅ |
| manage_context | 统一压缩入口 | ✅ |
| Governance Layer | Policy Engine 信号 → 约束注入 | ✅ |

即使 gitgo 永远只用单 A→单 B，这些组件依然完整发挥价值。多 Agent 协作不是这些组件的存在前提——它是 Context Runtime 成熟后自然延伸出的能力。

---

## 十二、实施路径

### Phase 1: 基础设施层（本次实施）

| 做什么 | 文件 |
|--------|------|
| RuntimeConstitution 文档 | `docs/technical-reports/runtime-constitution.md` |
| TaskSpec / ContextSnapshot / Capability / TaskRuntime / TaskSlot dataclass | `backend/core/loop/task_slot.py` |
| SlotBackend ABC + ForkBackend | `backend/core/loop/task_slot.py` |
| `AgentProcess` 增加 `depends_on` 字段 | `loop/models.py` |
| SlotScheduler（Partition → DAG → Execute → Collect）| `backend/core/loop/scheduler.py` |
| InterfaceContract（freeze + verify，方案 B）| `backend/core/loop/interface_contract.py` |
| decomposition.py（双层决策 + 过度分解防护）| `backend/core/loop/decomposition.py` |
| assemble_context 增加 L1 强制注入逻辑 | `daemon/__init__.py`（修改）|

### Phase 2: 数据驱动迭代（未来）

- DAG 深度可配置化（当前 depth≤2）
- Escalate recovery policy 多分支（当前默认重跑）
- SSHBackend / RemoteLLMBackend

### Phase 3: 不做

- LocalBackend —— 同进程直接用 agent_step，不需要 Backend 抽象
- 通用 DAG 引擎 —— depends_on 列表 + Scheduler 排序足够
- 预设 Agent 角色名 —— Capability 四元组替代

---

## 十三、验证

```bash
pytest tests/ -q

# P1: 组件导入
python -c "
from backend.core.loop.task_slot import TaskSpec, ContextSnapshot, Capability, TaskRuntime, TaskSlot
from backend.core.loop.scheduler import SlotScheduler
from backend.core.loop.interface_contract import InterfaceSpec, InterfaceContract, freeze_contract
from backend.core.loop.decomposition import should_decompose, suggest_split
print('All new modules importable')
"

# P1: 兄弟依赖 DAG
python -c "
from backend.core.loop.task_slot import TaskSlot
b1 = TaskSlot(slot_id='b1', parent_slot_id='a', depends_on=[], ...)
b2 = TaskSlot(slot_id='b2', parent_slot_id='a', depends_on=['b1'], ...)
assert b2.depends_on == ['b1']
print('DAG sibling dependency OK')
"
```
