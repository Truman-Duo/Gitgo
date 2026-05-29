# gitgo v0.27 交接文档

> 日期：2026-05-29

---

## 本次更新（v0.25-v0.27）

**v0.25**: State Convergence — C1-C3 全局 Runtime 化（governance event / --layered / StateReader）
**v0.26**: Runtime Discipline — 4 条硬约束 + discipline validation lint
**v0.27**: Runtime Constitution — 8 条规则 + 三层状态机 + Policy Engine daemon + Dashboard + MCP round_complete + Dogfooding 7 条设计纠错

### 当前工作流
- **Lexi CC**: 每轮结束调 MCP `gitgo_round_complete("lexi")` → Gate A 自动检查
- **人**: `gitgo --mode dashboard --refresh 5` 实时查看所有项目状态
- **Daemon**: 后台 Policy Engine 持续扫描 + 积累 governance event
- **Release**: `gitgo push --project X --strip-authorship` 手动发布

### 必读文件
- `docs/RuntimeConstitution.md` — Runtime 根本规则（8 条）
- `docs/HUMAN_IN_THE_LOOP.md` — 人机协作治理模型
- `lexi/DESIGN_CORRECTIONS.md` — Dogfooding 设计纠错（7 条）

### 执行优先级
1. **P0（GUI Track）** — 前端架构调整（B-1 + F-1）

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
