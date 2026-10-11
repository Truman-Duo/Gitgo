"""Compose tool adapters through declared application-service dependencies."""
from dataclasses import dataclass
from typing import Any
from ..context_store import build_context_tools


@dataclass
class ToolBindings:
    process: Any
    available_tools: dict
    workspace_path: str
    llm_provider: Any
    dispatcher: Any
    on_stream_event: Any
    tools: dict
    manager: Any
    engineering: Any = None
    code_dossier: Any = None
    dossier_store: Any = None
    owned_child: Any = None
    spawn_child: Any = None
    delegate_task: Any = None
    prepare_task_bundle: Any = None


@dataclass(frozen=True)
class Factory:
    module: Any
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()


def install_factories(ctx, factories):
    """Resolve declared dependencies; refuse cycles/missing exports at assembly."""
    pending = list(factories)
    while pending:
        ready = [spec for spec in pending if all(getattr(ctx, name, None) is not None for name in spec.requires)]
        if not ready:
            details = {spec.module.__name__: [name for name in spec.requires if getattr(ctx, name, None) is None]
                       for spec in pending}
            raise RuntimeError(f"TOOL_FACTORY_DEPENDENCY_UNRESOLVED: {details}")
        for spec in ready:
            spec.module.install(ctx)
            missing = [name for name in spec.provides if getattr(ctx, name, None) is None]
            if missing:
                raise RuntimeError(f"TOOL_FACTORY_EXPORT_MISSING: {spec.module.__name__}: {missing}")
            pending.remove(spec)


def build_internal_tools(process, available_tools, workspace_path, llm_provider, dispatcher, on_stream_event):
    from . import (requirements_and_decisions, coordination, task_contracts,
        artifacts, child_execution, delegation, task_dag, bundle_preparation,
        bundle_execution, child_review, dynamic_and_completion)
    ctx = ToolBindings(process, available_tools or {}, workspace_path, llm_provider,
        dispatcher, on_stream_event, build_context_tools(process, workspace_path), getattr(process, "_manager", None))
    factories = [Factory(requirements_and_decisions, provides=("engineering",))]
    if process.actor_kind == "supervisor":
        factories.extend([
            Factory(coordination),
            Factory(task_contracts, requires=("engineering",)),
            Factory(artifacts, provides=("owned_child",)),
            Factory(child_execution, requires=("owned_child",), provides=("spawn_child",)),
            Factory(delegation, requires=("spawn_child",), provides=("delegate_task",)),
            Factory(task_dag, requires=("delegate_task",)),
            Factory(bundle_preparation, requires=("engineering",), provides=("prepare_task_bundle",)),
            Factory(bundle_execution, requires=("prepare_task_bundle", "spawn_child")),
            Factory(child_review, requires=("owned_child", "spawn_child")),
        ])
    factories.append(Factory(dynamic_and_completion))
    install_factories(ctx, factories)
    return ctx.tools
