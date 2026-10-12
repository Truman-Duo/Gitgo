"""Verify exact manifest bytes before interpreting any release metadata."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from types import MappingProxyType
from typing import Mapping

from .validation import (
    UpdateError, bounded_bytes, digest, fields, json_object, number, reject, text, timestamp, url_text,
)
from .version import Version


MAX_MANIFEST_BYTES = 128 * 1024
MAX_SIGNATURE_BYTES = 1024
MAX_MANIFEST_LIFETIME = timedelta(days=7)
TARGETS = frozenset({("windows", "x86_64"), ("macos", "arm64"), ("linux", "x86_64")})


@dataclass(frozen=True)
class DataCompatibility:
    state_migrations: tuple[tuple[int, str], ...]
    observability_migrations: tuple[tuple[int, str], ...]
    config_schema_version: int
    config_migration_sha256: str
    provider_schema_version: int
    provider_migration_sha256: str

    def __post_init__(self):
        for history in (self.state_migrations, self.observability_migrations):
            if type(history) is not tuple or not 1 <= len(history) <= 256:
                reject("UPDATE_COMPATIBILITY_INVALID", "Invalid local migration history")
            for expected, item in enumerate(history, start=1):
                if type(item) is not tuple or len(item) != 2:
                    reject("UPDATE_COMPATIBILITY_INVALID", "Invalid migration fingerprint")
                if number(item[0], maximum=256) != expected:
                    reject("UPDATE_COMPATIBILITY_INVALID", "Migration history must be ordered")
                digest(item[1])
        number(self.config_schema_version, maximum=2**31 - 1)
        number(self.provider_schema_version, maximum=2**31 - 1)
        digest(self.config_migration_sha256)
        digest(self.provider_migration_sha256)

    @classmethod
    def parse(cls, value: dict) -> DataCompatibility:
        fields(value, {
            "state_migrations", "observability_migrations", "config_schema_version",
            "config_migration_sha256", "provider_schema_version", "provider_migration_sha256",
        })

        def migrations(items):
            if type(items) is not list or not 1 <= len(items) <= 256:
                reject("UPDATE_COMPATIBILITY_INVALID", "Expected a bounded migration history")
            result = []
            for expected, item in enumerate(items, start=1):
                fields(item, {"version", "sha256"})
                version = number(item["version"], maximum=256)
                if version != expected:
                    reject("UPDATE_COMPATIBILITY_INVALID", "Migration history must be consecutive and ordered")
                result.append((version, digest(item["sha256"])))
            return tuple(result)

        return cls(migrations(value["state_migrations"]), migrations(value["observability_migrations"]),
                   number(value["config_schema_version"], maximum=2**31 - 1),
                   digest(value["config_migration_sha256"]),
                   number(value["provider_schema_version"], maximum=2**31 - 1),
                   digest(value["provider_migration_sha256"]))


@dataclass(frozen=True)
class UpdateManifest:
    product_id: str
    release_id: str
    channel: str
    sequence: int
    published_at: datetime
    expires_at: datetime
    version: str
    platform: str
    architecture: str
    archive_format: str
    url: str
    bytes: int
    sha256: str
    source_commit: str
    source_tree: str
    data_compatibility: DataCompatibility
    minimum_updater_version: str
    manifest_sha256: str
    key_id: str

    @property
    def scope(self) -> tuple[str, str, str, str]:
        return self.product_id, self.channel, self.platform, self.architecture


class ManifestVerifier:
    """Trust roots are local raw public keys, never supplied by the manifest."""

    def __init__(self, trusted_keys: Mapping[str, bytes] | None = None):
        copied = dict(trusted_keys or {})
        if len(copied) > 32:
            reject("UPDATE_TRUST_INVALID", "Too many trust roots")
        for key_id, key in copied.items():
            text(key_id, r"[A-Za-z0-9][A-Za-z0-9._-]*", 64)
            if type(key) is not bytes or len(key) != 32:
                reject("UPDATE_TRUST_INVALID", "Ed25519 public keys must be 32 raw bytes")
        self._keys = MappingProxyType(copied)

    @property
    def configured(self) -> bool:
        return bool(self._keys)

    def verify(self, raw: bytes, detached: bytes) -> UpdateManifest:
        bounded_bytes(raw, MAX_MANIFEST_BYTES)
        envelope = json_object(detached, MAX_SIGNATURE_BYTES)
        fields(envelope, {"schema_version", "algorithm", "key_id", "signature"})
        if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
            reject("UPDATE_SIGNATURE_INVALID", "Unsupported signature schema")
        if envelope["algorithm"] != "ed25519":
            reject("UPDATE_SIGNATURE_INVALID", "Unsupported signature algorithm")
        key_id = text(envelope["key_id"], r"[A-Za-z0-9][A-Za-z0-9._-]*", 64)
        key = self._keys.get(key_id)
        if key is None:
            reject("UPDATE_KEY_UNKNOWN", "Signature key is not trusted")
        encoded = text(envelope["signature"], r"[A-Za-z0-9+/]{86}==", 88)
        try:
            signature = base64.b64decode(encoded, validate=True)
        except binascii.Error as error:
            raise UpdateError("UPDATE_SIGNATURE_INVALID", "Invalid base64 signature") from error
        if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != encoded:
            reject("UPDATE_SIGNATURE_INVALID", "Invalid Ed25519 signature encoding")
        # Lazy import: normal Gitgo startup and disabled discovery require no crypto runtime.
        try:
            from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError as error:
            raise UpdateError("UPDATE_CRYPTO_UNAVAILABLE", "Signature backend is unavailable") from error
        try:
            Ed25519PublicKey.from_public_bytes(key).verify(signature, raw)
        except InvalidSignature:
            reject("UPDATE_SIGNATURE_INVALID", "Manifest signature verification failed")
        except UnsupportedAlgorithm:
            reject("UPDATE_CRYPTO_UNAVAILABLE", "Ed25519 verification is unavailable")
        value = json_object(raw, MAX_MANIFEST_BYTES)
        fields(value, {
            "schema_version", "product_id", "release_id", "channel", "sequence",
            "published_at", "expires_at", "version", "platform", "architecture",
            "archive_format", "url", "bytes", "sha256", "source", "data_compatibility",
            "minimum_updater_version",
        })
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            reject("UPDATE_SCHEMA_INVALID", "Unsupported manifest schema")
        channel = value["channel"]
        if channel not in ("stable", "preview"):
            reject("UPDATE_CHANNEL_INVALID", "Unsupported release channel")
        version = text(value["version"], r"[0-9A-Za-z.+-]+")
        minimum = text(value["minimum_updater_version"], r"[0-9A-Za-z.+-]+")
        parsed_version = Version.parse(version)
        Version.parse(minimum)
        if channel == "stable" and parsed_version.prerelease:
            reject("UPDATE_CHANNEL_INVALID", "Stable releases cannot be prereleases")
        platform = text(value["platform"], r"[a-z]+", 16)
        architecture = text(value["architecture"], r"[a-z0-9_]+", 16)
        if (platform, architecture) not in TARGETS:
            reject("UPDATE_TARGET_INVALID", "Unsupported release target")
        archive_format = value["archive_format"]
        if archive_format != ("zip" if platform == "windows" else "tar.gz"):
            reject("UPDATE_SCHEMA_INVALID", "Unsupported archive format for target")
        published, expires = timestamp(value["published_at"]), timestamp(value["expires_at"])
        if not timedelta(0) < expires - published <= MAX_MANIFEST_LIFETIME:
            reject("UPDATE_FRESHNESS_INVALID", "Manifest validity must be positive and at most seven days")
        source = fields(value["source"], {"commit", "tree"})
        return UpdateManifest(
            text(value["product_id"], r"[a-z0-9][a-z0-9-]*", 64),
            text(value["release_id"], r"[A-Za-z0-9][A-Za-z0-9._-]*", 128),
            channel, number(value["sequence"]), published, expires, version, platform,
            architecture, archive_format, url_text(value["url"]),
            number(value["bytes"], maximum=2 * 1024**3), digest(value["sha256"]),
            text(source["commit"], r"[0-9a-f]{40}", 40),
            text(source["tree"], r"[0-9a-f]{40}", 40),
            DataCompatibility.parse(value["data_compatibility"]), minimum,
            hashlib.sha256(raw).hexdigest(), key_id,
        )
