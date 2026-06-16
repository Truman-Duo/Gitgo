# State Log：从 Git Commit 到工作区状态原生记录

> 日期：2026-06-12
> 触发：Dashboard 开发 18 次重复 fix + Git 历史清洗
> 状态：设计讨论记录，待进入迭代计划

---

## 一、问题起源

### 1.1 Dashboard CLI 开发的 git 灾难

Dashboard 从 Python Rich Live → TypeScript Ink 的重写过程中，产生了 18 个 `fix(dashboard): ...` commit。每一个都是对 stdin/stdout 竞争问题的修补尝试，最终证明——全部无效，正确解是换技术栈。

这 18 个 commit 被 gitgo sync 进了 release repo，产生了 22 个重复的 `[GITGO-33]`。清洗后 56 commits → 31 commits。

暴露的问题：
- git commit 不能表达"这个方向后来被放弃了"
- git commit 不能表达"18 个 fix 是一个探索过程，不是 18 个独立 bug"
- 事后清洗成本高，且依赖 GitHub 保留的 PR refs 才勉强恢复被覆盖的贡献

### 1.2 Git 清洗引申

清洗过程中进一步发现：
- 巨型聚合 commit 会吞没外部 PR 的署名
- 直接 git commit 到 release repo 的裸 commit 破坏编号体系
- `_find_next_number` 自引用锁死——扫 release repo 取最大 N，每次 sync 都生成同一个号

### 1.3 核心矛盾

git commit 同时承担了两个不该它承担的职责：

| 职责 | 颗粒度要求 | git commit 表现 |
|---|---|---|
| **状态记录**（"我在调试，改了 18 次"） | 细、需要元信息、可查询 | 勉强可用，但丢失上下文 |
| **发布载体**（"这是 v0.28 的正式代码"） | 干净、可读、可 revert | 被 18 个 fix 污染 |

硬塞在同一个 git 模型里 → 发布历史被污染，状态记录不完整。

---

## 二、State Log 的概念

### 2.1 核心思想

**git commit 降级为导出格式。真正的"状态"存在 State Log 里。**

```
workspace 层:  state log 条目（每步操作+元信息+关联）
                ├─ 是 governance event 的源头
                ├─ 是 lesson 收割的输入
                ├─ 是 contract 变更的触发
                ├─ 关联 memory / identity 快照
                └─ 编译 → git commit（发布用，干净可读）
```

工作区的 State Log 本身就有 git 的功能——记录变更、支持回滚、追溯历史——但自由度更高，能记录更多方面、更多信息、颗粒度更高。

### 2.2 一条 State Log 条目长什么样

```json
{
  "seq": 1042,
  "timestamp": "2026-06-11T02:30:00+08:00",
  "type": "exploration",

  "summary": "Dashboard stdin/stdout 竞争修复尝试 #12",
  "target_files": ["cli/dashboard.py"],
  "target_modules": ["dashboard"],

  "conclusion": "abandoned",
  "replaced_by_seq": 1060,
  "conclusion_reason": "Windows 控制台 stdin/stdout 共享同一 fd，Python 线程模型无法根本解决。最终方案：TypeScript + Ink，event loop 天然解耦。",

  "lesson_triggered": "stdin_stdout_race_windows",
  "contract_drift": "tech_stack: rich → ink",
  "governance_event": "exploration_abandoned",

  "git_commit_hash": "abc123f",
  "parent_seq": 1041
}
```

同样的操作，git commit 只有：
```
fix(dashboard): 删除 refresh_per_second — Rich 自动刷新与 msvcrt 竞态导致卡死+无限滚动
```

丢失了：
- 这是第 12 次尝试（前面 11 次都失败了）
- 结论：这个方向被放弃了
- 被什么替代：seq=1060 的 Ink 重写
- 触发了什么 lesson：stdin_stdout_race
- 关联合约变更：tech_stack 漂移

### 2.3 State Log 条目类型

| type | 含义 | 例子 |
|---|---|---|
| `incremental` | 正常增量开发 | 新增一个 feature，修改一个函数 |
| `exploration` | 探索性修改，结论可能是 abandoned | 18 次 dashboard fix |
| `decision` | 架构/技术决策 | 放弃 Rich，改用 Ink |
| `governance` | 治理层操作 | merge PR, contract update, lesson verify |
| `rollback` | 回滚操作 | 指向被回滚的 seq |

---

## 三、与现有系统的联动

### 3.1 全局联动链路

gitgo 已经有的 layer（gate、lesson、contract、governance、memory）本质上就是一个分散的 state log。State Log 是让它们从"各自为战"变成"一条链路"。

```
state log 条目
  ↓ Gate A
  ↓ 分类: incremental / exploration / decision
  ↓ 探索型 → 触发 Decision 流程（不逐条 formalize）
  ↓ 增量型 → 正常 sync 流程
  
  ↓ Lesson
  ↓ 扫 state log 找 conclusion: abandoned 的条目
  ↓ 生成总结性 lesson（不是 30 条碎片）
  ↓ "同一文件连续 N 次 exploration → abandoned → 换方案"
  
  ↓ Contract
  ↓ state log 里的 contract_drift 字段
  ↓ tech_stack 实际使用 vs 声明不一致 → 触发 contract_update
  
  ↓ Governance
  ↓ state log 编译 → governance event
  ↓ 新增 event 类型: governance_decision
  
  ↓ Memory
  ↓ 决策性条目 → 提取结论 → 写入 CLAUDE.md / memory/
  
  ↓ Release
  ↓ state log 编译 → 干净的 git commit
  ↓ exploration 条目被折叠（只保留结论）
  ↓ 外部 PR 条目保留署名
```

### 3.2 各层需要的改动

| 层 | 当前能力 | State Log 化以后 |
|---|---|---|
| **State Log** | 不存在（借用 git log + history.json） | 一级对象，所有操作写入 |
| **Gate** | 逐 commit 无差别扫描 | 区分 incremental / exploration，后者触发决策流程 |
| **Lesson** | "文件被改 N 次" → pending | 扫 `conclusion: abandoned` → 生成总结性 lesson |
| **Contract** | 检测文件删除/签名丢失 | 读 `contract_drift` 字段，检测 tech_stack 实际 vs 声明 |
| **Governance** | event log 记录 sync/push | 新增 `governance_decision` event 类型 |
| **Release** | sync 逐 workspace commit 映射 | **编译** state log → 干净 git commit |
| **Numbering** | `_find_next_number` 扫 release repo | 本地 state log 计数器，单调递增 |

### 3.3 探索型 commit 的处理

当前：18 个 fix commit → sync → 18 个 formal commit → 污染发布历史。

State Log 化后：
```
state log seq 1030-1047: 18 条 exploration 条目
  ↓ Gate 判断: exploration 链
  ↓ 不逐条 formalize
  ↓ 当 seq 1060 标记 conclusion=adopted, replaces=1030..1047
  ↓ 自动生成一条 decision 条目:
      "[GITGO-31] decision(dashboard): 放弃 Python Rich，改用 TypeScript Ink"
  ↓ 18 条 exploration + 1 条 decision → 编译 → 1 个 git commit
  ↓ 干净发布，但 state log 保留完整探索链路
```

### 3.4 外部 PR 保护

当前：GitHub PR merge → gitgo sync 覆盖 → PR commit 消失。

State Log 化后：
```
state log 检测到 release repo 有外来 commit（GitHub PR merge）
  ↓ 不是直接覆盖
  ↓ 生成 state log 条目: type=governance, source=external_pr
  ↓ 保留原 PR 的 Author + Committer + Message
  ↓ 二次确认后写入 release repo
  ↓ 不丢失署名
```

---

## 四、git 的角色变化

```
          当前                        State Log 化后
          
  workspace git commit              state log 条目
       │                                 │
       │ sync                            │ 编译
       ▼                                 ▼
  release git commit                release git commit
  （直接搬运）                       （state log 编译产物）
  
  git 被当做状态管理工具             state log 是真正的状态记录
  commit message 是唯一元信息        state log 条目携带完整元信息
  回滚靠 git reset                  回滚靠 state log 反向操作
  历史靠 git log                    历史靠 state log 查询
```

**不是不要 git commit，而是不让它当主角。** git commit 仍然是发布格式——GitHub 需要它、revert 需要它、外部协作者需要它——但它不再承担状态记录的职责。

---

## 五、对 gitgo 功能的启发

从 git 历史清洗 + Dashboard 开发过程中提炼出的 5 条功能改进：

1. **编号改为本地计数** — 不扫 release repo，在 state log 或 `.gitgo/next_number` 里单调递增
2. **sync 前检测外来 commit** — release repo 有非 gitgo 产生的 commit → 警告 + 二次确认（不是硬禁止）
3. **新增 MCP overview 工具** — 轻量版 list_projects，不做 step_scan()，Dashboard 和其他客户端直接用
4. **聚合粒度可配置** — 按 type 分组（incremental 正常 sync，exploration 折叠为 decision）
5. **新增 decision 类型 formal commit** — 记录"为什么换方向"，不是把 18 个 fix 捆成一个

---

## 六、后续设计待定

- StateLogEntry 完整数据模型（字段、索引、查询 API）
- State Log → git commit 编译规则（哪些条目合并、哪些折叠、哪些保留）
- State Log 与现有 history.json 的迁移路径
- State Log 的回滚语义（如何从一条 state log 恢复到之前状态）
- MCP tool `gitgo_state_log` 的设计
