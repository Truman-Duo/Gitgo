"""Release-to-release preflight against the updater that users already have.

This is a release gate, not a migration executor. Program size, changed files,
features and commit distance are not compatibility criteria. A v1 updater has
no migration or bootstrap-delegation protocol; report that missing path rather
than signing an apparently usable release which it will inevitably reject.
"""
from dataclasses import dataclass

from .manifest import UpdateManifest
from .version import Version


@dataclass(frozen=True)
class UpgradePath:
    source_version: str
    target_version: str
    ready: bool
    requirements: tuple[str, ...]
    data_changes: tuple[str, ...]
    route: str = "direct"

    def to_dict(self):
        return {"schema_version": 1, "source_version": self.source_version,
                "target_version": self.target_version, "ready": self.ready,
                "requirements": list(self.requirements), "data_changes": list(self.data_changes), "route": self.route,
                "migration_executed": False, "installed_upgrade_verified": False}


class UpgradePathRequired(ValueError):
    def __init__(self, assessment):
        self.assessment = assessment
        super().__init__("Release upgrade path is incomplete: " + ", ".join(assessment.requirements))


def assess_upgrade_path(previous: UpdateManifest, candidate: UpdateManifest, *,
                        previous_updater_version: str | None = None, route: str = "direct") -> UpgradePath:
    """Assess verified metadata; never infer data compatibility from code size.

    The caller verifies both signatures against installed publisher trust. The
    previous manifest may be expired: historical provenance remains useful; it
    must never substitute for fresh candidate evidence at install time.
    """
    if route not in {"direct", "legacy-repair"}: raise ValueError("Unknown upgrade route")
    requirements = []
    # A signed baseline requiring v2 can only have been installed by a v2
    # updater. Older manifests do not prove a newer launcher exists.
    engine = Version.parse(previous_updater_version or previous.minimum_updater_version)
    if route == "legacy-repair":
        from .data_transition import ENGINE_VERSION
        engine = Version.parse(ENGINE_VERSION)
    if previous.scope != candidate.scope:
        requirements.append("RELEASE_TARGET_OR_CHANNEL_CHANGED")
    if Version.parse(candidate.version) <= Version.parse(previous.version):
        requirements.append("NEW_RELEASE_VERSION_REQUIRED")
    if (candidate.sequence <= previous.sequence
            or candidate.published_at < previous.published_at):
        requirements.append("MONOTONIC_RELEASE_EVIDENCE_REQUIRED")
    if Version.parse(candidate.minimum_updater_version) > engine:
        requirements.append("UPDATER_BRIDGE_REQUIRED")

    changes = data_contract_changes(previous.data_compatibility, candidate.data_compatibility)
    if changes and engine < Version.parse("0.2.0"):
        # Do not call these differences intrinsically unsafe. The existing v1
        # consumer cannot negotiate them, even when actual formats are equal.
        requirements.append("DATA_TRANSITION_PATH_REQUIRED")
    return UpgradePath(previous.version, candidate.version, not requirements,
                       tuple(requirements), changes, route)


def data_contract_changes(old, new):
    """Describe runtime evidence changes without inventing a release identity."""
    changes = []
    for name in ("state_migrations", "observability_migrations"):
        before, after = getattr(old, name), getattr(new, name)
        if before == after:
            continue
        if len(after) > len(before) and after[:len(before)] == before:
            changes.append(name + ":extended")
        elif len(after) < len(before) and before[:len(after)] == after:
            changes.append(name + ":removed")
        else:
            changes.append(name + ":rewritten")
    for name in ("config", "provider"):
        if getattr(old, name + "_schema_version") != getattr(new, name + "_schema_version"):
            changes.append(name + ":schema_changed")
        if getattr(old, name + "_migration_sha256") != getattr(new, name + "_migration_sha256"):
            changes.append(name + ":writer_evidence_changed")
    return tuple(changes)


def require_upgrade_path(previous, candidate, **options):
    result = assess_upgrade_path(previous, candidate, **options)
    if not result.ready:
        raise UpgradePathRequired(result)
    return result
