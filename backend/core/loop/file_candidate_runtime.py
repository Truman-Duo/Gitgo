"""Task/runtime binding shared by trusted file services."""
class FileCandidateRuntime:
    def __init__(self, pipeline, ctx, registration, owner, tool):
        self.pipeline, self.ctx, self.registration = pipeline, ctx, registration
        self.tool, self.owner = tool, tuple(owner)
        self.workspace = str(ctx.workspace_path)
        self.session_id = ctx.session.session_id
        from backend.core.storage import get_storage
        self.storage = ctx.storage or get_storage(ctx.workspace_path)
        self.last_render = {}

    def is_current(self):
        from .permission_broker import tool_contract_digest
        return (self.owner == (self.ctx.process.process_id, self.ctx.process.active_task_id or self.ctx.process.process_id)
                and self.ctx.session.session_id == self.session_id and str(self.ctx.workspace_path) == self.workspace
                and tool_contract_digest(self.tool) == self.registration)
