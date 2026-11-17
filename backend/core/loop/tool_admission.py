"""Task Runtime's consumer at the canonical tool admission boundary.

This coordinates task generations and advancement, not permission or native
isolation. The Pipeline still consumes grants and executes every composite
leaf. Registered mechanism components extend this existing boundary; they do
not get an alternative launch path or an authorization result to forge.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import re

from .operation_policy import is_effectful_mutation


class AdmissionError(RuntimeError):
    def __init__(self, code, message, *, subsystem="task_runtime", nature="system", cause_code=""):
        super().__init__(message)
        self.code, self.subsystem, self.nature = code, subsystem, nature
        self.cause_code = cause_code

    def diagnostics(self):
        return {"code": self.code, "nature": self.nature, "subsystem": self.subsystem,
                "cause_code": self.cause_code, "effect_state": "not_committed", "execution_state": "not_started"}


@contextmanager
def runtime_check():
    try:
        yield
    except AdmissionError:
        raise
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        candidate = str(getattr(exc, "code", "") or str(exc).split(":", 1)[0])
        cause = candidate if re.fullmatch(r"[A-Z][A-Z0-9_]{2,99}", candidate) else type(exc).__name__
        mechanism = cause.startswith(("MECHANISM_", "ADVANCEMENT_"))
        from .budget import TaskBudgetExceeded
        code = exc.code if isinstance(exc, TaskBudgetExceeded) else (
            "MECHANISM_ADMISSION_BLOCKED" if mechanism else "TOOL_RUNTIME_ADMISSION_FAILED")
        raise AdmissionError(code, str(exc), nature="governance" if mechanism else "system",
                             cause_code=cause) from exc


def refresh_tool_consumers(ctx):
    from .mechanism_lifecycle import fence
    with runtime_check(), fence(ctx.process):
        ctx.synchronize_tools()


@dataclass(frozen=True)
class AdmissionHold:
    kind: str
    value: dict


class RuntimeToolAdmission:
    def __init__(self, ctx, tool, raw_args, args, execution_id, call_index, call_id, metadata, alignment_check):
        self.ctx, self.tool = ctx, tool
        self.raw_args, self.args = raw_args, args
        self.execution_id, self.call_index, self.call_id = execution_id, call_index, call_id
        self.metadata, self.alignment_check = metadata, alignment_check
        self.composition = None

    def prepare(self):
        with runtime_check():
            from backend.core.mechanism_runtime.application import bound_application
            self.composition = bound_application(self.ctx.process)
            if not self.composition or self.tool.execution_contract.broker == "host.runtime.harness_mechanism":
                return None
            from .mechanisms import unit_identity
            from backend.core.mechanism_runtime.contracts import digest
            unit = unit_identity(self.ctx.process, self.tool, self.raw_args,
                                 self.execution_id, self.call_index, self.call_id)
            preview = {str(key): (value[:2000] if isinstance(value, str)
                else value if type(value) in {bool, int, float, type(None)}
                else {"shape": type(value).__name__, "digest": digest(value)})
                for key, value in list(dict(self.raw_args or {}).items())[:16]}
            pause = self.composition.before_tool(unit, {"tool_name": self.tool.name,
                "arguments": preview, "arguments_digest": digest(self.raw_args),
                "arguments_are_preview": preview != self.raw_args,
                "effect": str(getattr(self.tool.effect, "value", self.tool.effect))})
            self.metadata["harness_unit"] = unit
            if pause:
                return AdmissionHold("mechanism", pause)
            self.metadata["harness_instances"] = [{"id": value["id"], "epoch": value["epoch"]}
                for value in self.composition.repo.list_instances(self.composition.task, limit=100)
                if value["phase"] == "ready"]

    @contextmanager
    def boundary(self):
        """Same activation fence through the Pipeline's final grant consumption."""
        from .mechanism_lifecycle import fence
        with runtime_check(), fence(self.ctx.process):
            yield self._check_current()

    def _check_current(self):
        ctx, tool = self.ctx, self.tool
        if (ctx.process.recovery or {}).get("checkpoint_commit") == "unknown":
            # A peer checkpoint may have become uncertain after the provider
            # started. Reject only new admission; a governance stop must not
            # roll back an already executed batch prefix.
            # Other recovery flags retain their own gates: native cleanup debt
            # must still permit Host-only inspection of the retained evidence.
            raise AdmissionError("PROCESS_RECOVERY_REQUIRED",
                "Inspect the retained checkpoint before admitting another tool.",
                nature="governance")
        if self.composition:
            for expected in self.metadata.get("harness_instances", []):
                current, _ = self.composition.repo.instance(self.composition.task, expected["id"])
                if current["phase"] != "ready" or current["epoch"] != expected["epoch"]:
                    raise ValueError("MECHANISM_GENERATION_CHANGED_BEFORE_TOOL_ADMISSION")
        if is_effectful_mutation(tool.effect):
            from .task_maintenance import guard, synchronize, state_for
            from .permission_broker import tool_contract_digests
            from .mechanism_catalog import digest
            ctx.synchronize_tools()
            synchronize(ctx.process, ctx.tool_dependencies or ctx.tool_catalog or {tool.name: tool})
            error = guard(ctx.process, tool.name)
            mounted = ctx.tool_catalog
            if error is None and state_for(ctx.process) and mounted:
                projection = getattr(ctx.process, "_mechanism_projection", {})
                if projection.get("tool_digest") != digest(tool_contract_digests(mounted)):
                    error = ("TASK_MANIFEST_NOT_READY", "Tool registrations changed after prompt compilation; refresh the current generation before admission.")
            if error:
                raise AdmissionError(*error, nature="governance")
        if "task_contract_revision" in self.metadata:
            current = int((ctx.process.read_context_snapshot()[0].get("task_contract") or {}).get("revision", 0))
            error = self.alignment_check(ctx.process, tool, self.args)
            if current != self.metadata["task_contract_revision"] or error:
                raise AdmissionError(*(error or ("TASK_CONTRACT_REVISION_CONFLICT",
                    "Task contract changed before admission; recompute the proposal against its current revision.")), nature="governance")
        # A composed callback may refresh context, but cannot substitute a
        # callable, owner or native artifact and spend an earlier exact grant.
        from .execution_contract import verify_registration
        try:
            verify_registration(tool, ctx.process, self.metadata["owner"], self.metadata["registration"])
        except (AttributeError, ValueError, OSError, RuntimeError) as exc:
            raise AdmissionError("TOOL_IMPLEMENTATION_UNAVAILABLE" if isinstance(exc, (OSError, RuntimeError))
                else "TOOL_EXECUTION_CONTRACT_INVALID", str(exc), subsystem="execution_contract") from exc
        from .mechanisms import admit, snapshot
        unit = admit(ctx.process, tool, self.raw_args, self.execution_id, self.call_index, self.call_id)
        if unit is not None:
            return AdmissionHold("advancement", unit)
        self.metadata["mechanism_revision"] = snapshot(ctx.process)
        if (self.composition and self.metadata.get("harness_instances")
                and (tool.composite_spec or {}).get("execution_mode") != "host_pipeline"):
            from backend.core.mechanism_runtime.invocations import current_scope
            current_scope(ctx).admit(self.composition, self.metadata["harness_unit"], self.metadata["harness_instances"])
