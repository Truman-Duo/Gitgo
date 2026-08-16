"""Session 持久化 — save_session / load_session。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from backend.core.config import Config, ProjectConfig

from backend.core.sync_session.models import SessionStage, FormalCommit


class PersistenceMixin:
    def save_session(self) -> Path:
        """持久化当前 session 状态到 .gitgo/session.json"""
        import json
        from backend.models import TrialAction

        session_dir = self.workspace_path / ".gitgo"
        session_dir.mkdir(exist_ok=True)
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
        path = session_dir / "session.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load_session(cls, project: ProjectConfig, config: Config):
        """从 .gitgo/session.json 恢复 session。返回 None 如果文件不存在。"""
        import json

        path = Path(project.workspace_path or Path.cwd()) / ".gitgo" / "session.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        session = cls(project, config)
        session.stage = SessionStage[data.get("stage", "IDLE")]
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
            session.formal_commits.append(fc)
        return session
