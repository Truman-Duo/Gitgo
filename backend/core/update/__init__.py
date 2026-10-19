"""Opt-in discovery/preparation; no startup hooks or install mutations."""

from .checker import CheckResult, CurrentInstallation, ReplayState, UpdatePolicy, check_for_update
from .manifest import DataCompatibility, ManifestVerifier, UpdateManifest
from .validation import UpdateError
from .archive import ExtractionLimits
from .staging import PreparationResult, prepare_update
from .transport import HttpsTransport

__all__ = [
    "CheckResult", "CurrentInstallation", "DataCompatibility", "ManifestVerifier",
    "ReplayState", "UpdateError", "UpdateManifest", "UpdatePolicy", "check_for_update",
    "ExtractionLimits", "HttpsTransport", "PreparationResult", "prepare_update",
]
