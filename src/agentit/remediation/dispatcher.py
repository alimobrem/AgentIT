"""Generic remediation dispatcher — routes findings to the right agent generator."""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from agentit.agents.base import GeneratedFile
from agentit.portal.store import AssessmentStore
from agentit.remediation.registry import FIX_REGISTRY, get_agent_class, lookup

logger = logging.getLogger(__name__)


class RemediationDispatcher:
    def __init__(self, store: AssessmentStore) -> None:
        self._store = store

    def dispatch(
        self,
        assessment_id: str,
        finding_category: str,
        app_name: str = "",
    ) -> dict:
        """Route a finding to the right agent generator and produce a fix.

        Returns {"files": list[dict], "agent": str, "method": str, "error": str | None}
        """
        match = lookup(finding_category)
        if match is None:
            return {
                "files": [],
                "agent": "",
                "method": "",
                "error": f"No fix registered for category '{finding_category}'",
            }

        agent_key, method_name = match

        report = self._store.get(assessment_id)
        if report is None:
            return {
                "files": [],
                "agent": agent_key,
                "method": method_name,
                "error": f"Assessment {assessment_id} not found",
            }

        if method_name == "patch_base_image":
            return self._dispatch_patch(assessment_id, agent_key, report, app_name)

        return self._dispatch_generate(assessment_id, agent_key, method_name, report)

    def _dispatch_generate(
        self,
        assessment_id: str,
        agent_key: str,
        method_name: str,
        report: object,
    ) -> dict:
        """Run a standard agent generator method."""
        agent_cls = get_agent_class(agent_key)

        with tempfile.TemporaryDirectory(prefix="agentit-fix-") as tmpdir:
            agent = agent_cls(report, Path(tmpdir))
            method = getattr(agent, method_name, None)
            if method is None:
                return {
                    "files": [],
                    "agent": agent_key,
                    "method": method_name,
                    "error": f"Method {method_name} not found on {agent_key} agent",
                }

            try:
                result = method()
            except Exception as exc:
                logger.exception("Fix generator %s.%s failed", agent_key, method_name)
                return {
                    "files": [],
                    "agent": agent_key,
                    "method": method_name,
                    "error": str(exc),
                }

            if isinstance(result, GeneratedFile):
                result = [result]
            elif result is None:
                result = []

            files = [
                {
                    "category": agent_key,
                    "path": f.path,
                    "content": f.content,
                    "description": f.description,
                }
                for f in result
            ]

        return {
            "files": files,
            "agent": agent_key,
            "method": method_name,
            "error": None,
        }

    def _dispatch_patch(
        self,
        assessment_id: str,
        agent_key: str,
        report: object,
        app_name: str,
    ) -> dict:
        """Special case: patch an existing file rather than generating a new one."""
        from agentit.agents.hardening import patch_base_image

        onboarding = self._store.get_latest_onboarding(assessment_id)
        if onboarding is None:
            return {
                "files": [],
                "agent": agent_key,
                "method": "patch_base_image",
                "error": "No onboarding results to patch",
            }

        files_raw = onboarding.get("files_json", "[]")
        files = json.loads(files_raw) if isinstance(files_raw, str) else files_raw

        lang = "unknown"
        if hasattr(report, "stack") and report.stack.languages:
            lang = report.stack.languages[0].name.lower()

        patched_files = []
        for f in files:
            if f["path"].lower() in ("containerfile", "dockerfile"):
                result = patch_base_image(f["content"], lang)
                if result:
                    patched_files.append({
                        "category": "hardening",
                        "path": f["path"],
                        "content": result,
                        "description": f"Patched base image to UBI ({lang})",
                    })

        return {
            "files": patched_files,
            "agent": agent_key,
            "method": "patch_base_image",
            "error": None if patched_files else "No patchable Containerfile found",
        }
