"""Run release tests with an explicit, build-only pytest dependency set."""
from pathlib import Path
import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packages', type=Path)
    args, pytest_args = parser.parse_known_args()
    if args.packages:
        packages = args.packages.resolve(strict=True)
        if not (packages / 'pytest/__init__.py').is_file():
            parser.error('--packages must contain a complete pytest installation')
        sys.path.insert(0, str(packages))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import pytest
    if not callable(getattr(pytest, 'main', None)):
        parser.error('pytest is unavailable; provide -TestPackages to the release script')
    return pytest.main(pytest_args)


if __name__ == '__main__':
    raise SystemExit(main())
