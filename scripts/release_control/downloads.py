"""Acquire immutable CI artifacts once, then verify before reusing/extracting."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import stat
import zipfile

from .evidence import file_fact


def unpack(archive, output):
    """Only flat release artifacts, never archive-directed paths or links."""
    from backend.core.update.archive import _parts
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if (not 1 <= len(entries) <= 32 or sum(i.file_size for i in entries) > 1024**3
                or len({i.filename.casefold() for i in entries}) != len(entries)):
            raise ValueError('CI artifact inventory is too large or has colliding names')
        for item in entries:
            _parts(item.filename)
            mode = item.external_attr >> 16
            if len(Path(item.filename).parts) != 1 or item.is_dir() or stat.S_ISLNK(mode):
                raise ValueError('Expected a flat regular CI artifact')
        outputs = {}
        for item in entries:
            path = output/item.filename
            if path.is_symlink():
                raise ValueError('Redirected CI artifact destination')
            temporary = output/(item.filename + '.partial')
            if temporary.is_symlink():
                raise ValueError('Redirected CI artifact temporary destination')
            with bundle.open(item) as source, temporary.open('wb') as destination:
                total = 0
                while chunk := source.read(1024*1024):
                    total += len(chunk)
                    if total > item.file_size:
                        raise ValueError('Expanded CI artifact exceeds inventory')
                    destination.write(chunk)
            if total != item.file_size:
                raise ValueError('Truncated CI artifact entry')
            temporary.replace(path)
            outputs[str(path)] = file_fact(path)
        return outputs


def acquire(plan, evidence, remote, workers):
    declarations = plan.get('artifacts', [])
    def receive(item):
        artifact_id = item['artifact_id']
        metadata = remote.api('actions/artifacts/' + str(artifact_id))
        if (metadata['id'] != artifact_id or metadata['expired'] or metadata['name'] != item['name']
                or metadata['workflow_run']['id'] != item['run_id']
                or metadata['workflow_run']['head_sha'] != plan['release']['commit']
                or item['run_id'] not in {ci['run_id'] for ci in plan['ci']}):
            raise ValueError('CI artifact is expired or belongs to another source/run/name')
        size, digest = metadata['size_in_bytes'], metadata.get('digest', '')
        if not 1 <= size <= 1024**3 or not re.fullmatch('sha256:[0-9a-f]{64}', digest):
            raise ValueError('Expected a bounded artifact with a GitHub transport digest')
        name = 'artifact-' + str(artifact_id)
        archive = evidence.root/(name + '.zip')
        expected = {'bytes': size, 'sha256': digest.removeprefix('sha256:')}
        previous = evidence.read(name)
        valid = archive.is_file() and file_fact(archive) == expected
        if not valid:
            evidence.record(name, 'running', artifact_id=artifact_id)
            temporary = archive.with_suffix('.partial')
            remote.download_artifact(artifact_id, temporary, size)
            if file_fact(temporary) != expected:
                raise ValueError('CI artifact transport SHA-256 mismatch')
            temporary.replace(archive)
        if previous and previous.get('state') == 'success' and previous.get('transport') == expected:
            try:
                if all(file_fact(p) == f for p, f in previous['outputs'].items()):
                    evidence.record('reuse-' + name, 'reused', transport=expected)
                    return previous
            except ValueError:
                pass
        outputs = unpack(archive, Path(item['output']))
        return evidence.record(name, 'success', artifact_id=artifact_id, run_id=item['run_id'],
                               transport=expected, outputs=outputs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(receive, declarations))
