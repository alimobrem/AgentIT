from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_CLASSIFY_SYSTEM = (
    "You are a security analyst reviewing source code for hardcoded secrets. "
    "Respond ONLY with valid JSON: {\"is_secret\": bool, \"confidence\": float, \"reason\": str}. "
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

_REMEDIATION_SYSTEM = (
    "You are a DevSecOps engineer. Given a security finding, "
    "produce a specific, actionable remediation description in 1-2 sentences."
)


class LLMClient:
    """OpenAI-compatible LLM client. Works with vLLM, RHOAI, OpenAI, etc."""

    def __init__(
        self,
        endpoint: str,
        model: str = "default",
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get("AGENTIT_LLM_API_KEY")
        self._client = httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def classify_secret(
        self,
        file_path: str,
        matched_line: str,
        context_lines: list[str],
    ) -> dict | None:
        """Ask the LLM whether *matched_line* is a real secret or a false positive.

        Returns ``{"is_secret": bool, "confidence": float, "reason": str}``
        or ``None`` on any failure.
        """
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
        """Produce a short architecture summary from stack info and file listing."""
        user_msg = (
            f"Stack info:\n{json.dumps(stack_info, indent=2)}\n\n"
            f"Files ({len(file_list)} total, first 80 shown):\n"
            + "\n".join(file_list[:80])
        )
        return self._chat(_SUMMARIZE_SYSTEM, user_msg)

    def enhance_remediation(self, finding: dict) -> str | None:
        """Produce a specific remediation description for *finding*."""
        user_msg = json.dumps(finding, indent=2)
        return self._chat(_REMEDIATION_SYSTEM, user_msg)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _chat(self, system: str, user: str) -> str | None:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": 512,
        }

        try:
            resp = self._client.post(
                f"{self.endpoint}/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            logger.warning("LLM call failed: %s", exc)
            return None
