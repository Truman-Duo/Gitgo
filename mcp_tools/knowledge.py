"""MCP tools — knowledge lessons and commit templates."""

from mcp_tools.helpers import get_config, get_project


def register(mcp):
    """Register lesson and template tools on FastMCP instance."""

    # ── Lesson Tools ──

    @mcp.tool(description="列出项目的所有知识教训（含抽象层、实例层、待确认草稿）。")
    def gitgo_lesson_list(project: str) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        from backend.core.knowledge.lesson import LessonManager
        from pathlib import Path
        session = SyncSession(proj, cfg)
        ws = Path(session.workspace_path)
        return {
            "abstract": [l.to_dict() for l in LessonManager.load_abstract(ws)],
            "instances": [l.to_dict() for l in LessonManager.load_instance(ws, project)],
            "pending": [l.to_dict() for l in LessonManager.load_pending(ws, project)],
        }

    @mcp.tool(description="确认一条知识教训（从 pending 转为正式，或增加 verified_count）。")
    def gitgo_lesson_verify(project: str, lesson_id: str) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        from backend.core.knowledge.lesson import LessonManager
        from pathlib import Path
        session = SyncSession(proj, cfg)
        ws = Path(session.workspace_path)
        result = LessonManager.verify(ws, lesson_id, project_name=project)
        if result:
            return {"verified": lesson_id, "verified_count": result.verified_count}
        return {"error": "LESSON_NOT_FOUND", "lesson_id": lesson_id}

    @mcp.tool(description="搜索知识教训（在抽象层和实例层中全文搜索）。")
    def gitgo_lesson_search(project: str, query: str, tech_stack: str = "") -> list[dict]:
        cfg, proj = get_project(project)
        if proj is None:
            return [{"error": "PROJECT_NOT_FOUND", "project": project}]
        from backend.core.sync_session import SyncSession
        from backend.core.knowledge.lesson import LessonManager
        from pathlib import Path
        session = SyncSession(proj, cfg)
        ws = Path(session.workspace_path)
        results = LessonManager.search(ws, query, project_name=project, tech_stack=tech_stack)
        return [l.to_dict() for l in results]

    @mcp.tool(description="将实例层知识提升为抽象层（跨项目通用）。")
    def gitgo_lesson_promote(project: str, lesson_id: str, tech_stack: str) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        from backend.core.knowledge.lesson import LessonManager
        from pathlib import Path
        session = SyncSession(proj, cfg)
        ws = Path(session.workspace_path)
        result = LessonManager.promote_to_abstract(ws, lesson_id, project_name=project, tech_stack=tech_stack)
        if result:
            return {"promoted": lesson_id, "tech_stack": tech_stack}
        return {"error": "LESSON_NOT_FOUND", "lesson_id": lesson_id}

    # ── Template Tools ──

    @mcp.tool(description="列出所有可用的 commit message 模板（含默认模板）。")
    def gitgo_template_list() -> list[dict]:
        from backend.core.template_manager import TemplateManager
        templates = TemplateManager.load()
        return [{"name": t.name, "description": t.description,
                 "header_format": t.header_format, "body_format": t.body_format,
                 "prefix_override": t.prefix_override} for t in templates]

    @mcp.tool(description="添加新的 commit message 模板。header_format/body_format 使用 {prefix}/{number}/{type_str}/{scope_str}/{subject}/{project_name}/{commit_count}/{commit_list} 变量。")
    def gitgo_template_add(name: str, description: str, header_format: str, body_format: str, prefix_override: str | None = None) -> dict:
        from backend.core.template_manager import TemplateManager, CommitTemplate
        templates = TemplateManager.load()
        if any(t.name == name for t in templates):
            return {"error": "TEMPLATE_EXISTS", "name": name}
        tpl = CommitTemplate(name=name, description=description,
                             header_format=header_format, body_format=body_format,
                             prefix_override=prefix_override)
        templates.append(tpl)
        TemplateManager.save(templates)
        return {"added": name}

    @mcp.tool(description="更新已有模板的字段。只需传要更新的字段。")
    def gitgo_template_edit(name: str, description: str | None = None, header_format: str | None = None, body_format: str | None = None, prefix_override: str | None = None) -> dict:
        from backend.core.template_manager import TemplateManager
        templates = TemplateManager.load()
        idx = next((i for i, t in enumerate(templates) if t.name == name), None)
        if idx is None:
            return {"error": "TEMPLATE_NOT_FOUND", "name": name}
        t = templates[idx]
        if description is not None:
            t.description = description
        if header_format is not None:
            t.header_format = header_format
        if body_format is not None:
            t.body_format = body_format
        if prefix_override is not None:
            t.prefix_override = prefix_override
        TemplateManager.save(templates)
        return {"updated": name}

    @mcp.tool(description="删除模板（不可删除 'default'）。")
    def gitgo_template_delete(name: str) -> dict:
        if name == "default":
            return {"error": "CANNOT_DELETE_DEFAULT"}
        from backend.core.template_manager import TemplateManager
        templates = TemplateManager.load()
        idx = next((i for i, t in enumerate(templates) if t.name == name), None)
        if idx is None:
            return {"error": "TEMPLATE_NOT_FOUND", "name": name}
        templates.pop(idx)
        TemplateManager.save(templates)
        return {"deleted": name}
