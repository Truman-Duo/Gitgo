"""Compatibility policy using the same successful tool history."""
from .models import AgentProcess
from .harness.tool_history import tool_already_called as _tool_already_called
from .harness.tool_history import tools_already_called as _tools_already_called

def _policy_pre_check(tool_name: str, args: dict, process: AgentProcess) -> dict:
    """工具调用前检查 harness 规则（旧格式兼容路径）。

    检查项:
    - lesson_triggers 中标记的危险操作 → 验证前提工具已执行
    - contract_drift 文件上的写入操作 → 要求先执行 drift check
    """
    context, _ = process.read_context_snapshot()
    lessons = context.get("lesson_triggers", [])
    drift_files = [d.get("file", "") for d in context.get("contract_drift", [])]

    for lt in lessons:
        dangerous = lt.get("dangerous_tools", [])
        if tool_name in dangerous:
            prereqs = lt.get("prerequisite_tools", [])
            if prereqs and not _tools_already_called(process, prereqs):
                return {
                    "allowed": False,
                    "reason": f"Lesson '{lt.get('rule', '')}' 要求先执行: {prereqs}",
                }

    write_tools = {"formalize", "push", "sync"}
    if tool_name in write_tools:
        target_file = args.get("file", "")
        if target_file in drift_files:
            if not _tool_already_called(process, "scan"):
                return {
                    "allowed": False,
                    "reason": f"文件 {target_file} 有 contract drift，写入前请先 scan",
                }

    return {"allowed": True}


