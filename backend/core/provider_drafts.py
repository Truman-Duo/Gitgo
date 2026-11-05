"""Ephemeral configuration requests and evidence receipts, never configuration authority."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

from .provider_catalog import discover, safe_opener
from .loop.provider_probe import ProviderProbe
from .loop.provider_transport import HttpProviderTransport, ProviderTransportCancelled


def identity(draft):
    return hashlib.sha256(json.dumps({k: draft.get(k, "") for k in (
        "base_url", "api_key", "model_id", "protocol", "kind", "auth_mode", "request_preferences",
    )}, sort_keys=True).encode()).hexdigest()


class ProviderDrafts:
    def __init__(self):
        self.lock = threading.RLock()
        self.requests = {}
        self.receipts = {}

    def cancel(self, request_id):
        with self.lock:
            self._prune()
            event, _ = self.requests.setdefault(request_id, (threading.Event(), time.monotonic()))
            event.set()
        return {"cancelled": True}

    def _prune(self):
        now = time.monotonic()
        self.requests = {k: v for k, v in self.requests.items() if now - v[1] < 120}
        self.receipts = {k: v for k, v in self.receipts.items() if now - v[0] < 900}
        if len(self.requests) >= 128 or len(self.receipts) >= 128:
            raise ValueError("TOO_MANY_PROVIDER_REQUESTS")

    def run(self, action, draft, request_id, timeout):
        if not request_id or len(request_id) > 128:
            raise ValueError("REQUEST_ID_REQUIRED")
        with self.lock:
            self._prune()
            if request_id in self.requests:
                raise ValueError("REQUEST_CANCELLED_OR_DUPLICATE")
            event = threading.Event()
            self.requests[request_id] = (event, time.monotonic())
        try:
            started_at = time.monotonic()
            timer = threading.Timer(max(1, min(float(timeout), 60)), event.set)
            timer.daemon = True
            timer.start()
            if action == "models":
                from .provider_metadata import catalogue, enrich, reference_models, CATALOG_URL
                if not draft["api_key"].strip() and draft["auth_mode"] == "api_key":
                    data, state = catalogue(event, timeout=max(0,float(timeout)-(time.monotonic()-started_at)-0.2))
                    candidates = reference_models(draft["base_url"], data)
                    # Entry-time reference discovery is not an anonymous attempt
                    # against an account endpoint. An unavailable catalogue is
                    # an incomplete draft, not a failed credential check.
                    result = {"models": candidates, "source": CATALOG_URL, "reference_only": True,
                              "credentials_required": not bool(candidates), "manual_allowed": True,
                              "metadata_catalogue": state}
                else:
                    result = discover(**{k: draft[k] for k in ("base_url", "api_key", "protocol", "kind", "auth_mode")},timeout=timeout,cancel_event=event)
                    data, state = catalogue(event, timeout=max(0,float(timeout)-(time.monotonic()-started_at)-0.2))
                    result["models"] = enrich(result["models"], draft["base_url"], data)
                    result["metadata_catalogue"] = state
            else:
                result = ProviderProbe(HttpProviderTransport(safe_opener(draft["kind"]))).probe(
                    **draft, timeout=timeout, cancel_event=event, **({"connection_only": True} if action == "connect" else {}),
                ).to_dict()
            if event.is_set():
                raise ProviderTransportCancelled()
            receipt = uuid.uuid4().hex
            with self.lock:
                self.receipts[receipt] = (time.monotonic(), identity(draft), action, result)
            return {**result, "receipt_id": receipt}
        finally:
            timer.cancel()
            with self.lock:
                self.requests.pop(request_id, None)

    def evidence(self, receipt_id, draft, action):
        if not receipt_id:
            return None
        with self.lock:
            self._prune()
            receipt = self.receipts.get(receipt_id)
            expected = identity({**draft, "api_key": ""}) if receipt and action == "models" and receipt[3].get("reference_only") else identity(draft)
            if receipt is None or receipt[1] != expected or receipt[2] != action:
                raise ValueError("PROVIDER_EVIDENCE_STALE")
            return receipt[3]
