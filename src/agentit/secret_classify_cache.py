"""Sync facade for secret-classify verdict persistence.

``SecurityAnalyzer`` runs synchronously (including from background threads /
``asyncio.to_thread``). The portal store is asyncpg-bound to the portal event
loop, so callers that have a store pass a ``BridgedSecretClassifyCache`` which
schedules each lookup/upsert/touch/delete back onto that loop — the same
``run_coroutine_threadsafe`` pattern ``assess_pipeline.start_assess_job`` uses
for every other store call from its worker thread.

Keyed by ``(app_name, file_path, snippet_hash)`` so a line edit invalidates
the prior verdict (new hash → first sight again).
"""

from __future__ import annotations

import hashlib
from typing import Protocol


def snippet_hash(matched_line: str) -> str:
    """Stable fingerprint of a secret-match line (strip + sha256 hex)."""
    normalized = matched_line.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class SecretClassifyCache(Protocol):
    def lookup(self, app_name: str, file_path: str, snippet_hash: str) -> dict | None: ...

    def touch(self, app_name: str, file_path: str, snippet_hash: str) -> None: ...

    def upsert(
        self,
        app_name: str,
        file_path: str,
        snippet_hash: str,
        secret_type: str,
        outcome: str,
        confidence: float,
        reason: str,
        source: str = "llm",
    ) -> None: ...

    def delete(self, app_name: str, file_path: str, snippet_hash: str) -> None: ...


class BridgedSecretClassifyCache:
    """Sync adapter that drives ``AssessmentStore`` secret-classify methods
    via a caller-supplied ``bridge(coro) -> result`` callable."""

    def __init__(self, store, bridge) -> None:
        self._store = store
        self._bridge = bridge

    def lookup(self, app_name: str, file_path: str, snippet_hash: str) -> dict | None:
        return self._bridge(
            self._store.lookup_secret_classify(app_name, file_path, snippet_hash)
        )

    def touch(self, app_name: str, file_path: str, snippet_hash: str) -> None:
        self._bridge(
            self._store.touch_secret_classify(app_name, file_path, snippet_hash)
        )

    def upsert(
        self,
        app_name: str,
        file_path: str,
        snippet_hash: str,
        secret_type: str,
        outcome: str,
        confidence: float,
        reason: str,
        source: str = "llm",
    ) -> None:
        self._bridge(
            self._store.upsert_secret_classify(
                app_name, file_path, snippet_hash, secret_type,
                outcome, confidence, reason, source=source,
            )
        )

    def delete(self, app_name: str, file_path: str, snippet_hash: str) -> None:
        self._bridge(
            self._store.delete_secret_classify(app_name, file_path, snippet_hash)
        )


class InMemorySecretClassifyCache:
    """Test / CLI helper — same semantics as the Postgres table, no I/O."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], dict] = {}

    def lookup(self, app_name: str, file_path: str, snippet_hash: str) -> dict | None:
        row = self._rows.get((app_name, file_path, snippet_hash))
        return dict(row) if row is not None else None

    def touch(self, app_name: str, file_path: str, snippet_hash: str) -> None:
        key = (app_name, file_path, snippet_hash)
        row = self._rows.get(key)
        if row is None:
            return
        row["hit_count"] = int(row["hit_count"]) + 1

    def upsert(
        self,
        app_name: str,
        file_path: str,
        snippet_hash: str,
        secret_type: str,
        outcome: str,
        confidence: float,
        reason: str,
        source: str = "llm",
    ) -> None:
        key = (app_name, file_path, snippet_hash)
        prev = self._rows.get(key)
        hit_count = int(prev["hit_count"]) + 1 if prev else 1
        self._rows[key] = {
            "app_name": app_name,
            "file_path": file_path,
            "snippet_hash": snippet_hash,
            "secret_type": secret_type,
            "outcome": outcome,
            "confidence": confidence,
            "reason": reason,
            "source": source,
            "hit_count": hit_count,
        }

    def delete(self, app_name: str, file_path: str, snippet_hash: str) -> None:
        self._rows.pop((app_name, file_path, snippet_hash), None)
