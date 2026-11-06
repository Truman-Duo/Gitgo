"""Bind Dashboard reuse to this checkout and declare source-suite ownership.

GitHub job dependencies provide the successful execution. These checks bind that
output to the exact checked-out commit; they are not independent attestations.
Standalone workflows have no upstream output and must run their own suites.
"""
import os
import re
import subprocess
from pathlib import Path


def required(value, head):
    if not value:
        return True
    if not re.fullmatch(r'[0-9a-f]{40}', value) or value != head:
        raise ValueError('Regression prerequisite belongs to a different source commit')
    return False


def main():
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    delegated = os.environ.get('SOURCE_SUITE_BY_CALLER', 'false')
    if delegated not in ('true', 'false'):
        raise ValueError('Source-suite delegation must be an explicit boolean')
    decisions = {'source': delegated != 'true',
                 'dashboard': required(os.environ.get('DASHBOARD_VERIFIED_COMMIT', ''), head)}
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
        for name, run in decisions.items():
            stream.write(f'{name}={str(run).lower()}\n')
    print('Dashboard identity checked; source suite is delegated to the caller final gate.'
          if delegated == 'true' else 'Missing upstream suites will run here.')


if __name__ == '__main__':
    main()
