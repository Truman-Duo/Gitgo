"""Pure opt-in discovery with injected bounded transport and replay evidence.

An available candidate is NOT permission to download, migrate, or replace files.
Installation identity and the replay watermark must come from local authority;
their durable adapters are deliberately outside this discovery stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import re
from typing import Callable

from .manifest import (
    DataCompatibility, ManifestVerifier, MAX_MANIFEST_BYTES, MAX_SIGNATURE_BYTES,
    TARGETS, UpdateManifest,
)
from .validation import UpdateError, bounded_bytes, digest, https_url, number, reject, text
from .version import Version


@dataclass(frozen=True)
class UpdatePolicy:
    enabled: bool = False
    automatic_check: bool = False
    channel: str = "stable"
    manifest_url: str = ""
    signature_url: str = ""
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.automatic_check) is not bool:
            reject("UPDATE_POLICY_INVALID", "Update switches must be booleans")
        if self.channel not in ("stable", "preview"):
            reject("UPDATE_POLICY_INVALID", "Invalid update channel")
        if type(self.allowed_hosts) is not tuple or len(self.allowed_hosts) > 16:
            reject("UPDATE_POLICY_INVALID", "Expected a bounded tuple of release hosts")
        for host in self.allowed_hosts:
            if (type(host) is not str or len(host) > 253 or "." not in host
                    or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) is None
                    or any(not label or len(label) > 63 or label.startswith("-")
                           or label.endswith("-") for label in host.split("."))):
                reject("UPDATE_POLICY_INVALID", "Invalid release host")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                continue
            reject("UPDATE_POLICY_INVALID", "Release hosts must be explicit DNS names")


@dataclass(frozen=True)
class CurrentInstallation:
    product_id: str
    version: str
    platform: str
    architecture: str
    updater_version: str
    data_compatibility: DataCompatibility | None = None
    managed: bool = False

    def __post_init__(self):
        text(self.product_id, r"[a-z0-9][a-z0-9-]*", 64)
        Version.parse(self.version)
        Version.parse(self.updater_version)
        text(self.platform, r"[a-z]+", 16)
        text(self.architecture, r"[a-z0-9_]+", 16)
        if type(self.managed) is not bool:
            reject("UPDATE_INSTALL_INVALID", "Installation registration must be boolean")
        if self.data_compatibility is not None and not isinstance(self.data_compatibility, DataCompatibility):
            reject("UPDATE_COMPATIBILITY_INVALID", "Invalid local data compatibility evidence")


@dataclass(frozen=True)
class ReplayState:
    """One watermark per product/channel/platform/architecture, not a global counter."""

    product_id: str
    channel: str
    platform: str
    architecture: str
    sequence: int
    manifest_sha256: str
    artifact_sha256: str
    version: str
    published_at: datetime

    def __post_init__(self):
        text(self.product_id, r"[a-z0-9][a-z0-9-]*", 64)
        if self.channel not in ("stable", "preview"):
            reject("UPDATE_REPLAY_STATE_INVALID", "Invalid watermark channel")
        text(self.platform, r"[a-z]+", 16)
        text(self.architecture, r"[a-z0-9_]+", 16)
        number(self.sequence)
        digest(self.manifest_sha256)
        digest(self.artifact_sha256)
        Version.parse(self.version)
        if (not isinstance(self.published_at, datetime) or self.published_at.tzinfo is None
                or self.published_at.utcoffset() is None):
            reject("UPDATE_REPLAY_STATE_INVALID", "Watermark timestamp must be aware")

    @property
    def scope(self) -> tuple[str, str, str, str]:
        return self.product_id, self.channel, self.platform, self.architecture


@dataclass(frozen=True)
class CheckResult:
    status: str
    candidate: UpdateManifest | None = None
    watermark: ReplayState | None = None


def check_for_update(
    policy: UpdatePolicy,
    current: CurrentInstallation,
    verifier: ManifestVerifier,
    fetch_bytes: Callable[[str, int], bytes],
    *,
    now: datetime | None = None,
    previous: ReplayState | None = None,
    automatic: bool = False,
) -> CheckResult:
    # These gates precede crypto, clock, local state, and transport access.
    if not policy.enabled:
        return CheckResult("disabled")
    if type(automatic) is not bool:
        reject("UPDATE_POLICY_INVALID", "Check mode must be boolean")
    if automatic and not policy.automatic_check:
        return CheckResult("disabled")
    if current.managed is not True:
        reject("UPDATE_INSTALL_UNMANAGED", "Installation is not registered for updates")
    text(current.product_id, r"[a-z0-9][a-z0-9-]*", 64)
    current_version = Version.parse(current.version)
    updater_version = Version.parse(current.updater_version)
    if (current.platform, current.architecture) not in TARGETS:
        reject("UPDATE_TARGET_INVALID", "Unsupported local target")
    if not isinstance(current.data_compatibility, DataCompatibility):
        reject("UPDATE_COMPATIBILITY_MISSING", "Local data compatibility evidence is required")
    if not verifier.configured:
        reject("UPDATE_TRUST_MISSING", "No trusted release public keys configured")
    manifest_url = https_url(policy.manifest_url, policy.allowed_hosts)
    signature_url = https_url(policy.signature_url, policy.allowed_hosts)
    instant = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        reject("UPDATE_CLOCK_INVALID", "An aware trusted clock is required")
    if previous is not None:
        expected_scope = (current.product_id, policy.channel, current.platform, current.architecture)
        if not isinstance(previous, ReplayState) or previous.scope != expected_scope:
            reject("UPDATE_REPLAY_STATE_INVALID", "Watermark belongs to a different release scope")
        number(previous.sequence)
        digest(previous.manifest_sha256)
        Version.parse(previous.version)
        if (not isinstance(previous.published_at, datetime) or previous.published_at.tzinfo is None
                or previous.published_at.utcoffset() is None):
            reject("UPDATE_REPLAY_STATE_INVALID", "Invalid watermark timestamp")
        if instant < previous.published_at:
            reject("UPDATE_CLOCK_INVALID", "Clock precedes accepted release evidence")
    try:
        raw = fetch_bytes(manifest_url, MAX_MANIFEST_BYTES)
        # Bound the first response before making even the second request.
        bounded_bytes(raw, MAX_MANIFEST_BYTES)
        detached = fetch_bytes(signature_url, MAX_SIGNATURE_BYTES)
    except UpdateError:
        raise
    except Exception:
        # Do not echo transport errors: proxy URLs can contain passwords/tokens.
        raise UpdateError("UPDATE_FETCH_FAILED", "Release metadata could not be read; current installation unchanged") from None
    manifest = verifier.verify(raw, detached)
    if manifest.scope != (current.product_id, policy.channel, current.platform, current.architecture):
        reject("UPDATE_SCOPE_MISMATCH", "Release product, channel, or target does not match")
    if not manifest.published_at <= instant < manifest.expires_at:
        reject("UPDATE_FRESHNESS_INVALID", "Release metadata is expired or not yet valid")
    if previous is not None:
        if manifest.sequence < previous.sequence:
            reject("UPDATE_REPLAY_REJECTED", "Release sequence is older than accepted evidence")
        if manifest.sequence == previous.sequence and manifest.manifest_sha256 != previous.manifest_sha256:
            reject("UPDATE_REPLAY_REJECTED", "Release sequence was reused with different metadata")
        if manifest.published_at < previous.published_at or Version.parse(manifest.version) < Version.parse(previous.version):
            reject("UPDATE_REPLAY_REJECTED", "Release evidence regressed")
        if (Version.parse(manifest.version) == Version.parse(previous.version)
                and manifest.sha256 != previous.artifact_sha256):
            reject("UPDATE_RELEASE_REWRITTEN", "An accepted version cannot be republished with another package")
    https_url(manifest.url, policy.allowed_hosts)
    if updater_version < Version.parse(manifest.minimum_updater_version):
        reject("UPDATE_CLIENT_TOO_OLD", "This release requires a newer updater")
    if manifest.data_compatibility != current.data_compatibility:
        reject("UPDATE_MIGRATION_REQUIRED", "Data compatibility changed; automatic migration is not supported")
    target_version = Version.parse(manifest.version)
    if target_version < current_version:
        reject("UPDATE_DOWNGRADE_REJECTED", "Ordinary updates cannot downgrade the installed version")
    watermark = ReplayState(*manifest.scope, manifest.sequence, manifest.manifest_sha256,
                            manifest.sha256, manifest.version, manifest.published_at)
    if target_version == current_version:
        return CheckResult("up_to_date", watermark=watermark)
    return CheckResult("available", manifest, watermark)
