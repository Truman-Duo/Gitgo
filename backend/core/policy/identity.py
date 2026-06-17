"""Identity integrity — check identity files haven't been deleted or overwritten."""

from typing import TYPE_CHECKING
from backend.core.policy.base import PolicyCheck

if TYPE_CHECKING:
    from backend.core.sync_session import SyncSession
    from backend.core.config import ProjectConfig


class IdentityIntegrityCheck(PolicyCheck):
    name = "identity_integrity"
    description = "Check identity files for deletion or mass override"

    def check(self, session: "SyncSession",
              project: "ProjectConfig") -> list[dict]:
        from backend.core.identity import _run_integrity_checks
        return _run_integrity_checks(
            session.entries, session.workspace_path, project)
