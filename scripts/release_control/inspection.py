"""Read cross-target archive evidence without installing or creating symlinks."""
import hashlib
from pathlib import Path
import time

from backend.core.update.archive import _extract_tar, _extract_zip, _validate, ExtractionLimits
from backend.core.update.contracts import _verify_bundle_evidence
from backend.core.update.validation import json_object

METADATA = {'product.json', 'build-manifest.json', 'update-data-contract.json', 'release-trust.json'}


class ArchiveEvidence:
    def __init__(self, archive, work):
        self.records, self.raw = {}, {}
        limits, started = ExtractionLimits(), time.monotonic()
        def checkpoint():
            if time.monotonic() - started > limits.seconds:
                raise ValueError('Release archive inspection timed out')
        with Path(archive).open('rb') as source:
            if str(archive).endswith('.zip'):
                _extract_zip(source, None, limits, checkpoint, writer=self.read)
            else:
                _extract_tar(source, None, work, limits, checkpoint, writer=self.read)

    def read(self, entries, opener, destination, limits, checkpoint, *, wrapped):
        root = _validate(entries, limits, checkpoint, wrapped=wrapped)
        prefix = root + '/' if wrapped else ''
        for item in entries:
            checkpoint()
            name = item.name[len(prefix):] if prefix else item.name
            if item.name == root and wrapped:
                continue
            record = {'path': name, 'kind': item.kind, 'mode': 0o700 if item.executable else 0o600}
            if item.kind == 'symlink':
                record['link'] = item.link
            elif item.kind == 'file':
                hasher, size, metadata = hashlib.sha256(), 0, bytearray()
                with opener(item.source) as stream:
                    while chunk := stream.read(1024*1024):
                        checkpoint()
                        size += len(chunk)
                        if size > item.size:
                            raise ValueError('Archive content exceeds its declaration')
                        hasher.update(chunk)
                        if name in METADATA:
                            if size > 2*1024**2:
                                raise ValueError('Release metadata exceeds its limit')
                            metadata.extend(chunk)
                if size != item.size:
                    raise ValueError('Truncated release archive content')
                record.update(bytes=size, sha256=hasher.hexdigest())
                if name in METADATA:
                    self.raw[name] = bytes(metadata)
            self.records[name] = record

    def metadata(self, name):
        if name not in self.raw or self.records[name]['kind'] != 'file':
            raise ValueError('Missing regular archive metadata: ' + name)
        return json_object(self.raw[name], 2*1024**2)

    def verify(self, manifest):
        _verify_bundle_evidence(manifest, self.records, self.metadata)
