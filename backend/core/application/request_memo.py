"""Concurrent transport replay protection. Not an authorization cache."""
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import threading


@dataclass
class RequestClaim:
    digest: str
    response: dict | None = None


class RequestMemo:
    def __init__(self, capacity=4096):
        self.capacity = capacity
        self.lock = threading.Lock()
        self.claims = OrderedDict()

    def claim(self, key, operation, arguments):
        # Fingerprint the complete JSON value losslessly, including malformed
        # text. Domain validation must report that text at its own boundary;
        # transport replay admission must not fail first while encoding it.
        digest = hashlib.sha256(json.dumps([operation, arguments], ensure_ascii=True,
                                sort_keys=True, separators=(",", ":")).encode('ascii')).hexdigest()
        with self.lock:
            existing = self.claims.get(key)
            if existing is not None:
                if digest != existing.digest:
                    raise ValueError("REQUEST_IDENTITY_CONFLICT")
                return False, existing.response
            if len(self.claims) >= self.capacity:
                removable = next((k for k, v in self.claims.items() if v.response is not None), None)
                if removable is None:
                    raise ValueError("REQUEST_ADMISSION_CAPACITY")
                del self.claims[removable]
            self.claims[key] = RequestClaim(digest)
            return True, None

    def complete(self, key, response):
        # Copy the wire result; consumers must not mutate a cached receipt.
        with self.lock:
            self.claims[key].response = json.loads(json.dumps(response, ensure_ascii=False))
