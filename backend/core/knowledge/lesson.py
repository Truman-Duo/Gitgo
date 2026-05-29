"""Lesson System — 知识传承

抽象层（跨项目通用）+ 实例层（单项目具体）。
JSONL 格式，一行一条，方便追加和 grep。
自动收割：sync 成功后检测"反复修改→成功"模式。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from backend.core.history import HistoryManager

MEMORY_SOURCES = [".claude", ".codex", ".codebuddy"]

# 知识存储根目录（在 workspace 的 .gitgo/knowledge/ 下）
KNOWLEDGE_DIR = ".gitgo/knowledge"


# ── 数据模型 ─────────────────────────────────────────────

@dataclass
class Lesson:
    """一条知识教训。抽象层不含项目名/文件路径/人名。"""
    id: str = ""
    tech_stack: str = ""
    category: str = ""  # api_migration | architecture | dependency | process
    severity: str = "medium"  # low | medium | high | critical

    trigger: str = ""  # 什么情况下触发（LLM 判据）
    rule: str = ""     # 正确做法（LLM 判据）

    # 解析历史（默认截断，节省 token）
    resolution_history: dict | None = None

    # 检查规则（自动执行）
    check: dict | None = None

    # 元数据
    source: str = "manual"  # manual | auto_harvested
    abstract: bool = False  # True=抽象层, False=实例层
    project_name: str = ""
    verified_at: str = ""
    created_at: str = ""
    verified_count: int = 0
    verified_in: list[str] | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != 0}

    @classmethod
    def from_dict(cls, d: dict) -> Lesson:
        return cls(
            id=d.get("id", ""),
            tech_stack=d.get("tech_stack", ""),
            category=d.get("category", ""),
            severity=d.get("severity", "medium"),
            trigger=d.get("trigger", ""),
            rule=d.get("rule", ""),
            resolution_history=d.get("resolution_history"),
            check=d.get("check"),
            source=d.get("source", "manual"),
            abstract=d.get("abstract", False),
            project_name=d.get("project_name", ""),
            verified_at=d.get("verified_at", ""),
            created_at=d.get("created_at", ""),
            verified_count=d.get("verified_count", 0),
            verified_in=d.get("verified_in"),
        )


# ── 管理器 ───────────────────────────────────────────────

class LessonManager:
    """管理知识的读写和搜索。"""

    @staticmethod
    def _abstract_dir(workspace_path: Path) -> Path:
        return workspace_path / KNOWLEDGE_DIR / "abstract"

    @staticmethod
    def _instance_dir(workspace_path: Path, project_name: str) -> Path:
        return workspace_path / KNOWLEDGE_DIR / "instances" / project_name

    @staticmethod
    def _abstract_path(workspace_path: Path, tech_stack: str) -> Path:
        name = tech_stack.replace("/", "_").replace(" ", "_")
        return LessonManager._abstract_dir(workspace_path) / f"{name}.jsonl"

    @staticmethod
    def _instance_path(workspace_path: Path, project_name: str) -> Path:
        return LessonManager._instance_dir(workspace_path, project_name) / "lessons.jsonl"

    @staticmethod
    def _pending_path(workspace_path: Path, project_name: str) -> Path:
        return LessonManager._instance_dir(workspace_path, project_name) / "pending.jsonl"

    # ── 读取 ────────────────────────────────────────────

    @staticmethod
    def load_abstract(workspace_path: Path, tech_stack: str = "") -> list[Lesson]:
        """加载抽象层知识。tech_stack 为空时加载全部。"""
        lessons = []
        ad = LessonManager._abstract_dir(workspace_path)
        if not ad.exists():
            return lessons
        for fp in sorted(ad.glob("*.jsonl")):
            if tech_stack and fp.stem.replace("_", " ") != tech_stack.replace("/", "_").replace(" ", "_"):
                continue
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    lessons.append(Lesson.from_dict(json.loads(line)))
                except json.JSONDecodeError:
                    continue
        return lessons

    @staticmethod
    def load_instance(workspace_path: Path, project_name: str) -> list[Lesson]:
        """加载实例层知识。"""
        fp = LessonManager._instance_path(workspace_path, project_name)
        if not fp.exists():
            return []
        lessons = []
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lessons.append(Lesson.from_dict(json.loads(line)))
            except json.JSONDecodeError:
                continue
        return lessons

    @staticmethod
    def load_pending(workspace_path: Path, project_name: str) -> list[Lesson]:
        """加载待确认的自动收割草稿。"""
        fp = LessonManager._pending_path(workspace_path, project_name)
        if not fp.exists():
            return []
        lessons = []
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lessons.append(Lesson.from_dict(json.loads(line)))
            except json.JSONDecodeError:
                continue
        return lessons

    # ── 写入 ────────────────────────────────────────────

    @staticmethod
    def save(workspace_path: Path, lesson: Lesson) -> Path:
        """保存一条知识。根据 abstract 标志决定写入位置。"""
        if lesson.abstract:
            fp = LessonManager._abstract_path(workspace_path, lesson.tech_stack)
        else:
            fp = LessonManager._instance_path(workspace_path, lesson.project_name)
        fp.parent.mkdir(parents=True, exist_ok=True)

        if not lesson.id:
            lesson.id = f"{lesson.tech_stack or 'general'}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        if not lesson.created_at:
            lesson.created_at = datetime.now().isoformat()

        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(lesson.to_dict(), ensure_ascii=False) + "\n")
        return fp

    @staticmethod
    def save_pending(workspace_path: Path, lesson: Lesson) -> Path:
        """保存自动收割草稿（去重：相同 id 不重复写入）。"""
        fp = LessonManager._pending_path(workspace_path, lesson.project_name)
        fp.parent.mkdir(parents=True, exist_ok=True)
        if not lesson.id:
            lesson.id = f"pending_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        if not lesson.created_at:
            lesson.created_at = datetime.now().isoformat()
        lesson.source = "auto_harvested"

        # 去重：检查相同 id 是否已存在
        existing_ids = set()
        if fp.exists():
            for line in fp.read_text(encoding="utf-8").splitlines():
                try:
                    existing = json.loads(line.strip())
                    existing_ids.add(existing.get("id", ""))
                except json.JSONDecodeError:
                    continue
        if lesson.id in existing_ids:
            return fp  # 跳过重复

        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(lesson.to_dict(), ensure_ascii=False) + "\n")
        return fp

    # ── 操作 ────────────────────────────────────────────

    @staticmethod
    def verify(workspace_path: Path, lesson_id: str, project_name: str = "") -> Lesson | None:
        """确认一条知识（从 pending 转为正式，或增加 verified_count）。"""
        # 先查 pending
        if project_name:
            pending = LessonManager.load_pending(workspace_path, project_name)
            for i, p in enumerate(pending):
                if p.id == lesson_id:
                    pending.pop(i)
                    # 重写 pending 文件
                    pp = LessonManager._pending_path(workspace_path, project_name)
                    pp.write_text("\n".join(
                        json.dumps(l.to_dict(), ensure_ascii=False) for l in pending
                    ) + ("\n" if pending else ""), encoding="utf-8")
                    # 保存到正式
                    p.verified_at = datetime.now().isoformat()
                    p.verified_count = 1
                    p.source = "auto_harvested"
                    LessonManager.save(workspace_path, p)
                    return p

        # 再查实例层
        if project_name:
            lessons = LessonManager.load_instance(workspace_path, project_name)
            for l in lessons:
                if l.id == lesson_id:
                    l.verified_count += 1
                    l.verified_at = datetime.now().isoformat()
                    l.verified_in = (l.verified_in or []) + [project_name]
                    LessonManager.save(workspace_path, l)
                    return l

        # 查抽象层
        abstract = LessonManager.load_abstract(workspace_path)
        for l in abstract:
            if l.id == lesson_id:
                l.verified_count += 1
                l.verified_at = datetime.now().isoformat()
                l.verified_in = (l.verified_in or []) + [project_name]
                LessonManager.save(workspace_path, l)
                return l

        return None

    @staticmethod
    def promote_to_abstract(
        workspace_path: Path, lesson_id: str,
        project_name: str, tech_stack: str,
    ) -> Lesson | None:
        """将实例层知识提升为抽象层。"""
        lessons = LessonManager.load_instance(workspace_path, project_name)
        for l in lessons:
            if l.id == lesson_id:
                l.abstract = True
                l.tech_stack = tech_stack
                l.project_name = ""  # 抽象层不存项目名
                LessonManager.save(workspace_path, l)
                return l
        return None

    @staticmethod
    def search(
        workspace_path: Path,
        query: str,
        project_name: str = "",
        tech_stack: str = "",
    ) -> list[Lesson]:
        """在抽象层和实例层中搜索。"""
        results = []
        q = query.lower()
        for l in LessonManager.load_abstract(workspace_path, tech_stack):
            text = json.dumps(l.to_dict(), ensure_ascii=False).lower()
            if q in text:
                results.append(l)
        if project_name:
            for l in LessonManager.load_instance(workspace_path, project_name):
                text = json.dumps(l.to_dict(), ensure_ascii=False).lower()
                if q in text:
                    results.append(l)
        return results


# ── 自动收割 ─────────────────────────────────────────────

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

        for path, subjects in file_commits.items():
            if len(subjects) >= 3:
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
