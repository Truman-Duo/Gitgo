# 版本记录

> 格式: v主版本.次版本 (日期)

---

- 12 项目完成, 0 修改中, 0 待归档

## 已知问题 — Windows Terminal CJK IME 输入停止

**现象**：Dashboard 中输入 CJK 字符（中文/日文/韩文）约 19 个字后 IME 停止响应。
光标仍然显示，但 IME 候选窗不再出现，无法继续输入。英文输入不受影响。

**根因**：**Windows Terminal v1.21-v1.22 的 TSF 重写 Bug**（非 gitgo 代码问题）。

- Windows Terminal PR [#17067](https://github.com/microsoft/terminal/pull/17067) 重写了 TSF（Text Services Framework）输入处理
- 该重写导致 IME composition 状态在 ~19 次 composition 周期后损坏，`compositionend` 事件停止触发
- 每个 CJK 字符 = 一次 IME composition 周期（compositionstart → compositionupdate → compositionend），约 19 个周期后 TSF 内部状态溢出或错乱
- 英文不经过 IME composition 生命周期，因此不受影响

**影响范围**：所有 Windows Terminal 上的终端 TUI 应用，包括 Claude Code、OpenCode [#14761](https://github.com/anomalyco/opencode/issues/14761)、Qwen Code、gitgo Dashboard

**gitgo 侧验证**：`TextInput` 的光标位置计算（`wrapText` + `stringWidth` + `useDeclaredCursor`）已经过单元测试验证正确（`test_cursor.ts`），问题不在 gitgo 代码层面。

**解决方案**：用户升级 Windows Terminal 到 **Preview v1.24+**。
- Windows Terminal PR [#19738](https://github.com/microsoft/terminal/pull/19738) 恢复了中日韩 IME 兼容性
- Preview v1.24 changelog 明确列出了 IME bug 修复

**日期**：2026-08-09 诊断确认

---

## 已知问题 — Windows ConPTY Resize 主屏幕重复渲染

**现象**：Windows 上 Dashboard 在主屏幕（非 alt-screen）模式下，resize 终端窗口后出现重复/堆叠渲染——旧视口尺寸的内容残留在上方，新尺寸内容在下方。非 Windows 平台不受影响。

**根因**：ConPTY `ResizePseudoConsole` 在 resize 时会 reflow scrollback 历史，将旧视口内容重新注入可视区域。Ink 主屏幕渲染使用 `\n` 换行，每帧（60fps × 40行）在 scrollback 中产生约 150,000 行/分钟的堆积。ConPTY 的 reflow 发生在所有应用输出之后，不受 ANSI 控制。

相关 Issue：
- [microsoft/terminal#16911](https://github.com/microsoft/terminal/issues/16911) — ConPTY resize reflow
- [microsoft/terminal#19086](https://github.com/microsoft/terminal/issues/19086) — `\x1b[3J` (erase scrollback) broken on WT v1.22+（by design / not planned）

**三种已知解法**：

| 方案 | 说明 | 可行性 |
|------|------|--------|
| A. 延迟重绘 debounce | resize 后等待 ConPTY reflow 完成再重绘 | 时机不可靠，无法确定 ConPTY 何时完成 reflow |
| B. `PSEUDOCONSOLE_RESIZE_QUIRK` (0x2) | 禁用 ConPTY resize reflow | 需由 PTY host（终端模拟器）设置，应用层无法控制 |
| C. Alt-Screen | alt-screen 无 scrollback，ConPTY 无历史可 reflow | **已采用**，Claude Code 同方案 |

**gitgo 侧决策**：选 C。Windows 上默认 `GITGO_ALT_SCREEN=1`（alt-screen），保留 `GITGO_ALT_SCREEN=0` 显式退出开关。

- `GITGO_ALT_SCREEN=1` → 强制 alt-screen（所有平台）
- `GITGO_ALT_SCREEN=0` → 强制主屏幕（包括 Windows，可复现 resize 重复渲染）
- （未设置）→ Windows 默认 alt-screen，其他平台默认主屏幕

实现位置：`cli/dashboard/src/main.tsx`，详见代码注释。

**与 Claude Code 对比**：Claude Code 同样使用 alt-screen，无主屏幕 TUI 支持，无退出开关。gitgo 保留显式开关，不隐藏问题。

详细分析：`cli/dashboard/docs/resize-duplicate-analysis.md`（14 次实验全记录）

**日期**：2026-08-12 决策

---

## v0.35 (2026-07-16)

**Knowledge System 三期 —— 收割/检索/注射/分离/回收 + Testing Infrastructure**

### K1-K3: Knowledge 核心
- `knowledge/models.py` — Lesson 数据模型升级：10 个新字段 (dangerous_tools / prerequisite_tools / required_tools / trigger_count / recent_retrievals 等)
- `knowledge/manager.py` — save_pending 内容哈希去重 + discard_lesson / revert_to_pending / pending_count
- `knowledge/harvest.py` — 信号捕获 + 多维调度算法 (事件密度+来源多样性+批量+冷却) + LLM 总结 + `is_testable_proposition` 门禁 + `auto_discard_invalid`
- Daemon 集成 — workspace_dirty 后自动信号捕获 + harvest 触发 + pending digest 定时

### K4-K5: Retrieval + Recall
- `knowledge/recall.py` — **新建** (290 行) L0 grep + 轻量排序 / L1 多向量语义搜索 / L2 RAG
- `knowledge/embedding.py` — **新建** (56 行) Provider-agnostic EmbeddingProvider
- `filter_by_relevance()` — Per-agent scope 实时过滤
- `record_retrieval()` — 检索持久化 + 热/温/冷分层

### K6: Recycle
- `round_complete` 集成 — 回收信号 + sticky lesson 管理
- 热/温/冷分类 (`classify_lesson_heat`) + sticky cap

### Testing Infrastructure
- `tests/factory/` — **新建** (7 文件, ~800 行) TestDataFactory：种子可复现的通用测试数据生成器
- 跨 Knowledge / Governance / Identity / SyncSession / Contract / Config 子系统
- 501 测试 (从 334 增长 +167)

## v0.34 (2026-07-15)

**系统整合 —— 原生 Task 命令 + 断裂修复 + 迭代规划**

### S1: 原生 Task 命令（架构下沉）
- Daemon 新增 `_handle_command("task")`：整合 LLM 配置解析 + Agent 生命周期 + 治理上下文注入 + agent_step 执行
  - `action: "chat"` — 完整 Agent 编排（fork + context + execute）
  - `action: "fork"` — 仅创建 Agent
  - `action: "status"` — 查询进程树
  - `action: "kill"` — 终止 Agent
- `_resolve_llm_config()` 从 mcp_tools/loop.py 移入 daemon
- `mcp_tools/loop.py` 重构为薄适配器：从 ~360 行缩减到 ~180 行，编排逻辑全部下沉
- `DaemonClient.send_task()` 新增异步 task 通信方法
- RingGate / ToolRegistry 原生化：`ring_level` 作为 task 命令的显式参数，系统自行维护
- MCP 回归设计定位：兼容层（虚线），Agent Loop 自包含

### S2: 断裂修复
- **断裂 1+3**：daemon 治理上下文从单源 → 四源归一化
  - `_do_workspace_scan` 内联代码补 `lessons` / `rejections` / `facts` 三源
  - `derive_facts()` 返回值与 normalize 连通
- **断裂 2**：Gate 缓存 + Dirty Flag
  - PolicyEngine 产出 → HistoryManager `drift_cache` event
  - watcher 文件变更 → dirty=true
  - `ContractDriftGate.check()` 优先读缓存，dirty 时现场检测
  - 系统维护 flag，非 LLM 维护

### S3: 迭代计划
- v0.35：Agent Loop 工具注册完善（3→10+ 工具执行器）
- v0.36：真实 LLM API 端到端验证（Groq）
- v0.37：Git 性能优化（大仓库 + libgit2/pygit2 选项）
- 断裂 4（Lesson Harvest 多触发点）延期至 v0.35
- 手动新建 Agent 进程（人工子 agent 挂靠 A 树下）：**延期** —— 依赖「A 如何接收/治理突然出现的人工子 agent」的 agent 治理设计未定，暂不实现

## v0.33 (2026-07-07)

**三个 P0 Bug 修复 —— 外审回应**

### E1: HistoryManager 并发安全
- JSONL 逐行追加写入（O(1) per add）+ `threading.Lock`
- 向后兼容旧 JSON 数组格式
- 超过 400 条触发 compact 保留 200 条

### Gate A/B 可插拔化
- 新增 `backend/core/policy/gates.py`：`SyncGate` ABC + 3 内置 Gate
  - `ForeignCommitGate` / `ContractDriftGate` / `PrivacyScanGate`
- `load_gates()` 从 contract.yaml 加载配置
- `step_sync` / `step_push` 改为遍历 Gate 列表

### Fact 时间窗口
- `consecutive_policy_warnings`: ≥3 within 1h
- `rejection_chain`: ≥3 within 24h
- `burst_formalize`: ≥5 within 1h
- `repeated_contract_drift`: ≥5 within 24h

## v0.32 (2026-07-07)

**6 份完全透底技术报告**
- `docs/technical-reports/01-agent-loop.md` — agent_step 全流程 + LLMProvider + RingGate + ContextWindow + SignalBus/Harness
- `docs/technical-reports/02-daemon-dispatch.md` — 三线程架构 + workspace_dirty 链路 + DaemonClient 协议 + ToolDispatcher
- `docs/technical-reports/03-policy-governance.md` — PolicyEngine 四检查 + HistoryManager 事件溯源 + Fact 推导 + Governance 度量
- `docs/technical-reports/04-syncsession-operations.md` — 24 step 状态机 + Gate A/B 链 + compare_files 算法 + 适配器工厂
- `docs/technical-reports/05-knowledge-identity-authorship.md` — harvest 四源 + Lesson 三层 + Identity 三规则 + AI 痕迹清洗
- `docs/technical-reports/06-external-interfaces.md` — 47 MCP 工具 + 21 CLI 命令 + Dashboard 18 组件 + IME + Ink 管线

## v0.31 (2026-07-05)

**Dashboard 颜色修复 + IME 中文输入 + 前端模块化 + 文档刷新**
- CommandBar 标签颜色修正（COMMAND=绿, NORMAL=蓝）
- `useDeclaredCursor` IME 物理光标定位
- 模块化拆分：`types.ts` / `ToolCallDisplay.tsx` / `usePoll.ts`
- 12 个文件归档 + README 架构图重写（Agent Loop 中心 + MCP 虚线兼容层）

## v0.30 (2026-07-05)

**Dispatch Layer + LLM Provider 配置面板 — A→B Agent 通路真实化 + Ink 终端 Provider 管理**

### D1: Dispatch Layer（MCP → Daemon 真实通路）
- `backend/core/daemon/client.py` — **新建** (~200 行) DaemonClient：subprocess-based daemon 通信
  - `subprocess.Popen` stdin/stdout pipe，后台 reader 线程
  - `threading.Event` response routing，支持 command + async llm_call
  - CWD = project_root.parent 确保 `python -m gitgo` 可发现模块
- `mcp_tools/daemon_registry.py` — **新建** (~40 行) 单例 DaemonClient 缓存 + atexit shutdown
- `mcp_tools/loop.py` — **重写** (~280 行) 三个 A→B 工具全部走 daemon 真实通路
  - `_chat_via_daemon` + `_resolve_llm_config` 配置优先级链
  - `_chat_fallback` Mock 保留作为最后兜底
- `mcp_server.py` — 新增 SIGTERM/SIGINT signal handler 清理 daemon

### D2: LLM Provider 配置系统
- `backend/core/llm_config.py` — **新建** (~130 行) LLMConfigManager：JSON 文件 CRUD
  - `.gitgo/llm_config.json` 存储，支持多 Provider + active_provider 切换
  - API key 在 MCP 响应中自动 mask（前 4 + *** + 后 4）
- `mcp_tools/llm_config.py` — **新建** (~130 行) 4 个 MCP 工具
  - `gitgo_llm_status` / `gitgo_llm_save` / `gitgo_llm_switch` / `gitgo_llm_delete`
- `mcp_tools/__init__.py` — 注册 llm_config 模块 + atexit daemon 清理

### D3: Ink Dashboard LLM 配置面板
- `cli/dashboard/src/hooks/useLLMConfig.ts` — **新建** (~90 行) React hook 封装 MCP 调用
- `cli/dashboard/src/components/LLMConfigPanel.tsx` — **新建** (~290 行) Ink 终端 UI
  - 列表模式：Provider 卡片 + ●/○ 激活标记 + API key 遮罩
  - 编辑模式：内联表单 + Tab/Shift+Tab 字段切换 + Enter 保存
  - 连接测试：调 gitgo_agent_chat 验证连通性
- `cli/dashboard/src/components/App.tsx` — 新增 `llm_config` scene + L 键快捷入口 + `:llm` 命令
- `cli/dashboard/src/state/store.ts` — 新增 `previousScene` 状态（返回导航）
- `cli/dashboard/src/commands.ts` — 新增 `llm` 命令

### D4: 模块结构优化
- `backend/core/cache/` — 文件哈希缓存（file_hash.py）
- `backend/core/dispatch/` — ToolDispatcher 命令分发（dispatcher.py）
- `backend/core/fact/` — 模式匹配（contract/file/workflow patterns）
- `backend/core/loop/` — Agent 循环（context_builder / gate / llm / manager / models / tools）
- `backend/core/policy/` — Policy Engine 可插拔（base / contract / dependency / identity / lessons / registry）
- `backend/core/steps/` — 纯函数管线（commits / scan / sync）

### Bug 修复
- `daemon/__init__.py`: UnboundLocalError — `evq` 初始化移至 `daemon_ctx` 引用之前
- DaemonClient CWD: `project_root` → `project_root.parent` 修复 "No module named gitgo"

### 认证
- 334 passed, 1 skipped（零回归）

---

## v0.29 ( 2026-06-17)

**StateLog Governance Loop — Policy Engine 可插拔 + Dashboard Governance Tab + 链式依赖 + 增量扫描 + 全链路解耦**

### P1: Policy Engine 可插拔架构
- `backend/core/policy/` — 7 文件，330 行
  - `PolicyCheck` ABC + 4 策略：LessonTrigger / ContractDrift / IdentityIntegrity / DependencyChain
  - `PolicyEngine.run(session, project)` — loop 唯一入口
  - `registry.py` — 项目级策略配置（contract.yaml `policy_checks`）+ `register_check()` 自定义扩展
  - 条件 harvest：`should_harvest()` + `run_harvest_if_needed()`
- Daemon 从 4 个硬编码函数调用改为 `engine.run()`，删 ~200 行

### P2: Steps 纯函数管线
- `backend/core/steps/` — 4 文件，200 行
  - `scan_and_compare()` / `scan_incremental()` / `load_workspace_commits()` / `create_formal_commit()` / `sync_files()` / `push_to_remote()`
  - 零依赖 SyncSession 对象，loop 可直接调用

### P3: Daemon 治理链路
- Policy Engine 三步检查（lesson trigger 匹配 + contract drift + identity integrity）+ 第四步 dependency chain
- workspace_dirty 去抖 + 增量扫描（watchdog 传 changed_files → `step_scan_files`）
- `workspace_state_snapshot` git commit（`round_complete` stdin 命令）
- Rejection 系统：`reject` stdin 命令 → rejection_count ≥ 3 → lesson harvest
- HistoryManager per-project 隔离（`.gitgo/gitgo_history.json`）

### P4: Dashboard CLI 升级
- Governance Tab（第 4 Tab）— 实时展示 policy_check_result / drift / snapshot / rejection
- Tab 跳底修复（L2 容器固定高度 + margin 移除）
- 命令补全（↑↓ 选 + Tab 填）
- 三级导航（Overview → Tab → 条目详情）
- CC Skill `/gitgo-check`（`.claude/skills/gitgo-check.md`）

### P5: 链式依赖检测
- `build_dep_graph()` — 正则提取 Python import 构建反向依赖图
- `get_dependents()` — 查文件被哪些文件引用
- Policy Engine `DependencyChainCheck` — 自动标记受影响文件

### P6: 修复 + 清理
- `_find_next_number` 本地计数器（`.gitgo/next_number`）
- Lesson harvest 文件类型过滤（排除 .spec/.json/.txt/version.md 等噪音文件）
- Lesson harvest 去重（生成前检查已有 pending trigger）
- Identity guard 误报修复（mass_override ≥3 / structure_collapse ≥2）
- Contract 数据清理（27→4 features + 4 constraints + tech_stack）
- MCP `gitgo_overview` + `gitgo_governance_feed`
- 外来 commit 检测（`step_sync` 前对比 release HEAD）
- `daemon.bat` 一键启动

### 认证
- 334 passed, 1 skipped（零回归）
- lexi workspace daemon 端到端验证通过
- Dashboard Governance Tab 实时展示通过

### 解耦状态
| 模块 | 行数 | 耦合度 | loop 可用 |
|---|---|---|---|
| `policy/` | 330 | 低 | ✅ 直接调 |
| `steps/` | 200 | 低 | ✅ 直接调 |
| `daemon/` | 610 | 中 | ⚠️ |
| `sync_session.py` | 1315 | 中 | ⚠️ |
| `mcp_server.py` | 920 | 中 | ⚠️ |

---

## v0.28 (2026-06-13)

**Dashboard 异构重写 — Python Rich → TypeScript Ink。**

### D1: gitgo-dashboard 新建
- 技术栈：TypeScript + Bun + @anthropic/ink (React for terminals) + MCP stdio
- 目录：`gitgo-dashboard/` 独立项目，7 源文件 + 1 构建脚本，~600 行
- 构建产物：`dist/cli.js` 548KB 单文件
- 运行：`bun run src/main.tsx [refresh_seconds]`

### D2: 架构对比

| 维度 | 旧 (Python Rich) | 新 (TypeScript Ink) |
|---|---|---|
| 渲染方式 | Rich Live 整屏重写 | Ink React reconciler 逐行 diff |
| 键盘输入 | msvcrt 轮询/阻塞 | Node.js event-driven 非阻塞 |
| stdin/stdout | 同一线程竞争 → 卡死 | event loop 自然解耦 → 零竞争 |
| 布局 | Rich Panel/Layout | Yoga FlexBox |
| 数据获取 | 直接读本地 JSON 文件 | MCP stdio 协议（无文件锁冲突） |

### D3: 命令栏
- 光标编辑：← → Home End Backspace Delete + 任意位置插入
- 粘贴支持：bracketed paste 一次插入全部字符
- 历史翻页：↑↓ 翻历史命令，到头按↑退出命令模式
- 焦点模型：table ↔ command 双向焦点切换（↓到底 → 命令栏，↑到顶 → 表格）
- 反馈内嵌：命令执行结果显示在命令栏边框内

### P0 待执行（仅记录，不执行代码）
- **Git 记录清洗**：整理 git 历史 + GitHub 提交记录重新梳理（2026-06-11 记录，明日执行）

---

## v0.27 (2026-05-29)

**Runtime Constitution + Architecture Formalization。** 不扩功能，显式化已有结构。

### R1: Runtime Constitution 补完
- `docs/RuntimeConstitution.md` — 8 条根本规则（从 v0.26 的 4 条扩展）
  - 新增: Layer Mutation Rules / Gate Extension Policy / Semantic Reversibility / Event Taxonomy
- `docs/RuntimeDiscipline.md` — 退役，替换为链接

### R2: 三层状态机显式化
- `sync_session.py` — module docstring 声明 Operational State Machine
- Constitution 文档声明 Semantic State Machine + Governance State Machine

### R3: 命名修正
- release = Canonical Release Space（非"备份区"）
- 代码变量不改（`backup_path` 嵌入过多引用）

### 认证
- 334 passed, 1 skipped（零功能变更，零回归）

---

## v0.26 (2026-05-29)

**Runtime Discipline — 硬约束声明 + discipline validation lint。**

### D1: Runtime Discipline 文档
- `docs/RuntimeDiscipline.md` — 4 条硬约束
  - Canonical Events / State Authority / Derivation Rules / Observer Constraint

### D2: Discipline Validation
- `state_reader.py` — `_validate_discipline()` + `validate_semantic_consistency()`
- `--mode status --json --layered` 自动执行，输出 `_discipline_warnings`

### 认证
- 334 passed, 1 skipped（零回归）
- GitHub 三仓验证：discipline check 0 warning

---

## v0.25 (2026-05-29)

**State Convergence — 全局 Runtime 化。不扩功能，只收拢。**

### C1: Governance Event Completeness
- `sync_session.py` — 9 个 governance event 写入点
  - `governance_synced` / `governance_pushed` / `governance_dissolved`
  - `governance_edited` / `governance_renumbered`
  - `governance_drift` / `governance_contract_updated`
  - `governance_lesson` / `governance_memory_snapshot`
- 所有 governance state 变更现在有不可变 event log 记录

### C2: Three-Layer State Distinction
- `status_dict(layered=True)` — 三层显式输出
  - `operational`: stage / entries
  - `governance`: formal counts / trial / contract
  - `semantic`: entropy / next_action / action_queue
- CLI: `--mode status --json --layered`
- MCP: `gitgo_status(project, layered=True)`
- 旧 `--json` 输出格式不变

### C3: Unified State Query
- `backend/core/state_reader.py` — StateReader 统一查询接口
  - `get_formal_commits` / `get_contract` / `get_lessons`
  - `get_integrity_warnings` / `get_memory_snapshots`
  - `get_governance_events`
- 6 个方法，零新依赖

### 认证
- 334 passed, 1 skipped（零回归）
- GitHub 私有仓库 `gitgo-integration-test` 三仓验证通过
- governance/quality + governance/patterns 从 event log 完整推导

---

## v0.24.1 (2026-05-26)

**全量 Bug 修复 + 回归测试 + 三仓集成验证**

### Bug 修复（6个，全量回归）
- B1: `IncomingChange.date` → `.timestamp`（`cli/commands.py`）
- B2: `Config.get_project()` 不存在 → 手动遍历（`cli/commands.py`）
- B3: `CommitInfo.commit_type` → `.type`（`cli/commands.py`）
- B4: `_cmd_push` 跨进程 session 丢失 → `load_session` 恢复（`cli/commands.py`）
- B5: Trial accept cherry-pick 冲突 → `-X theirs` 自动重试（`sync_session.py`）
- B6: `_find_next_number` 编号不递增 → `\[PREFIX-\d+\]` 模式修复（`git.py`）

### 新增测试
- `tests/test_regression.py` — 12 个回归测试（B1-B6 + 字段名审计）
- `tests/test_self_referential.py` — 8 个自指流程测试（EOL / hash / compare_files）
- 总测试: 334 passed, 1 skipped (+12 over v0.24)

### Bootstrap 自举命令
- `--mode bootstrap` — 一键注册 gitgo 自身项目配置
- 自动检测 workspace/release 路径 + 推断下一个编号

### 换行符归一化
- `compare_files(normalize_eol=True)` — CRLF/LF 不再误报 modified
- `_hash_file(normalize_eol=True)` — 流式替换 `\r\n` → `\n`

### 三仓集成测试
- 真实 GitHub 私有仓库 `Truman-Duo/gitgo-integration-test`
- workspace → release → trial 完整流程验证
- 测试报告: `Desktop/gitgo_test_ws/TEST_REPORT.md`

### 测试日志
- `docs/bootstrap_test.log` — 自指流程详细日志
- 记录每一步的命令、输出、问题、修复

---

## v0.24 (2026-05-19)

**Authorship + Drift Detection + Lesson System — v0.24 设计全部实现**

### Authorship 过滤（著作权管理）
- `backend/core/authorship.py` — push 前 AI 痕迹清洗
  - commit message: 去除 Co-authored-by / Generated with 等模式
  - 激进模式: 去除代码中的 AI 生成注释
  - AI 配置文件排除: CLAUDE.md / .claude/ / .codex/ / .cursor/ 等
- CLI: `--mode push --strip-authorship [--aggressive]`
- MCP: `gitgo_push` 新增 strip_authorship / aggressive 参数
- 配置: `authorship.mode` / `strip_commit_coauthors` / `exclude_tool_configs`

### Drift Detection（漂移检测 + Project Contract）
- `backend/core/contract.py` — ProjectContract + ContractManager + detect_drift
  - 功能删除检测: decided_feature 文件消失或签名丢失
  - 技术栈漂移: 新增未声明 import
  - 架构违反: architecture_constraints 被新代码打破
- sync 成功后自动更新合约（confirmed_count +1）
- push 前自动漂移检测（展示告警）
- CLI: `--mode contract`
- MCP: `gitgo_contract_show` / `gitgo_contract_update`

### Lesson 系统（知识传承）
- `backend/core/knowledge/lesson.py` — Lesson 数据类 + LessonManager + harvest_lessons
  - 抽象层（跨项目通用）+ 实例层（单项目具体）
  - JSONL 格式，一行一条
  - sync 成功后自动收割（同一文件反复修改3+次 → pending lesson）
  - verify / search / promote_to_abstract
- CLI: `--mode lesson`
- MCP: `gitgo_lesson_list/verify/search/promote`（4 tools）

### CLI 拆分
- `cli/commands.py` 1397行 → 863行（核心工作流）
- `cli/commands_ext.py` 新建 573行（扩展功能）
- CLI modes: 20 → 22

### 测试
- tests/test_authorship.py — 21 个
- tests/test_contract.py — 15 个
- tests/test_lesson.py — 13 个
- pytest: 314 passed, 1 skipped (+49 over v0.23)

---

## v0.23 (2026-05-19)

**Identity Guard — 项目环境完整性保护**

事故驱动设计：CC 覆盖项目文件夹 → LLM 看不到"项目身份" → 需要 runtime 层约束。

### Layer 1: Integrity Detection
- `backend/core/identity/guard.py` — **新建** — 三条检测规则
  - `_detect_mass_override` — 全量覆盖检测（阈值 0.80，可配置）
  - `_detect_identity_file_deletion` — 身份文件删除告警（CLAUDE.md/.claude/.codex 等）
  - `_detect_structure_collapse` — 目录骨架崩塌检测（Jaccard < 0.3）
  - `_save_directory_skeleton` — sync 成功后自动写入基线
- `backend/core/config.py` — `DEFAULT_INTEGRITY_CONFIG` + `integrity` 字段（项目级可配置）
- `sync_session.py:step_scan()` — 插入 integrity checks，警告写入 HistoryManager
- `sync_session.py:step_sync()` — 成功后自动 snapshot + skeleton save

### Layer 2: Memory Snapshot
- `backend/core/identity/snapshot.py` — **新建**
  - `snapshot_tool_memories` — .claude/.codex/.codebuddy 增量快照到 backup
  - 首次全量 copytree，之后 filecmp 增量拷贝
  - 保留最近 5 次，旧快照自动清理
- CLI: `--mode memory --memory-action snapshot/restore/list`
- MCP: `gitgo_memory_snapshot/restore/list`（3 tools）

### Layer 3: Identity Bundle
- `state_bundle.py` — `collect_state_bundle()` 新增 `include_identity=True`
  - 目录骨架 + 身份文件状态 + 工具记忆摘要
- CLI: `--mode export --include-identity`
- MCP: `gitgo_export` 新增 `include_identity` 参数

### 测试
- `tests/test_identity_guard.py` — 18 个测试
- pytest: 265 passed, 1 skipped (+18 over v0.22)

---

## v0.22 (2026-05-17)

**模板系统 + CLI/MCP 补齐 — Phase 6 + Phase 5.2 完结**

### 模板系统（Phase 6 核心）
- `backend/core/template_manager.py` — **新建** — `CommitTemplate` 数据类 + `TemplateManager` 持久化到 `commit-config.json`
  - 格式变量: `{prefix}` `{number}` `{type_str}` `{scope_str}` `{subject}` `{project_name}` `{commit_count}` `{commit_list}`
  - `prefix_override` 支持模板覆盖项目 prefix
  - 内置默认模板与旧硬编码输出逐字一致（向后兼容）
- `backend/core/config.py` — `commit_format` 新增 `template_name: "default"` 键
- `backend/core/operations/git.py` — `build_commit_template()` 接受 `template_name` 参数，用 `str.format()` 填充
- `backend/core/sync_session.py` — `step_create_formal_commit()` 透传 `template_name`
- CLI: `--mode template` + `--template-action list/add/edit/delete`
- CLI: `--mode formalize --template <name>` 选择模板
- MCP: 4 个模板工具（list/add/edit/delete）+ template 参数 on formalize/run_workflow

### CLI 补齐（6 个 formal 管理操作）
- `--mode formal --formal-action list/delete/edit-message/edit-number/dissolve/clear-sources`
- `cli/commands.py` — `_cmd_formal()` + `_cmd_template()`
- CLI modes: 17 → **19**

### MCP 补齐（16 个新工具）
- Formal 管理: `gitgo_formal_list/delete/edit_message/edit_number/dissolve/clear_sources`（6）
- Release: `gitgo_release_info` / `gitgo_release_create`（2）
- `gitgo_history` / `gitgo_session` / `gitgo_export`（3）
- `gitgo_remote_issues`（1）
- Template: `gitgo_template_list/add/edit/delete`（4）
- MCP tools: 17 → **33**

### SMB 适配器 + GitHub/GitLab Issue/PR（Phase 6 补齐）
- `backend/adapters/smb_file_adapter.py` — **新建** — UNC 路径访问，工厂接线
- `backend/remote/github.py` — `list_issues()` / `create_pr()` 实现
- `backend/remote/gitlab.py` — `list_issues()` / `create_pr(MR)` 实现
- `backend/remote/connector.py` — ABC 改为抽象方法

### 测试
- `tests/test_template_manager.py` — 13 个测试
- `tests/test_smb_adapter.py` — 13 个测试
- `tests/test_remote.py` — +15 个测试（issue/PR/ABC）
- pytest: 247 passed, 1 skipped (+41 over v0.21)

---

## v0.21 (2026-05-16)

**P5：Protocol & Ecosystem**

### P5-A.0: 事件名归一化
- `cli/commands.py` — `sync`/`scan`/`push` 流式事件统一为 `operation_started` / `operation_complete`
- 修改 6 处事件名字符串

### P5-A: Protocol Specification
- `docs/Gitgo_Protocol_v1.0.md` — **新建** — 六种 schema 的统一协议规范
  - State / Operation / Stream / Daemon / Suggestion / Governance
  - 覆盖全部 17 MCP tools + 16 CLI modes + daemon 8 command/10 event
  - 版本化策略（additive = 不 bump，removal = major bump）
- `docs/AI_Protocol.md` — 内容迁移，替换为链接

### P5-A.1: Protocol Schema 校验测试
- `tests/test_protocol_schema.py` — 11 个 schema 校验测试（status/quality/patterns/graph/releases/stream/errors）

### P5-B: Reference Agent
- `examples/agent_loop.py` — ~200 行参考 agent，subprocess CLI 调用，Human-in-the-Loop

### P5-C: Plugin API Formalization
- `docs/Plugin_API.md` — 8 个 hook 完整 API 文档 + 2 层搜索路径 + 开发约定
- `plugins/slack_notify.py` — Slack 通知参考插件（`on_sync_complete` / `on_push_complete`）
- `plugins/jira_link.py` — Jira 关联参考插件（`on_commit_select`）

### P5-D: State Bundle
- `backend/core/governance/state_bundle.py` — `collect_state_bundle()` collector 函数
- `cli/commands.py` — `_cmd_export` verb + `state-bundle` 子动作
- `__main__.py` — `--mode export --export-type state-bundle --minimal`
- `tests/test_state_bundle.py` — 6 个测试
- `docs/Gitgo_Protocol_v1.0.md` — State Bundle 附录

### 认证
- pytest: 206 passed, 1 skipped (17 new)

### P5 完成
- Gitgo 从 "可被 agent 调用的工具" 升级为 "有正式接口契约的运行时标准"
- 协议规范是第三方集成的唯一入口 — 不需要读代码
- agent_loop.py 证明协议完整性 — 任何语言都能实现同样循环
- Plugin API 文档 + 3 个参考插件让第三方可扩展 Gitgo
- State Bundle 让治理状态可脱离 Gitgo 实例存在

---

## v0.20 (2026-05-16)

**P4-D：发布推理**

### 新模块
- `backend/core/governance/releases.py` — 发布推理引擎
  - `list_releases(project_name)` — 从 push 记录构建发布列表（按时间倒序），每项含 pushed_at / commits / reason
  - `add_release_note(project_name, message)` — 为最新 push 记录写入 release_note
- `tests/test_releases.py` — 10 个测试

### CLI
- `cli/commands.py` — `_cmd_governance` 新增 `releases` + `release-note` 子动作 + `_print_releases`
- `release-note` 要求 `--message`，缺失时报错 exit(1)

### MCP
- `mcp_server.py` — `gitgo_governance_releases(project)` + `gitgo_governance_release_note(project, message)` tools（17 tools 总计）

### 认证
- pytest: 189 passed, 1 skipped (10 new)

### P4 完成
- P4 (Governance Layer) — P4-Pre/A/B/C/D 全部完成
- 治理层具备完整能力：质量度量 + 模式检测 + 语义变更图 + 发布推理
- MCP 17 tools, CLI 4 governance sub-actions

---

## v0.19 (2026-05-14)

**P4-C：语义变更图**

### 新模块
- `backend/core/governance/graph.py` — 语义变更图构建器
  - `build_graph(project_name)` — 从 formalize/triage_accept/push 记录构建 nodes + edges
  - 节点：formal（来自 formalize，含 files_changed）+ incoming（来自 triage_accept）
  - 边：file_overlap (Jaccard≥0.3) / same_push (batch push commits) / trial_source (correlation_id 匹配)
- `tests/test_graph.py` — 13 个测试

### CLI
- `cli/commands.py` — `_cmd_governance` 新增 `graph` 子动作 + `_print_graph`

### MCP
- `mcp_server.py` — `gitgo_governance_graph(project)` tool（15 tools 总计）

### 认证
- pytest: 179 passed, 1 skipped (13 new)

---

## v0.18 (2026-05-14)

**P4-B：变更模式检测**

### 新模块
- `backend/core/governance/patterns.py` — 变更模式检测引擎
  - `detect_co_changing()` — 共变模块（从 formalize files_changed 提取跨目录配对）
  - `detect_type_clusters()` — commit 类型聚类（类型分布 + 多源合并率）
  - `detect_trial_impact()` — trial 后续影响（accept 后触发 workspace 变更概率，按 correlation_id 关联）
  - `build_patterns_report()` — 聚合三种检测器
- `tests/test_patterns.py` — 16 个测试

### CLI
- `cli/commands.py` — `_cmd_governance` 新增 `patterns` 子动作 + `_print_patterns`

### MCP
- `mcp_server.py` — `gitgo_governance_patterns(project)` tool（14 tools 总计）

### 认证
- pytest: 166 passed, 1 skipped (16 new)

---

## v0.17 (2026-05-13)

**P4-A：建议质量度量**

### 新模块
- `backend/core/governance/__init__.py` — 治理层门面
- `backend/core/governance/quality.py` — 建议质量度量引擎
  - `load_suggestion_pairs()` — 提取 suggest_* 条目，按 correlation_id 匹配执行记录
  - `compute_quality_metrics()` — 采纳率/修改率/拒绝率（仅 indices Jaccard）
  - `group_by_commit_type()` / `group_by_module()` — 维度切片
- `tests/test_quality.py` — 20 个测试

### CLI
- `cli/commands.py` — `_cmd_governance` verb + quality 子动作
- `__main__.py` — `--mode governance` + `--governance-type` (quality/patterns/graph/releases/release-note)
- `cli/__init__.py` — export `_cmd_governance`

### MCP
- `mcp_server.py` — `gitgo_governance_quality(project)` tool（13 tools 总计）

### 设计决策
- 仅用 indices Jaccard 重叠度（≥0.8 accepted, ≥0.3 modified, <0.3 rejected）
- 不做 message 文本相似度比较
- correlation_id 匹配 + add_suggestion 直存两种模式

### 认证
- pytest: 150 passed, 1 skipped (20 new)

---

## v0.16 (2026-05-13)

**P4-Pre：数据基础增强**

为 P4 Governance Layer 的分析功能补齐数据基础：

### correlation_id
- `backend/core/history.py` — `HistoryEntry` 新增 `correlation_id: str = ""` 字段（向后兼容）
- `HistoryManager.add_operation()` / `add_suggestion()` / `add_entry()` 新增 `correlation_id` 参数
- `backend/core/sync_session.py` — `__init__` 生成 `self._correlation_id = str(uuid.uuid4())`；全部 9 处 history 调用传入

### Batch Push
- `step_push()` 改为批量推送：一次推送所有 `synced=True, pushed=False` 的 formal commit
- push history detail 格式变更：`{"commit": "..."}` → `{"commits": ["[PREFIX-1]", "[PREFIX-2]"]}`
- 支持 P4-C same_push 边和 P4-D 多 commit 发布单元

### files_changed in formalize detail
- `step_create_formal_commit()` 的 history detail 新增 `files_changed` 列表
- P4-C graph builder 可直接读取变更文件，无需反查 scan 记录

### 设计文档
- `docs/iterations/Phase4_GovernanceLayer.md` — **审阅修订版** P4 设计（去掉 `_messages_similar`、新增 P4-Pre、batch push、correlation_id、P4-D 命名修正）
- `docs/P4执行计划.md` — P4 分阶段执行任务（P4-Pre → P4-D）

### 认证
- pytest: 130 passed, 1 skipped
- 全部向后兼容

---

## v0.15 (2026-05-13)

**Phase 5 RemoteConnector + Phase 3 AI-Augmented Workflow**

### Phase 5：RemoteConnector（GitHub/GitLab API）

- `backend/remote/gitlab.py` — GitLab REST API v4 连接器（`get_repo_info` / `create_release`）
- `backend/remote/github.py` — URL regex 修复（前缀锚定防误匹配）
- `backend/remote/__init__.py` — `create_connector` 工厂支持 `kind="gitlab"` + `GITLAB_TOKEN` 环境变量
- `backend/core/sync_session.py` — `step_create_release()` 从最新 pushed formal commit 自动生成 tag
- `cli/commands.py` — `_cmd_release` CLI verb（get-info / create-release）
- `__main__.py` — `--mode release` + `--release-action`/`--tag`/`--release-name`/`--release-body`
- `tests/test_remote.py` — 28 个测试（URL 解析 + 工厂 + mock API）

### Phase 3：AI-Augmented Workflow（P3-A → P3-D）

**P3-A：基础设施**
- `backend/core/plugin.py` — 第 8 个 hook `on_triage_recommend`
- `backend/core/plugin_loader.py` — `on_triage_recommend` 分发（合并去重）
- `backend/core/history.py` — `add_suggestion()` 记录 ai_proposal vs human_decision
- `cli/commands.py` — `_cmd_suggest` + 3 个 context builder（formalize/triage/summary）
- `__main__.py` — `--mode suggest --suggest-type formalize|triage|summary`
- `docs/AI_Protocol.md` — context / suggest / error 三种 JSON schema 规范
- `mcp_server.py` — 3 个 suggest MCP tool（12 tools 总计）

**P3-B：Commit Proposal**
- `backend/core/operations/diff.py` — `get_diff_summary()` 文件级轻量统计（行数+顶层符号，不含行级 diff）
- `tests/test_diff.py` — 5 个测试（新文件/修改/多文件/空仓库/符号上限）

**P3-C：Triage Recommendation**
- `_build_triage_context` 含 `files_changed` diff 统计 + `release_context`

**P3-D：Change Summary**
- `_build_summary_context` workspace/trial/release 三段统计

### 设计文档
- `docs/iterations/Phase3_AIAugmentedWorkflow.md` — 修订版 P3 设计（合并 P3-B/C，明确定义 diff_summary，增加 rejection 记录）
- `docs/iterations/Phase3_AI_Augmented_Workflow.md` — 原始 P3 参考
- `docs/P3执行计划.md` — 分阶段执行任务

### 认证
- pytest: 130 passed, 1 skipped
- build: 54.3 MB
- 现有 7 个 hook + `auto_merge` 插件行为不变

---

## v0.14 (2026-05-13)

**项目文件重组 + PanelState 显式化 + 技术债消除**

### 项目文件重组（两层架构）

根目录 18 个 `.py` 文件 + 10 个子目录 → 引擎层 / 接口层分离：

| 移动 | 源 → 目标 |
|------|-----------|
| 8 文件 | 根目录 `config.py`/`i18n.py`/`history.py`/`plugin.py`/`plugin_loader.py`/`migrate.py` → `backend/core/` |
| 2 目录 | `core/` → `backend/core/`（sync_session + daemon + operations） |
| 3 目录 | `adapters/` / `models/` / `remote/` → `backend/adapters/` / `backend/models/` / `backend/remote/` |
| 2 文件 | `gui_main.py` / `debug_entry.py` → `frontend/` |
| 1 文件 | `cui_main.py` → `cui/main.py` |
| 1 文件 | `debug_launcher.py` → `scripts/` |

根目录保留 5 文件：`__init__.py` / `__main__.py` / `build.py` / `mcp_server.py` / `requirements.txt`

导入全统一为 `from backend.xxx import`，~45 文件受影响的 import 全部更新。旧 `backend/` 目录已溶解到新 `backend/core/` 中。

### PanelState 显式化（UI 交接）

**动机**：WorkspacePanel 10 个 Mixin 间通过 `self.xxx` 隐式共享 39+ 个属性，新开发者无法判断属性的 Producer/Consumer。

**方案**：提取 `frontend/workspace/panel_state.py` — `PanelState` 类，所有跨 Mixin 属性集中定义并标注 P/C：

```python
self.state.config: Config | None = None        # P: panel, C: all
self.state.selected_formal: int | None = None  # P: panel.init, C: commits/syncpush/trial
self.state._merging: bool = False              # P: commits, C: commits
# ... 39+ attributes total
```

访问规范：Mixin 内通过 `self.state.xxx` 访问共享属性，局部属性仍用 `self._xxx`。

### 技术债消除

| 问题 | 修复 |
|------|------|
| `__main__.py` line 140/147 旧路径 import | `from frontend.gui_main import` / `from cui.main import` |
| `cli/commands.py` `_cmd_sync` 绕过 SyncSession | 委托 `SyncSession.run_full_workflow(skip_push=True)` |
| `backend/core/sync_session.py` `datetime` 影子变量 | 删除 `discard` 路径内的 `from datetime import datetime`（模块级已导入） |
| `docs/VERSION.md` 旧路径引用 | 全部更新为 `backend/core/` 等新路径 |
| `frontend/workspace/commits.py` 错误 import | `from backend import submit_commit_message` → `from backend.core.submitter import` |

### 认证

- pytest: 97/97 passed, 1 skipped
- headless: `verify_headless.sh` 11/13 passed（2 个为测试环境 pre-existing）
- build: `python build.py` → `dist/gitgo.exe` (54.3 MB)
- import: `from frontend.workspace import WorkspacePanel` OK

### 修改文件

- `backend/core/sync_session.py` — 移除 datetime 影子导入
- `cli/commands.py` — `_cmd_sync` 重写（113→43 行）
- `frontend/workspace/panel_state.py` — **新建**（39+ 属性 PanelState 类）
- `frontend/workspace/panel.py` — `self.state = PanelState()`，全部方法更新
- `frontend/workspace/*.py` — 10 个 Mixin 文件全部 `self.xxx` → `self.state.xxx`
- `docs/VERSION.md` — 更新旧路径引用
- `docs/HANDOFF.md` — 更新文件地图 + 新增 PanelState 章节
- `docs/CLAUDE.md` — 更新模块布局

---

## v0.13 (2026-05-13)

**Phase 2 Agent-Ready Runtime — P2-A + P2-B + P2-C 完成**

### P2-A: Semantic State + Streaming

**`status_dict()` 扩展：**
- 新增 `semantic` 参数（默认 `True`），控制是否附加语义块
- 新增 `_build_semantic_layer()` 方法，计算 `workspace_entropy` / `suggested_next_action` / `action_queue` / `blocked_reason`
- `semantic=False`（`--raw`）与 Phase 1 完全兼容

**新 CLI 标志：**
- `--raw` — 仅输出原始计数（不含 semantic 块）
- `--semantic-only` — 仅输出 semantic 块
- `--stream` — 流式输出 line-delimited JSON 进度（支持 scan/sync/push/daemon）

**附带修复：**
- `save_session()` 持久化 `is_incoming` + `sources_cleared`
- `load_session()` 恢复 `is_incoming` + `sources_cleared`

**修改：** `backend/core/sync_session.py` (+60行) / `cli/commands.py` (+40行) / `__main__.py` (+20行)

### P2-B: Unified Operation History

**HistoryManager 重构：**
- `HistoryEntry` 新增 `operation` / `status` / `detail` 字段（旧字段保留向后兼容）
- `add_operation()` 新 API — 记录任意操作类型，带结构化 detail
- `add_entry()` 委托到 `add_operation("sync", ...)` — 旧调用点零改动
- 记录上限从 100 扩展到 200 条

**9 种操作类型全部覆盖：** scan / formalize / sync / push / triage_accept / triage_promote / triage_discard / delete_formal / dissolve_formal

**历史写入点：** `step_scan()` / `step_create_formal_commit()` / `step_sync()` / `step_push()` / `step_triage_incoming()` / `step_delete_formal()` / `step_dissolve_formal()`

**CLI 扩展：** `--mode history --json` / `--project` / `--op` / `--limit` 过滤器 + 人类可读中文模式

**修改：** `backend/core/history.py` (重写, 110行) / `backend/core/sync_session.py` (+30行) / `cli/commands.py` (+80行) / `cli/__init__.py` / `__main__.py`

### P2-C: Persistent Daemon Core

**架构：** 纯线程 (3 后台线程 + 主循环)，`queue.Queue` 事件分发

| 组件 | 位置 | 功能 |
|------|------|------|
| WorkspaceWatcher | `backend/core/daemon/watcher.py` | watchdog 文件监控 + 2s 去抖 |
| TrialPoller | `backend/core/daemon/poller.py` | 定时轮询 trial 仓库 (默认 300s) |
| CommandReader | `backend/core/daemon/commands.py` | stdin 逐行 JSON 命令读取 |
| Main Loop | `backend/core/daemon/__init__.py` | `run_daemon()` 事件循环 + 调度 |

**Daemon CLI 子命令：**
- `--daemon-action start` — 启动持久守护进程，stdout 输出 line-delimited JSON 事件流
- `--daemon-action stop` — 发送 SIGTERM 停止
- `--daemon-action status` — 查询运行状态 (running/pid)
- `--daemon-action run` — (默认) 保留旧一次性全流程

**stdin 命令协议：** status / scan / formalize / sync / push / trial / session / shutdown

**进程管理：**
- PID 文件 `.gitgo/daemon.pid` — 双启动防护
- Stale PID 检测 — `os.kill(pid, 0)` + `ProcessLookupError` 自动清理
- `atexit` + `finally` 双重确保 PID 文件清理
- SIGTERM/SIGINT 优雅退出
- stdin EOF 自动触发 shutdown

**新增依赖：** `watchdog>=6.0`

**新建文件：** `backend/core/daemon/__init__.py` (~260行) / `backend/core/daemon/watcher.py` (60行) / `backend/core/daemon/poller.py` (28行) / `backend/core/daemon/commands.py` (33行)

**修改文件：** `cli/commands.py` (+60行) / `__main__.py` (+20行) / `requirements.txt` (+1行)

**认证：** `verify_headless.sh` 13/13 通过 | `python build.py` OK

### P2-D: Agent Interface (MCP Server)

**MCP Server (`mcp_server.py`)：**
- FastMCP stdio server — Claude Desktop 可直连
- 9 个 tool 覆盖完整 Gitgo 工作流
- 每个 tool 为无状态 one-shot 调用（直接 import SyncSession，无 subprocess 开销）

**9 个 MCP Tools：**

| Tool | 功能 |
|------|------|
| `gitgo_list_projects` | 列出所有已配置项目 |
| `gitgo_status` | 完整项目状态 + semantic 语义分析 |
| `gitgo_scan` | 扫描工作区文件变更 |
| `gitgo_formalize` | 从 workspace commits 创建 formal commit |
| `gitgo_sync` | 同步 formal commits 到备份仓库 |
| `gitgo_push` | 推送 formal commits 到远程 |
| `gitgo_trial_list` | 列出 Trial incoming changes |
| `gitgo_trial_triage` | 三叉决策（accept/promote/discard） |
| `gitgo_run_workflow` | 一键全流程（scan→formalize→sync→push） |

**Claude Desktop 配置：**
```json
{"mcpServers": {"gitgo": {"command": "python", "args": ["mcp_server.py"], "cwd": "/path/to/gitgo"}}}
```

**新增依赖：** `mcp>=1.0`

**新建文件：** `mcp_server.py` (~250行)

**修改文件：** `requirements.txt` (+1行)

### Phase 2: Agent-Ready Runtime — 全阶段完成

| Stage | 名称 | 核心产出 | 状态 |
|-------|------|---------|------|
| P2-A | Semantic State + Streaming | `semantic` 块 + `--stream` 流式输出 | ✅ 完成 |
| P2-B | Unified Operation History | HistoryManager 覆盖 9 种操作类型 | ✅ 完成 |
| P2-C | Persistent Daemon Core | watchdog + trial 轮询 + stdin 命令（纯线程） | ✅ 完成 |
| P2-D | Agent Interface | MCP Server 9 tools + daemon JSON 事件流 | ✅ 完成 |

**里程碑达成：** Agent 可通过两种模式使用 Gitgo：
1. **One-shot CLI** — `gitgo status --json --semantic-only` 等（P2-A/B）
2. **Persistent Daemon** — `gitgo daemon start` + stdin JSON 命令（P2-C）
3. **MCP Protocol** — Claude Desktop 直连 `mcp_server.py`（P2-D）

**认证：** `verify_headless.sh` 13/13 通过 | `python build.py` OK

### 源码审计发现

- `status_dict()` (`sync_session.py:140-165`) 仅输出原始计数，无语义判断
- `HistoryManager` (`history.py:60-81`) 只记录 sync 操作，`action_type` 字段未使用
- `run_full_workflow()` (`sync_session.py:761-802`) 一次性执行，非持续守护进程
- `save_session()` 遗漏 `is_incoming` / `sources_cleared` 字段持久化
- 当前 `--mode daemon` 实为一次性全流程 runner，非后台服务

### 审阅调整

- `--stream` 从 P2-D 移至 P2-A（低风险，独立于守护进程）
- P2-C 从 asyncio 改为纯线程（Windows 兼容性 + 已知模式一致性）
- P2-B 操作类型从 7 种扩展到 9 种（含 v0.12 新增的 delete_formal / dissolve_formal）
- `add_entry()` 委托到 `add_operation()` 保持向后兼容
- MCP server 预估从 ~100 行修正为 ~300-500 行

### 文件

- `docs/iterations/Phase2_AgentReadyRuntime.md` — Phase 2 完整计划（含源码审计、审阅记录）
- `docs/iterations/README.md` — 更新当前状态和待启动列表
- `docs/HANDOFF.md` — 更新必读文件列表和执行优先级

---

## v0.12 (2026-05-13)

**状态驱动闭环 — 所有 core 数据变异收口到 SyncSession step 方法**

### 动机

P1 完成架构解耦后，分析发现前端 12 处直接变异 core 数据结构（`formal_commits.pop/append`、`fc.message =`、`selected_workspace.add/discard`），绕过状态机。UI 改动可能破坏 core 完整性。

### 闭合的四个缺口

| 缺口 | 修复 |
|------|------|
| formal_commits 直接 mutation | 7 个新 step 方法（delete / edit_message / edit_number / dissolve / clear_sources / add_incoming / toggle_selection） |
| Push 路径分裂 | `PushWorker` 统一走 `step_push()`，删除直接调 `push_to_backup()` 分支 |
| `on_stage_changed` 未使用 | 接线 + 线程安全（`QTimer.singleShot(0)`）+ `_refresh_button_states()` 集中推导 |
| `submit_commit_message` 绕过 | 委托 `step_create_formal_commit(selected_indices=set())` |

### 修改文件

- `backend/core/sync_session.py` — +180 行（7 新 step 方法 + `FormalCommit.sources_cleared` 字段 + `step_create_formal_commit` 支持直接提交）
- `frontend/workers.py` — PushWorker 统一路径（-20 行，删除 `push_to_backup` import）
- `frontend/workspace/commits.py` — 7 处直接变异 → step 调用
- `frontend/workspace/trial.py` — incoming accept → `step_add_incoming_formal`
- `frontend/workspace/syncpush.py` — ~10 处手动 setEnabled → `_refresh_button_states()`
- `frontend/workspace/panel.py` — `_on_stage_changed` / `_apply_stage` / `_refresh_button_states`
- `backend/submitter.py` — 委托 `step_create_formal_commit`

### 认证

```
scripts/verify_headless.sh: 13/13 PASSED
python build.py: OK (54.1 MB, 167s)
```

---
## v0.11 (2026-05-13)

**Phase 1 Runtime Foundation 完成 + 代码模块化拆分**

### Phase 1 完成（全 5 阶段）

- **P1-A Import 解耦**：`__main__.py` 延迟 import gui/cui entry，headless 模式零 Qt/Rich 加载。`SyncSession.status_dict()` 机器可读状态输出。`--mode status --json` 结构化项目状态查询。
- **P1-B CLI Verb 矩阵**：`_init_session()` 工厂函数 + 9 个 CLI verb（list/status/sync/daemon/trial/formalize/scan/push/session），每个支持 `--json`。Agent 可通过 CLI 完全操作 Gitgo 工作流。
- **P1-C Governance 状态机**：`docs/GOVERNANCE_STATE.md` 定义 6 governance states（workspace/trial/curated/formalized/release_ready/published）+ 非法转移显式拒绝 + 错误码。
- **P1-D Session 持久化**：`.gitgo/session.json` checkpoint 策略（只持久化 formal_commits），4 个自动保存时机。`--mode session save/status/resume` CLI。
- **P1-E Headless 集成验证**：`scripts/verify_headless.sh` 13 项检查全部通过。

### 代码模块化拆分

按耦合度分析拆分 3 个超长文件：

| 源 | 前 | 后 | 新建 |
|----|----|----|------|
| `__main__.py` | 643行 | ~180行 | `cli/commands.py` (400行) + `cli/__init__.py` |
| `backend/core/operations/sync.py` | 324行 | ~195行 | `backend/core/operations/security.py` (120行) |
| `frontend/project_list.py` | 543行 | ~310行 | `frontend/project_edit_dialog.py` (210行) |

### 修改文件

- `__main__.py` — 重写（删除所有内联 `_cmd_*` 函数，改为 `from cli import ...` 延迟导入）
- `cli/commands.py` — **新建**（所有 CLI verb 实现 + `_init_session`）
- `cli/__init__.py` — **新建**（门面 re-export）
- `backend/core/operations/sync.py` — 移除安全扫描代码块（`DEFAULT_SECURITY_PATTERNS`/`_get_push_diff`/`_security_scan`），改为从 `.security` 导入
- `backend/core/operations/security.py` — **新建**（独立安全检查模块）
- `backend/core/operations/__init__.py` — 新增 `DEFAULT_SECURITY_PATTERNS` 导出
- `frontend/project_list.py` — 移除 `_ProjectEditDialog` 类及未使用的 Qt import
- `frontend/project_edit_dialog.py` — **新建**（`_ProjectEditDialog` 独立模块）
- `frontend/workspace/panel.py` — 更新 import 路径
- `scripts/verify_headless.sh` — **新建**（Phase 1 一键验证脚本）

### 认证结果

```
=== P1-E: Summary ===
  Passed: 13
  Failed: 0
=== VERIFICATION PASSED ===
```

---
## v0.10 (2026-05-12)

**CommitBox / CommitCanvas v2 重构 — 消除三层样式冲突，一揽子修复 6 个 UI Bug**

### 重构动机

v0.x 的 CommitBox 存在三层样式系统冲突：

```
Layer 1: app.setStyleSheet()       ← themes/qss.py 全局 QSS
Layer 2: widget.setStyleSheet()    ← _apply_style() 每状态变化调用
Layer 3: QPainter paintEvent()     ← fillRect 绘制竖线，不参与 QSS 层级
```

每 box 调 `setStyleSheet()` → Qt 触发级联样式重算 → Canvas paintEvent 被反复触发 → 可见闪烁。

### 修复的 6 个 Bug

| # | Bug | 根因 | 修复 |
|---|-----|------|------|
| 1 | Canvas 闪烁 | `setStyleSheet` 级联重算 | box 不再调 setStyleSheet，状态切换用 setProperty+polish（Qt 内部优化的增量重算） |
| 2 | WS box 文字被遮盖 | 默认态缺 `padding-right` | QSS 统一设 `padding-right: 22px`，所有状态一致 |
| 3 | 标题与 box 不对齐 | **4 轮返工** (见下) | 最终: setFixedWidth(148) 替代 setMinimumWidth(148) |
| 4 | Formal box 高亮残留 | enterEvent 写死 QFrame-only stylesheet | 删除所有 enterEvent/leaveEvent，QSS :hover + [selected] 接管 |
| 5 | 主题切换后颜色残留 | `_apply_style()` 未覆盖 enterEvent 临时样式 | 无 enterEvent 临时样式，主题切换只需 unpolish/polish |
| 6 | Formal 竖线异步 | QPainter fillRect 不参与 QSS 渲染管线 | 改为 QSS `border-left: 3px solid`，与背景/边框同步渲染 |

### 标题对齐 Bug — 4 轮返工（最顽固 Bug）

| 轮 | 尝试 | 结果 |
|----|------|------|
| 1 | `addSpacing` vs `setSpacing` 语义不同 | ❌ 102px 间距 |
| 2 | 标题行移入 Canvas 内部（同一 widget 树） | ⚠️ 改善但 resize 仍有偏差 |
| 3 | fm_hdr padding-left 调整: 10→13→16px (margin + border-left 占用) | ⚠️ 初始对齐, resize 后偏差 |
| 4 | **`setMinimumWidth(148)` → `setFixedWidth(148)`** | ✅ 根因解决 |

**根因**：`setMinimumWidth` 只设下限，stretch=0 时 QHBoxLayout 按 sizeHint() 分配实际宽度。QLabel vs QWidget 的 sizeHint 不同 → 动态宽度偏差。

### 关键架构决策

1. **单一 QSS 源**：删除所有 `_apply_style()` 和动态 `setStyleSheet()` 调用，状态全部通过 `setProperty` + `unpolish/polish` 驱动
2. **标题行内移**：从 workshop_tab.py 移入 CommitCanvas 内部（共享 widget 树，天然对齐）
3. **QSS border-left 替代 QPainter**：FormalCommitBox 删除 paintEvent，竖线由 QSS `border-left: 3px solid` 渲染
4. **`:hover` 替代 enterEvent/leaveEvent**：Qt 内置伪状态更高效
5. **删除 `_set_active_formal`**：不再需要 QGraphicsOpacityEffect
6. **批量刷新包裹 `setUpdatesEnabled(False/True)`**：防中间态渲染闪烁
7. **16ms 防抖 QTimer**：scroll 事件去抖，避免重复计算贝塞尔线坐标

### QSS 优先级陷阱

Qt QSS 不支持 CSS specificity——相同 specificity 的选择器按源码顺序决定优先级。`[selected="true"]` 必须写在 QSS 文件最末尾（在 synced/pushed/incoming 之后），否则会被后续规则覆盖。这与 CSS "最后匹配 wins" 不同——Qt 是 "后定义 wins"。

### 修改文件 (8 files)

- `frontend/commit_box.py` — 完全重写（删除基类 / enterEvent / leaveEvent / paintEvent / _apply_style）
- `frontend/commit_canvas.py` — 结构重写（标题行内移 + setFixedWidth + resizeEvent 联动）
- `themes/qss.py` — 大幅扩展（删除全局 QScrollArea border + 新增 #ws_card / #fm_card 全部 QSS 规则）
- `frontend/workspace/workshop_tab.py` — 简化（删除标题行创建 → Canvas 管理 + 防抖 timer）
- `frontend/workspace/commits.py` — 简化（删除 _set_active_formal + setUpdatesEnabled 包裹）
- `frontend/workspace/theme.py` — 简化（删除 _apply_style 调用 → unpolish/polish 循环）
- `frontend/workspace/panel.py` — 1 行（scan finished 增加 _refresh_formal_boxes）
- `frontend/widgets.py` — 清理（删除 CommitBox re-export）

**后端 B-2/3/4 全部落地 + 主题刷新集中化 + 7 个代码审查修复**

### B-4: processed_incoming 持久化

- `_record_processed(hash, action)` — triage 成功后写入 `project.processed_incoming` + `ConfigManager.save()`
- `step_check_trial()` 中过滤已处理的 hash，重启后不再重复显示
- `step_triage_incoming()` 每个成功分支自动调用 `_record_processed`

### B-3: INCOMING_CONFIRMING 状态机

- `SessionStage` 新增 `INCOMING_CONFIRMING` 枚举
- `step_start_accept_confirm(change)` — TRIAL_REVIEWING → INCOMING_CONFIRMING
- `step_confirm_accept()` — INCOMING_CONFIRMING → IDLE，返回暂存的 change
- `step_cancel_accept()` — INCOMING_CONFIRMING → TRIAL_REVIEWING

### B-2: Dissolve vs Clear Sources 语义区分

| 操作 | Formal commit | Workspace commits | 连接线 |
|------|--------------|-------------------|--------|
| Dissolve | 删除(pop) | 刷新为新卡片 | 消失 |
| Clear Sources | 保留(sources_cleared) | opacity 0.3 + setEnabled(False) | 消失 |

### 代码审查修复（7 项）

| # | 问题 | 修复 |
|---|------|------|
| 1 | IncomingChangeCard 缺 objectName | `setObjectName("inc_card")` |
| 2 | 主题切换后 Formal opacity 丢失 | `_apply_theme_colors()` 末尾重调 `_set_active_formal()` |
| 3 | Incoming Tab 多处 inline 样式主题不更新 | 新建 `_refresh_incoming_styles()` 集中管理，`_apply_theme_colors()` 调用 |
| 4 | `_merge_selected` double return | 删多余 `return` |
| 5 | inccoming_dot/widge_from/widge_to 等 inline 样式 | 全部移入 `_refresh_incoming_styles()` |

---

## v0.8 (2026-05-11)

**UI 修复 + Accept 两阶段 + 模块化重构 + 文档整理**

### 返工最多的 Bug 记录

以下按时间顺序列出反复出现、需要多次迭代才解决的关键 Bug：

#### Bug 1：返回崩溃（0xC0000409，6 次迭代）

| 迭代 | 方案 | 结果 |
|------|------|------|
| 1 | `deleteLater()` → Qt 事件循环自然清理 | 崩溃依然 |
| 2 | linkActivated lambda 加 url 参数 | 无效 |
| 3 | QTimer.singleShot 加 guard lambda | 无效 |
| 4 | `shiboken6.delete()` 同步删除 | 崩溃依然 |
| 5 | QTimer.singleShot(0) 延迟删除 | 崩溃依然 |
| 6 | 不主动删除 C++ 对象：`setParent(None)` + `hide()` | ✅ 成功 |

**根因**：Qt 事件链重入。在 click/key event 处理中同步 `shiboken6.delete()` + `processEvents()` → 已删 widget 的残留事件触发 `STATUS_STACK_BUFFER_OVERRUN`。

**最终方案**：`setCurrentIndex(0)` → `removeWidget(ws)` → `ws.setParent(None)`。不调 `deleteLater()` / `shiboken6.delete()`，Python GC 自然回收。

#### Bug 2：`_on_breadcrumb_click` 缩进错误（2 小时损失）

**现象**：进入项目后返回按钮无反应，整个 toolbar/sidebar/stack 从未创建。

**根因**：把 `lambda` 改为独立方法 `_on_breadcrumb_click` 时，`def` 后面的整个 `__init__`（约 150 行）缩进出函数内——不是类的方法，而是 `__init__` 内的局部函数。所有 widget（toolbar、sidebar、stack、log_bar、status_bar）从未执行创建代码。

**教训**：在 `__init__` 内 `def` 新方法时，确认后续代码的缩进级别。`def` 在 Python 中会创建作用域边界，之后的非缩进代码才属于原作用域。

#### Bug 3：breadcrumb "所有项目"不可点击（同样问题 2 次各自出现）

**现象**：面包屑文字可见但点击无反应。

**根因**：`_open_project` 中 `breadcrumb.setText()` 设置的是纯文本 `<span>`（无 `<a>` 标签），而设置可点击 `<a>` 版本代码在 `_apply_theme_colors()` 内，但 `_apply_theme_colors()` 被调用时 `self.workspace` 为 `None`，`if self.workspace:` 守卫跳过了面包屑更新。

**最终方案**：`_apply_theme_colors()` 移到 `WorkspacePanel(...)` 创建之后执行。

#### Bug 4：debug_logger.py `Invalid format string`

**现象**：logger 的 f-string 在某些 Python 版本/系统 locale 下抛 `ValueError: Invalid format string`，阻断项目打开。

**最终方案**：删除整个 `debug_logger.py` 模块，所有日志改为 `print("[LOG] ...", file=sys.stderr, flush=True)` 纯字符串拼接。

#### Bug 5：`QPushButton` 缺失导入

**现象**：`commit_box.py` 加 ⋯ 按钮时用了 `QPushButton` 但忘了加 import，导致 `_refresh_formal_boxes` 时报 `NameError`，commit box 消失。

**教训**：每改动 import 列表后立即 `python -c "from ... import ..."` 验证，避免提交后才发现。

#### Bug 6：`FileAccess.SSH` 不存在

**现象**：`_refresh_incoming_info_bar` 使用了 `FileAccess.SSH`，正确的枚举是 `FileAccessKind.SSH`。导致 `WorkspacePanel.__init__` → `_init_ui` → `_build_incoming_tab` → `_refresh_incoming_info_bar` 整条链路崩溃。

**教训**：使用枚举类型时先 grep 确认类定义的位置和值。`FileAccess` 是数据类，`FileAccessKind` 才是枚举。

### Builder 按 Tab 拆分（653→4文件）

- `explorer.py` — `_BranchLineStyle` + `ExplorerMixin`（文件树/Diff/Node）
- `workshop_tab.py` — `WorkshopTabMixin`（Workshop Tab + 底部操作行）
- `incoming_tab.py` — `IncomingTabMixin`（Incoming Tab）
- `builder.py` → 162行核心（`BuilderMixin(Explorer, Workshop, Incoming)` + ActionBar + Remotes/History）

### CUI 拆分为子包（636→`cui/` 4文件+门面）

- `cui/projects.py` — 项目 CRUD
- `cui/display.py` — Rich 表格渲染
- `cui/workflow.py` — 工作流步骤
- `cui/main_flow.py` — 主流程编排 + 入口
- `cui/main.py` → 15行门面

### Themes 拆 QSS（279→93+186）

- `themes/qss.py` — `build_qss(t)` 独立（186行）
- `themes/__init__.py` → 93行（令牌+门面）

### Widgets 拆 CommitBox/Canvas（265→3行门面）

- `commit_box.py` — `CommitBox` + `WorkspaceCommitBox` + `FormalCommitBox`
- `commit_canvas.py` — `CommitCanvas`
- `widgets.py` → 3行 re-export 门面
- `CommitConnector` 已废弃类删除

### 文档整理

- 全部文档（HANDOFF / VERSION / README / CLAUDE / iterations）移入 `docs/`

---

## v0.7 (2026-05-11)

**提交区统一 Canvas 重构 + 返回崩溃修复 + 全面 bug 修复**

### 提交区重构 — CommitCanvas

删除两个独立 QScrollArea + CommitConnector，替换为**统一 CommitCanvas**：
- 单个 QScrollArea 内含 `CommitCanvas(QWidget)`
- 内部 QHBoxLayout(spacing=52)：`ws_column`(左, minWidth=148) + `fm_column`(右, stretch=1)
- paintEvent 从 `ws_column.right()` 到 `fm_column.left()` 绘制贝塞尔连接线（x0→x1 间距 52px）
- `setBrush(NoBrush)` 防止 path 被 fill 导致弧形阴影
- `_refresh_commit_lines()` 遍历 `formal_commits[].source_indices` 计算坐标，`mapTo(canvas, ...)` 映射
- scrollBar.valueChanged 联动刷新

### 返回崩溃 — 完整修复链

**根因**：Qt 事件链重入。在 link click / Escape key event 处理中同步 `shiboken6.delete()` + `processEvents()` → 已删 widget 的残留事件触发 → `STATUS_STACK_BUFFER_OVERRUN` (0xC0000409)

**最终解法**（经历 6 轮迭代验证）：
1. 信号 disconnect → 消除 paint 回调
2. **先 `setCurrentIndex(0)` 再 `removeWidget`** — 关键顺序
3. `hide()` + `setParent(None)` — 彻底脱离 widget 树
4. **不调用 `deleteLater()` / `shiboken6.delete()`** — 交由 Python GC 自然回收，完全避开 Qt 事件链中销毁 C++ 对象的重入问题
5. Esc 快捷键移出 WorkspacePanel → MainWindow 注册（避免 key event 处理中 workspace 被删）

**其他崩溃相关修复**：
- `QPointF` 导入（QtCore 非 QtGui）→ 消除 paintEvent NameError
- `QPainter.end()` try/finally → 防止 Qt backing store 报 active painter
- `showEvent` 的 `QTimer.singleShot(0)` → 直接调用 `_update_action_bar()`
- `COMMIT_NO_WINDOW` → subprocess 闪窗修复

### UI 修复

- **侧边栏折叠**：`sidebar_wrap.setMinimumWidth(16)` + `sidebar_toggle.setFixedWidth(24)`
- **树引导线**：`State_Open` → `widget.itemAt().isExpanded()`；颜色改用 `get_theme()["bdr2"]`；删除 6 条 `::branch` QSS 规则
- **合并弹窗**：删除 QDialog → 模板直接填入 `msg_box.setPlainText()`，`_merging` 标志区分合并/直接提交
- **主题颜色残留**：`_apply_theme_colors()` 遍历全部 CommitBox 调 `_apply_style()`
- **边距/QSS**：status_bar `(10,3,10,3)`；secondary hover border-color；explorer_panel QSS；overflow:hidden 删除
- **贝塞尔线**：spacing=52 解决 gap=1 不可见；`setBrush(NoBrush)` 消除弧形阴影

### Debug 基础设施

- `debug_entry.py`：Python 异常保活 + `input("按 Enter...")` 
- `run_debug.bat`：bat 包裹启动，即使 C++ segfault 控制台也不消失
- 全链路调试日志（已清理为生产版）

### 已确认残留问题

- 进程退出时偶发 segfault（无 Python frame，纯 C++ 清理阶段，不影响功能）
- Action Bar 首次渲染偶有不到位



---

## v0.6 (2026-05-10)

**崩溃修复 + 构建流程改进 + 文档全面更新**

### 已修复

- **`QWidget.foreground()` → `foregroundRole()` — 崩溃根本原因**：`_BranchLineStyle.drawPrimitive()` 中 `widget.foreground()` 在 Qt6/PySide6 中不存在，导致 `AttributeError` 在 C++ 层反复传播后 segfault。修复后进入项目不再闪退。
- **`_restyle()` unpolish access violation**：Qt 的 `unpolish/polish` 在 widget 未完全初始化时调用会触发 access violation。移除 `_restyle` 调用（QSS 全局样式已足够）。
- **全局异常捕获**：`sys.excepthook` + `_open_project` 时间戳日志双重保障，崩溃信息写入 `%TEMP%\gitgo_crash.log`。

### 构建流程改进

- **两阶段打包**：`python build.py --debug` 先打带控制台的调试版（`dist/gitgo_debug.exe`），测试通过后再 `python build.py` 打正式版（`dist/gitgo.exe`，无控制台）。
- **`--debug` 标志**：调试版保留 `--noconsole` 的开关，可见 Qt/Python stderr 输出。
- **文件已占用处理**：`PermissionError` 时跳过被占用文件而非崩溃。

### 文档更新

- 前端设计报告：补充右侧栏架构(rsb)、弹出设置面板(spop)、详细 QSS 令牌表、完整交互序列、构建/部署说明
- CLAUDE.md：更新 Qt 开发指南（`foregroundRole()` / 构建流程 / 文档规范）
- HANDOFF.md：同步当前进度
- VERSION.md：新增 v0.6 记录

---

**前端架构重构 + P6 全局功能 + 全面 bug 修复**

### P6 全局功能

- **Action Bar（操作栏）**：Tab 栏与内容区之间 28px 操作栏，每个 Tab 独立按钮配置（Workshop: Undo merge/Save draft/Export tasks/Re-scan；Incoming: Undo last decision/Export list/Re-fetch；Remotes: Refresh all；History: Export history/Filter），`QTabBar.currentChanged` 驱动 `_update_action_bar()` 动态重建
- **QTabBar + QStackedWidget 替代 QTabWidget**：操作栏需要"Tab 栏 → 操作栏 → 内容"三层布局，QTabWidget 无法在 Tab 和内容间插入 widget
- **键盘快捷键**：Ctrl+Shift+S(扫描)/Ctrl+Shift+M(合并)/Ctrl+S(Sync)/Ctrl+Shift+P(Push)/Ctrl+Return(提交)/Escape(返回)
- **QProxyStyle 引导线**：`_BranchLineStyle` 拦截 `drawPrimitive(PE_IndicatorBranch)` 绘制树引导线
- **QSS 动态生成**：`themes/__init__.py` 的 `_build_qss(t)` 运行时插值生成完整样式表，替代静态 QSS 字符串

### Mixin 架构落地

- **WorkspacePanel 聚合 7 个 Mixin**：`BuilderMixin(UI构建) + CommitMixin(commit操作) + SyncPushMixin(同步/推送) + TrialMixin(三叉决策) + RemotesMixin(远程仓库) + HistoryMixin(历史记录) + ThemeMixin(主题刷新)`
- **`frontend/workspace/` 子包**：从单一 `workspace.py`（3000+行）拆分为 9 个文件按职责隔离
- **ThemeMixin 单点刷新**：`_apply_theme_colors()` 覆盖 22+ widget，`_restyle(widget)` 的 `unpolish/polish` 强制 Qt 重计算 QSS

### 已修复

- **空白面板 BUG**：`splitter.addWidget(left_widget)` 误删（已加回）+ `_animate_page()` 动画 GC 导致 opacity 卡 0（anim 存为 `self._page_anim`）
- **右侧 splitter 拖拽跳动**：右侧步骤面板改回扁平 QVBoxLayout + QScrollArea
- **内层 splitter 跳变**：`left_splitter` / `right_splitter` 的 `setChildrenCollapsible(True)` → `False`
- **主题切换颜色残留**：`_apply_theme_colors()` 单点刷新覆盖 19 个 widget（explorer/diff/commit/incoming/trial），`_open_settings` 调 `workspace._apply_theme_colors()` 连锁刷新。根因定位：Python 脚本字符串替换静默失败导致 4 个 widget 刷新代码缺失
- **嵌套 QSplitter 水平拖拽跳变**：`setMinimumWidth/Height` 替代 `setFixedWidth/Height`，对称保护各层子对象
- **程序卡死（冻结）**：`_setup_shortcuts()` 引用已删除按钮（`self.scan_btn`/`self.merge_btn`/`self.sync_btn`/`self.push_btn`）导致 `AttributeError`。修复：Workshop tab 底部恢复操作行 + 删除无效引用
- **项目列表"反主题颜色"**：`_style_project_row()` 硬编码浅色背景（`#f0faf5` 等）→ 半透明 `QColor(r,g,b,alpha)` 兼容暗/亮双主题
- **`trial.py` 残留 self.tabs**：旧 `self.tabs.setCurrentIndex(0)` → `self.tab_bar.setCurrentIndex(0)`

### 已完成的 UI 重构

- **Tab 驱动工作区**：进入项目后 4 Tab（提交工作区/传入/远程/历史），Explorer 文件树嵌入 Workshop Tab 内
- **前端/后端包分离**：`frontend/`（main_window / workspace/ / project_list / widgets / workers / settings）+ `backend/`（scanner），gui_main.py 薄入口
- **主题系统模块化**：`themes/` 包（light.py/dark.py/__init__.py），ThemeColors 类支持属性访问（`t.bg`/`t.txt`），QSS 集中管理
- **项目列表**：三列"项目名""备注""状态"，列宽可拖拽；"+"添加行；右键菜单编辑/删除；Last sync 列 + 定时 30s 刷新
- **备注系统**：ProjectConfig 新增 `note` 字段，双击表格直接编辑
- **自动隐藏滚动条**：鼠标移入/滚轮显示，离开 2s 隐藏
- **节点远程配置**：每个 RepoNode 独立选择本地/SSH，对话框动态切换输入组
- **中文本地化**：zh.json 覆盖 ~30 个翻译键
- **文件浏览器**：进入项目自动加载 workspace 目录树，扫描对比后更新 N/M badge

---

### 专项：嵌套 QSplitter 水平拖拽跳变修复

**场景**：Workshop Tab 水平三列（Explorer | Center | Diff），Center 内含垂直 QSplitter（提交区 | 消息区）。

```python
# 控件树
ws_hsplitter (QSplitter, Horizontal)
├── [0] explorer_panel    stretchFactor=0
├── [1] ctr_widget        stretchFactor=1
│   └── center_splitter (QSplitter, Vertical)
│       ├── commit_frame   setMinimumHeight(100)
│       └── msg_frame      第 262 行，缺最小高度！
└── [2] diff_panel        stretchFactor=0
```

**失败方案 1**：外层用 QHBoxLayout 固定宽度，完全删除水平拖拽。用户拒绝——要求保留拖拽功能。

**失败方案 2**：仅给 commit_frame 设 minHeight，msg_frame 无保护。拖拽手柄 → ctr_widget resize → center_splitter 连锁重算高度 → msg_frame 无 minHeight 被压缩到接近 0 → 弹回 → 跳变。

**成功方案**：三步组合

1. **对称最小高度**：`commit_frame.setMinimumHeight(100)` + `msg_frame.setMinimumHeight(54)`，确保两个子面板都不会在 resize 连锁反应中被压到消失。

2. **移除 setFixedWidth**：explorer / ws_scroll / diff_panel 全部从 `setFixedWidth` 改为 `setMinimumWidth`。`setFixedWidth` 与 QSplitter 拖拽逻辑冲突——QSplitter 试图调整子对象宽度时遇到不可改变的固定宽度，handle 位置计算与视觉渲染不一致，导致反复跳变（Qt 内部 layout 循环：尝试分配 → 固定宽度阻挡 → 重新计算 → 分配冲突 → 循环）。

3. **setSizes 设初始比例**：`ws_hsplitter.setSizes([138, 800, 150])` 给三列合理的初始像素分配，之后手柄自由拖拽不受固定宽度干扰。

**泛化规则**：
- QSplitter 子对象用 `setMinimumWidth/Height` 设下限，不用 `setFixedWidth/Height`
- 嵌套 QSplitter 中，每一层的所有子对象都要设最小尺寸保护
- 初始比例用 `setSizes()` 而非依赖 `sizeHint`

---

### 未解决

- **workspace 滚动条**：自动隐藏/显示偶有不到位，待优化（非阻塞）
- **QTreeWidget 引导线**：`QProxyStyle` 方案基本可用但颜色适配待完善
- **Daemon 模式**：单次执行，非持续守护进程（无轮询/调度/FS 监控）

### 架构设计：异构开发

见 v0.4 记录，策略不变。
├── file_scanner   ← 文件扫描（已足够快，最后考虑）
└── git_ops        ← libgit2 封装，替代 subprocess

Python 胶水层

**实施顺序**：

| 步   | 模块         | 语言                      | 预期收益                        | 复杂度 |
| ---- | ------------ | ------------------------- | ------------------------------- | ------ |
| 1    | diff_engine  | C++ pybind11 或 Rust PyO3 | 大文件 diff 100ms→5ms，10-50x   | 低     |
| 2    | git_ops      | C++ libgit2 或 Rust git2  | 消除进程 fork 开销，5-10ms/次   | 中     |
| 3    | file_scanner | 暂不考虑                  | 收益最小，os.walk 已够快        | —      |
| —    | GUI 重写     | C++ Qt 原生               | 55MB→~15-20MB，但需数周全量重写 | 极高   |

---

## 各阶段实施状态总览 (2026-05-10)

| 阶段                               | 承诺内容                           | 状态     | 完成度 |
| ---------------------------------- | ---------------------------------- | -------- | ------ |
| **Phase 0.5** — 插件系统           | Hook 接口 + 发现/加载              | ✅ 已完成 | 100%   |
| **Phase 1** — 数据模型             | RepoNode / ProjectConfig / migrate | ✅ 已完成 | 100%   |
| **Phase 2** — SyncSession + Daemon | 状态机 + Daemon CLI                | ⚠️ 部分   | 80%    |
| **Phase 3** — SSH 适配器           | paramiko SFTP + exec               | ✅ 已完成 | 100%   |
| **Phase 4** — Trial 三叉           | IncomingChange 三叉决策            | ✅ 已完成 | 100%   |
| **Phase 5** — RemoteConnector      | GitHub/GitLab API                  | ❌ 未开始 | 0%     |
| **Phase 6** — 可选增强             | Action Bar / 快捷键 / 动态QSS      | ✅ 已完成 | 100%   |

### 详细

- **Phase 0.5**：SyncPlugin 基类 7 个钩子、PluginOrchestrator 发现/加载、auto_merge 示例插件、sync/push 中已接入钩子调用
- **Phase 1**：RepoNode + FileAccess + FileAccessKind(LOCAL/SSH/SMB)、旧格式自动迁移
- **Phase 2**：SyncSession 全状态机 (IDLE→TRIAL→SCAN→SELECT→COMMIT→SYNC→PUSH)、GUI/CUI/Daemon 三前端驱动。⚠️ **Daemon 模式是单次执行，非持续守护进程**（无轮询/调度/FS 监控）
- **Phase 3**：SSHFileAdapter (14 方法)、SSHGitRunner (10 方法)、create_adapters_for_node() 工厂
- **Phase 4**：TrialAction/IncomingChange、step_check_trial()/step_triage_incoming()、GUI 三按钮 + CUI 命令、全部通过
- **Phase 5**：RemoteTarget 模型已定义但未被使用，无 GitHub/GitLab API 代码
- **Phase 6**：i18n 完成 (zh/en)、SMB 适配器缺失 (FileAccessKind.SMB 已定义)、无 UPX 压缩

### 下一阶段建议优先级

1. **Phase 5 RemoteConnector** — 按计划推进 GitHub/GitLab API
2. **Daemon 模式完善** — 将单次执行改为持续监控（FS watch + 定时轮询）
3. **异构开发 Phase 1** — diff_engine C++/Rust 扩展

---

## v0.4 (2026-05-09)

**Phase 3 SSH + Phase 4 Trial + 测试套件 + UI 全面优化**

- **Phase 3 SSH 适配器**：SSHFileAdapter（paramiko SFTP 14 个方法）、SSHGitRunner（paramiko exec 全部 git 操作）、工厂函数 `create_adapters_for_node()`
- **Phase 4 Trial 三叉工作流**：TrialAction/IncomingChange 数据模型、`get_trial_log()`、SyncSession TRIAL_CHECKING/TRIAL_REVIEWING 状态、accept/promote/discard 三叉决策
- **完整测试套件**：98 个测试覆盖 models/adapters/factory/config/operations/sync_session，含 SSH mock 测试
- **UI 重命名**：工作区(workspace node)、发布备份区(release backup node)、试验区(trial node)
- **跟随系统主题**：注册表检测 Windows 主题、浅色/深色/跟随系统三档
- **设置面板重构**：删除"其他"分区、新增"动画"开关、新增"版本"分区
- **UI 细节修复**：Splitter 拖拽平滑、项目列表光标 BUG、工具栏紧凑布局
- **国际化更新**：中英文各新增 ~10 个翻译键

---

## v0.3 (2026-05-08)

**项目重命名 + Push 安全检查 + GUI 界面优化 + 同步前差异预览 + 程序国际化 + CLI 模式增强 + 同步历史日志 + 插件系统 + 数据模型重构 (RepoNode)**

- 项目正式命名 gitgo：目录重命名、配置文件名迁移、窗口标题/文档全部更新
- Push 前安全检查：9 条内置规则（API key、密码、私钥、token、AWS key 等）扫描待推送 commit，命中后用户确认是否强制推送
- 支持行尾 `gitgo-ignore-sensitive` 豁免、规则级禁用、自定义扩展规则
- 可配置 `severity_threshold` 控制告警灵敏度
- GUI 界面优化: SettingsDialog 设置面板（主题切换 + 语言预留 + 扩展位）、QSplitter 自由调整、应用图标蓝色圆形"G"、CommitBox 悬停高亮、窗口默认 1200×750
- 程序国际化：locales/zh.json + locales/en.json 双语言文件，i18n.py 翻译模块，SettingsDialog 语言切换下拉菜单，Config 存储语言设置，GUI/CUI 界面全部字符串可翻译
- CLI 模式增强：新增 --mode list（列出项目）、--mode sync --project NAME（无 UI 直接同步）、--mode history（查看同步历史）、--help 参数更新
- 同步历史日志：HistoryManager 记录每次同步的时间、项目、文件数、commit hash（最多 100 条），GUI/CUI/CLI 三端均可查看
- 插件系统（Phase 0.5）：SyncPlugin 基类 + 7 个钩子点
- 数据模型重构（Phase 1）：引入 RepoNode 三角色模型（workspace/release/trial），新增 models/ 包（FileAccessKind、SyncStatus、RemoteTarget、FileAccess、RepoNode），旧配置自动迁移，向后兼容 property 确保零中断，修复 3 个潜伏类型错误（scan_complete/commit_select/commit_message/sync_start/sync_complete/push_start/push_complete），PluginOrchestrator 发现/加载/编排，支持 3 级搜索路径（exe/plugins/、~/.vernier/plugins/、项目级 .gitgo/plugins/），内置 auto-merge 示例插件

---

## v0.2 (2026-05-08)

**CUI 同步** — 终端界面功能等效于 GUI

- CUI 支持多项目管理（项目列表 → 选择 → 操作）
- CUI 支持 box commit 合并 + Sync/Push 分离
- 修复 `--mode config` 适配多项目输出

---

## v0.1 (2026-05-08)

**初始版本**

- GUI 和 CUI 双界面
- 文件 SHA256 对比扫描
- Commit 整合：多 workspace commit 合并为正式 commit
- Sync 到备份仓库 + Push 到 GitHub（分离操作）
- 多项目管理：ProjectConfig + 项目列表首页
- 旧配置自动迁移
