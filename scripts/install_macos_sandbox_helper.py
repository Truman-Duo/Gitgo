"""Install a root-controlled frozen helper for explicit local acceptance only."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core.macos_helper_install import install,inventory,identity

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',required=True)
    parser.add_argument('--install',action='store_true')
    parser.add_argument('--allow-local-adhoc',action='store_true')
    args=parser.parse_args()
    if args.install:
        print(json.dumps(dict(helper=str(install(args.bundle,allow_local_adhoc=args.allow_local_adhoc)),production_enabled=False)))
    else:
        files=inventory(Path(args.bundle).resolve(strict=True))
        print(json.dumps(dict(bundle_id=identity(files),entries=len(files),production_enabled=False)))

if __name__=='__main__': main()
