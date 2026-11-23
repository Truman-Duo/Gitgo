"""Explicit project/account-scoped publisher keys in the existing native store."""
import base64
import re

from backend.core.secret_store import SecretStoreError
from backend.remote.publish_accounts import account_store
from backend.remote.publish_platform import PlatformTarget
from .state import PublishError, digest


def scope(storage, target, key_id):
    target = target if isinstance(target, PlatformTarget) else PlatformTarget.parse(target)
    if not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key_id):
        raise PublishError("PUBLISH_SIGNING_KEY_INVALID")
    return "publish-signing-" + digest({"project_id":storage.paths.project_id, "target":target.descriptor(), "key_id":key_id})


def _key(secret):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    try:
        if not isinstance(secret, str) or len(secret) > 8192: raise ValueError()
        key = load_pem_private_key(secret.encode("ascii"), password=None) if secret.startswith("-----BEGIN") else Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret, validate=True))
        if not isinstance(key, Ed25519PrivateKey): raise ValueError()
        return key
    except (ValueError, TypeError, UnicodeError) as error:
        raise PublishError("PUBLISH_SIGNING_KEY_INVALID") from error


def _descriptor(storage, target, key_id, key):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return {"key_id":key_id, "secret_ref":scope(storage,target,key_id),
            "ed25519_base64":base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode("ascii")}


def provision(storage, target, key_id, secret, *, store=None):
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    reference = scope(storage,target,key_id)
    key = _key(secret)
    pem = key.private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()).decode("ascii")
    try: (store or account_store()).upsert({reference:pem})
    except (OSError, SecretStoreError) as error:
        raise PublishError("PUBLISH_SIGNING_KEY_UNAVAILABLE") from error
    return _descriptor(storage,target,key_id,key)


def descriptor(storage, target, key_id, *, store=None):
    reference = scope(storage,target,key_id)
    try: secret = (store or account_store()).read_all().get(reference)
    except (OSError, SecretStoreError) as error:
        raise PublishError("PUBLISH_SIGNING_KEY_UNAVAILABLE") from error
    if not secret: raise PublishError("PUBLISH_SIGNING_KEY_UNCONFIGURED")
    return _descriptor(storage,target,key_id,_key(secret))


def resolve(storage, target, expected, *, store=None):
    reference = scope(storage,target,expected["key_id"])
    if reference != expected.get("secret_ref"): raise PublishError("PUBLISH_SIGNING_SCOPE_MISMATCH")
    try: secret = (store or account_store()).read_all().get(reference)
    except (OSError, SecretStoreError) as error:
        raise PublishError("PUBLISH_SIGNING_KEY_UNAVAILABLE") from error
    if not secret: raise PublishError("PUBLISH_SIGNING_KEY_UNCONFIGURED")
    key = _key(secret)
    if _descriptor(storage,target,expected["key_id"],key) != expected:
        raise PublishError("PUBLISH_SIGNING_KEY_CHANGED")
    return key
