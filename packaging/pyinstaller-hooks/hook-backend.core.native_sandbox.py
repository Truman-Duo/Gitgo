"""Require the actual Rust library in every final Host artifact."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
name = {'win32': 'gitgo_native_sandbox.dll', 'darwin': 'libgitgo_native_sandbox.dylib',
        'linux': 'libgitgo_native_sandbox.so'}[sys.platform]
image = root / 'backend/native_runtime' / name
if not image.is_file():
    raise RuntimeError('Build the Rust sandbox module before packaging the Host')
binaries = [(str(image), 'backend/native_runtime')]
