"""Release lifecycle shared by entry points, with project-owned recipes.

The engine orders phases and journals their boundaries. Recipes own candidate
validation, artifact acquisition/checks and reconciliation of external effects.
Neither a journal receipt nor a phase request grants publication authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, ContextManager, Protocol


PHASES = ("prepared", "draft", "published", "verified")
ENGINE_VERSION = 1


class ReleaseJournal(Protocol):
    def locked(self) -> ContextManager: ...
    def record(self, name: str, state: str, **fields) -> dict: ...


@dataclass(frozen=True)
class ReleaseContext:
    plan: dict
    evidence: ReleaseJournal
    remote: Any
    workers: int = 4
    cancelled: Callable[[], bool] | None = None


class ReleaseRecipe(Protocol):
    identity: dict
    supported_phases: tuple[str, ...]

    def validate(self, context: ReleaseContext) -> None: ...
    def prepare(self, context: ReleaseContext) -> Any: ...
    def guard(self, context: ReleaseContext, prepared: Any) -> None: ...
    def draft(self, context: ReleaseContext, prepared: Any) -> Any: ...
    def publish(self, context: ReleaseContext, prepared: Any, draft: Any) -> None: ...
    def verify(self, context: ReleaseContext, prepared: Any) -> None: ...
    def result(self, phase: str, draft: Any) -> dict: ...


class ReleaseCancelled(RuntimeError):
    def __init__(self, before_phase: str):
        self.before_phase = before_phase
        super().__init__("Release cancelled at a phase boundary")


class ReleaseBlocked(RuntimeError):
    """An observed unmet condition, distinct from an unknown external result."""
    def __init__(self, phase: str, reason: str):
        self.phase, self.reason = phase, reason
        super().__init__(reason)


class ReleaseEngine:
    def __init__(self, recipe: ReleaseRecipe):
        self.recipe = recipe

    def _validate_request(self, context: ReleaseContext, through: str) -> None:
        if through not in PHASES or type(context.workers) is not int or not 1 <= context.workers <= 16:
            raise ValueError("Invalid release phase or concurrency")
        if through not in self.recipe.supported_phases:
            raise ValueError("Release recipe does not support the requested phase")
        phases = self.recipe.supported_phases
        if ("prepared" not in phases or any(phase not in PHASES for phase in phases)
                or len(set(phases)) != len(phases)
                or tuple(phase for phase in PHASES if phase in phases) != tuple(phases)
                or "verified" in phases and "published" not in phases):
            raise ValueError("Invalid release recipe phase contract")

    def run(self, context: ReleaseContext, through: str = "prepared") -> dict:
        self._validate_request(context, through)
        with context.evidence.locked():
            started = time.monotonic()
            identity = {"engine_version": ENGINE_VERSION, "recipe": self.recipe.identity}
            context.evidence.record("run", "running", through=through, **identity)
            try:
                result = self.advance(context, through)
            except ReleaseBlocked as exc:
                context.evidence.record("run", "blocked", through=through, phase=exc.phase, reason=exc.reason,
                                        seconds=time.monotonic() - started, **identity)
                raise
            except ReleaseCancelled as exc:
                context.evidence.record("run", "cancelled", through=through, before_phase=exc.before_phase,
                                        seconds=time.monotonic() - started, **identity)
                raise
            except BaseException as exc:
                # Exceptions can contain credentials or local paths. Preserve
                # the failure class; detailed diagnostics belong to the adapter.
                context.evidence.record("run", "failed", through=through, error=type(exc).__name__,
                                        seconds=time.monotonic() - started, **identity)
                raise
            context.evidence.record("run", "success", result=result,
                                    seconds=time.monotonic() - started, **identity)
            return result

    def _phase(self, context: ReleaseContext, phase: str, action: Callable):
        if context.cancelled is not None and context.cancelled():
            raise ReleaseCancelled(phase)
        self.recipe.validate(context)
        started = time.monotonic()
        context.evidence.record("phase-" + phase, "running")
        try:
            result = action()
        except ReleaseBlocked as exc:
            context.evidence.record("phase-" + phase, "blocked", reason=exc.reason,
                                    completion_unconfirmed=False, seconds=time.monotonic() - started)
            raise
        except BaseException as exc:
            context.evidence.record("phase-" + phase, "stopped", error=type(exc).__name__,
                                    completion_unconfirmed=True, seconds=time.monotonic() - started)
            raise
        context.evidence.record("phase-" + phase, "observed" if getattr(self.recipe,"observe_only",False) else "success",
                                seconds=time.monotonic() - started)
        return result

    def advance(self, context: ReleaseContext, through: str = "prepared") -> dict:
        """Advance under the caller's journal lease; never acquire a second lock.

        Cancellation here is between phases. A recipe's process/transfer
        adapter remains responsible for cancellation during an active phase.
        """
        self._validate_request(context, through)
        def prepare():
            value = self.recipe.prepare(context)
            self.recipe.guard(context, value)
            return value
        prepared = self._phase(context, "prepared", prepare)
        if through == "prepared":
            return self.recipe.result(through, None)
        def guarded(action):
            self.recipe.guard(context, prepared)
            return action()
        draft = None
        if "draft" in self.recipe.supported_phases:
            draft = self._phase(context, "draft", lambda: guarded(lambda: self.recipe.draft(context, prepared)))
        if through == "draft":
            return self.recipe.result(through, draft)
        self._phase(context, "published", lambda: guarded(lambda: self.recipe.publish(context, prepared, draft)))
        if through == "verified":
            self._phase(context, "verified", lambda: guarded(lambda: self.recipe.verify(context, prepared)))
        return self.recipe.result(through, draft)
