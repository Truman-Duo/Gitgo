"""Bounded official asset downloads; integrity failures are never retried."""
from __future__ import annotations
import hashlib
import time
import urllib.error
import urllib.request


def fetch_verified(url: str, expected: str, *, algorithm='sha256', limit: int) -> bytes:
    if not url.startswith('https://'):
        raise ValueError('Pinned CI assets require HTTPS')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
            print(f'CI asset HTTP {error.code}; retry {attempt + 1}/2', flush=True)
        except (TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            print(f'CI asset connection timeout/failure; retry {attempt + 1}/2', flush=True)
        else:
            if len(data) > limit or hashlib.new(algorithm, data).hexdigest() != expected:
                raise ValueError('Pinned CI asset failed digest or size validation')
            return data
        time.sleep(attempt + 1)
    raise AssertionError('Unreachable bounded download state')
