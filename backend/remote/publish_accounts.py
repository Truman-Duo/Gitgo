"""Explicit Publish account scopes backed by the existing OS encrypted store."""
from pathlib import Path

from backend.core.config import ConfigManager
from backend.core.secret_store import EncryptedSecretStore, SecretStoreError


def account_store():
    return EncryptedSecretStore(Path(ConfigManager.default_path()).with_name("publish-secrets.json"))


def provision_secret(target, token, *, store=None):
    """Called by an explicit credential-settings flow, never by a query/draft."""
    from .publish_platform import PlatformTarget, PlatformError
    target = target if isinstance(target, PlatformTarget) else PlatformTarget.parse(target)
    if not isinstance(token, str) or not token or len(token) > 8192 or any(ord(c) < 33 for c in token):
        raise PlatformError("PUBLISH_ACCOUNT_SECRET_INVALID")
    (store or account_store()).upsert({target.scope_id: token})
    return {"scope_id": target.scope_id, "account": target.account, "configured": True}


def resolve_secret(target, *, store=None):
    from .publish_platform import PlatformTarget, PlatformError
    target = target if isinstance(target, PlatformTarget) else PlatformTarget.parse(target)
    try:
        token = (store or account_store()).read_all().get(target.scope_id)
    except (OSError, SecretStoreError) as exc:
        raise PlatformError("PUBLISH_ACCOUNT_UNAVAILABLE") from exc
    if not token:
        raise PlatformError("PUBLISH_ACCOUNT_UNCONFIGURED")
    return token
