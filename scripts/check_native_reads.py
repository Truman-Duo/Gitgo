"""Exercise Rust-backed reads inside an actual installed/frozen Host."""
import argparse
from pathlib import Path
import subprocess
import sys

SOURCE = r'''
import hashlib, os, sys, tempfile
from pathlib import Path
from backend.core.native_sandbox import get_native, NativeBoundaryError
from backend.core.sandbox_read import ReadRoot, ReadBoundaryError
assert getattr(sys, 'frozen', False), 'Frozen Host required'
native = get_native()
assert native.call(op='capabilities')['safe_read_version'] == 2
with tempfile.TemporaryDirectory(prefix='gitgo-read-accept-') as raw:
    directory = Path(raw)
    body = bytes(range(256)) * 1024
    (directory / 'file').write_bytes(body)
    with ReadRoot(directory) as root:
        with root.pin('file') as pin:
            try:
                native.call(op='read_hash', token=pin._token, max_bytes=1024*1024)
            except NativeBoundaryError:
                pass
            else:
                raise AssertionError('Metadata pin read content')
        with root.open('file') as opened:
            assert not hasattr(opened, '_fd')
            assert opened.read_bytes() == body
            assert opened.sha256() == (hashlib.sha256(body).hexdigest(), len(body))
        os.link(directory / 'file', directory / 'alias')
        try:
            root.open('alias')
        except ReadBoundaryError:
            pass
        else:
            raise AssertionError('Multiply linked object accepted')
        (directory / 'alias').unlink()
        (directory / 'replacement').write_bytes(b'attacker')
        with root.open('file') as opened:
            if sys.platform == 'win32':
                try:
                    os.replace(directory / 'replacement', directory / 'file')
                except OSError:
                    pass
                else:
                    raise AssertionError('Pinned Windows file was replaced')
                assert opened.read_bytes() == body
            else:
                os.replace(directory / 'replacement', directory / 'file')
                try:
                    opened.read_bytes()
                except ReadBoundaryError:
                    pass
                else:
                    raise AssertionError('Unlinked held object was read')
print('Frozen Rust object reads verified')
'''


def check(host, *, env=None, cwd=None):
    subprocess.run([str(Path(host).resolve(strict=True)), '--gitgo-internal-role',
                    'python', '-I', '-c', SOURCE], check=True, timeout=90, env=env, cwd=cwd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    args = parser.parse_args()
    check(args.host)
