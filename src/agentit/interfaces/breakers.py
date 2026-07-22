"""Process-wide circuit breakers for LLM and Kubernetes clients.

Lives outside ``portal`` so ``llm`` / ``kube`` do not import the portal
package (breaks the llm↔portal and kube↔portal import cycles).
"""
from __future__ import annotations

import threading
import time as _time


class CircuitBreaker:
    """Opens after ``threshold`` failures; resets after ``reset_after`` seconds.

    ``llm_breaker`` / ``kube_breaker`` are each one shared instance called
    concurrently from many OS threads (``asyncio.to_thread`` portal handlers
    and watcher threads). A ``threading.Lock`` protects the counters.
    """

    def __init__(self, name: str, threshold: int = 3, reset_after: float = 30.0):
        self.name = name
        self._threshold = threshold
        self._reset_after = reset_after
        self._failures = 0
        self._last_failure: float = 0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._failures < self._threshold:
                return False
            return (_time.monotonic() - self._last_failure) < self._reset_after

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._last_failure = _time.monotonic()

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def __repr__(self) -> str:
        state = "OPEN" if self.is_open else "CLOSED"
        return f"CircuitBreaker({self.name}, {state}, failures={self._failures})"


llm_breaker = CircuitBreaker("llm", threshold=3, reset_after=30)
kube_breaker = CircuitBreaker("kube", threshold=5, reset_after=60)

_ALL_BREAKERS: dict[str, CircuitBreaker] = {"llm": llm_breaker, "kube": kube_breaker}


def get_circuit_breaker_states() -> dict[str, dict[str, object]]:
    """Expose open/closed state for Health / Prometheus gauges."""
    return {
        name: {"open": breaker.is_open, "failures": breaker._failures}
        for name, breaker in _ALL_BREAKERS.items()
    }
