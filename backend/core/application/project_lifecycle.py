"""Project catalog lifecycle, independent of runtime state and file deletion."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import stat

DEFAULT_UNAVAILABLE_ARCHIVE_MINUTES = 60

def workspace_missing(workspace: str) -> bool:
    # stat distinguishes missing paths from access/storage failures. Only a
    # proven missing directory can schedule automatic catalog archiving.
    if not str(workspace).strip():
        return True
    try:
        return not stat.S_ISDIR(Path(workspace).stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return True

def archive_delay(config) -> int:
    value = config.safety.get("unavailable_project_archive_delay_minutes", DEFAULT_UNAVAILABLE_ARCHIVE_MINUTES)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 43200:
        raise ValueError("Invalid unavailable project archive delay")
    return value

def set_archived(project, archived: bool) -> None:
    """Shared manual/automatic archive transition; never deletes storage."""
    project.archived = archived
    project.unavailable_entry = {}

def maintain_unavailable_projects(config, *, now=None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    delay = archive_delay(config)
    changes = []
    for project in config.projects:
        entry = project.unavailable_entry
        if not entry:
            continue
        if project.archived or entry.get("workspace") != project.workspace_path:
            project.unavailable_entry = {}
            changes.append({"project": project.name, "state": "schedule_cleared"})
            continue
        try:
            missing = workspace_missing(project.workspace_path)
        except OSError:
            continue  # Unknown access state must not authorize archiving.
        if not missing:
            project.unavailable_entry = {}
            changes.append({"project": project.name, "state": "workspace_restored"})
            continue
        if not delay:
            continue  # zero disables automatic archive, not project entry checks
        started = datetime.fromisoformat(entry["first_failed_at"])
        if now >= started + timedelta(minutes=delay):
            set_archived(project, True)
            changes.append({"project": project.name, "state": "archived"})
    return changes
