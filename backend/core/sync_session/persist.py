"""Session 持久化 — save_session / load_session。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from backend.core.config import Config, ProjectConfig

from backend.core.sync_session.models import SessionStage, FormalCommit


class PersistenceMixin:
    def save_session(self) -> Path:
        """Publish a versioned project state; preserve legacy import sources."""
        from backend.models import TrialAction

        data = {
            "project": self.project.name,
            "updated_at": datetime.now().isoformat(),
            "stage": self.stage.name,
            "entries_summary": {
                "total": len(self.entries),
                "new": sum(1 for e in self.entries if e.status == "new"),
                "modified": sum(1 for e in self.entries if e.status == "modified"),
            },
            "workspace_commits_since_base": len(self.commits),
            "formal_commits": [
                {
                    "message": fc.message,
                    "number": fc.number,
                    "prefix": fc.prefix,
                    "synced": fc.synced,
                    "pushed": fc.pushed,
                    "is_incoming": fc.is_incoming,
                    "sources_cleared": fc.sources_cleared,
                    "source_indices": list(fc.source_indices),
                    "created_at": fc.created_at,
                }
                for fc in self.formal_commits
            ],
            "incoming_summary": {
                "total": len(self.incoming_changes),
                "pending": sum(1 for c in self.incoming_changes
                              if c.triage == TrialAction.PENDING),
            },
            "last_operation": getattr(self, '_last_op', None),
        }
        from .state import save_state
        reference, path = save_state(self.workspace_path, data,
            expected_ref=getattr(self, "_governance_state_ref", None))
        self._governance_state_ref = reference
        self._governance_state_present = True
        return path

    def _restore_saved_session(self):
        from .state import read_state
        data, reference = read_state(self.workspace_path)
        self._governance_state_ref = reference
        self._governance_state_present = data is not None
        if data is None:
            return
        self.stage = SessionStage[data.get("stage", "IDLE")]
        self.formal_commits = []
        for fc_data in data.get("formal_commits", []):
            fc = FormalCommit(
                message=fc_data["message"],
                number=fc_data["number"],
                prefix=fc_data["prefix"],
                synced=fc_data.get("synced", False),
                pushed=fc_data.get("pushed", False),
                is_incoming=fc_data.get("is_incoming", False),
                sources_cleared=fc_data.get("sources_cleared", False),
                source_indices=set(fc_data.get("source_indices", [])),
                created_at=fc_data.get("created_at", ""),
            )
            self.formal_commits.append(fc)
        self._last_op = data.get("last_operation")

    @classmethod
    def load_session(cls, project: ProjectConfig, config: Config):
        session = cls(project, config)
        return session if session._governance_state_present else None
