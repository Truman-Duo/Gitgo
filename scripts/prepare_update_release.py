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



def staged_policy(stage, public, key_id, platform, architecture, repository, channel, *, raw=None):
    """Sign only packages whose installed trust accepts this publisher and feed."""
    raw = (stage / "release-trust.json").read_bytes() if raw is None else raw
    parsed = parse_release_policy(raw, platform, architecture)
    value = json.loads(raw)
    expected = github_policy(public, key_id, platform, architecture, repository)
    if expected["public_keys"][0] not in value["public_keys"]:
        raise ValueError("Package trust does not contain the release signing key")
    if set(value["allowed_hosts"]) != set(expected["allowed_hosts"]):
        raise ValueError("Package trust does not match the release hosts")
    for name, declaration in value["channels"].items():
        if declaration != expected["channels"][name]:
            raise ValueError("Package trust does not match the release feeds")
    parsed.channel(channel)
    return raw, parsed


def prepare(stage, archive, private_key, key_id, channel, sequence, repository, release_tag, now=None,
            *, baseline_manifest=None, baseline_signature=None, initial_release=False, upgrade_route="direct",
            archive_evidence=None):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    metadata = archive_evidence.metadata if archive_evidence is not None else lambda name: json.loads((stage / name).read_bytes())
    product = metadata("product.json")
    build = metadata("build-manifest.json")
    contract = metadata("update-data-contract.json")
    if build["source"]["dirty"] is not False:
        raise ValueError("Release signing requires clean committed source")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_tag):
        raise ValueError("Expected an explicit release tag")
    public = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    policy, installed_trust = staged_policy(stage, public, key_id, product["platform"],
        product["architecture"], repository, channel,
        raw=archive_evidence.raw["release-trust.json"] if archive_evidence is not None else None)
    instant = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    stamp = lambda value: value.isoformat().replace("+00:00", "Z")
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    from backend.core.update.data_transition import ENGINE_VERSION
    wire = {"schema_version": 1, "product_id": product["product_id"], "release_id": release_tag,
        "channel": channel, "sequence": sequence, "published_at": stamp(instant),
        "expires_at": stamp(instant + timedelta(days=7)), "version": product["version"],
        "platform": product["platform"], "architecture": product["architecture"],
        "archive_format": "zip" if product["platform"] == "windows" else "tar.gz",
        "url": f"https://github.com/{repository}/releases/download/{release_tag}/{archive.name}",
        "bytes": archive.stat().st_size, "sha256": digest, "source": {k: build["source"][k] for k in ("commit", "tree")},
        "data_compatibility": contract["data_compatibility"], "minimum_updater_version": ENGINE_VERSION}
    raw = encode(wire)
    signature = encode({"schema_version": 1, "algorithm": "ed25519", "key_id": key_id,
        "signature": base64.b64encode(private_key.sign(raw)).decode()})
    manifest = ManifestVerifier({key_id: public}).verify(raw, signature)
    installed_trust.verifier.verify(raw, signature)
    validate_upgrade_baseline(installed_trust.verifier, manifest,
        baseline_manifest=baseline_manifest, baseline_signature=baseline_signature,
        initial_release=initial_release, upgrade_route=upgrade_route)
    if (channel == "preview") != bool(__import__("backend.core.update.version", fromlist=["Version"]).Version.parse(manifest.version).prerelease):
        raise ValueError("Stable and preview releases must use their respective version class")
    if archive_evidence is not None:
        archive_evidence.verify(manifest)
    else:
        verify_bundle_contract(stage, manifest)
    # Windows archive and reviewed stage must describe exactly the same files.
    if manifest.archive_format == "zip" and archive_evidence is None:
        expected = {p.relative_to(stage).as_posix(): p for p in stage.rglob("*") if p.is_file()}
        with zipfile.ZipFile(archive) as package:
            entries = [item for item in package.infolist() if not item.is_dir()]
            if len(entries) != len(expected) or {i.filename for i in entries} != set(expected):
                raise ValueError("Archive differs from reviewed stage")
            for item in entries:
                with package.open(item) as source, expected[item.filename].open("rb") as local:
                    if hashlib.file_digest(source, "sha256").digest() != hashlib.file_digest(local, "sha256").digest():
                        raise ValueError("Archive differs from reviewed stage")
    return raw, signature, policy


def validate_upgrade_baseline(verifier, candidate, *, baseline_manifest=None,
                              baseline_signature=None, initial_release=False, upgrade_route="direct"):
    """Never sign an update using only the candidate updater's own assumptions."""
    if type(initial_release) is not bool or upgrade_route not in {"direct", "legacy-repair"}:
        raise ValueError("Initial-release selection must be explicit")
    if initial_release:
        if baseline_manifest is not None or baseline_signature is not None or upgrade_route != "direct":
            raise ValueError("Initial releases cannot also claim a previous release")
        return None
    if baseline_manifest is None or baseline_signature is None:
        raise ValueError("Updates require the previous published manifest and signature")
    # Uses the pinned publisher public key; no unverified baseline JSON or
    # caller-authored compatibility override can authorize a release path.
    previous = verifier.verify(baseline_manifest, baseline_signature)
    from backend.core.update.upgrade_path import require_upgrade_path
    return require_upgrade_path(previous, candidate, route=upgrade_route)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("stage", "archive", "private-key", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--channel", choices=("stable", "preview"), required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--repository", default="Truman-Duo/Gitgo")
    parser.add_argument("--baseline-manifest", type=Path)
    parser.add_argument("--baseline-signature", type=Path)
    parser.add_argument("--upgrade-route", choices=("direct", "legacy-repair"), default="direct",
                        help="legacy-repair requires a separately delivered trusted repair executor; never direct /update")
    parser.add_argument("--initial-release", action="store_true",
                        help="Only for the first release of a new update channel/target")
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
            args.sequence, args.repository, args.release_tag,
            baseline_manifest=args.baseline_manifest.read_bytes() if args.baseline_manifest else None,
            baseline_signature=args.baseline_signature.read_bytes() if args.baseline_signature else None,
            initial_release=args.initial_release, upgrade_route=args.upgrade_route)
        value = json.loads(raw)
        args.output.mkdir(parents=True, exist_ok=False)
        target = value["platform"] + "-" + value["architecture"]
        (args.output / (target + ".manifest.json")).write_bytes(raw)
        (args.output / (target + ".manifest.sig")).write_bytes(signature)
        (args.output / "release-trust.json").write_bytes(policy)
    except Exception as error:
        # Never print exceptions that could contain private key material.
        print("Release assets were not prepared. Check committed source, package, signing key and explicit channel.", file=sys.stderr)
        from backend.core.update.upgrade_path import UpgradePathRequired
        if isinstance(error, UpgradePathRequired):
            print(json.dumps(error.assessment.to_dict(), sort_keys=True), file=sys.stderr)
        return 2
    print("Signed release assets prepared locally; no Release was published.")
    return 0

if __name__ == "__main__": raise SystemExit(main())
