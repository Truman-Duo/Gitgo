"""Real disposable CI keyring round-trip plus frozen cleanup (never user keys)."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.secret_store import EncryptedSecretStore, SecretStoreError

def check(host):
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise RuntimeError('Real keyring acceptance requires a disposable hosted runner')
    with tempfile.TemporaryDirectory(prefix='gitgo-keyring-') as raw:
        root = Path(raw)
        store = EncryptedSecretStore(root / 'secrets.json')
        # Only newly allocated, opaque UUID references from this fixture are used.
        value = 'ci-fixture-' + uuid.uuid4().hex
        try:
            store.upsert({'fixture': value})
            assert store.read_all() == {'fixture': value}
            old = store._read_ciphertexts()['fixture']
            store.upsert({'fixture': value + '-replacement'})
            assert store.read_all() == {'fixture': value + '-replacement'}
            try:
                store.protector.unprotect(old)
            except SecretStoreError:
                pass
            else:
                raise AssertionError('Replaced native credential was not deleted')
            active = store._read_ciphertexts()['fixture']
            env = dict(os.environ, GITGO_LLM_CONFIG_PATH=str(root / 'llm.json'),
                       GITGO_LLM_SECRET_PATH=str(store.path))
            subprocess.run([str(host), '--gitgo-internal-role', 'credential-cleanup'],
                           env=env, check=True, timeout=60, capture_output=True)
            assert store.read_all() == {} and store.pending_count(store.path) == 0
            try:
                store.protector.unprotect(active)
            except SecretStoreError:
                pass
            else:
                raise AssertionError('Frozen Host did not delete native credential')
        finally:
            # A failure retains the fixture index until clear has been attempted;
            # hosted runner destruction is the final independent cleanup boundary.
            store.clear()
    print('PASS real CI keyring round-trip, replacement and frozen cleanup')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', type=Path, required=True)
    check(parser.parse_args().host.resolve(strict=True))
