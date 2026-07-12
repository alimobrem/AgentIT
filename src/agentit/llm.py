from __future__ import annotations

import json
import logging
import os

import anthropic

from agentit.portal.helpers import llm_breaker

logger = logging.getLogger(__name__)

_CLASSIFY_SYSTEM = (
    "You are a security analyst reviewing source code for hardcoded secrets. "
    'Respond ONLY with valid JSON: {"is_secret": bool, "confidence": float, "reason": str}. '
    "confidence is 0.0-1.0."
)

_CLASSIFY_USER = (
    "File: {file_path}\n"
    "Matched line:\n{matched_line}\n\n"
    "Surrounding context:\n{context}\n\n"
    "Is this a real hardcoded secret, or a false positive "
    "(e.g. variable reference, template placeholder, env var lookup, test fixture)?"
)

_SUMMARIZE_SYSTEM = (
    "You are a software architect. Given stack info and a file listing, "
    "produce a 2-3 sentence architecture summary. Be concise."
)

_ACTION_CLASSIFY_SYSTEM = (
    "You are a Kubernetes security reviewer. Classify whether the following "
    "action is DESTRUCTIVE (could cause downtime, data loss, security regression, "
    "or break a running workload). Be conservative — if uncertain, classify as destructive.\n\n"
    "Destructive examples: deleting resources, scaling to zero, removing NetworkPolicies, "
    "changing RBAC to grant cluster-admin, modifying secrets, removing health probes.\n\n"
    "Safe examples: adding new resources (NetworkPolicy, ServiceMonitor, ConfigMap), "
    "adding labels, creating RBAC with minimal permissions, adding probes.\n\n"
    'Respond ONLY with valid JSON: {"is_destructive": bool, "confidence": float, "reason": str}. '
    "confidence is 0.0-1.0."
)


def _create_client() -> anthropic.Anthropic | anthropic.AnthropicVertex:
    project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    region = os.environ.get("CLOUD_ML_REGION", "")
    if project and region:
        return anthropic.AnthropicVertex(region=region, project_id=project)
    return anthropic.Anthropic()


class LLMClient:
    """Claude client via Anthropic SDK. Supports Vertex AI and direct API.

    Backend selection (same as pulse-agent):
    - ANTHROPIC_VERTEX_PROJECT_ID + CLOUD_ML_REGION → Vertex AI
    - ANTHROPIC_API_KEY → direct Anthropic API
    """

    DEFAULT_MODEL = os.environ.get("AGENTIT_LLM_MODEL", "claude-sonnet-4-6")

    def __init__(self, model: str | None = None, **_kwargs) -> None:
        model = model or self.DEFAULT_MODEL
        self.model = model
        self._client = _create_client()

    def classify_secret(
        self,
        file_path: str,
        matched_line: str,
        context_lines: list[str],
    ) -> dict | None:
        user_msg = _CLASSIFY_USER.format(
            file_path=file_path,
            matched_line=matched_line,
            context="\n".join(context_lines),
        )
        raw = self._chat(_CLASSIFY_SYSTEM, user_msg)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
            return {
                "is_secret": bool(parsed["is_secret"]),
                "confidence": float(parsed["confidence"]),
                "reason": str(parsed["reason"]),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("LLM returned unparseable classify response: %s", raw)
            return None

    def summarize_architecture(
        self,
        stack_info: dict,
        file_list: list[str],
    ) -> str | None:
        user_msg = (
            f"Stack info:\n{json.dumps(stack_info)}\n\n"
            f"Files ({len(file_list)} total, first 80 shown):\n"
            + "\n".join(file_list[:80])
        )
        return self._chat(_SUMMARIZE_SYSTEM, user_msg)

    def classify_action(
        self,
        action_type: str,
        manifests: list[str],
        context: str,
    ) -> dict | None:
        """Classify a K8s action as destructive or safe.

        Returns {"is_destructive": bool, "confidence": float, "reason": str}
        or None on failure (caller must treat None as destructive — fail-closed).
        """
        manifest_text = "\n---\n".join(manifests[:5])
        user_msg = (
            f"Action: {action_type}\n"
            f"Context: {context}\n\n"
            f"Manifests ({len(manifests)} total, first 5 shown):\n{manifest_text}"
        )
        raw = self._chat(_ACTION_CLASSIFY_SYSTEM, user_msg)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
            return {
                "is_destructive": bool(parsed["is_destructive"]),
                "confidence": float(parsed["confidence"]),
                "reason": str(parsed["reason"]),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("LLM returned unparseable action classification: %s", raw)
            return None

    def review_fix(
        self,
        finding_description: str,
        finding_category: str,
        fix_content: str,
        app_summary: str,
    ) -> dict | None:
        """Review a generated fix before applying. First approver gate.

        Returns {"approved": bool, "confidence": float, "reason": str}
        or None on failure (caller must treat None as rejected — fail-closed).
        """
        system = (
            "You are a senior platform engineer reviewing auto-generated Kubernetes "
            "manifests. Your job is to decide if a proposed fix is CORRECT and SAFE "
            "for the specific application. Respond with JSON only:\n"
            '{"approved": true/false, "confidence": 0.0-1.0, "reason": "one sentence"}\n\n'
            "Approve if: the fix addresses the finding, uses correct API versions, "
            "doesn't break existing functionality, and is appropriate for the app's stack.\n"
            "Reject if: wrong API version, wrong ports/resources for this app, "
            "overly permissive security settings, or the fix doesn't match the finding."
        )
        user = (
            f"Application: {app_summary}\n\n"
            f"Finding: [{finding_category}] {finding_description}\n\n"
            f"Proposed fix:\n{fix_content[:3000]}\n\n"
            "Is this fix correct and safe to apply? JSON only."
        )
        raw = self._chat(system, user)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
            return {
                "approved": bool(parsed["approved"]),
                "confidence": float(parsed["confidence"]),
                "reason": str(parsed["reason"]),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("LLM returned unparseable fix review: %s", raw)
            return None

    def _chat(self, system: str, user: str) -> str | None:
        if llm_breaker.is_open:
            logger.warning("LLM circuit breaker open — skipping call")
            return None
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=512,
                system=system,
                messages=[{"role": "user", "content": user}],
                temperature=0.2,
                timeout=60,
            )
            llm_breaker.record_success()
            return resp.content[0].text.strip()
        except Exception as exc:
            llm_breaker.record_failure()
            logger.warning("LLM call failed: %s", exc)
            return None
