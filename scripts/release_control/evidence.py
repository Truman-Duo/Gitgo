"""Atomic, process-locked local evidence. A receipt is never an authorization."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + '\n').encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def file_fact(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing or redirected release input/output: ' + str(path))
    with path.open('rb') as stream:
        value = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'bytes': path.stat().st_size, 'sha256': value}


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
        if os.name != 'nt':
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def atomic(path, value):
    atomic_bytes(path, encode(value))


class Evidence:
    def __init__(self, root, plan):
        self.root, self.plan = Path(root), plan
        if self.root.is_symlink() or {'.gitgo', '.git'} & {p.casefold() for p in self.root.resolve().parts}:
            raise ValueError('Release evidence belongs outside product .gitgo')
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity = digest(plan)

    @contextmanager
    def locked(self):
        # OS locks survive a persistent lock file but are released on crash;
        # never unlink it and create two independently lockable inodes.
        with (self.root / 'controller.lock').open('a+b') as stream:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ValueError('Another release controller owns this evidence') from exc
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ValueError('Another release controller owns this evidence') from exc
            try:
                path = self.root / 'plan.json'
                if path.exists() and json.loads(path.read_bytes()) != self.plan:
                    raise ValueError('Plan changed; use a new evidence directory')
                if not path.exists():
                    atomic(path, self.plan)
                yield self
            finally:
                if os.name == 'nt':
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def read(self, name):
        path = self.root / (name + '.json')
        return json.loads(path.read_bytes()) if path.exists() else None

    def record(self, name, state, **fields):
        value = {'plan': self.identity, 'state': state, 'time': time.time(), **fields}
        # Preserve every attempt/intent, including a write with a lost response.
        history = self.root / 'history'
        history.mkdir(exist_ok=True)
        atomic(history / (str(time.time_ns()) + '-' + name + '.json'), value)
        atomic(self.root / (name + '.json'), value)
        return value
