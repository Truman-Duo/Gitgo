"""Preserve bounded test state before cleanup; never copy provider configuration."""
import hashlib
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import time


def archive_state(root: Path, destination: Path, *, max_bytes=128 * 1024 * 1024):
    state = root / 'state'
    if state.resolve() != state.absolute():
        raise ValueError('redirected test state')
    projects = state / 'projects'
    files = []
    if projects.exists():
        for project in projects.iterdir():
            if not project.is_dir():
                continue
            files.extend(p for p in (project / 'state.sqlite3', project / 'observability.sqlite3') if p.is_file())
            files.extend(p for p in (project / 'cas').glob('*/*') if p.is_file())
    for source in files:
        if source.is_symlink() or not source.resolve().is_relative_to(state.resolve()):
            raise ValueError('redirected evidence file')
    if sum(p.stat().st_size for p in files) > max_bytes:
        raise ValueError('test evidence archive exceeds limit')
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {'complete': False, 'source_run': root.name, 'files': {}, 'contains_provider_configuration': False}
    deadline = time.monotonic() + 20
    try:
        for source in files:
            if time.monotonic() >= deadline:
                raise TimeoutError('evidence archive deadline')
            target = destination / source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == '.sqlite3':
                def progress(_status, _remaining, _total):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('sqlite snapshot deadline')
                with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as reader:
                    with closing(sqlite3.connect(target)) as writer:
                        reader.backup(writer, pages=256, progress=progress, sleep=0.05)
            else:
                shutil.copyfile(source, target)
            manifest['files'][target.relative_to(destination).as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest['complete'] = True
        return {'ok': True, 'path': str(destination), 'files': len(files)}
    except Exception as error:
        manifest['error_type'] = type(error).__name__
        raise
    finally:
        (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
