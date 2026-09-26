"""Idempotency-key store with request fingerprinting.

Semantics:
- First submission of a key claims it and executes; the response is stored.
- Replays of the same key with the *same* fingerprint return the stored
  response without re-executing (no duplicate funds movement).
- Replays of the same key with a *different* fingerprint raise
  IdempotencyConflict, distinguishable from other failures.
- Concurrent submissions of the same key block until the owner completes,
  then return the owner's response -- no partial or duplicate posting.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any

from app.errors import IdempotencyConflict


@dataclass
class IdempotencyRecord:
    key: str
    fingerprint: str
    response: Any = None
    completed: bool = False
    done: threading.Event = field(default_factory=threading.Event)


class IdempotencyStore:
    """Thread-safe store ensuring one execution per key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, IdempotencyRecord] = {}

    @staticmethod
    def fingerprint_of(payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def begin(self, key: str, fingerprint: str) -> tuple[IdempotencyRecord, bool]:
        """Claim a key.

        Returns (record, True) if the caller owns the key and must execute.
        Returns (record, False) if another caller owns/owned it; the caller
        must wait on record.done and reuse record.response.
        Raises IdempotencyConflict on fingerprint mismatch.
        """
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                record = IdempotencyRecord(key=key, fingerprint=fingerprint)
                self._records[key] = record
                return record, True
            if existing.fingerprint != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency key {key!r} replayed with different request content"
                )
            return existing, False

    def complete(self, key: str, response: Any) -> None:
        with self._lock:
            record = self._records[key]
            record.response = response
            record.completed = True
            record.done.set()

    def abandon(self, key: str) -> None:
        """Release a claimed key after a failed execution so retries may proceed."""
        with self._lock:
            record = self._records.pop(key, None)
            if record is not None:
                record.completed = False
                record.done.set()

    def get(self, key: str) -> IdempotencyRecord | None:
        with self._lock:
            return self._records.get(key)
