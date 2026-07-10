from __future__ import annotations

import json
import logging
import os

import anthropic

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

    def __init__(self, model: str = "claude-sonnet-4-5-20250514", **_kwargs) -> None:
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

    def _chat(self, system: str, user: str) -> str | None:
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=512,
                system=system,
                messages=[{"role": "user", "content": user}],
                temperature=0.2,
            )
            return resp.content[0].text.strip()
        except (anthropic.APIError, anthropic.APIConnectionError) as exc:
            logger.warning("LLM call failed: %s", exc)
            return None
