"""Install the sealed local helper, then exercise its full six-case lifecycle.

Requires explicit administrator invocation. Preserves the normal user app and
projects. Temporary launchd jobs are removed only after UID cleanup is proven;
the installed immutable helper and private evidence remain for inspection.
"""
import argparse
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core.macos_helper_install import install


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--bundle',required=True)
    parser.add_argument('--allow-local-adhoc',action='store_true')
    args=parser.parse_args()
    if not args.run or os.getuid()!=0 or not 501<=int(os.environ.get('SUDO_UID','0'))<200000000:
        parser.error('Run with sudo --run from the ordinary login account')
    helper=install(args.bundle,allow_local_adhoc=args.allow_local_adhoc)
    print('Installed sealed acceptance helper: '+str(helper),file=sys.stderr,flush=True)
    harness=Path(__file__).with_name('check_macos_user_broker.py')
    os.execv(sys.executable,[sys.executable,'-B','-I',str(harness),'--run','--helper',str(helper)])


if __name__=='__main__': main()
