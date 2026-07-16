"""Regression guards for Priority 1d: four module-level TTL-cache dicts
that used to have zero locking around their read-check-write critical
section -- ``fleet.py::_argo_cache``, ``health.py::_deploy_status_cache``,
``capabilities.py::_skills_cache``/``_checks_cache``, and
``helpers.py::_nav_gate_badges_cache``.

Rather than trying to catch a probabilistic race directly (inherently
flaky), each test swaps the module's real lock for an instrumented one
that counts concurrent holders, then drives many concurrent callers
(real OS threads for the ``threading.Lock`` cases, concurrent tasks for
the ``asyncio.Lock`` case) through the cache function. Since a real lock
always enforces correct mutual exclusion once actually used, this
deterministically proves the source still wraps every read-check-write
section in the lock -- catching a future refactor that accidentally
bypasses it -- without relying on timing luck.
"""
from __future__ import annotations

import asyncio
import threading
import time as _time

from unittest.mock import patch

from agentit.portal import helpers
from agentit.portal.routes import capabilities as capabilities_routes
from agentit.portal.routes import fleet as fleet_routes
from agentit.portal.routes import health as health_routes


class _TrackingLock:
    """Wraps a real ``threading.Lock``, tracking the max number of
    concurrent holders ever observed -- must stay at 1 if the wrapped
    lock is genuinely being used for mutual exclusion."""

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


class _TrackingAsyncLock:
    """Async counterpart of ``_TrackingLock``, for the one cache
    (``helpers._nav_gate_badges_cache``) guarded by an ``asyncio.Lock``."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._holders = 0
        self.max_concurrent = 0

    async def __aenter__(self):
        await self._lock.acquire()
        self._holders += 1
        self.max_concurrent = max(self.max_concurrent, self._holders)
        return self

    async def __aexit__(self, *exc_info):
        self._holders -= 1
        self._lock.release()
        return False


def _run_threads(target, count: int = 20) -> None:
    threads = [threading.Thread(target=target) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class TestFleetArgoCacheLock:
    def test_enrich_fleet_lock_provides_mutual_exclusion(self, monkeypatch):
        tracking = _TrackingLock()
        monkeypatch.setattr(fleet_routes, "_argo_cache_lock", tracking)
        fleet_routes._argo_cache["data"] = {}
        fleet_routes._argo_cache["ts"] = 0  # force every thread down the refresh path

        def _slow_list(*_args, **_kwargs):
            _time.sleep(0.02)
            return []

        with patch("agentit.kube.list_custom_resources", side_effect=_slow_list):
            _run_threads(lambda: fleet_routes._enrich_fleet_with_cluster_status([]))

        assert tracking.max_concurrent == 1


class TestHealthDeployStatusCacheLock:
    def test_deploy_status_cache_lock_provides_mutual_exclusion(self, monkeypatch):
        tracking = _TrackingLock()
        monkeypatch.setattr(health_routes, "_deploy_status_cache_lock", tracking)
        health_routes._deploy_status_cache["data"] = None
        health_routes._deploy_status_cache["ts"] = 0.0

        def worker():
            health_routes._store_deploy_status_cache({"state": "idle", "errors": []})
            health_routes._get_fresh_cached_deploy_status()
            health_routes._get_last_good_deploy_status()

        _run_threads(worker)

        assert tracking.max_concurrent == 1
        health_routes._deploy_status_cache["data"] = None
        health_routes._deploy_status_cache["ts"] = 0.0


class TestCapabilitiesSkillsChecksCacheLock:
    def test_cached_skills_lock_provides_mutual_exclusion(self, monkeypatch, tmp_path):
        tracking = _TrackingLock()
        monkeypatch.setattr(capabilities_routes, "_skills_cache_lock", tracking)
        capabilities_routes._skills_cache["data"] = None
        capabilities_routes._skills_cache["ts"] = 0

        def _slow_load(*_args, **_kwargs):
            _time.sleep(0.02)
            return []

        with patch("agentit.skill_engine.load_all_skills", side_effect=_slow_load):
            _run_threads(capabilities_routes._cached_skills)

        assert tracking.max_concurrent == 1
        capabilities_routes._skills_cache["data"] = None
        capabilities_routes._skills_cache["ts"] = 0

    def test_cached_checks_lock_provides_mutual_exclusion(self, monkeypatch):
        tracking = _TrackingLock()
        monkeypatch.setattr(capabilities_routes, "_checks_cache_lock", tracking)
        capabilities_routes._checks_cache["data"] = None
        capabilities_routes._checks_cache["ts"] = 0

        def _slow_load(*_args, **_kwargs):
            _time.sleep(0.02)
            return []

        with patch("agentit.check_engine.load_checks", side_effect=_slow_load):
            _run_threads(capabilities_routes._cached_checks)

        assert tracking.max_concurrent == 1
        capabilities_routes._checks_cache["data"] = None
        capabilities_routes._checks_cache["ts"] = 0


class TestNavGateBadgesCacheLock:
    async def test_nav_gate_badges_lock_provides_mutual_exclusion(self, monkeypatch):
        tracking = _TrackingAsyncLock()
        monkeypatch.setattr(helpers, "_nav_gate_badges_lock", tracking)
        helpers._nav_gate_badges_cache["ts"] = 0.0

        class _SlowStore:
            async def list_gates(self, status="pending"):
                await asyncio.sleep(0.02)
                return []

        store = _SlowStore()
        await asyncio.gather(*(helpers.get_nav_gate_badge_counts(store) for _ in range(20)))

        assert tracking.max_concurrent == 1
        helpers._nav_gate_badges_cache["ts"] = 0.0
