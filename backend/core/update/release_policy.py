"""Publisher policy pinned at initial installation, never supplied by a request.

Only public Ed25519 keys live here. No private key, provider setting, environment
URL or candidate file can replace the stable bootstrap's registered policy.
"""
from dataclasses import dataclass
import base64
import binascii
import os
import stat
from types import MappingProxyType

from .checker import UpdatePolicy
from .manifest import ManifestVerifier, TARGETS
from .validation import fields, json_object, reject, text, https_url

POLICY_FILE = "release-trust.json"
MAX_POLICY_BYTES = 64 * 1024


def read_policy_file(path):
    from .persistence import open_readonly
    fd = open_readonly(path)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_POLICY_BYTES
                or (os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022))):
            reject("UPDATE_RELEASE_POLICY", "Release policy ownership or size is invalid")
        return handle.read(MAX_POLICY_BYTES + 1)


@dataclass(frozen=True)
class ReleasePolicy:
    channels: object
    verifier: ManifestVerifier

    def channel(self, name):
        if name not in self.channels:
            reject("UPDATE_CHANNEL_UNCONFIGURED", "Selected release channel is not configured")
        return self.channels[name]


def parse_release_policy(raw, platform, architecture):
    value = fields(json_object(raw, MAX_POLICY_BYTES), {
        "schema_version", "product_id", "platform", "architecture", "allowed_hosts", "public_keys", "channels"})
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["product_id"] != "gitgo-terminal" or (platform, architecture) not in TARGETS
            or (value["platform"], value["architecture"]) != (platform, architecture)):
        reject("UPDATE_RELEASE_POLICY", "Release policy does not match the installed product target")
    hosts = value["allowed_hosts"]
    if (type(hosts) is not list or not 1 <= len(hosts) <= 16
            or any(type(host) is not str for host in hosts) or len(set(hosts)) != len(hosts)):
        reject("UPDATE_RELEASE_POLICY", "Explicit release DNS hosts are required")
    hosts = tuple(hosts)
    UpdatePolicy(allowed_hosts=hosts)
    public = value["public_keys"]
    if type(public) is not list or not 1 <= len(public) <= 16:
        reject("UPDATE_RELEASE_POLICY", "A bounded set of publisher public keys is required")
    keys = {}
    for item in public:
        item = fields(item, {"key_id", "ed25519_base64"})
        key_id = text(item["key_id"], r"[A-Za-z0-9][A-Za-z0-9._-]*", 64)
        encoded = item["ed25519_base64"]
        if key_id in keys or type(encoded) is not str or len(encoded) != 44:
            reject("UPDATE_RELEASE_POLICY", "Publisher public key declaration is invalid")
        try: decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error): reject("UPDATE_RELEASE_POLICY", "Publisher public key encoding is invalid")
        if len(decoded) != 32 or base64.b64encode(decoded).decode() != encoded:
            reject("UPDATE_RELEASE_POLICY", "Expected a canonical Ed25519 public key")
        keys[key_id] = decoded
    channels = value["channels"]
    if type(channels) is not dict or not channels or set(channels) - {"stable", "preview"}:
        reject("UPDATE_RELEASE_POLICY", "Release channels must be stable or preview")
    policies = {}
    for channel, declaration in channels.items():
        declaration = fields(declaration, {"manifest_url", "signature_url"})
        manifest = https_url(declaration["manifest_url"], hosts)
        signature = https_url(declaration["signature_url"], hosts)
        if manifest == signature: reject("UPDATE_RELEASE_POLICY", "Manifest and signature endpoints must be distinct")
        policies[channel] = UpdatePolicy(True, False, channel, manifest, signature, hosts)
    return ReleasePolicy(MappingProxyType(policies), ManifestVerifier(keys))


def installed_release_policy(store):
    store._guard()
    path = store.root / "bootstrap" / POLICY_FILE
    if not path.exists() and not path.is_symlink(): return None
    local = store.registration
    return parse_release_policy(read_policy_file(path), local["platform"], local["architecture"])
