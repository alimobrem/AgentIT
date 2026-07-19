"""Fault-injection tests proving `llm.py`'s `LLMClient._chat()` is
correctly wired into `llm_breaker` (`agentit.portal.helpers`) -- the
`llm`-side counterpart to `test_kube_breaker.py`'s coverage of
`kube_breaker`. That file already proves the *pattern* works for
`kube.py`; this extends the exact same fault-injection shape to `llm.py`,
which previously had graceful-failure tests (`test_llm_graceful.py`) but
nothing proving repeated real failures actually *trip* `llm_breaker`, or
that an open breaker actually *skips* the real Anthropic call.

Every test here mocks `LLMClient._client` (`agentit.llm._create_client()`'s
return value) -- none of these ever attempt a real Anthropic/Vertex AI
call.

Reset explicitly around every test (not via the session's
`_reset_kube_breaker`-style autouse fixture, which deliberately does not
cover `llm_breaker` -- see its docstring) so failures/opens injected here
never leak into unrelated tests elsewhere in the suite.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from agentit.llm import LLMClient
from agentit.portal.helpers import llm_breaker


@pytest.fixture(autouse=True)
def _reset_llm_breaker():
    llm_breaker._failures = 0
    llm_breaker._last_failure = 0
    yield
    llm_breaker._failures = 0
    llm_breaker._last_failure = 0


def _client_raising(exc: Exception) -> LLMClient:
    with patch("agentit.llm._create_client") as mock_factory:
        mock_factory.return_value = MagicMock()
        client = LLMClient(model="test")
    client._client.messages.create.side_effect = exc
    return client


def _client_returning(text: str) -> LLMClient:
    with patch("agentit.llm._create_client") as mock_factory:
        mock_factory.return_value = MagicMock()
        client = LLMClient(model="test")
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    resp.stop_reason = "end_turn"
    client._client.messages.create.return_value = resp
    return client


class TestBreakerOpensAfterThreshold:
    """(a) repeated real-call failures (mocked) actually open `llm_breaker`
    after its threshold -- mirrors `test_kube_breaker.py`'s coverage for
    `kube_breaker`."""

    def test_three_consecutive_failures_open_the_breaker(self):
        assert llm_breaker._threshold == 3  # documents the threshold this test proves against
        client = _client_raising(TimeoutError("upstream timed out"))

        for _ in range(llm_breaker._threshold):
            assert not llm_breaker.is_open
            assert client.summarize_architecture({"languages": []}, ["a.py"]) is None

        assert llm_breaker.is_open
        assert llm_breaker._failures == llm_breaker._threshold

    def test_fewer_than_threshold_failures_leave_the_breaker_closed(self):
        client = _client_raising(RuntimeError("connection reset"))

        for _ in range(llm_breaker._threshold - 1):
            assert client.summarize_architecture({"languages": []}, ["a.py"]) is None

        assert not llm_breaker.is_open

    def test_failures_from_different_callers_accumulate_on_the_shared_breaker(self):
        """`llm_breaker` is one shared instance -- failures from different
        `LLMClient` methods (all funneling through `_chat()`) must all
        count toward the same threshold."""
        client = _client_raising(RuntimeError("rate limited"))

        assert client.classify_secret("f.py", "x=1", ["x=1"]) is None
        assert client.summarize_architecture({"languages": []}, ["a.py"]) is None
        assert not llm_breaker.is_open
        assert client.review_fix("finding", "category", "fix content", "app summary") is None

        assert llm_breaker.is_open


class TestOpenBreakerSkipsRealCalls:
    """(b) once open, further calls are skipped/fail-fast rather than
    attempting the real Anthropic API -- every `LLMClient` public method
    already treats a `None` `_chat()` result as "unavailable" (fail-closed
    for the classifiers, `None` propagated for summaries), so the assertion
    here is specifically that the real client is never invoked while the
    breaker is open."""

    def test_chat_skips_the_real_call_and_returns_none(self):
        for _ in range(llm_breaker._threshold):
            llm_breaker.record_failure()

        client = _client_returning("this should never be read")
        result = client.summarize_architecture({"languages": []}, ["a.py"])

        assert result is None
        client._client.messages.create.assert_not_called()

    def test_review_fix_fails_closed_without_calling_the_real_api(self):
        for _ in range(llm_breaker._threshold):
            llm_breaker.record_failure()

        client = _client_returning('{"approved": true, "confidence": 0.99, "reason": "ok"}')
        result = client.review_fix("finding", "container", "fix content", "app summary")

        assert result is None
        client._client.messages.create.assert_not_called()


class TestSuccessResetsFailureCount:
    """(c) a success resets the failure count -- proven against a real
    (mocked-transport) `_chat()` round trip, not just the `CircuitBreaker`
    unit directly (see `test_durability.py::TestCircuitBreaker` for that)."""

    def test_success_after_failures_resets_the_count(self):
        client = _client_raising(TimeoutError("slow upstream"))
        for _ in range(llm_breaker._threshold - 1):
            assert client.summarize_architecture({"languages": []}, ["a.py"]) is None
        assert llm_breaker._failures == llm_breaker._threshold - 1
        assert not llm_breaker.is_open

        client._client.messages.create.side_effect = None
        resp = MagicMock()
        resp.content = [MagicMock(text="A concise architecture summary.")]
        resp.stop_reason = "end_turn"
        client._client.messages.create.return_value = resp

        result = client.summarize_architecture({"languages": []}, ["a.py"])

        assert result == "A concise architecture summary."
        assert llm_breaker._failures == 0
        assert not llm_breaker.is_open


class _TrackingLock:
    """Wraps a real ``threading.Lock``, tracking the max number of
    concurrent holders ever observed -- must stay at 1 if the wrapped lock
    is genuinely providing mutual exclusion. Same technique
    ``test_ttl_cache_locking.py`` uses to deterministically prove a lock is
    on the critical path, rather than racing for a probabilistic (flaky)
    lost-update reproduction.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._count_guard = threading.Lock()
        self._holders = 0
        self.max_concurrent = 0

    def __enter__(self):
        self._lock.acquire()
        with self._count_guard:
            self._holders += 1
            self.max_concurrent = max(self.max_concurrent, self._holders)
        return self

    def __exit__(self, *exc_info):
        with self._count_guard:
            self._holders -= 1
        self._lock.release()
        return False


class TestConcurrentAccessIsThreadSafe:
    """Regression coverage for the finding that `CircuitBreaker.
    record_failure()`/`record_success()`/`is_open` read-modify-wrote
    `_failures`/`_last_failure` with no lock at all, even though
    `llm_breaker`/`kube_breaker` are each one shared instance called from
    many concurrent OS threads (every `_chat()` call and most of `kube.py`'s
    real API-calling functions run inside `asyncio.to_thread` from the
    portal's request handlers, plus every watcher's own thread). Swaps in
    an instrumented lock and drives many concurrent real threads through
    every mutating/reading method -- deterministically proves the source
    still wraps the critical section in the lock (would read
    `max_concurrent == 0`, an immediate failure, against the prior code,
    since that lock is never touched at all without the fix).
    """

    def test_record_failure_and_is_open_are_mutually_exclusive(self):
        from agentit.portal.helpers import CircuitBreaker
        cb = CircuitBreaker("concurrency-test", threshold=3, reset_after=60)
        tracking = _TrackingLock()
        cb._lock = tracking

        def hammer():
            for _ in range(50):
                cb.record_failure()
                cb.is_open
                cb.record_success()

        threads = [threading.Thread(target=hammer) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert tracking.max_concurrent == 1

    def test_llm_breaker_and_kube_breaker_use_a_real_lock_by_default(self):
        """Not mocked this time -- confirms the shared, process-global
        `llm_breaker`/`kube_breaker` instances themselves (not just a
        throwaway `CircuitBreaker`) are constructed with a real lock
        object, so the mutual-exclusion proven above actually applies to
        the two breakers every real caller uses."""
        from agentit.portal.helpers import kube_breaker
        for breaker in (llm_breaker, kube_breaker):
            assert hasattr(breaker._lock, "acquire") and hasattr(breaker._lock, "release")
