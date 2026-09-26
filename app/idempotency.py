"""Idempotency-key store with replay and conflict detection.

Semantics:
- First request with a key claims it (in_progress) and records a fingerprint
  of the request payload.
- A concurrent request with the same key sees the in-progress claim and gets
  IDEMPOTENCY_IN_PROGRESS instead of double-processing.
- Once completed, a replay with the same payload returns the stored response
  (REPLAYED); a replay with a different payload raises
  IDEMPOTENCY_CONFLICT, distinguishable from other failures.
- A failed attempt releases the claim so the caller may retry safely.
"""
from __future__ import annotations

import hashlib
import json
import threading

from .errors import IdempotencyConflictError, IdempotencyInProgressError
from .models import IdempotencyRecord


def fingerprint(payload: dict) -> str:
    """Stable fingerprint of a request payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyStore:
    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()

    def begin(self, key: str, request_fingerprint: str) -> tuple[IdempotencyRecord, bool]:
        """Claim a key.

        Returns (record, is_replay). Raises on conflict or in-progress claim.
        """
        with self._lock:
            record = self._records.get(key)
            if record is None:
                record = IdempotencyRecord(
                    key=key, request_fingerprint=request_fingerprint, in_progress=True
                )
                self._records[key] = record
                return record, False
            if record.request_fingerprint != request_fingerprint:
                raise IdempotencyConflictError(
                    f"idempotency key '{key}' reused with different payload"
                )
            if record.in_progress:
                raise IdempotencyInProgressError(
                    f"idempotency key '{key}' is being processed concurrently"
                )
            return record, True

    def complete(
        self, key: str, payment_id: str, status_code: int, response_body: dict
    ) -> None:
        with self._lock:
            record = self._records[key]
            record.in_progress = False
            record.payment_id = payment_id
            record.status_code = status_code
            record.response_body = response_body

    def fail(self, key: str) -> None:
        """Release an in-progress claim after a retryable failure."""
        with self._lock:
            record = self._records.get(key)
            if record is not None and record.in_progress:
                del self._records[key]

    def get(self, key: str) -> IdempotencyRecord | None:
        return self._records.get(key)
