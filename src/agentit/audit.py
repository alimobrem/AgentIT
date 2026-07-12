"""Audit logging for privileged actions and data access.

Wraps the structured JSON logger to emit audit-specific events
with actor, action, resource, and outcome fields.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("agentit.audit")


def audit_log(
    *,
    actor: str,
    action: str,
    resource: str,
    outcome: str = "success",
    details: dict[str, Any] | None = None,
) -> None:
    """Emit a structured audit log entry."""
    extra: dict[str, Any] = {
        "audit": True,
        "actor": actor,
        "action": action,
        "resource": resource,
        "outcome": outcome,
    }
    if details:
        extra["details"] = details
    logger.info(
        "audit: %s %s %s -> %s",
        actor,
        action,
        resource,
        outcome,
        extra=extra,
    )
