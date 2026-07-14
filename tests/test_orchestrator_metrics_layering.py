"""Regression guard for the orchestrator/portal metrics layering fix.

`agents/orchestrator.py` used to import `agent_runs_total`/
`agent_run_duration_seconds` directly from `portal/metrics.py` at module
level. Since `portal/metrics.py` also imports
`prometheus_fastapi_instrumentator` (and effectively pulls in the whole
FastAPI-based portal graph), that meant anyone using `FleetOrchestrator`
outside the portal -- the CLI's `onboard`/`orchestrate` commands, a
first-class, portal-independent use case -- transitively paid that import
cost just to run an agent. The two metric objects now live in the
top-level `agentit.metrics` module and are imported lazily (inside the
methods that actually record them), so merely importing
`agentit.agents.orchestrator` pulls in neither `prometheus_client` nor
`agentit.portal.metrics`.

This is checked in a subprocess (not via `sys.modules` in-process) because
other tests in the same pytest session may have already imported the
portal for unrelated reasons, which would make an in-process check of
`sys.modules` pass or fail depending on test order rather than on this
module's own import graph.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_import_check(*module_names: str) -> dict[str, bool]:
    checks = ", ".join(f"'{m}' in sys.modules" for m in module_names)
    code = (
        "import agentit.agents.orchestrator\n"
        "import sys, json\n"
        f"print(json.dumps([{checks}]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=30,
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    flags = json.loads(result.stdout.strip())
    return dict(zip(module_names, flags))


class TestOrchestratorDoesNotEagerlyImportPortalMetrics:
    def test_importing_orchestrator_does_not_pull_in_prometheus_client(self) -> None:
        flags = _run_import_check(
            "prometheus_client",
            "prometheus_fastapi_instrumentator",
            "agentit.portal.metrics",
            "fastapi",
        )
        assert flags["prometheus_client"] is False, (
            "Importing agentit.agents.orchestrator alone must not pull in "
            "prometheus_client -- metrics recording should be a lazy, "
            "function-scoped import (see agentit.metrics)."
        )
        assert flags["prometheus_fastapi_instrumentator"] is False
        assert flags["agentit.portal.metrics"] is False
        assert flags["fastapi"] is False

    def test_agent_run_metrics_are_still_the_same_objects_portal_uses(self) -> None:
        """The decoupling must not create two separate Prometheus metric
        registrations for the same metric name -- portal/metrics.py
        re-exports the canonical objects from agentit.metrics rather than
        redefining them."""
        from agentit.metrics import agent_run_duration_seconds as core_hist
        from agentit.metrics import agent_runs_total as core_counter
        from agentit.portal.metrics import agent_run_duration_seconds as portal_hist
        from agentit.portal.metrics import agent_runs_total as portal_counter

        assert core_counter is portal_counter
        assert core_hist is portal_hist
