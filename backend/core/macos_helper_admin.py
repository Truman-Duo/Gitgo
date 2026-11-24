"""Fixed administrator CLI for the explicitly selected macOS helper package.

The packaged wrapper dispatches this frozen module, never an adjacent Python
script. This removes that extra executable input; it does not authenticate
the first administrative runtime or its complete PyInstaller onedir. Local
use still depends on the operator selecting a verified package and explicitly
allowing a local build. Production publisher/bootstrap trust remains disabled.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


MANAGED_LOCATION_ERROR = (
    'MACOS_HELPER_MANAGED_LOCATION: Helper administration requires the extracted '
    'release package, outside the user-managed installation.'
)


def require_admin_entry():
    """Reject unsafe entry contexts before loading configuration or writing it."""
    if sys.platform != 'darwin' or os.getuid() != 0 or os.geteuid() != 0:
        raise PermissionError('macOS helper administration requires real and effective root authority')
    # Check both spellings: the frozen executable may have been invoked through
    # a symlink. The normal installed_runtime admission remains mandatory too.
    locations = (Path(sys.executable).absolute(), Path(__file__).absolute())
    for location in locations:
        for candidate in (location, location.resolve()):
            for root in candidate.parents:
                if root.name == 'active':
                    marker = root.parent / 'control/registration.json'
                    if os.path.lexists(marker):
                        raise PermissionError(MANAGED_LOCATION_ERROR)


def require_frozen_admin_entry():
    require_admin_entry()
    if not getattr(sys, 'frozen', False):
        raise PermissionError('The packaged helper administrator role requires a frozen Host')
    from backend.core.macos_helper_install import BASE, verify_bundle_path
    executable=Path(sys.executable).absolute()
    if BASE in executable.parents:
        verify_bundle_path(executable)


def _arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    for command in ('install', 'upgrade', 'status', 'recover', 'uninstall', 'renew', 'revoke'):
        sub = commands.add_parser(command)
        sub.add_argument('--name', required=True)
        if command in ('install', 'upgrade'):
            sub.add_argument('--bundle', required=True)
            trust=sub.add_mutually_exclusive_group()
            trust.add_argument('--allow-local-adhoc', action='store_true')
            trust.add_argument('--expected-bundle-sha256',
                help='Complete helper inventory digest independently confirmed by the administrator')
        if command == 'install':
            sub.add_argument('--project', required=True)
            sub.add_argument('--uid', type=int, default=os.environ.get('SUDO_UID', '0'))
            sub.add_argument('--lifetime', type=int, default=180)
        if command == 'renew':
            sub.add_argument('--lifetime', type=int, default=3600)
            sub.add_argument('--after-reboot', action='store_true',
                             help='Explicitly authorize this boot after old task cleanup is verified')
    return parser.parse_args(argv)


def main(argv=None):
    """Compatibility entry for explicit source-side administrator experiments."""
    require_admin_entry()
    args = _arguments(argv)
    # Do not import lifecycle/configuration code until the entry guards and
    # complete fixed CLI grammar have passed. There is no arbitrary -c/-m/path.
    from backend.core import macos_helper_lifecycle as lifecycle
    from backend.core import macos_project_authority as authority

    if args.operation == 'install':
        pin={} if args.expected_bundle_sha256 is None else dict(expected_bundle_sha256=args.expected_bundle_sha256)
        result = lifecycle.register(args.name, args.bundle, args.project, args.uid,
            lifetime=args.lifetime, allow_local_adhoc=args.allow_local_adhoc, **pin)
    elif args.operation == 'upgrade':
        pin={} if args.expected_bundle_sha256 is None else dict(expected_bundle_sha256=args.expected_bundle_sha256)
        result = lifecycle.upgrade(args.name, args.bundle, allow_local_adhoc=args.allow_local_adhoc, **pin)
    elif args.operation == 'renew':
        result = authority.renew(args.name, lifetime=args.lifetime, after_reboot=args.after_reboot)
    elif args.operation == 'revoke':
        result = authority.revoke(args.name)
    else:
        result = getattr(lifecycle, args.operation)(args.name)
    print(json.dumps(dict(result=result, production_enabled=False), indent=2))
    return 0


def frozen_main(argv):
    require_frozen_admin_entry()
    return main(argv)
