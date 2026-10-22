"""Stage publisher public trust for an explicit opt-in Unix build, before inventory."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.update.release_policy import POLICY_FILE, parse_release_policy, read_policy_file
from backend.core.update.validation import UpdateError


def stage_policy(stage: Path, policy: Path):
    product = json.loads((stage / "product.json").read_bytes())
    if not (stage / "update-data-contract.json").is_file():
        raise UpdateError("UPDATE_RELEASE_POLICY", "Public release trust requires verified runtime data evidence")
    raw = read_policy_file(policy)
    parse_release_policy(raw, product["platform"], product["architecture"])
    path = stage / POLICY_FILE
    with path.open("xb") as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    if os.name == "posix": path.chmod(0o400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    try: stage_policy(args.stage, args.policy)
    except Exception:
        print("Public release trust could not be staged; no policy contents are logged", file=sys.stderr)
        return 2
    print("Publisher public release trust staged; install/apply remains unavailable")
    return 0


if __name__ == "__main__": raise SystemExit(main())
