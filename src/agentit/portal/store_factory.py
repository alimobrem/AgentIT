"""Storage-agnostic async factory for ``AssessmentStore`` -- Phase 3 enabling
infrastructure from docs/postgres-migration-plan.md.

**Why this file exists:** Phase 3 converts the ~15 non-portal callers of the
store (``cli.py``, the watchers, ``remediation_loop.py``/``dispatcher.py``)
to `async def`/`await`-shaped code, per the plan's §9. But `store.py` itself
is plain synchronous SQLite (that's out of scope for this pass -- the async
Postgres counterpart, ``store_pg.py``, already landed separately in Phase
1+2 and isn't wired into anything yet). Callers therefore need something to
``await`` today that (a) behaves identically to calling `store.py` directly
when no backend switch has been made, and (b) can later point at
``store_pg.AssessmentStore`` via one env var, without every caller changing
again.

``create_store()`` is that one seam. It must default to the SQLite backend
-- flipping ``AGENTIT_DB_BACKEND=postgres`` before every one of the ~15
callers *and* the portal (``app.py``/``routes/*.py``/``helpers.py``) has
been converted would reintroduce exactly the "some components on SQLite,
some on Postgres" silent-divergence risk the plan's §7 calls out as the
biggest risk of this whole migration. That coordinated flip is a deliberate,
separate, future step -- not something this module does on its own.
"""

from __future__ import annotations

import asyncio
import functools
import os
from typing import Any


class AsyncSQLiteStore:
    """Async-compatible facade over the synchronous SQLite ``AssessmentStore``.

    ``store.py``'s ``AssessmentStore`` is entirely synchronous (blocking)
    ``sqlite3`` I/O -- there's no async SQLite driver involved. This wrapper
    exposes every one of its public methods as an ``async def`` of the same
    name, each running the real synchronous call in a worker thread via
    ``asyncio.to_thread``. That means:

    - Callers can be written uniformly as ``await store.some_method(...)``
      regardless of which backend ``create_store()`` ends up selecting.
    - While the backend stays "sqlite" (today's default, and the only
      behavior this pass is allowed to ship), the actual work performed is
      byte-for-byte identical to calling ``store.py`` directly -- just with
      a thread hop in between so the calling coroutine doesn't block the
      event loop.

    ``.raw`` exposes the underlying synchronous ``AssessmentStore`` instance
    for the call sites that remain genuinely synchronous -- background
    assessment threads (``app.py``'s ``assess_submit``) and metrics/
    inventory-diff/cluster-health helpers that run via ``asyncio.to_thread``.
    **The 4 watcher classes and their `EventConsumer` dependency no longer
    need `.raw`** -- their constructors and tick bodies (``check_fleet``/
    ``check_once``/``detect_once``/``research_once``) now genuinely `await`
    the async store directly (see docs/postgres-migration-plan.md's
    "watcher CLI entry points" blocker-resolution section, and
    watchers/*.py), the same design already used by ``FleetOrchestrator``/
    ``AutoMode``/``RemediationDispatcher``/``RemediationLoop``. There is
    deliberately no Postgres equivalent of ``.raw``: handing a
    not-yet-converted synchronous consumer a Postgres-backed store would be
    exactly the kind of partial, uncoordinated cutover the plan's §7 warns
    against, so that combination fails loudly (``AttributeError``, since
    ``store_pg.AssessmentStore`` has no ``.raw``) instead of silently
    diverging.
    """

    def __init__(self, db_path: str | None = None) -> None:
        from agentit.portal.store import AssessmentStore

        self.raw = AssessmentStore(db_path)

    @classmethod
    def wrap(cls, raw_store: Any) -> "AsyncSQLiteStore":
        """Wrap an already-constructed synchronous ``AssessmentStore``.

        Used by tests that need the exact same underlying connection (e.g.
        an in-memory ``:memory:`` DB, which is a fresh, isolated database
        per new ``sqlite3.connect()`` call) to be reachable both directly
        (synchronous test assertions) and through this async facade (what
        the app itself calls) -- constructing a second, separate
        ``AsyncSQLiteStore(":memory:")`` would silently point at a
        different, empty database.
        """
        self = cls.__new__(cls)
        self.raw = raw_store
        return self

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.raw, name)
        if not callable(attr):
            return attr

        @functools.wraps(attr)
        async def _call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attr, *args, **kwargs)

        return _call


async def create_store(
    db_path: str | None = None,
    *,
    min_size: int = 5,
    max_size: int = 20,
) -> Any:
    """Return an async-compatible store; backend chosen by ``AGENTIT_DB_BACKEND``.

    - ``AGENTIT_DB_BACKEND`` unset or ``"sqlite"`` (default): returns an
      :class:`AsyncSQLiteStore` wrapping ``store.py``'s ``AssessmentStore``
      -- today's exact behavior, async-shaped.
    - ``AGENTIT_DB_BACKEND=postgres``: returns a ready-to-use
      ``store_pg.AssessmentStore`` (pool created, schema applied), sized by
      ``min_size``/``max_size`` -- watchers should pass their own smaller
      values per the plan's §5 pool-sizing table; the defaults here match
      that table's Portal row.

    Not a safe "flip it on gradually" mechanism (see plan §7) -- this is
    the single seam a future, deliberate, all-components-at-once cutover
    would change, not something to be set differently per component.
    """
    backend = os.environ.get("AGENTIT_DB_BACKEND", "sqlite").strip().lower()
    if backend == "postgres":
        from agentit.portal import store_pg

        return await store_pg.AssessmentStore.create(min_size=min_size, max_size=max_size)
    return AsyncSQLiteStore(db_path)
