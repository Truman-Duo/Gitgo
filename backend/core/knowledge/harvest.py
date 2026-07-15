import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from backend.core.history import HistoryManager
from .models import Lesson
from .manager import LessonManager


def harvest_lessons(
    workspace_path: Path,
    project_name: str,
    tech_stack: str = "",
) -> list[Lesson]:
    """sync 成功后自动检测值得记录的教训。

    四个数据源（功能耦合，代码解耦）：
    1. WORKSPACE 侧 — 从 git log + CLAUDE.md 直接收割
    2. BACKUP 侧 — 从 scan history 检测跨轮次反复修改
    3. GOVERNANCE 侧 — 所有 governance event + 操作级 event → lesson bridge
    4. 实例→抽象提升 — 按 tech_stack 标签自动提升到对应抽象文件
    """
    harvested = []

    # ── Phase 1: Workspace 侧收割 ──
    harvested.extend(_harvest_from_git_log(workspace_path, project_name, tech_stack))
    harvested.extend(_harvest_from_claude_md(workspace_path, project_name, tech_stack))

    # ── Phase 2: Backup 侧收割 ──
    harvested.extend(_harvest_from_scan_history(project_name, tech_stack))

    # ── Phase 3: Governance signals → lesson bridge ──
    harvested.extend(_harvest_from_governance_signals(workspace_path, project_name, tech_stack))

    # ── Dedup: skip triggers already in pending ──
    existing = LessonManager.load_pending(workspace_path, project_name)
    existing_triggers = {getattr(e, 'trigger', '') for e in existing}
    harvested = [h for h in harvested if getattr(h, 'trigger', '') not in existing_triggers]

    return harvested


def _harvest_from_governance_signals(
    workspace_path: Path,
    project_name: str,
    tech_stack: str = "",
) -> list[Lesson]:
    """从 governance event log + 操作级 event 中提取信号生成 lesson。

    覆盖全部 governance event 类型：
    - integrity_warning → [identity] lesson
    - governance_drift → [drift] lesson
    - governance_synced → [workflow] burst detection（单次聚合过多 commit）
    - governance_memory_snapshot → [identity] snapshot trend
    - governance_contract_updated → [contract] feature tracking
    - governance_lesson → [meta] harvest trend
    - sync → [workflow] file count trend
    - formalize → [workflow] commit pattern
    - scan → [workflow] entropy trend
    """
    harvested = []
    entries = HistoryManager.load()
    recent = [e for e in entries if e.project_name == project_name][-20:]

    # 统计聚合信息
    sync_events = [e for e in recent if e.operation == "sync"]
    formalize_events = [e for e in recent if e.operation == "formalize"]
    scan_events = [e for e in recent if e.operation == "scan"]

    for e in recent:
        detail = e.detail if isinstance(e.detail, dict) else {}

        # ── integrity_warning ──
        if e.operation == "integrity_warning":
            rule = detail.get("rule", "")
            if rule == "mass_override":
                lesson = Lesson(
                    tech_stack=tech_stack, category="identity", severity="high",
                    trigger="Mass override detected by Identity Guard",
                    rule=f"首次 sync 或项目被覆盖：{detail.get('changed_count', 0)}/{detail.get('total_count', 0)} 文件变更。"
                          "如果这不是故意的项目替换，请检查 workspace 是否被其他项目覆盖。",
                    source="auto_harvested", abstract=False, project_name=project_name,
                )
                lesson.id = f"signal_mass_override_{project_name}"
                LessonManager.save_pending(workspace_path, lesson)
                harvested.append(lesson)
            elif rule == "structure_collapse":
                lesson = Lesson(
                    tech_stack=tech_stack, category="identity", severity="high",
                    trigger="Directory structure collapsed",
                    rule=f"目录骨架崩塌：Jaccard {detail.get('jaccard', 0):.2f}。"
                          "旧目录: {', '.join(detail.get('old_dirs', []))}。"
                          "项目可能已被替换。",
                    source="auto_harvested", abstract=False, project_name=project_name,
                )
                lesson.id = f"signal_collapse_{project_name}"
                LessonManager.save_pending(workspace_path, lesson)
                harvested.append(lesson)

        # ── governance_drift ──
        elif e.operation == "governance_drift":
            rules = detail.get("rules", [])
            lesson = Lesson(
                tech_stack=tech_stack, category="drift", severity="high",
                trigger="Drift detected during push",
                rule=f"检测到 {detail.get('alert_count', 0)} 项合约偏差: {', '.join(rules)}。"
                      "检查是否 LLM 在绕过问题而非解决。",
                source="auto_harvested", abstract=False, project_name=project_name,
            )
            lesson.id = f"signal_drift_{project_name}"
            LessonManager.save_pending(workspace_path, lesson)
            harvested.append(lesson)

        # ── governance_synced → burst detection ──
        elif e.operation == "governance_synced":
            commit = detail.get("commit", "")
            # 从对应的 formalize event 获取 source_indices 数量
            related_fc = [fe for fe in formalize_events
                          if fe.detail and isinstance(fe.detail, dict)
                          and fe.detail.get("commit") == commit]
            if related_fc:
                source_count = len(related_fc[0].detail.get("source_indices", []))
                if source_count >= 5:
                    lesson = Lesson(
                        tech_stack=tech_stack, category="workflow", severity="low",
                        trigger=f"Burst formalize: {source_count} workspace commits aggregated into {commit}",
                        rule=f"单次 sync 聚合了 {source_count} 个 workspace commit。"
                              "如果这是首次 sync 则正常；如果是增量 sync，说明 sync 间隔过长。",
                        source="auto_harvested", abstract=False, project_name=project_name,
                    )
                    lesson.id = f"signal_burst_{project_name}_{commit.replace('[','').replace(']','').replace(' ','_')}"
                    LessonManager.save_pending(workspace_path, lesson)
                    harvested.append(lesson)

        # ── governance_memory_snapshot → trend ──
        elif e.operation == "governance_memory_snapshot":
            sources = detail.get("sources", [])
            if not sources:
                lesson = Lesson(
                    tech_stack=tech_stack, category="identity", severity="medium",
                    trigger="Memory snapshot returned empty",
                    rule="工具记忆快照为空。可能 .claude/ .codex/ .codebuddy/ 目录均不存在或为空。",
                    source="auto_harvested", abstract=False, project_name=project_name,
                )
                lesson.id = f"signal_empty_snapshot_{project_name}"
                LessonManager.save_pending(workspace_path, lesson)
                harvested.append(lesson)

        # ── governance_contract_updated → feature tracking ──
        elif e.operation == "governance_contract_updated":
            feature = detail.get("feature", "")[:80]
            # 检查是否是新的 feature type
            existing_features = [
                fe for fe in recent
                if fe.operation == "governance_contract_updated"
                and fe.detail and isinstance(fe.detail, dict)
            ]
            if len(existing_features) >= 5:
                lesson = Lesson(
                    tech_stack=tech_stack, category="contract", severity="low",
                    trigger=f"Contract growing: {len(existing_features)} features confirmed",
                    rule=f"项目合约已积累 {len(existing_features)} 个 decided features。"
                          "建议定期 review 合约，清理已废弃的功能条目。",
                    source="auto_harvested", abstract=False, project_name=project_name,
                )
                lesson.id = f"signal_contract_growth_{project_name}"
                LessonManager.save_pending(workspace_path, lesson)
                harvested.append(lesson)

    # ── 聚合趋势检测（跨事件分析）──

    # sync 文件数趋势
    if len(sync_events) >= 3:
        file_counts = [
            e.detail.get("file_count", 0) for e in sync_events
            if isinstance(e.detail, dict)
        ][-5:]
        if file_counts and max(file_counts) > 50:
            lesson = Lesson(
                tech_stack=tech_stack, category="workflow", severity="low",
                trigger=f"Large sync: avg {sum(file_counts)//len(file_counts)} files over last {len(file_counts)} syncs",
                rule="sync 文件数持续较高。考虑将大文件（data/、dist/、build/）加入 force_exclude。",
                source="auto_harvested", abstract=False, project_name=project_name,
            )
            lesson.id = f"trend_large_sync_{project_name}"
            LessonManager.save_pending(workspace_path, lesson)
            harvested.append(lesson)

    # ── push 频率趋势 ──
    push_events = [e for e in recent if e.operation in ("push", "governance_pushed")]
    if len(push_events) >= 3:
        lesson = Lesson(
            tech_stack=tech_stack, category="workflow", severity="low",
            trigger=f"Push frequency: {len(push_events)} pushes in recent history",
            rule="push 频率正常。" if len(push_events) <= 5
            else "push 频率较高，考虑减少 push 次数以保持 commit 历史整洁。",
            source="auto_harvested", abstract=False, project_name=project_name,
        )
        lesson.id = f"trend_push_freq_{project_name}"
        LessonManager.save_pending(workspace_path, lesson)
        harvested.append(lesson)

    # ── post-hoc 修正模式 ──
    edit_events = [e for e in recent
                   if e.operation in ("governance_edited", "governance_renumbered",
                                      "governance_dissolved")]
    if len(edit_events) >= 2:
        lesson = Lesson(
            tech_stack=tech_stack, category="workflow", severity="medium",
            trigger=f"{len(edit_events)} post-hoc corrections to formal commits",
            rule="formal commit 创建后被编辑/重新编号/dissolve。"
                  "这可能表示提交前的 review 不够充分。",
            source="auto_harvested", abstract=False, project_name=project_name,
        )
        lesson.id = f"trend_posthoc_{project_name}"
        LessonManager.save_pending(workspace_path, lesson)
        harvested.append(lesson)

    # ── meta: 系统自省 ──
    lesson_events = [e for e in recent if e.operation == "governance_lesson"]
    if len(lesson_events) >= 3:
        total_harvested = sum(
            e.detail.get("harvested_count", 0)
            for e in lesson_events if isinstance(e.detail, dict)
        )
        lesson = Lesson(
            tech_stack=tech_stack, category="meta", severity="low",
            trigger=f"Lesson system: {total_harvested} lessons harvested over {len(lesson_events)} rounds",
            rule="知识传承系统正在积累教训。定期 review pending lessons 并确认有价值的条目。",
            source="auto_harvested", abstract=False, project_name=project_name,
        )
        lesson.id = f"meta_lesson_harvest_{project_name}"
        LessonManager.save_pending(workspace_path, lesson)
        harvested.append(lesson)

    # ── trial 外部贡献 ──
    trial_accepts = [e for e in recent if e.operation in ("triage_accept", "triage_promote")]
    if trial_accepts:
        lesson = Lesson(
            tech_stack=tech_stack, category="workflow", severity="low",
            trigger=f"{len(trial_accepts)} external contributions processed via trial",
            rule="trial 仓库有外部贡献被 accept/promote。"
                  "定期检查 trial 仓库的健康状态和贡献质量。",
            source="auto_harvested", abstract=False, project_name=project_name,
        )
        lesson.id = f"trend_trial_{project_name}"
        LessonManager.save_pending(workspace_path, lesson)
        harvested.append(lesson)

    # ── formal commit 生命周期 ──
    delete_events = [e for e in recent if e.operation == "delete_formal"]
    if delete_events:
        lesson = Lesson(
            tech_stack=tech_stack, category="workflow", severity="low",
            trigger=f"{len(delete_events)} formal commits deleted",
            rule="formal commit 被删除。检查是否有 workflow 流程问题导致需要经常删除 formal commit。",
            source="auto_harvested", abstract=False, project_name=project_name,
        )
        lesson.id = f"trend_delete_{project_name}"
        LessonManager.save_pending(workspace_path, lesson)
        harvested.append(lesson)

    return harvested


def _harvest_from_git_log(
    workspace_path: Path,
    project_name: str,
    tech_stack: str = "",
) -> list[Lesson]:
    """从 workspace git log 中检测同一文件被反复修改的模式。

    例：连续 5 个 commit 都在改同一个文件 → 生成 pending lesson。
    不需要任何 scan history——只看 workspace 自己的 git log。
    """
    harvested = []
    try:
        import subprocess, sys
        result = subprocess.run(
            ["git", "log", "--format=%H|%s", "--name-only", "-30"],
            cwd=str(workspace_path), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        if result.returncode != 0:
            return harvested

        # 解析 git log: 每个 commit 后跟修改的文件列表
        file_commits: dict[str, list[str]] = {}
        current_commit = ""
        current_subject = ""
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if "|" in line and not line.startswith(" ") and not line.endswith((".py", ".md", ".json", ".txt", ".yaml", ".toml")):
                # commit line: hash|subject
                current_commit, current_subject = line.split("|", 1)
            elif current_commit and not line.startswith(" "):
                # file line
                file_commits.setdefault(line, []).append(current_subject)

        # Exclude build artifacts and version logs — not code, not knowledge
        _NOISE_EXTS = {".spec", ".json", ".txt", ".toml", ".cfg", ".ini", ".lock", ".pyc"}
        _NOISE_FILES = {"version.md", "pyproject.toml", "requirements.txt"}
        for path, subjects in file_commits.items():
            if len(subjects) >= 3:
                ext = Path(path).suffix.lower()
                fname = Path(path).name.lower()
                if ext in _NOISE_EXTS or fname in _NOISE_FILES:
                    continue
                lesson = Lesson(
                    tech_stack=tech_stack,
                    category="process",
                    severity="medium",
                    trigger=f"文件 {path} 被连续修改 {len(subjects)} 次（最近 commit）",
                    rule=f"文件 {path} 经历了多次修改后最终通过 sync 确认。"
                          f"最近修改: {', '.join(subjects[:3])}",
                    source="auto_harvested",
                    abstract=False,
                    project_name=project_name,
                    resolution_history={
                        "file": path,
                        "commit_count": len(subjects),
                        "recent_subjects": subjects[:5],
                        "show_by_default": False,
                    },
                )
                lesson.id = f"gitlog_{project_name}_{path.replace('/', '_').replace('.', '_')}"
                LessonManager.save_pending(workspace_path, lesson)
                harvested.append(lesson)

    except (OSError, subprocess.SubprocessError):
        pass

    return harvested


def _harvest_from_claude_md(
    workspace_path: Path,
    project_name: str,
    tech_stack: str = "",
) -> list[Lesson]:
    """从 workspace 的 CLAUDE.md 中提取已记录的教训/约束。

    解析标题含以下关键词的章节:
    - 中文: 已知问题 / 注意事项 / 约束 / 禁止 / 避坑 / 关键 / 打包
    - 英文: pitfall / constraint / rule / warning
    - 符号: ⚠️
    - 表格行: | 问题 | 原因 | 解决 | → 每行一条 lesson
    """
    harvested = []
    claude_md = workspace_path / "CLAUDE.md"
    if not claude_md.exists():
        return harvested

    try:
        content = claude_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return harvested

    import re

    # 匹配关键词
    section_kw = ("已知问题", "注意事项", "约束", "禁止", "避坑", "关键设计约束",
                  "打包", "踩坑", "API 差异", "API 改名",
                  "pitfall", "constraint", "rule", "warning", "⚠")

    # 按 H2 拆分大节，保留完整的子节内容
    h2_sections = re.split(r'^##\s+', content, flags=re.M)
    for h2 in h2_sections:
        lines = h2.strip().split("\n")
        title = lines[0].strip().lower() if lines else ""
        if not any(kw.lower() in title for kw in section_kw):
            continue

        # 该节（含所有子节）的全部文本作为收割范围
        body = "\n".join(lines[1:])

        # 提取列表项
        items = []
        for line in body.split("\n"):
            stripped = line.strip()
            # 列表项
            if (stripped.startswith("- ") or stripped.startswith("* ") or
                re.match(r'^\d+\.\s', stripped)):
                text = re.sub(r'^\d+\.\s+', '', stripped)
                text = text.lstrip('-* ').strip()
                if len(text) >= 10:
                    items.append(text)
            # 表格行
            elif stripped.startswith("|") and "---" not in stripped and "问题" not in stripped:
                parts = [p.strip() for p in stripped.split("|") if p.strip()]
                if len(parts) >= 3:
                    items.append(f"{parts[0]} -> {parts[-1]}")

        for item in items:
            if len(item) < 10:
                continue
            lesson = Lesson(
                tech_stack=tech_stack,
                category="documented",
                severity="high",
                trigger=title,
                rule=item[:200],
                source="auto_harvested",
                abstract=False,
                project_name=project_name,
            )
            lesson.id = f"claude_{project_name}_{hash(item) & 0xffff:04x}"
            LessonManager.save_pending(workspace_path, lesson)
            harvested.append(lesson)

    return harvested


def _harvest_from_scan_history(
    project_name: str,
    tech_stack: str = "",
) -> list[Lesson]:
    """从 scan history 检测跨轮次反复修改（原有逻辑）。"""
    harvested = []
    entries = HistoryManager.load()
    project_entries = [e for e in entries if e.project_name == project_name]

    if len(project_entries) < 2:
        return harvested

    recent = project_entries[-20:]
    scan_entries = [
        e for e in recent
        if e.operation == "scan" and e.detail and isinstance(e.detail, dict)
    ]
    if not scan_entries:
        return harvested

    file_occurrences: dict[str, list[str]] = {}
    for e in scan_entries:
        detail = e.detail or {}
        entries_list = detail.get("entries", [])
        for entry in entries_list if isinstance(entries_list, list) else []:
            if isinstance(entry, dict):
                path = entry.get("path", "")
                status = entry.get("status", "")
                if status != "same":
                    file_occurrences.setdefault(path, []).append(e.timestamp)

    for path, timestamps in file_occurrences.items():
        if len(timestamps) >= 3:
            lesson = Lesson(
                tech_stack=tech_stack,
                category="process",
                severity="medium",
                trigger=f"文件 {path} 在多次 sync 中反复修改（{len(timestamps)}次）",
                rule=f"跨轮次收割: 文件 {path} 经历了 {len(timestamps)} 次独立 sync 后才最终确认。",
                source="auto_harvested",
                abstract=False,
                project_name=project_name,
                resolution_history={
                    "file": path, "occurrences": timestamps,
                    "show_by_default": False,
                },
            )
            lesson.id = f"scan_{project_name}_{path.replace('/', '_').replace('.', '_')}"
            harvested.append(lesson)

    return harvested


# ── v0.35: 信号捕获 + 调度 + LLM 总结 ──────────────────────

MIN_BATCH_SIZE = 5
DENSITY_WINDOW = 50
DENSITY_THRESHOLD = 2.0
MIN_SOURCES = 2
COOLDOWN_SECONDS = 300
LLM_BATCH_MAX = 30
MAX_HARVEST_RETRY = 5

_last_harvest_time: dict[str, float] = {}


def capture_signal(signal_type: str, detail: dict, project_name: str) -> None:
    """捕获一条未处理信号 → HistoryManager (operation='unprocessed_signal')。"""
    HistoryManager.add_operation(
        project_name, "unprocessed_signal", "recorded",
        {"signal_type": signal_type, **detail},
    )


def get_unprocessed_signals(project_name: str,
                            signal_type: str | None = None) -> list[dict]:
    """获取未处理信号。signal_type=None → 全部类型。"""
    entries = HistoryManager.load()
    signals = []
    for e in entries:
        if e.operation != "unprocessed_signal":
            continue
        if e.project_name != project_name:
            continue
        d = e.detail if isinstance(e.detail, dict) else {}
        if signal_type and d.get("signal_type") != signal_type:
            continue
        signals.append({"timestamp": e.timestamp,
                        "correlation_id": e.correlation_id, **d})
    return signals


def get_signal_baseline(signal_type: str, project_name: str) -> float:
    """滚动 100 个事件的某类信号密度基线。"""
    entries = HistoryManager.load()
    recent = [e for e in entries[-100:]
              if e.project_name == project_name]
    if not recent:
        return 0.0
    matching = sum(
        1 for e in recent
        if e.operation == "unprocessed_signal"
        and (e.detail or {}).get("signal_type") == signal_type
    )
    return matching / len(recent)


def signal_density(project_name: str, signal_type: str | None = None,
                   window: int = 50) -> float:
    """最近 window 个事件中未处理信号的占比。"""
    entries = HistoryManager.load()
    recent = [e for e in entries[-window:]
              if e.project_name == project_name]
    if not recent:
        return 0.0
    matching = sum(
        1 for e in recent
        if e.operation == "unprocessed_signal"
        and (signal_type is None
             or (e.detail or {}).get("signal_type") == signal_type)
    )
    return matching / len(recent)


def source_diversity(signals: list[dict]) -> int:
    """统计信号不同的来源类型数。"""
    return len(set(s.get("signal_type", "unknown") for s in signals))


def should_trigger_harvest(signal_type: str, project_name: str) -> bool:
    """多维条件调度算法。事件驱动的，不是时间驱动的。"""
    signals = get_unprocessed_signals(project_name, signal_type)
    if len(signals) < MIN_BATCH_SIZE:
        return False
    baseline = get_signal_baseline(signal_type, project_name)
    density = signal_density(project_name, signal_type, DENSITY_WINDOW)
    if density <= baseline * DENSITY_THRESHOLD:
        return False
    if source_diversity(signals) < MIN_SOURCES:
        return False
    now = time.time()
    last = _last_harvest_time.get(signal_type, 0)
    if now - last < COOLDOWN_SECONDS:
        return False
    return True


def mark_harvest_triggered(signal_type: str) -> None:
    """记录 harvest 时间（冷却期用）。"""
    _last_harvest_time[signal_type] = time.time()


def is_testable_proposition(rule: str) -> bool:
    """LLM 输出门禁。不合规直接丢弃，不写入 pending。"""
    if len(rule) < 20:
        return False
    keywords = ["if", "when", "must", "should",
                "禁止", "必须", "需要先", "不能直接", "前需先"]
    return any(kw in rule.lower() for kw in keywords)


def harvest_llm_summary(signals: list[dict], llm_provider,
                        workspace_path: str, project_name: str) -> list[Lesson]:
    """LLM 总结未处理信号 → lesson。降级：失败→重试→退避→废弃。"""
    if not signals:
        return []

    batch = signals[:LLM_BATCH_MAX]
    signal_text = []
    for i, s in enumerate(batch):
        signal_text.append(
            f"信号{i+1}: type={s.get('signal_type')} "
            f"trigger={s.get('trigger','')} detail={s.get('detail',{})}"
        )

    prompt = (
        "你是项目知识收割 Agent。根据以下信号提取教训。\n\n"
        "严格要求:\n"
        "1. rule 必须是 'if X, then must/should/should not Y' 格式\n"
        "2. 不接受纯描述性 lesson\n"
        "3. trigger 必须能被子字符串匹配\n"
        "4. 信号不足以形成 actionable lesson → 返回 []\n"
        "5. 每条默认写入 pending\n\n"
        "返回 JSON: "
        '[{"trigger":"...","rule":"if X, then must Y",'
        '"severity":"high|medium|low","category":"process|...",'
        '"dangerous_tools":[],"prerequisite_tools":[],"required_tools":[]}]\n\n'
        f"信号 ({len(batch)} 条):\n" + "\n".join(signal_text)
    )

    try:
        response = llm_provider.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=4096, timeout=30,
        )
        text = response.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        lessons_data = json.loads(text.strip())
    except Exception:
        for s in batch:
            s["harvest_retry_count"] = s.get("harvest_retry_count", 0) + 1
        viable = [s for s in batch
                  if s.get("harvest_retry_count", 0) < MAX_HARVEST_RETRY]
        for s in viable:
            capture_signal(s.get("signal_type", "unknown"),
                           {k: v for k, v in s.items()
                            if k not in ("timestamp", "correlation_id")},
                           project_name)
        return []

    lessons = []
    for data in lessons_data if isinstance(lessons_data, list) else []:
        rule = data.get("rule", "")
        if not is_testable_proposition(rule):
            continue
        lessons.append(Lesson(
            trigger=data.get("trigger", ""), rule=rule,
            severity=data.get("severity", "medium"),
            category=data.get("category", "process"),
            dangerous_tools=data.get("dangerous_tools", []),
            prerequisite_tools=data.get("prerequisite_tools", []),
            required_tools=data.get("required_tools", []),
            source="auto_harvested", origin="harvest",
            project_name=project_name,
        ))
    return lessons


# ── v0.35: Pending 消化 ─────────────────────────────────────

def auto_discard_invalid(workspace_path: Path, project_name: str) -> int:
    """L1 Digest: 扫描 pending，自动 discard 明显无效的 lesson。

    判据: trigger 文件不存在 / rule 过短。
    """
    pending = LessonManager.load_pending(workspace_path, project_name)
    discarded = 0
    for lesson in pending:
        if lesson.trigger and "/" in lesson.trigger:
            if not (Path(workspace_path) / lesson.trigger).exists():
                LessonManager.discard_lesson(
                    workspace_path, lesson.id, project_name,
                )
                discarded += 1
                continue
        if len(lesson.rule) < 15:
            LessonManager.discard_lesson(
                workspace_path, lesson.id, project_name,
            )
            discarded += 1
    return discarded


def auto_verify_high_confidence(workspace_path: Path, project_name: str) -> int:
    """L2 Digest: LLM 自动 verify 高置信度 pending。

    只在 pending >= medium_threshold 时由 daemon 调用。
    目前为 stub（需要 LLM provider 注入），返回 0。
    """
    return 0
