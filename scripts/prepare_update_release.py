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


from backend.core.publish.release_signing import (
    github_policy, staged_policy, prepare, validate_upgrade_baseline,
)


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
