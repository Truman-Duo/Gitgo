"""Prepare signed GitHub Release assets offline; this command never publishes.

The operator supplies a private Ed25519 PEM stored outside the checkout and
packages. Only detached signatures, manifests and public trust are emitted.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.update.manifest import ManifestVerifier
from backend.core.update.contracts import verify_bundle_contract
from backend.core.update.release_policy import parse_release_policy
from backend.core.update.tree import encode
from backend.core.update.validation import reject


def github_policy(public, key_id, platform, architecture, repository="Truman-Duo/Gitgo"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected a GitHub owner/repository")
    base = f"https://github.com/{repository}/releases/download"
    target = platform + "-" + architecture
    value = {"schema_version": 1, "product_id": "gitgo-terminal", "platform": platform,
        "architecture": architecture, "allowed_hosts": ["github.com", "release-assets.githubusercontent.com"],
        "public_keys": [{"key_id": key_id, "ed25519_base64": base64.b64encode(public).decode()}],
        "channels": {channel: {"manifest_url": f"{base}/updates-{channel}/{target}.manifest.json",
            "signature_url": f"{base}/updates-{channel}/{target}.manifest.sig"} for channel in ("stable", "preview")}}
    parse_release_policy(encode(value), platform, architecture)
    return value


def prepare(stage, archive, private_key, key_id, channel, sequence, repository, release_tag, now=None):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    product = json.loads((stage / "product.json").read_bytes())
    build = json.loads((stage / "build-manifest.json").read_bytes())
    contract = json.loads((stage / "update-data-contract.json").read_bytes())
    if build["source"]["dirty"] is not False:
        raise ValueError("Release signing requires clean committed source")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_tag):
        raise ValueError("Expected an explicit release tag")
    public = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    policy = github_policy(public, key_id, product["platform"], product["architecture"], repository)
    instant = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    stamp = lambda value: value.isoformat().replace("+00:00", "Z")
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    wire = {"schema_version": 1, "product_id": product["product_id"], "release_id": release_tag,
        "channel": channel, "sequence": sequence, "published_at": stamp(instant),
        "expires_at": stamp(instant + timedelta(days=7)), "version": product["version"],
        "platform": product["platform"], "architecture": product["architecture"],
        "archive_format": "zip" if product["platform"] == "windows" else "tar.gz",
        "url": f"https://github.com/{repository}/releases/download/{release_tag}/{archive.name}",
        "bytes": archive.stat().st_size, "sha256": digest, "source": {k: build["source"][k] for k in ("commit", "tree")},
        "data_compatibility": contract["data_compatibility"], "minimum_updater_version": "0.1.0"}
    raw = encode(wire)
    signature = encode({"schema_version": 1, "algorithm": "ed25519", "key_id": key_id,
        "signature": base64.b64encode(private_key.sign(raw)).decode()})
    manifest = ManifestVerifier({key_id: public}).verify(raw, signature)
    if (channel == "preview") != bool(__import__("backend.core.update.version", fromlist=["Version"]).Version.parse(manifest.version).prerelease):
        raise ValueError("Stable and preview releases must use their respective version class")
    verify_bundle_contract(stage, manifest)
    # Windows archive and reviewed stage must describe exactly the same files.
    if manifest.archive_format == "zip":
        expected = {p.relative_to(stage).as_posix(): p for p in stage.rglob("*") if p.is_file()}
        with zipfile.ZipFile(archive) as package:
            entries = [item for item in package.infolist() if not item.is_dir()]
            if len(entries) != len(expected) or {i.filename for i in entries} != set(expected):
                raise ValueError("Archive differs from reviewed stage")
            for item in entries:
                with package.open(item) as source, expected[item.filename].open("rb") as local:
                    if hashlib.file_digest(source, "sha256").digest() != hashlib.file_digest(local, "sha256").digest():
                        raise ValueError("Archive differs from reviewed stage")
    return raw, signature, encode(policy)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("stage", "archive", "private-key", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--channel", choices=("stable", "preview"), required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--repository", default="Truman-Duo/Gitgo")
    args = parser.parse_args()
    try:
        key_path = args.private_key.resolve(strict=True)
        if key_path.is_relative_to(ROOT) or key_path.is_relative_to(args.stage.resolve(strict=True)):
            raise ValueError("Private release key must remain outside source and package")
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = load_pem_private_key(key_path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey): raise ValueError("Expected an Ed25519 signing key")
        raw, signature, policy = prepare(args.stage, args.archive, key, args.key_id, args.channel,
            args.sequence, args.repository, args.release_tag)
        value = json.loads(raw)
        args.output.mkdir(parents=True, exist_ok=False)
        target = value["platform"] + "-" + value["architecture"]
        (args.output / (target + ".manifest.json")).write_bytes(raw)
        (args.output / (target + ".manifest.sig")).write_bytes(signature)
        (args.output / "release-trust.json").write_bytes(policy)
    except Exception:
        # Never print exceptions that could contain private key material.
        print("Release assets were not prepared. Check committed source, package, signing key and explicit channel.", file=sys.stderr)
        return 2
    print("Signed release assets prepared locally; no Release was published.")
    return 0

if __name__ == "__main__": raise SystemExit(main())
