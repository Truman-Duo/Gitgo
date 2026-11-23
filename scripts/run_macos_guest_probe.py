"""Build and run development-only Apple Virtualization experiments.

No production capability is enabled. Installation targets a NEW private VM
directory; boot starts that experiment for 45 seconds then force-stops it.
Neither action mounts host directories or attaches a network interface.
The large official restore image must already exist locally for installation.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile


def build_probe(name: str, directory: Path) -> Path:
    source = Path(__file__).with_name(f'probe_macos_{name}.m')
    executable = directory / name
    entitlements = directory / 'entitlements.plist'
    entitlements.write_bytes(plistlib.dumps({'com.apple.security.virtualization': True}))
    subprocess.run([
        '/usr/bin/xcrun', 'clang', '-fobjc-arc', '-Wall', '-Wextra', '-Werror',
        str(source), '-framework', 'Foundation', '-framework', 'Virtualization',
        '-o', str(executable),
    ], check=True, timeout=120)
    subprocess.run([
        '/usr/bin/codesign', '--force', '--sign', '-', '--identifier',
        f'org.gitgo.sandbox.probe.{name.replace("_", "-")}',
        '--entitlements', str(entitlements), str(executable),
    ], check=True, timeout=30)
    return executable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('requirements', help='Query Apple restore metadata; no image download')
    configuration = commands.add_parser('configuration', help='Validate device variants without booting')
    configuration.add_argument('--metadata', type=Path, required=True)
    configuration.add_argument('--output-directory', type=Path, required=True)
    install = commands.add_parser('install', help='Install only in a new, separate VM disk')
    install.add_argument('--restore-image', type=Path, required=True)
    install.add_argument('--new-vm-directory', type=Path, required=True)
    boot = commands.add_parser('boot', help='Start an installed experiment and force-stop after 45 seconds')
    boot.add_argument('--vm-directory', type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != 'darwin':
        parser.error('Apple Virtualization experiments require a real Mac')
    if args.command == 'requirements':
        name, parameters = 'guest_requirements', []
    elif args.command == 'configuration':
        name = 'vm_configuration'
        parameters = [str(args.metadata.absolute()), str(args.output_directory.absolute())]
    elif args.command == 'install':
        name = 'guest_lifecycle'
        if os.path.lexists(args.new_vm_directory):
            parser.error('Installation requires a NEW directory; existing VM assets are never overwritten')
        if not args.restore_image.is_file():
            parser.error('A previously downloaded official restore image is required')
        parameters = ['install', str(args.restore_image.absolute()), str(args.new_vm_directory.absolute())]
    else:
        name = 'guest_lifecycle'
        parameters = ['boot', str(args.vm_directory.absolute())]
    try:
        with tempfile.TemporaryDirectory(prefix='gitgo-macos-guest-probe-') as raw:
            executable = build_probe(name, Path(raw))
            # The installer owns its cancellation protocol. Do not impose a
            # subprocess timeout that kills it mid-installation or claim RAII
            # proves VM cleanup after a forced parent/helper exit.
            return subprocess.run([str(executable), *parameters], check=False).returncode
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f'Guest experiment did not complete: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
