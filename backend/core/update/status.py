"""Read-only first-release status, not permission to activate the updater.

There is intentionally no enable/apply operation: current installers do not
participate in the transaction engine's runtime gate. Environment variables,
provider settings and remotely supplied metadata cannot confer that authority.
"""

from .checker import UpdatePolicy


def update_status() -> dict:
    from backend.version import __version__
    policy = UpdatePolicy()
    return {
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
