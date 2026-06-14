# gitgo v0.27 交接文档

> 日期：2026-05-30

---

## 当前状态

**v0.27 完成。** Runtime Constitution 8 条规则 + Policy Engine daemon + Dashboard + MCP round_complete + Dogfooding 7 条设计纠错。

### 当前工作流
- **Agent**: 每轮结束调 MCP `gitgo_round_complete("project")` → Gate A 检查 → passed 继续 / blocked 修复
- **人**: `gitgo --mode dashboard --refresh 5` 实时查看所有项目状态
- **Daemon**: 后台 Policy Engine 持续扫描 + 积累 governance event（可选）
- **Release**: `gitgo push --project X --strip-authorship` 手动发布

### 已知理论困境
Agent 框架（Claude Code、Codex 等）不提供进程内 hook 接口。Gitgo 的 Policy Engine 可以检测到 Agent 的每一次文件变更，但不能在 Agent 执行下一步推理之前强制暂停它。MCP tool 依赖 Agent 主动调用。详见 README "当前理论困境"。

### 代码拆分
- `backend/core/knowledge/` 拆为 models.py / manager.py / harvest.py（lesson.py 736→3 文件）
- 硬编码保护：`get_exclude_patterns` 保证 `.claude/` `.git/` 等目录永不被 sync

### 必读文件
- `docs/RuntimeConstitution.md` — Runtime 根本规则（8 条）
- `README.md` — 完整 README（架构图 + 理论困境 + 能力表）
- `lexi/DESIGN_CORRECTIONS.md` — Dogfooding 设计纠错（7 条）
- `docs/HUMAN_IN_THE_LOOP.md` — 人机协作治理模型
- `lexi/DESIGN_CORRECTIONS.md` — Dogfooding 设计纠错（7 条）

### 执行优先级
1. **P0（GUI Track）** — 前端架构调整（B-1 + F-1）
2. **P0（Git 记录清洗）** — 整理 git 历史 + GitHub 提交记录重新梳理（2026-06-11 记录，明日执行，仅记录不执行代码）

---

## 当前进度总览（v0.27）

| 区域 | 完成度 | 状态 |
|------|--------|------|
| Runtime Kernel | 100% | SyncSession + step orchestration ✅ |
| Event Bus | 100% | HistoryManager append-only governance event log ✅ |
| State Store | 100% | StateReader unified query + persistent files ✅ |
| Policy Engine | 80% | 各自独立，缺统一 Policy.check() interface |
| 测试 | 100% | 334 passed / 24 测试文件 ✅ |
| Constitution | 100% | 8 条规则 + 三层状态机 ✅ |
