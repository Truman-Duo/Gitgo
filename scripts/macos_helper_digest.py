"""Inspect a complete helper bundle without running it or requiring sudo.

Run from a trusted source checkout. A digest printed by an untrusted candidate
is not independent approval. Compare this value with the build/review record
before passing it to the administrator's installation command.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core.macos_helper_install import identity, inventory


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',required=True)
    args=parser.parse_args()
    entries=inventory(Path(args.bundle))
    print(json.dumps(dict(schema=1,algorithm='gitgo-helper-inventory-sha256-v1',
        bundle_sha256=identity(entries),entries=len(entries),
        bytes=sum(e[3] for e in entries if e[1]=='file'),
        publisher_authenticated=False),indent=2))


if __name__=='__main__': main()
