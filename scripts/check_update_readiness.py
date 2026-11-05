"""Read-only source compatibility audit; does not build, sign or install.

Supply the historical publisher policy and its independently pinned raw public
key SHA256. An expired historical manifest can establish provenance, never a
fresh install permission. This report deliberately creates no future version.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def inspect(manifest, signature, policy, publisher_key_sha256):
    from backend.core.update.manifest import DataCompatibility, ManifestVerifier
    from backend.core.update.release_policy import parse_release_policy
    from backend.core.update.runtime_contract import runtime_evidence
    from backend.core.update.upgrade_path import data_contract_changes
    from backend.core.update.validation import digest

    expected = digest(publisher_key_sha256)
    declaration = json.loads(policy)
    # The pin is required independently of the downloaded trust declaration.
    # Do not let a substituted policy select a new publisher key for this audit.
    keys = {item["key_id"]: base64.b64decode(item["ed25519_base64"], validate=True)
            for item in declaration["public_keys"]}
    matched = {name: key for name, key in keys.items() if hashlib.sha256(key).hexdigest() == expected}
    if not matched:
        raise ValueError("Historical publisher public key differs from the explicit pin")
    previous = ManifestVerifier(matched).verify(manifest, signature)
    parse_release_policy(policy, previous.platform, previous.architecture)
    local, _ = runtime_evidence()
    changes = data_contract_changes(previous.data_compatibility, DataCompatibility.parse(local))
    return {"schema_version": 1, "baseline": {"version": previous.version,
                "source_commit": previous.source_commit, "manifest_sha256": previous.manifest_sha256,
                "signature_verified": True, "channel": previous.channel,
                "platform": previous.platform, "architecture": previous.architecture},
            "local_source": {"data_compatibility": local, "changes": list(changes)},
            "legacy_data_gate_would_accept": not changes,
            "requirements": ["DATA_TRANSITION_PATH_REQUIRED"] if changes else [],
            "candidate_release_created": False, "installed_upgrade_verified": False,
            "release_ready": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "signature", "trust"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--publisher-key-sha256", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = inspect(args.manifest.read_bytes(), args.signature.read_bytes(), args.trust.read_bytes(),
                     args.publisher_key_sha256)
    encoded = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    print(encoded)
    return 0 if result["legacy_data_gate_would_accept"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
