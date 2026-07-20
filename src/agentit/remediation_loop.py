"""Shared post-delivery verification/rollback helpers.

Originally home to the ``RemediationLoop`` class (detect -> assess ->
onboard -> auto-apply -> verify), which is now deleted: it had zero live
callers -- its only entry point, ``POST /api/webhook/remediate``, was never
wired to any chart Sensor/CronJob/watcher (only tests and README referenced
it), and its ``_auto_apply()`` step called ``/api/webhook/auto-apply``,
which (since AutoMode's removal, see README "AutoMode removed entirely as a
concept") can only ever return ``{"action": "ready_for_delivery"}`` -- so
even a hand-triggered loop was guaranteed to report ``"outcome": "failed"``.
Both routes and the Argo Events Sensor that fired ``auto-apply`` on every
``onboarding-complete`` event have been deleted alongside the class
(2026-07-20 architecture-review cleanup).

``verify_slos()``/``rollback_action()`` below survive because they are
genuinely still used by the unified delivery router
(``portal/delivery.py::verify_and_close_delivery``) and the recommendations
route's manual rollback action (``portal/routes/recommendations.py``) --
see each function's own docstring.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def verify_slos(store: object | None, assessment_id: str, app_name: str) -> dict:
    """Watch SLOs for 60s after a delivery.

    Originally ``RemediationLoop._verify_slos``, extracted here so the
    unified delivery router (``portal/delivery.py``) can run the exact same
    verify tail for every delivery, not just the (now-removed) fully
    autonomous fleet-watcher-triggered path -- see
    docs/unified-apply-flow.md section (C), "one loop shape."
    """
    if store is None:
        return {"healthy": True, "reason": "No store -- skipping SLO check"}

    from agentit.slo_collector import collect_slo, is_breached

    VERIFY_WINDOW_SECONDS = 60  # 60s SLO watch after apply
    VERIFY_POLL_INTERVAL = 5  # poll every 5s
    VERIFY_MAX_POLLS = 12  # 12 * 5s = 60s

    for _ in range(VERIFY_MAX_POLLS):
        slos = await store.list_slos(assessment_id)
        if not slos:
            await asyncio.sleep(VERIFY_POLL_INTERVAL)
            continue

        breached = []
        for s in slos:
            # collect_slo does blocking kubernetes-client I/O -- narrowly
            # wrapped in to_thread at this call site.
            value = await asyncio.to_thread(collect_slo, s["metric_name"], app_name)
            if value is not None:
                status = "breached" if is_breached(s["metric_name"], value, s["target_value"]) else "met"
                await store.update_slo(s["id"], value, status)
                if status == "breached":
                    breached.append(s["metric_name"])

        if breached:
            return {
                "healthy": False,
                "reason": f"{len(breached)} SLO(s) breached: {', '.join(breached)}",
                "breached": breached,
            }
        await asyncio.sleep(VERIFY_POLL_INTERVAL)

    return {"healthy": True, "reason": f"All SLOs healthy after {VERIFY_WINDOW_SECONDS}s"}


async def rollback_action(app_name: str, namespace: str) -> dict:
    """Execute rollback via ``kube.rollout_undo``.

    Originally ``RemediationLoop._rollback``, extracted here so the unified
    delivery router's direct-apply verification tail
    (``portal/delivery.py::verify_and_close_delivery``) and the
    recommendations route's manual rollback action
    (``portal/routes/recommendations.py``) can both trigger the same
    rollback without duplicating the kube call.
    """
    from agentit import kube

    result = await asyncio.to_thread(kube.rollout_undo, app_name, namespace)
    if result["success"]:
        return {"outcome": "rolled_back", "details": result["message"]}
    return {"outcome": "rollback_failed", "error": result["message"]}
