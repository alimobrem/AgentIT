"""Generic remediation dispatcher — routes findings to the skill engine.

Historically this instantiated a Python agent (hardening/compliance/cicd/
observability) and called a specific generator method on it. Those agents
were removed once skills gained full template-fallback parity for their
domains (see docs/agent-removal-readiness.md) -- this now resolves the
matching skill by name and renders it with ``SkillEngine.generate()``
(template mode, no LLM, to keep this path deterministic like the agent
methods it replaces). ``base_image`` is the one category that isn't
skill-shaped (it patches an *existing* file rather than generating a new
one) and is special-cased to ``remediation.base_image.patch_base_image``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agentit.remediation.registry import lookup
from agentit.skill_engine import SkillEngine

logger = logging.getLogger(__name__)


def _default_skills_dir() -> Path:
    """Return the ``skills/`` directory at the project root."""
    here = Path(__file__).resolve()
    candidate = here.parent.parent.parent.parent / "skills"
    return candidate if candidate.exists() else Path("skills")


class RemediationDispatcher:
    """``store`` is an ``AssessmentStore`` -- every store call below is ``await``ed.
    ``lookup()`` (registry.py) and the skill engine's template-mode
    ``generate()`` are pure/CPU-bound with no I/O, so they stay plain sync
    calls made directly from these async methods -- there's no blocking
    call here that would justify a ``to_thread`` wrap.
    """

    def __init__(self, store: object) -> None:
        self._store = store

    async def dispatch(
        self,
        assessment_id: str,
        finding_category: str,
        app_name: str = "",
    ) -> dict:
        """Route a finding to the matching skill and produce a fix.

        Returns {"files": list[dict], "agent": str, "method": str, "error": str | None}
        (``agent``/``method`` are kept for backward-compat with callers that
        attribute/log by these field names; ``agent`` now holds the skill
        domain and ``method`` the skill name.)
        """
        match = lookup(finding_category)
        if match is None:
            return {
                "files": [],
                "agent": "",
                "method": "",
                "error": f"No fix registered for category '{finding_category}'",
            }

        domain, skill_name = match

        report = await self._store.get(assessment_id)
        if report is None:
            return {
                "files": [],
                "agent": domain,
                "method": skill_name,
                "error": f"Assessment {assessment_id} not found",
            }

        if skill_name == "patch_base_image":
            return await self._dispatch_patch(assessment_id, domain, report, app_name)

        return self._dispatch_generate(domain, finding_category, report)

    def _dispatch_generate(
        self,
        domain: str,
        finding_category: str,
        report: object,
    ) -> dict:
        """Render the matching skill in template mode (no LLM -- deterministic,
        matching the fully-offline behavior the Python agent methods had).

        Resolves the skill via ``SkillEngine.skill_for_category()`` -- the
        same function ``generate_for_finding()`` (the CLI's ``self-fix``
        path) routes through, so this call site and that one can never
        disagree on which skill handles a given finding category.
        """
        engine = SkillEngine(_default_skills_dir(), platform=None)
        skill = engine.skill_for_category(finding_category)
        if skill is None:
            return {
                "files": [],
                "agent": domain,
                "method": "",
                "error": f"No skill found for category '{finding_category}' (domain '{domain}')",
            }

        try:
            result = engine.generate(skill, report, llm_client=None)
        except Exception as exc:
            logger.exception("Skill generator %s/%s failed", domain, skill.name)
            return {
                "files": [],
                "agent": domain,
                "method": skill.name,
                "error": str(exc),
            }

        files = [
            {
                "category": domain,
                "path": f.path,
                "content": f.content,
                "description": f.description,
            }
            for f in result
        ]

        return {
            "files": files,
            "agent": domain,
            "method": skill.name,
            "error": None,
        }

    async def _dispatch_patch(
        self,
        assessment_id: str,
        domain: str,
        report: object,
        app_name: str,
    ) -> dict:
        """Special case: patch an existing file rather than generating a new one."""
        from agentit.remediation.base_image import patch_base_image

        onboarding = await self._store.get_latest_onboarding(assessment_id)
        if onboarding is None:
            return {
                "files": [],
                "agent": domain,
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
                        "category": domain,
                        "path": f["path"],
                        "content": result,
                        "description": f"Patched base image to UBI ({lang})",
                    })

        return {
            "files": patched_files,
            "agent": domain,
            "method": "patch_base_image",
            "error": None if patched_files else "No patchable Containerfile found",
        }
