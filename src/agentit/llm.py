from __future__ import annotations

import json
import logging
import os
import re

import anthropic

from agentit.interfaces.breakers import llm_breaker

logger = logging.getLogger(__name__)

# Some models wrap an otherwise-correct JSON response in a markdown code
# fence (```json ... ``` or a bare ``` ... ``` with no language tag) despite
# every system prompt in this file instructing "Respond ONLY with valid
# JSON". Matches the whole (already-.strip()ped) response against an
# optional-language-tag fence and captures the interior; text outside a
# recognized fence is left untouched. This only strips wrapper syntax -- it
# never validates or repairs the interior content, so anything still
# malformed after stripping fails json.loads() in each caller exactly as it
# did before this existed.
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*(.*?)\s*```$", re.DOTALL)


def _strip_code_fence(raw: str) -> str:
    """Strip a leading/trailing markdown code fence from an LLM response, if present.

    Also handles *truncated* fences (opening ``` with no closing ```) — a real
    live failure mode when the model wraps a long capability proposal in
    `````json`` and then hits the max_tokens ceiling mid-object. Without this,
    ``json.loads`` sees the opening fence and fails even though the interior
    might still be recoverable on a retry with a tighter prompt.
    """
    text = raw.strip()
    match = _CODE_FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            body = text[first_nl + 1 :]
            if body.rstrip().endswith("```"):
                body = body.rstrip()[:-3]
            return body.strip()
    return text

# Token budgets: small JSON classifiers use the default; open-ended lists /
# prose proposals pass a higher max_tokens. See Anthropic model limits.
_DEFAULT_MAX_TOKENS = 512
_EOL_MAX_TOKENS = 1024
_CAPABILITY_PROPOSAL_MAX_TOKENS = 2048
_CAPABILITY_FILES_MAX_TOKENS = 16384
_CAPABILITY_FILES_TIMEOUT_SECONDS = 120.0

# Two skill-lifecycle callers outside this module -- learning_agent.py's
# generate_skill_from_research() (drafts a complete skill Markdown file:
# frontmatter + Property/Key-decisions/Constraints/Verification sections)
# and skill_engine.py's SkillEngine._generate_with_llm() (renders a matched
# skill's full manifest, potentially several K8s resources across the
# skill's declared output kinds) -- call llm_client._chat(system, user) with
# no override, so they silently inherited the 512-token classifier default
# above. Confirmed live: this truncated a learning-agent-drafted skill's own
# body mid-sentence (missing its Constraints/Verification sections
# entirely, stop_reason=max_tokens) and, separately, truncated that same
# skill's generated manifest on every retry attempt until
# SkillEngine.generate() gave up and returned no files at all --
# `verify_skill()` then correctly, but confusingly, reported this as
# "skill matched the verification fixture but generated no output" and
# blocked activation. Sized like _CAPABILITY_PROPOSAL_MAX_TOKENS's prose
# budget plus real headroom for a multi-resource YAML manifest body.
_SKILL_GENERATION_MAX_TOKENS = 4096

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

_EOL_SYSTEM = (
    "You are a software supply-chain analyst reviewing a repository's detected stack "
    "and key files for end-of-life (EOL) or soon-to-be-EOL software: base container "
    "images, language runtimes, and framework versions. Only flag a component if you "
    "are confident about its real, published end-of-life date -- an empty result is "
    "correct and preferred over a guess. Never invent or estimate a specific EOL date "
    "you are not sure of; if you can't cite one you trust, omit that item entirely.\n\n"
    'Respond ONLY with valid JSON: {"risks": [{"component": str, "version": str, '
    '"status": "eol" or "approaching_eol", "eol_date": str, "confidence": float, '
    '"reason": str}]}. confidence is 0.0-1.0. Return {"risks": []} if nothing qualifies.'
)

_EOL_USER = (
    "Detected stack:\n{stack_info}\n\n"
    "Relevant file excerpts:\n{file_excerpts}\n\n"
    "Identify any base images, language runtimes, or framework versions above that are "
    "already end-of-life, or approaching end-of-life within the next 6 months."
)


_CAPABILITY_PROPOSAL_SYSTEM = (
    "You are a senior engineer on the AgentIT project. AgentIT already improves the "
    "skills it generates for OTHER applications; your job here is different: propose "
    "AT MOST ONE small, evidence-grounded improvement to AgentIT's OWN codebase. "
    "Rules, non-negotiable:\n"
    "1. Prefer a gap explicitly documented in AgentIT's own docs (quoted verbatim in "
    "the evidence you are given) over inventing one from general knowledge. If the "
    "evidence given to you is too thin to ground a real, specific proposal, set "
    "has_proposal to false -- an honest 'nothing to propose' is correct and preferred "
    "over a guess. Never invent evidence that isn't in the data you were given.\n"
    "2. Only propose changes that are small, focused, and reviewable -- this project's "
    "own convention: only modify what's directly related to the gap, never refactor, "
    "rename, or reorder unrelated code, keep changes as minimal as possible.\n"
    "3. Never propose anything touching chart/, argocd/, .github/workflows/, "
    "Dockerfile, or any path containing 'secret' or 'rbac' — scope suggested target "
    "files to src/agentit/**/*.py, skills/, checks/, or tests/ only (prefer those "
    "executable paths over docs/ so source/auto mode can open a real code PR).\n"
    "4. Always name at least one test file under tests/ in target_files, and describe "
    "what it would assert in test_plan -- a change with no test coverage plan is not a "
    "complete proposal.\n"
    "5. Prefer NEW small modules (e.g. src/agentit/<feature>.py + "
    "tests/test_<feature>.py, or a small skills/checks file) over rewriting a large "
    "existing module — full-file rewrites of big files fail the source generator. "
    "Never list an existing file that is already large (e.g. capability_scout.py, "
    "llm.py, portal routes) in target_files; always invent a new sibling module path "
    "instead. The combined source you would write must fit in ≤3 files and ≤150 lines.\n"
    "6. Never re-propose a capability whose module basename already appears in "
    "evidence.existing_modules, or whose title overlaps evidence.recent_proposal_titles "
    "(including stack-signature / stack_signature_detector and tick-failure / "
    "tick_failure_classifier — already shipped). Prefer a different actionable doc "
    "gap, tick-failure, skill, or check signal.\n"
    "7. Honor evidence.proposal_outcomes and evidence.cited_merges: never re-propose a "
    "title/slug that was merged, closed as wontfix, or closed as duplicate. "
    "'closed, not merged' is NOT itself evidence the gap is still open -- it may mean "
    "a human correctly rejected the proposal because the capability already exists "
    "elsewhere (reject_reason='duplicate'), which is different from 'wontfix' (a real "
    "gap, just deprioritized) or a genuinely remediable rejection (bad implementation, "
    "not a bad idea -- e.g. a broken dependency or a missing real caller). Prefer gaps "
    "that have not been tried, or that were closed for a remediable reason. If "
    "evidence.fix_regression_only is true, propose only a narrow regression fix.\n"
    "8. Before proposing a NEW module or mechanism, check evidence.store_capabilities "
    "(the real, introspected list of methods this repo's own store already exposes) "
    "and evidence.recent_skill_activity (real sample rows already persisted, including "
    "a 'reason' field). If an existing method, column, or field already accomplishes "
    "the same goal under a different name (e.g. a 'record per-rejection reasons' idea "
    "when evidence.store_capabilities already lists record_skill_outcome, which takes "
    "a reason argument and persists it), that is NOT a real gap -- set has_proposal to "
    "false, or propose surfacing/using the existing mechanism rather than inventing a "
    "second, parallel one for the same job.\n\n"
    'Respond ONLY with valid JSON: {"has_proposal": bool, "title": str, '
    '"gap_description": str, "evidence": str, "target_files": [str], '
    '"change_summary": str, "risk": "low"|"medium"|"high", "test_plan": str}. '
    'If has_proposal is false, the other string fields may be empty and target_files '
    'an empty list.'
)

_CAPABILITY_PROPOSAL_USER = (
    "Real signal gathered this cycle (nothing below is invented -- every field comes "
    "from a real store query or a real grep of this repo's own docs/*.md):\n\n"
    "{evidence}\n\n"
    "Based ONLY on the evidence above, propose at most one small, focused improvement "
    "to AgentIT's own codebase, or set has_proposal to false if nothing here is strong "
    "enough evidence to ground a specific, real change. JSON only."
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

    Thread safety: one shared instance may call ``_chat`` from multiple
    worker threads (``SkillEngine.run_all`` ThreadPoolExecutor). Each
    ``messages.create`` is an independent HTTP request on the SDK client;
    the shared ``llm_breaker`` is protected by a ``threading.Lock``. Do not
    mutate ``self.model`` / ``self._client`` after construction from those
    workers.
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

    def review_final_manifests(
        self,
        files: list[dict],
        app_summary: str,
    ) -> dict | None:
        """Final pre-PR quality/completeness opinion over the WHOLE validated
        batch of generated manifests -- advisory only, never a gate (see
        below) -- distinct from ``review_fix()`` above, which
        reviews one fix against one finding. Answers a different question:
        "looking at everything about to be committed together, does this
        look complete, internally consistent, and safe to open as a PR for
        a human to merge." Called once by ``portal/auto_delivery.py``, after
        its validate/fix loop has already converged (or given up), as the
        automated pipeline's own last look before it calls the real,
        non-dry-run ``route_and_deliver()``.

        This is a NEW, narrowly-scoped method, not a resurrection of
        AutoMode's removed ``classify_action`` -- that was a
        *destructiveness* classifier whose "safe" verdict let a batch skip
        human review entirely. This is a *quality/completeness* opinion
        that never skips anything: a human still reviews and merges the
        resulting PR on GitHub regardless of what this returns. Callers
        should therefore treat a "not approved" result as something to
        surface clearly on the PR/delivery record for that human, not as a
        reason to block PR creation.

        Returns {"approved": bool, "confidence": float, "reason": str,
        "concerns": list[str]}, or ``None`` on failure -- unlike
        ``review_fix()``, callers must NOT treat ``None`` as "rejected":
        there is still a full human review waiting on the PR itself, so an
        LLM outage here degrades to "no extra opinion offered", not "block
        everything".
        """
        max_files_shown = 30
        max_content_chars = 1200
        summaries = [
            f"--- {f.get('path', '?')} ({f.get('category', '?')}) ---\n"
            f"{(f.get('content') or '')[:max_content_chars]}"
            for f in files[:max_files_shown]
        ]
        files_text = "\n\n".join(summaries)
        if len(files) > max_files_shown:
            files_text += f"\n\n... and {len(files) - max_files_shown} more file(s) not shown."

        system = (
            "You are a senior platform engineer doing the final review of a batch of "
            "auto-generated Kubernetes/GitOps manifests before they are opened as a pull "
            "request. Look at the WHOLE batch together, not one file in isolation. "
            "Respond with JSON only:\n"
            '{"approved": true/false, "confidence": 0.0-1.0, "reason": "one or two '
            'sentences", "concerns": ["short phrase", ...]}\n\n'
            "Approve if: the manifests are internally consistent (e.g. Service/Deployment "
            "selectors match, referenced ConfigMaps/Secrets are accounted for), use "
            "correct API versions, and collectively look complete for what they claim "
            "to fix.\n"
            "Flag concerns (approved=false) if: manifests reference each other "
            "inconsistently, look incomplete, contain placeholder-looking values, or "
            "something about the batch as a whole looks wrong even if each file in "
            "isolation looked fine. 'concerns' should be empty when approved is true."
        )
        user = (
            f"Application: {app_summary}\n\n"
            f"Manifests about to be opened as a pull request ({len(files)} total):\n{files_text}\n\n"
            "Does this batch, taken as a whole, look complete and safe to open as a PR? JSON only."
        )
        raw = self._chat(system, user, max_tokens=1024)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
            concerns = parsed.get("concerns", [])
            if not isinstance(concerns, list):
                concerns = []
            return {
                "approved": bool(parsed["approved"]),
                "confidence": float(parsed["confidence"]),
                "reason": str(parsed["reason"]),
                "concerns": [str(c) for c in concerns],
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("LLM returned unparseable final manifest review: %s", raw)
            return None

    def detect_eol_risks(
        self,
        stack_info: dict,
        file_excerpts: dict[str, str],
    ) -> list[dict] | None:
        """Open-ended EOL/near-EOL detection across the repo's whole stack.

        Unlike ``classify_secret`` this isn't a
        yes/no filter over a heuristic hit -- it's genuinely open-ended
        reasoning, so the caller (``agentit.analyzers.eol.llm_findings``)
        treats this as purely additive to a deterministic baseline.

        Returns a list of risk dicts (possibly empty -- that's a valid "the
        LLM looked and found nothing" answer), or ``None`` if the LLM call
        failed or returned something unparseable (caller must fall back to
        the deterministic baseline only, never fabricate a result).
        """
        excerpts_text = "\n\n".join(
            f"--- {path} ---\n{content}" for path, content in file_excerpts.items()
        ) or "(no relevant files found)"
        user_msg = _EOL_USER.format(stack_info=json.dumps(stack_info), file_excerpts=excerpts_text)
        raw = self._chat(_EOL_SYSTEM, user_msg, max_tokens=_EOL_MAX_TOKENS)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
            risks = parsed["risks"]
            if not isinstance(risks, list):
                raise TypeError("'risks' is not a list")
            return [
                {
                    "component": str(r["component"]),
                    "version": str(r["version"]),
                    "status": str(r["status"]),
                    "eol_date": str(r.get("eol_date", "")),
                    "confidence": float(r["confidence"]),
                    "reason": str(r["reason"]),
                }
                for r in risks
            ]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("LLM returned unparseable EOL risk response: %s", raw)
            return None

    def propose_capability_improvement(self, evidence: dict) -> dict | None:
        """Given real, gathered signal (fleet rejection stats, agent/check
        health, skill effectiveness, and a static grep of this repo's own
        ``docs/*.md`` gap admissions), propose at most one small,
        evidence-cited improvement to AgentIT's own codebase -- see
        ``docs/self-improvement-for-agentit.md``. This is the
        ``capability-scout`` watcher's counterpart to ``research_cves()``/
        ``research_skill_improvement()`` in ``learning_agent.py``, except
        those target the skills catalog AgentIT generates for other apps;
        this targets AgentIT's own product surface.

        Returns a dict with ``has_proposal`` plus (when true)
        ``title``/``gap_description``/``evidence``/``target_files``/
        ``change_summary``/``risk``/``test_plan``, or ``None`` if the LLM
        call failed or returned something unparseable (caller must treat
        that as "no proposal this cycle", never fabricate one).
        """
        user_msg = _CAPABILITY_PROPOSAL_USER.format(evidence=json.dumps(evidence, default=str))
        raw = self._chat(_CAPABILITY_PROPOSAL_SYSTEM, user_msg, max_tokens=_CAPABILITY_PROPOSAL_MAX_TOKENS)
        parsed = self._parse_capability_proposal(raw)
        if parsed is not None:
            return parsed
        # One retry with a tighter instruction — the live failure mode was a
        # fenced, truncated JSON blob (evidence field too long). Ask for
        # compact output and no fences rather than inventing a proposal.
        if raw is not None:
            logger.warning("LLM returned unparseable capability proposal; retrying compact: %s", raw[:500])
            retry_system = (
                _CAPABILITY_PROPOSAL_SYSTEM
                + " CRITICAL RETRY: previous reply was unparseable. Respond with "
                "compact raw JSON only — no markdown fences, keep evidence under "
                "400 characters, do not truncate mid-object."
            )
            raw2 = self._chat(retry_system, user_msg, max_tokens=_CAPABILITY_PROPOSAL_MAX_TOKENS)
            parsed = self._parse_capability_proposal(raw2)
            if parsed is not None:
                return parsed
            logger.warning("LLM capability proposal still unparseable after retry: %s", (raw2 or "")[:500])
        return None

    def generate_capability_files(self, proposal: dict, current_files: dict[str, str]) -> dict[str, str] | None:
        """Given a capability-scout proposal and the current text of each
        target file, return a full-file replacement map for those paths only.

        Used by L3 ``source`` / ``auto`` mode in ``capability_scout.build_source_diff``.
        Returns ``None`` on LLM/parse failure (caller skips the cycle — no
        docs-only fallback). Never invents paths that weren't in ``current_files``.
        """
        user_msg = (
            "Proposal (JSON):\n"
            f"{json.dumps(proposal, default=str)}\n\n"
            "Current file contents (JSON object path -> text; empty string means new file):\n"
            f"{json.dumps(current_files)}\n\n"
            "Return ONLY valid JSON of the form "
            '{"files": {"relative/path": "full new file contents", ...}} '
            "including every path from the current-files object you choose to change. "
            "Do not add paths that were not provided. Keep each file as small as possible."
        )
        system = (
            "You implement a small, evidence-grounded change for AgentIT's own repo. "
            "Only edit skills/, checks/, tests/, or src/agentit/ files you were given. "
            "Prefer creating or lightly editing small new files; if a current file is "
            "truncated, do not attempt a full rewrite — return {\"files\": {}} instead. "
            "HARD SIZE BUDGET: across all returned file contents combined, at most "
            "3 files and 80 lines total (stay well under capability_scout.MAX_DIFF_LINES "
            "so JSON-escaped output fits). Short docstrings only. "
            "Respond ONLY with valid JSON — no markdown fences. "
            'If you cannot produce a safe change within that budget, return {"files": {}}.'
        )
        raw = self._chat(
            system, user_msg,
            max_tokens=_CAPABILITY_FILES_MAX_TOKENS,
            timeout=_CAPABILITY_FILES_TIMEOUT_SECONDS,
        )
        parsed = self._parse_capability_files(raw, current_files)
        if parsed is not None:
            return parsed
        if raw is not None:
            logger.warning(
                "LLM returned unparseable capability files; retrying compact: %s",
                raw[:500],
            )
            retry_system = (
                system
                + " CRITICAL RETRY: previous reply was truncated or unparseable. "
                "Respond with compact raw JSON only — no fences, at most 2 files and "
                "60 lines total, minimal comments, finish the JSON object completely."
            )
            raw2 = self._chat(
                retry_system, user_msg,
                max_tokens=_CAPABILITY_FILES_MAX_TOKENS,
                timeout=_CAPABILITY_FILES_TIMEOUT_SECONDS,
            )
            parsed = self._parse_capability_files(raw2, current_files)
            if parsed is not None:
                return parsed
            logger.warning(
                "LLM capability files still unparseable after retry: %s",
                (raw2 or "")[:500],
            )
        return None

    def _parse_capability_files(
        self, raw: str | None, current_files: dict[str, str],
    ) -> dict[str, str] | None:
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
            files = parsed.get("files", parsed)
            if not isinstance(files, dict):
                raise TypeError("'files' is not an object")
            allowed = set(current_files)
            out = {
                str(path): str(content)
                for path, content in files.items()
                if str(path) in allowed
            }
            return out or None
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _parse_capability_proposal(self, raw: str | None) -> dict | None:
        """Parse a capability-scout proposal payload. Returns None on failure."""
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
            has_proposal = bool(parsed["has_proposal"])
            if not has_proposal:
                return {"has_proposal": False}
            target_files = parsed["target_files"]
            if not isinstance(target_files, list):
                raise TypeError("'target_files' is not a list")
            return {
                "has_proposal": True,
                "title": str(parsed["title"]),
                "gap_description": str(parsed["gap_description"]),
                "evidence": str(parsed["evidence"]),
                "target_files": [str(f) for f in target_files],
                "change_summary": str(parsed["change_summary"]),
                "risk": str(parsed.get("risk", "medium")),
                "test_plan": str(parsed.get("test_plan", "")),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _chat(
        self,
        system: str,
        user: str,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout: float = 60.0,
    ) -> str | None:
        if llm_breaker.is_open:
            logger.warning("LLM circuit breaker open — skipping call")
            return None
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                temperature=0.2,
                timeout=timeout,
            )
            llm_breaker.record_success()
            text = _strip_code_fence(resp.content[0].text)
            stop = getattr(resp, "stop_reason", None)
            if stop and stop != "end_turn":
                logger.warning(
                    "LLM stop_reason=%s (len=%d) — output may be truncated",
                    stop, len(text or ""),
                )
            return text
        except (anthropic.APIError, anthropic.APIConnectionError, TimeoutError, OSError, RuntimeError) as exc:
            llm_breaker.record_failure()
            logger.warning("LLM call failed: %s", exc)
            return None
        except (TypeError, IndexError, AttributeError) as exc:
            llm_breaker.record_failure()
            logger.warning("LLM response parse failed: %s", exc)
            return None
        except Exception as exc:
            # Credentials / SDK init side-effects and other unexpected errors
            # must not crash assessments — same fail-soft contract as above.
            llm_breaker.record_failure()
            logger.warning("LLM call failed: %s", exc)
            return None
