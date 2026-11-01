"""Explicit legacy updater replacement/recovery; never called by an Agent.

Run only from trusted source or a separately verified official release. The
existing installation's pinned publisher key authorizes the candidate. This
does not build/sign/publish packages or select a future product version.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--recover", action="store_true")
    for name in ("manifest", "signature", "archive"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.recover and any((args.manifest, args.signature, args.archive)):
        parser.error("Recovery uses only the recorded installation transaction")
    if not args.recover and not all((args.manifest, args.signature, args.archive)):
        parser.error("A signed published manifest, signature and complete archive are required")
    from backend.core.update.repair import repair, recover
    from backend.core.update.validation import UpdateError
    try:
        result = recover(args.root) if args.recover else repair(args.root,
            args.manifest.read_bytes(), args.signature.read_bytes(), args.archive)
    except (UpdateError, OSError, ValueError) as error:
        print(json.dumps({"status": "blocked", "error_code": error.code if isinstance(error, UpdateError)
                          else "UPDATE_REPAIR_REQUIRED", "message": "Updater replacement did not complete. Close Gitgo instances and use --recover for an interrupted operation."}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"committed", "rolled_back"} else 2


if __name__ == "__main__": raise SystemExit(main())
