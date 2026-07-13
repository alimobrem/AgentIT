"""Guards against `store_pg.py` silently drifting out of parity with
`store.py` again (see docs/postgres-migration-plan.md's "Bringing
store_pg.py back into parity" note).

This only compares *public method names* via introspection — it does not
require a live Postgres connection, so it runs in the default (non-Postgres)
test suite unconditionally. Behavioral parity for the shared methods is
covered separately by `tests/test_store_pg.py` (gated behind
`--run-postgres-tests`, since it needs a real Postgres instance).
"""

from __future__ import annotations

import inspect

from agentit.portal import store, store_pg

# Methods that exist on exactly one backend, on purpose — documented here
# instead of silently excluded, so any *other* drift still fails loudly.
#
# - `create`/`close`: store_pg.AssessmentStore's pool-based lifecycle has no
#   sync equivalent. store.py opens its sqlite3 connection synchronously in
#   `__init__` and has no public `create()`/`close()` counterpart — a plain
#   `AssessmentStore(db_path=...)` constructor call is itself the "create",
#   and the connection is simply dropped rather than explicitly closed.
ONLY_IN_STORE_PG = {"create", "close"}
ONLY_IN_STORE = set()


def _public_methods(cls: type) -> set[str]:
    # `inspect.isroutine` (not `isfunction`) so classmethods like
    # `store_pg.AssessmentStore.create` — which come back as bound methods,
    # not plain functions, via `getmembers(cls, ...)` — are included too.
    return {
        name
        for name, member in inspect.getmembers(cls, predicate=inspect.isroutine)
        if not name.startswith("_")
    }


def test_public_method_names_match_between_backends():
    sqlite_methods = _public_methods(store.AssessmentStore)
    pg_methods = _public_methods(store_pg.AssessmentStore)

    only_in_sqlite = sqlite_methods - pg_methods
    only_in_pg = pg_methods - sqlite_methods

    assert only_in_sqlite == ONLY_IN_STORE, (
        f"store.AssessmentStore has public methods missing from "
        f"store_pg.AssessmentStore: {sorted(only_in_sqlite - ONLY_IN_STORE)}. "
        f"Port them to store_pg.py, or add them to ONLY_IN_STORE in this test "
        f"if the omission is deliberate."
    )
    assert only_in_pg == ONLY_IN_STORE_PG, (
        f"store_pg.AssessmentStore has public methods not present on "
        f"store.AssessmentStore: {sorted(only_in_pg - ONLY_IN_STORE_PG)}. "
        f"Either add a store.py counterpart, or add them to ONLY_IN_STORE_PG "
        f"in this test if the difference is deliberate."
    )


def test_shared_methods_are_all_coroutines_on_pg_backend():
    """Every method store_pg.AssessmentStore shares with store.py must be
    `async def` — a plain `def` slipping in would silently break every
    caller that `await`s it (per the mechanical async-mirroring convention
    documented in store_pg.py's module docstring)."""
    sqlite_methods = _public_methods(store.AssessmentStore)
    pg_methods = _public_methods(store_pg.AssessmentStore)
    shared = sqlite_methods & pg_methods

    non_async = [
        name
        for name in shared
        if not inspect.iscoroutinefunction(getattr(store_pg.AssessmentStore, name))
    ]
    assert non_async == [], (
        f"These store_pg.AssessmentStore methods are not `async def`, "
        f"breaking the mirroring convention: {sorted(non_async)}"
    )
