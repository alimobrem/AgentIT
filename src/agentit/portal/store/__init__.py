"""Async, Postgres-backed ``AssessmentStore`` -- the only supported store.

Postgres is not a backend option among several; it is the store. SQLite
support (the original prototype backend) and the backend-selection
machinery that briefly coexisted with it (``store_pg.py``, ``store_factory.py``,
``AGENTIT_DB_BACKEND``) have been removed -- see ``docs/postgres-migration-plan.md``
for the full history of how this cutover happened and why (that doc is now
marked historically-complete/superseded, not a live plan).

This class is raw-SQL (no ORM), one hand-written ``SELECT``/``INSERT``/
``UPDATE`` per method, using ``asyncpg`` for the async Postgres driver.
Construct via the ``create()`` classmethod (pool creation is inherently
async, so a plain ``__init__`` can't do it) -- ``dsn`` defaults to the
``AGENTIT_DB_DSN`` environment variable if not passed explicitly.

**Package layout (2026-07-20 domain split).** This used to be one 2685-line
``store.py`` file with ~109 methods on a single class -- flagged by an
external reuse/refactor review as the codebase's "god store" and hardest
test seam. It is now a package: this ``__init__.py`` defines the
``AssessmentStore`` facade class itself (plus ``create_store()`` and every
module-level symbol external callers already import, e.g.
``normalize_repo_url``/``SCHEMA_SQL``/``_ALL_TABLES``/``ASSESSMENT_CADENCES``,
all re-exported here from ``_shared.py``), while every one of its ~109
public methods now *lives* in a domain-specific mixin module alongside it
(``assessments.py``, ``events.py``, ``fleet.py``, ``deliveries.py``,
``jobs.py``, ``schedules.py``, ``agents.py``, ``skills.py``,
``feedback.py``, ``checks.py``, ``slos.py``, ``admin.py``) and
``AssessmentStore`` inherits from all of them.

**Why mixins, not composition.** Every one of the original class's ~109
methods depends on exactly one piece of shared state -- ``self._pool``,
the ``asyncpg.Pool`` set once in ``__init__`` below -- and nothing else (no
locks, no other instance attributes; verified by grepping the original file
for every ``self.<x> =`` assignment before starting this split). Several
methods also call sibling methods that now live in a *different* domain
module (``save()`` in ``assessments.py`` calls ``self.log_event(...)``,
which lives in ``events.py``; ``get_fleet_data()`` in ``fleet.py`` calls
``self.get_trend()``, which lives in ``assessments.py``). Multiple
inheritance (mixins) preserves this exactly: every domain mixin's methods
land in the same flat instance namespace, so ``self.log_event(...)`` from
inside ``assessments.py`` resolves through Python's normal attribute lookup
without either module importing the other -- no facade-forwarding
boilerplate, no ``__getattr__`` indirection, and (this is the part that
actually matters for the "zero behavior change" constraint) the resulting
public method names/signatures on ``AssessmentStore`` are *byte-for-byte*
identical to before the split, because they're the exact same function
objects, just defined in a different file. A composed-domain-objects
design (``self.jobs = JobsDomain(pool)``, called as ``store.jobs.
create_remediation_job(...)``) was considered and rejected specifically
because it would have required renaming every one of the ~2700+ existing
call sites across the whole codebase (``store.create_remediation_job(...)``
-> ``store.jobs.create_remediation_job(...)``) -- exactly the "import
churn"/breaking-API-redesign risk this refactor's own brief says to avoid.

**Where each domain's methods live, and why that boundary:** see each
mixin module's own docstring for the specific tables/methods it owns and
the reasoning behind that grouping (most map 1:1 onto the table(s) they
touch; a couple -- ``jobs.py``/``assessments.py`` -- group two of the
original file's section-comment headers together because they share one
underlying table or one tightly-coupled entity).

**``SCHEMA_SQL`` stayed centralized in ``_shared.py``**, not split
alongside each owning domain -- see that module's docstring for why (the
DDL interleaves several tables' triggers/backfill logic in ways that don't
cleanly decompose per-domain, and centralizing it is the lower-risk,
easier-to-audit-in-one-read option for something that runs once at every
``create()``).
"""

from __future__ import annotations

import logging
import os

import asyncpg

from ._shared import ASSESSMENT_CADENCE_INTERVALS, ASSESSMENT_CADENCES, SCHEMA_SQL, normalize_repo_url
from ._shared import _ALL_TABLES  # noqa: F401 -- re-exported; `tests/test_store.py` imports it directly
from .admin import AdminMixin
from .agents import AgentsMixin
from .assessments import AssessmentsMixin
from .checks import ChecksMixin
from .deliveries import DeliveriesMixin
from .events import EventsMixin
from .feedback import FeedbackMixin
from .fleet import FleetMixin
from .jobs import JobsMixin
from .schedules import SchedulesMixin
from .skills import SkillsMixin
from .slos import SLOsMixin

logger = logging.getLogger(__name__)

__all__ = [
    "AssessmentStore",
    "create_store",
    "normalize_repo_url",
    "SCHEMA_SQL",
    "ASSESSMENT_CADENCES",
    "ASSESSMENT_CADENCE_INTERVALS",
]


class AssessmentStore(
    AssessmentsMixin, EventsMixin, FleetMixin, DeliveriesMixin, AgentsMixin, SLOsMixin, JobsMixin,
    SchedulesMixin, FeedbackMixin, ChecksMixin, SkillsMixin, AdminMixin,
):
    """The one and only ``AssessmentStore``. Postgres-backed, fully async.

    Construct via the ``create()`` classmethod (pool creation is inherently
    async, so a plain ``__init__`` can't do it).

    Inherits its ~109 public domain methods from the mixin classes imported
    above (one per ``store/<domain>.py`` module) -- see this module's own
    docstring for the full rationale. This class itself only owns the
    lifecycle methods (``__init__``/``create``/``close``) and the shared
    ``self._pool`` every mixin method reads.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create(
        cls,
        dsn: str | None = None,
        *,
        min_size: int = 5,
        max_size: int = 20,
        command_timeout: float = 30.0,
        connect_timeout: float = 15.0,
    ) -> "AssessmentStore":
        """``command_timeout``/``connect_timeout`` are exposed as
        parameters (not just hardcoded) so a fault-injection test can
        construct a store with an aggressively short bound and prove the
        timeout actually fires against a real, deliberately-wedged query
        (``pg_sleep()``) in well under a second, instead of the real
        30s/15s defaults every production caller gets.
        """
        if dsn is None:
            dsn = os.environ.get("AGENTIT_DB_DSN")
        if not dsn:
            raise ValueError(
                "No Postgres DSN provided and AGENTIT_DB_DSN is not set."
            )
        # `command_timeout`/`timeout` are unset (unbounded) by default in
        # asyncpg -- every query issued through this pool (`fetch`/
        # `fetchrow`/`fetchval`/`execute`) would otherwise wait *forever*
        # against a wedged (not fully down, just stuck -- a lock wait, a
        # runaway query on someone else's connection, a half-open TCP
        # session) Postgres, with no timeout error ever raised. Worse,
        # since every FastAPI route holds its connection for that whole
        # wait, enough concurrently-stuck requests exhaust the pool
        # (`max_size=20`) and every *other* route needing the store hangs
        # too -- a single wedged query cascading into total portal
        # unavailability with zero user-facing signal. `command_timeout`
        # (default 30s) bounds every query to a generous ceiling well above
        # this app's real query shapes (raw hand-written SQL against a
        # modest-sized table set, no multi-second aggregations) while still
        # turning "wedged" into a clear, fast `asyncpg.QueryCanceledError`
        # instead of an indefinite hang. `connect_timeout` (default 15s,
        # vs. asyncpg's own 60s default) bounds each new connection attempt
        # to roughly the same ceiling this app already uses for its other
        # external dependencies (`kube.py`'s `_request_timeout`,
        # `github_pr.py`'s `requests.*` calls) rather than a 60s wait per
        # connection before a caller even learns Postgres is unreachable.
        pool = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size,
            command_timeout=command_timeout, timeout=connect_timeout,
        )
        await pool.execute(SCHEMA_SQL)
        store = cls(pool)
        # Heal any repo_url duplicates inherited from before the
        # normalize_repo_url_before_write trigger existed (or from any
        # other gap) right away, not just on the next 5-min maintenance
        # tick -- see dedupe_repo_urls()'s docstring.
        try:
            await store.dedupe_repo_urls()
        except Exception:
            logger.warning("Startup repo_url dedupe failed (non-fatal)", exc_info=True)
        return store

    async def close(self) -> None:
        await self._pool.close()


async def create_store(
    dsn: str | None = None,
    *,
    min_size: int = 5,
    max_size: int = 20,
) -> AssessmentStore:
    """Thin, backend-agnostic-in-name-only convenience wrapper around
    ``AssessmentStore.create()``.

    Every caller in this codebase used to go through
    ``store_factory.create_store()`` while a SQLite/Postgres backend
    selection existed (``AGENTIT_DB_BACKEND``). That selection is gone --
    Postgres is the only store -- but this function is kept as the one,
    consistent construction seam every caller (CLI commands, watchers,
    the portal) already uses, so those call sites don't all need an
    additional, purely-cosmetic rename on top of everything else this
    cutover already touches.
    """
    return await AssessmentStore.create(dsn, min_size=min_size, max_size=max_size)
