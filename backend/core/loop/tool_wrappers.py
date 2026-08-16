"""AgentTool 包装器 —— 后端函数的薄适配层。

每个函数签名 (args: dict) -> dict，内部只做三件事：
1. 从 args dict 解包参数 + 类型强转（str → Path）
2. 调用后端函数
3. 返回值序列化（Lesson → .to_dict(), bool → {"ok": bool}）

不重写任何业务逻辑。
"""

from __future__ import annotations

from pathlib import Path

from backend.core.contract import (
    detect_drift,
    get_dependents,
    get_callers,
    get_changed_symbols,
    ContractManager,
)
from backend.core.knowledge.harvest import harvest_lessons
from backend.core.knowledge.manager import LessonManager
from backend.core.authorship import scan_files_privacy
from backend.core.identity.snapshot import (
    snapshot_tool_memories,
    restore_tool_memories,
    list_memory_snapshots,
)


# ═══════════════════════════════════════════════════════════════
# Contract 合约工具
# ═══════════════════════════════════════════════════════════════

def contract_detect_drift(args: dict) -> dict:
    """检测本轮变更与合约的偏差。"""
    try:
        workspace = Path(args["workspace_path"])
        changed_files = args.get("changed_files", [])
        contract_path = args.get("contract_path", "")
        contract = ContractManager.load(Path(contract_path)) if contract_path else None
        result = detect_drift(workspace, changed_files, contract)
        return {"drifts": result, "count": len(result)}
    except Exception as e:
        return {"error": str(e)}


def contract_get_impact(args: dict) -> dict:
    """查询文件的影响面：哪些文件依赖它，哪些函数调用了它。"""
    try:
        workspace = Path(args["workspace_path"])
        file_path = args["file_path"]
        func_name = args.get("func_name", "")
        dependents = get_dependents(workspace, file_path)
        callers = get_callers(workspace, file_path, func_name)
        return {
            "file_path": file_path,
            "dependents": dependents,
            "callers": callers,
            "dependent_count": len(dependents),
            "caller_count": len(callers),
        }
    except Exception as e:
        return {"error": str(e)}


def contract_get_changed_symbols(args: dict) -> dict:
    """对比文件两个版本的 AST，返回变更的函数/类名。"""
    try:
        file_path = Path(args["file_path"])
        old_content = args.get("old_content", "")
        new_content = args.get("new_content", "")
        symbols = get_changed_symbols(file_path, old_content, new_content)
        return {"symbols": symbols, "count": len(symbols)}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# Knowledge 知识库工具
# ═══════════════════════════════════════════════════════════════

def lesson_search(args: dict) -> dict:
    """在抽象层和实例层中搜索经验教训。"""
    try:
        workspace = Path(args["workspace_path"])
        query = args["query"]
        project_name = args.get("project_name", "")
        tech_stack = args.get("tech_stack", "")
        results = LessonManager.search(workspace, query, project_name, tech_stack)
        return {
            "lessons": [l.to_dict() for l in results],
            "count": len(results),
        }
    except Exception as e:
        return {"error": str(e)}


def lesson_discard(args: dict) -> dict:
    """删除一条经验教训。"""
    try:
        workspace = Path(args["workspace_path"])
        lesson_id = args["lesson_id"]
        project_name = args.get("project_name", "")
        ok = LessonManager.discard_lesson(workspace, lesson_id, project_name)
        return {"ok": ok, "lesson_id": lesson_id}
    except Exception as e:
        return {"error": str(e)}


def lesson_verify(args: dict) -> dict:
    """确认一条知识（从 pending 转为正式，或增加 verified_count）。"""
    try:
        workspace = Path(args["workspace_path"])
        lesson_id = args["lesson_id"]
        project_name = args.get("project_name", "")
        result = LessonManager.verify(workspace, lesson_id, project_name)
        if result is None:
            return {"verified": False, "lesson_id": lesson_id,
                    "reason": "lesson not found in pending/instance/abstract"}
        return {"verified": True, "lesson": result.to_dict()}
    except Exception as e:
        return {"error": str(e)}


def lesson_harvest(args: dict) -> dict:
    """从 git log、CLAUDE.md、scan history、governance signals 收割新 lesson。"""
    try:
        workspace = Path(args["workspace_path"])
        project_name = args["project_name"]
        tech_stack = args.get("tech_stack", "")
        lessons = harvest_lessons(workspace, project_name, tech_stack)
        return {
            "lessons": [l.to_dict() for l in lessons],
            "count": len(lessons),
            "project_name": project_name,
        }
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# Privacy 隐私工具
# ═══════════════════════════════════════════════════════════════

def privacy_scan(args: dict) -> dict:
    """扫描变更文件的隐私风险（敏感信息、AI 痕迹等）。"""
    try:
        workspace = args["workspace_path"]
        file_list = args.get("file_list", [])
        level = args.get("level", 2)
        deep_scan = args.get("deep_scan", False)
        alerts = scan_files_privacy(workspace, file_list, level, deep_scan)
        return {"alerts": alerts, "count": len(alerts)}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# Identity / Memory 工具
# ═══════════════════════════════════════════════════════════════

def memory_snapshot(args: dict) -> dict:
    """快照工具记忆到 backup 目录，并列出所有可用快照。"""
    try:
        workspace = args["workspace_path"]
        backup = args["backup_path"]
        snap_result = snapshot_tool_memories(workspace, backup, None)
        all_snapshots = list_memory_snapshots(backup)
        return {
            "snapshot": snap_result,
            "all_snapshots": all_snapshots,
        }
    except Exception as e:
        return {"error": str(e)}


def memory_restore(args: dict) -> dict:
    """从 backup 的快照恢复工具记忆到 workspace。"""
    try:
        backup = args["backup_path"]
        workspace = args["workspace_path"]
        timestamp = args.get("snapshot_timestamp", None)
        result = restore_tool_memories(backup, workspace, timestamp)
        return result
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# Knowledge 补充
# ═══════════════════════════════════════════════════════════════

def lesson_promote(args: dict) -> dict:
    """将实例层经验教训提升为抽象层。"""
    try:
        workspace = Path(args["workspace_path"])
        lesson_id = args["lesson_id"]
        project_name = args.get("project_name", "")
        tech_stack = args.get("tech_stack", "")
        result = LessonManager.promote_to_abstract(
            workspace, lesson_id, project_name, tech_stack)
        if result is None:
            return {"promoted": False, "lesson_id": lesson_id,
                    "reason": "lesson not found in instances"}
        return {"promoted": True, "lesson": result.to_dict()}
    except Exception as e:
        return {"error": str(e)}


def lesson_list(args: dict) -> dict:
    """列出所有经验教训（抽象层 + 实例层 + 待确认）。"""
    try:
        workspace = Path(args["workspace_path"])
        project_name = args.get("project_name", "")
        abstract = LessonManager.load_abstract(workspace)
        instances = LessonManager.load_instance(workspace, project_name)
        pending = LessonManager.load_pending(workspace, project_name)
        return {
            "abstract": [l.to_dict() for l in abstract],
            "instances": [l.to_dict() for l in instances],
            "pending": [l.to_dict() for l in pending],
            "count_total": len(abstract) + len(instances) + len(pending),
        }
    except Exception as e:
        return {"error": str(e)}
