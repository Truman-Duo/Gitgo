"""Bind same-run upstream test outputs to this checkout, never to an old green run.

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
    decisions = {name: required(os.environ.get(name.upper() + '_VERIFIED_COMMIT', ''), head)
                 for name in ('source', 'dashboard')}
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
        for name, run in decisions.items():
            stream.write(f'{name}={str(run).lower()}\n')
    print('Exact-checkout prerequisites checked; missing upstream suites will run here.')


if __name__ == '__main__':
    main()
