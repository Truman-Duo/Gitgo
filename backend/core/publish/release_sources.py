"""Explicit source selection reads; saved version browsing stays offline."""
from .state import PublishError


def resolve_candidates(target, selected, oid):
    from backend.remote.publish_accounts import resolve_secret
    from backend.remote.publish_platform import PlatformClient
    from backend.remote.publish_ci import ProjectCI, number
    if not isinstance(selected, list) or not 1 <= len(selected) <= 16:
        raise PublishError("PUBLISH_CI_SELECTION_INVALID")
    identities = set()
    for row in selected:
        if not isinstance(row, dict) or set(row) != {"run_id", "attempt", "artifact_id"}:
            raise PublishError("PUBLISH_CI_SELECTION_INVALID")
        for value in row.values(): number(value)
        if row["artifact_id"] in identities: raise PublishError("PUBLISH_CI_SELECTION_INVALID")
        identities.add(row["artifact_id"])
    with PlatformClient(target, resolve_secret(target)) as client:
        ci = ProjectCI(client)
        return [ci.artifact(ci.run(row["run_id"], row["attempt"], oid), row["artifact_id"]) for row in selected]


def release_sources(project, item_id, *, plan_binding, page=1):
    from .release_query import release_status
    from backend.remote.publish_platform import PlatformClient, PlatformTarget
    from backend.remote.publish_accounts import resolve_secret
    from backend.remote.publish_ci import ProjectCI
    status = release_status(project, item_id, limit=1)
    if status["stale"] or status["control"]["plan"]["binding"] != plan_binding:
        raise PublishError("PUBLISH_RELEASE_PLAN_STALE")
    if not status["plan"]["capabilities"].get("ci_acquisition"):
        raise PublishError("PUBLISH_CI_UNSUPPORTED")
    if not status["plan"].get("source_oid"):
        raise PublishError("PUBLISH_CI_SOURCE_NOT_RECORDED")
    target = PlatformTarget.parse(status["plan"]["target"])
    with PlatformClient(target, resolve_secret(target)) as client:
        result = ProjectCI(client).candidates(status["plan"]["source_oid"], page)
    return {**result, "item_id":item_id, "plan_binding":plan_binding,
            "source_oid":status["plan"]["source_oid"], "remote_writes":False}
