"""Shared helpers for MCP tool modules."""


def get_config():
    from backend.core.config import ConfigManager
    return ConfigManager.load()


def get_project(project_name: str):
    cfg = get_config()
    for p in cfg.projects:
        if p.name == project_name:
            return cfg, p
    return cfg, None


def init_session(project_name: str):
    """Initialize SyncSession without scan."""
    from cli.commands import _init_session as cli_init
    cfg = get_config()
    return cli_init(cfg, project_name, with_scan=False)


def init_session_with_scan(project_name: str):
    """Initialize SyncSession with scan + load commits + trial check."""
    from cli.commands import _init_session as cli_init
    cfg = get_config()
    return cli_init(cfg, project_name, with_scan=True)


def build_suggest_result(project_name: str, suggest_type: str) -> dict:
    """Shared helper: init session, build suggest context, return full result dict."""
    cfg, proj = get_project(project_name)
    if proj is None:
        return {"error": "PROJECT_NOT_FOUND", "project": project_name}
    from backend.core.sync_session import SyncSession
    from cli.commands import (
        _build_formalize_context,
        _build_triage_context,
        _build_summary_context,
    )
    session = SyncSession(proj, cfg)
    if suggest_type == "formalize":
        session.step_scan()
        session.step_load_commits()
        context = _build_formalize_context(session)
    elif suggest_type == "triage":
        session.step_check_trial()
        context = _build_triage_context(session)
    elif suggest_type == "summary":
        context = _build_summary_context(session)
    else:
        return {"error": "UNKNOWN_SUGGEST_TYPE", "suggest_type": suggest_type}
    return {"suggest": suggest_type, "project": project_name, "context": context}
