"""Read-only status and legacy compatibility, never permission to apply updates.

Managed manual preparation is delegated to its application service; environment
variables, provider settings or remote metadata cannot confer installation trust.
"""

from .checker import UpdatePolicy


def legacy_status() -> dict:
    from backend.version import __version__
    policy = UpdatePolicy()
    result = {
        "schema_version": 1,
        "product_id": "gitgo-terminal",
        "version": __version__,
        "status": "disabled",
        "enabled": policy.enabled,
        "automatic_check": policy.automatic_check,
        "channel": policy.channel,
        "can_apply": False,
        "blockers": [
            "MANAGED_INSTALLATION_REQUIRED",
            "RELEASE_TRUST_REQUIRED",
            "PLATFORM_ACCEPTANCE_REQUIRED",
        ],
    }
    import os
    import sys
    from .installation import _ownership_supported
    if getattr(sys, "frozen", False) and _ownership_supported():
        from pathlib import Path
        from .installation import physical_installation, open_managed
        physical = physical_installation(Path(sys.executable))
        if physical is not None:
            open_managed(physical[0])
            result["blockers"] = ["RELEASE_TRUST_REQUIRED", "UPDATE_HANDOFF_REQUIRED", "PLATFORM_ACCEPTANCE_REQUIRED"]
    return result


def update_status() -> dict:
    from .preparation_service import UpdatePreparationService
    return UpdatePreparationService().status()
