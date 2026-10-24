"""Stage publisher public trust for an explicit opt-in Unix build, before inventory."""
import argparse
import json
import os
import stat
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.update.release_policy import POLICY_FILE, MAX_POLICY_BYTES, parse_release_policy
from backend.core.update.validation import UpdateError


def stage_policy(stage: Path, policy: Path):
    product = json.loads((stage / "product.json").read_bytes())
    if not (stage / "update-data-contract.json").is_file():
        raise UpdateError("UPDATE_RELEASE_POLICY", "Public release trust requires verified runtime data evidence")
    # Build inputs are public, reviewed source like this script. Installed
    # bootstrap policy still uses read_policy_file and private native ACLs.
    if policy.is_symlink():
        raise UpdateError("UPDATE_RELEASE_POLICY", "Public build policy must be a regular file")
    with policy.open("rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > MAX_POLICY_BYTES):
            raise UpdateError("UPDATE_RELEASE_POLICY", "Public build policy size or type is invalid")
        raw = source.read(MAX_POLICY_BYTES + 1)
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
