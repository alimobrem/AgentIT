"""Regression guards for Priority 1d: four module-level TTL-cache dicts
that used to have zero locking around their read-check-write critical
section -- ``fleet.py::_argo_cache``, ``deploy_status.py::
_deploy_status_cache`` (extracted from ``health.py``),
``capabilities.py::_skills_cache``/``_checks_cache``, and
``helpers.py::_nav_pending_actions_cache``.

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

from agentit.portal import deploy_status, helpers
from agentit.portal.routes import capabilities as capabilities_routes
from agentit.portal.routes import fleet as fleet_routes


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
    (``helpers._nav_pending_actions_cache``) guarded by an ``asyncio.Lock``."""

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


class TestFleetManagedDeployStatusLookup:
    """ApplicationSet apps are named managed-{app}; deploy_status must
    resolve that key (then fall back to the literal name) or Synced/Healthy
    apps like pinky / managed-pinky falsely show "not deployed"."""

    def test_enrich_resolves_managed_application_name(self):
        fleet_routes._argo_cache["data"] = {
            "managed-pinky": {
                "sync": "Synced",
                "health": "Healthy",
                "cluster": "https://cluster",
                "namespace": "pinky",
                "repo_url": "https://github.com/org/pinky.git",
            },
        }
        fleet_routes._argo_cache["ts"] = _time.monotonic()
        fleet = [{"id": "a1", "repo_name": "pinky", "repo_url": "https://github.com/org/pinky"}]

        with patch("agentit.kube.list_custom_resources", return_value=[]):
            out = fleet_routes._enrich_fleet_with_cluster_status(fleet)

        assert out[0]["deploy_status"] == "synced"
        assert out[0]["deploy_health"] == "healthy"
        assert out[0]["gitops_registered"] is True

    def test_enrich_falls_back_to_literal_application_name(self):
        fleet_routes._argo_cache["data"] = {
            "agentit": {
                "sync": "Synced",
                "health": "Healthy",
                "cluster": "https://cluster",
                "namespace": "agentit",
                "repo_url": "https://github.com/alimobrem/AgentIT.git",
            },
        }
        fleet_routes._argo_cache["ts"] = _time.monotonic()
        fleet = [{
            "id": "a2",
            "repo_name": "AgentIT",
            "repo_url": "https://github.com/alimobrem/AgentIT",
        }]

        with patch("agentit.kube.list_custom_resources", return_value=[]):
            out = fleet_routes._enrich_fleet_with_cluster_status(fleet)

        assert out[0]["deploy_status"] == "synced"
        assert out[0]["deploy_health"] == "healthy"


class TestHealthDeployStatusCacheLock:
    def test_deploy_status_cache_lock_provides_mutual_exclusion(self, monkeypatch):
        tracking = _TrackingLock()
        monkeypatch.setattr(deploy_status, "_deploy_status_cache_lock", tracking)
        deploy_status._deploy_status_cache["data"] = None
        deploy_status._deploy_status_cache["ts"] = 0.0

        def worker():
            deploy_status._store_deploy_status_cache({"state": "idle", "errors": []})
            deploy_status._get_fresh_cached_deploy_status()
            deploy_status._get_last_good_deploy_status()

        _run_threads(worker)

        assert tracking.max_concurrent == 1
        deploy_status._deploy_status_cache["data"] = None
        deploy_status._deploy_status_cache["ts"] = 0.0


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

    def test_bust_skills_cache_actually_blocks_on_the_read_paths_lock(self):
        """Regression guard: `webhook_skill_draft`/the manual "Research
        CVEs" route/`activate_skill_route` used to write
        `_skills_cache["data"] = None` directly, bypassing
        `_skills_cache_lock` entirely -- a bust racing a concurrent
        `_cached_skills()` refresh already past its own `is None`/TTL check
        could have its `None` silently overwritten by that refresh's
        stale-relative-to-the-bust result.

        A concurrency-counting lock (like `_TrackingLock` above) can't
        actually catch this: `bust_skills_cache()`'s own critical section is
        a single dict write, fast enough that even an unlocked version
        would rarely overlap another lock-holder in a `_holders` counter.
        Instead, this proves causation directly: hold
        `_skills_cache_lock` open on a background thread, then call
        `bust_skills_cache()` on the main thread and time it -- if it goes
        through the (currently-held) lock, it must block for roughly the
        hold duration; if it bypasses the lock (the pre-fix bug), it
        returns almost immediately regardless of who else holds the lock.
        """
        capabilities_routes._skills_cache["data"] = "stale"
        hold_seconds = 0.5
        released = threading.Event()

        def holder():
            with capabilities_routes._skills_cache_lock:
                _time.sleep(hold_seconds)
            released.set()

        t = threading.Thread(target=holder)
        t.start()
        _time.sleep(0.1)  # let the holder thread actually acquire first

        start = _time.monotonic()
        capabilities_routes.bust_skills_cache()
        elapsed = _time.monotonic() - start
        t.join()

        # Generous margin (70% of the hold duration): the point is
        # distinguishing "blocked for roughly the hold time" from the
        # pre-fix bug's "returned in microseconds regardless of the lock",
        # not asserting an exact wait time.
        assert elapsed >= hold_seconds * 0.7, (
            f"bust_skills_cache() returned in {elapsed:.3f}s while the lock was held for "
            f"{hold_seconds}s -- it did not actually block on _skills_cache_lock"
        )
        assert released.is_set()
        capabilities_routes._skills_cache["data"] = None
        capabilities_routes._skills_cache["ts"] = 0


class TestNavPendingActionsCacheLock:
    async def test_nav_pending_actions_lock_provides_mutual_exclusion(self, monkeypatch):
        tracking = _TrackingAsyncLock()
        monkeypatch.setattr(helpers, "_nav_pending_actions_lock", tracking)
        helpers._nav_pending_actions_cache["ts"] = 0.0

        class _SlowStore:
            # get_nav_pending_action_counts() now goes through pr_tracking.py's
            # count_fleet_prs_waiting_for_approval()/collect_fleet_pr_records()
            # (2026-07-19, PR-status-derived Ledger fix) -- get_fleet_data()
            # is its first, unguarded call, so slowing it down is enough to
            # exercise the same "many concurrent callers" scenario this test
            # existed for; an empty fleet short-circuits everything after it.
            async def get_fleet_data(self):
                await asyncio.sleep(0.02)
                return []

        store = _SlowStore()
        await asyncio.gather(*(helpers.get_nav_pending_action_counts(store) for _ in range(20)))

        assert tracking.max_concurrent == 1
        helpers._nav_pending_actions_cache["ts"] = 0.0
