"""Explicit logical project registration inside a closed checkpoint plan.

Physical storage initialization creates no business rows. The first qualified
checkpoint may include this registration in the same State/outbox transaction.
Workspace text is a display hint, never filesystem access authority.
"""
from dataclasses import dataclass

from .host_records import guid, require


def _metadata_text(value):
    if type(value) is not str or not 0 < len(value) <= 8192:
        return False
    try:
        return len(value.encode("utf8", errors="strict")) <= 8192
    except UnicodeError:
        return False


@dataclass(frozen=True)
class ProjectRegistration:
    project_id: str
    workspace_hint: str
    created_at: str

    def __post_init__(self):
        guid(self.project_id)
        require(all(_metadata_text(value) for value in (self.workspace_hint, self.created_at)),
                "bounded immutable project metadata")


@dataclass(frozen=True)
class ProjectBasis:
    request: ProjectRegistration
    previous: tuple[str, str, str, str] | None

    def __post_init__(self):
        require(type(self.request) is ProjectRegistration, "explicit project registration")
        require(self.previous is None or type(self.previous) is tuple and len(self.previous) == 4
                and all(_metadata_text(value) for value in self.previous),
                "immutable bounded project registration basis")

    def append(self, builder):
        if self.previous is not None:
            require(self.previous[:2] == (self.request.project_id, self.request.workspace_hint),
                    "existing logical project or workspace hint changed")
        if self.previous is None:
            builder.execute(
                "INSERT INTO projects(project_id,workspace_hint,created_at,updated_at) VALUES(?,?,?,?)",
                (self.request.project_id, self.request.workspace_hint,
                 self.request.created_at, self.request.created_at),
            )


def capture_project_basis(db, request):
    require(type(request) is ProjectRegistration and db.in_transaction,
            "qualified registration needs the shared planning snapshot")
    # Refuse malformed retained fields before returning their full values to
    # Python. The admitted owner still bounds SQLite pages, heap and work.
    fields = ("project_id", "workspace_hint", "created_at", "updated_at")
    columns = ",".join(
        f"CASE WHEN typeof({field})='text' "
        f"AND length(CAST({field} AS BLOB)) BETWEEN 1 AND 8192 THEN {field} END"
        for field in fields
    )
    cursor = db.execute("SELECT " + columns + " FROM projects LIMIT 2")
    try:
        rows = cursor.fetchall()
    finally:
        cursor.close()
    require(len(rows) <= 1, "one logical project in the protected store")
    return ProjectBasis(request, tuple(rows[0]) if rows else None)
