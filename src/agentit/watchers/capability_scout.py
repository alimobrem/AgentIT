"""capability-scout — periodically proposes small, evidence-grounded
improvements to AgentIT's own codebase (not the skills catalog it
generates for onboarded apps). See docs/self-improvement-for-agentit.md
for the full design.

Deliberately a separate watcher, separate pod, separate opt-in flag from
``skill-learner`` — this writes to AgentIT's own source tree and opens PRs
against AgentIT's own repo, materially higher-risk than drafting a skill
file, and deserves its own on/off switch and audit trail
(``agent_name = "capability-scout"``, not overloaded onto skill-learner's
rows). See the design doc's "What triggers it" section for the full
rationale against bolting this onto skill-learner's own tick.

Every cycle logs exactly one ``capability-run`` event — proposed,
gate-blocked, or no-signal are all normal, expected outcomes, never
exceptions. Never a direct commit to ``main``, never auto-merge: gates
either open a draft PR on a new branch, or discard the cycle's proposal
entirely.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from pathlib import Path

import click

from agentit.events import EventPublisher
from agentit.watchers import record_tick

logger = logging.getLogger(__name__)


class CapabilityScout:
    """Long-lived agent that periodically gathers real fleet/doc signal,
    asks the LLM for at most one evidence-grounded proposal, runs it
    through safety gates, and opens a draft PR when (and only when)
    everything passes.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        llm_model: str | None = None,
        interval: int = 86400,
        store: object | None = None,
        repo_dir: Path | None = None,
        max_open_prs: int = 1,
        startup_grace_seconds: int = 90,
        mode: str = "docs",
    ) -> None:
        self._publisher = publisher
        self._llm_model = llm_model
        self._interval = interval
        self._store = store
        self._repo_dir = repo_dir or Path.cwd()
        self._max_open_prs = max_open_prs
        self._startup_grace_seconds = startup_grace_seconds
        self._startup_grace_done = False
        self._mode = (mode or "docs").strip().lower()

    async def research_once(self) -> dict:
        """One capability-scout cycle. Always logs exactly one
        ``capability-run`` event before returning, whatever the outcome.
        """
        from agentit.capability_scout import (
            MIN_SIGNAL_ROWS,
            build_diff,
            describe_capability_run,
            gather_evidence,
            proposal_already_implemented,
            resolve_build_mode,
            run_safety_gates,
        )

        evidence = await gather_evidence(self._store, repo_dir=self._repo_dir)

        try:
            from agentit.llm import LLMClient
            llm_client = await asyncio.to_thread(LLMClient, model=self._llm_model)
        except Exception as exc:
            click.echo(f"[capability-scout] LLM unavailable this cycle: {exc}", err=True)
            severity, summary, details = describe_capability_run(evidence, None, None, None, error=str(exc))
            await self._log_run(severity, summary, details)
            return {"outcome": "no-llm"}

        if evidence.get("signal_count", 0) < MIN_SIGNAL_ROWS:
            click.echo(
                f"[capability-scout] Only {evidence.get('signal_count', 0)} signal row(s) "
                f"(need {MIN_SIGNAL_ROWS}) — skipping, no proposal this cycle.", err=True,
            )
            severity, summary, details = describe_capability_run(evidence, None, None, None)
            await self._log_run(severity, summary, details)
            return {"outcome": "no-signal"}

        proposal = await asyncio.to_thread(llm_client.propose_capability_improvement, evidence)
        # None = LLM call failed or JSON unparseable after retry — distinct from
        # an honest {"has_proposal": false}. Collapsing them made live parse
        # failures look like "nothing worth proposing."
        if proposal is None:
            click.echo(
                "[capability-scout] LLM returned unparseable/empty capability proposal "
                "this cycle (parse-error).",
                err=True,
            )
            severity, summary, details = describe_capability_run(
                evidence, None, None, None,
                error="unparseable or empty LLM capability proposal",
            )
            details["outcome"] = "parse-error"
            await self._log_run(severity, summary, details)
            return {"outcome": "parse-error"}

        if not proposal.get("has_proposal"):
            click.echo("[capability-scout] LLM found no evidence-grounded proposal this cycle.", err=True)
            severity, summary, details = describe_capability_run(evidence, proposal, None, None)
            await self._log_run(severity, summary, details)
            return {"outcome": "no-proposal"}

        if proposal_already_implemented(proposal, self._repo_dir):
            click.echo(
                f"[capability-scout] Skipping already-implemented proposal "
                f"'{proposal.get('title')}' (module present in tree).",
                err=True,
            )
            severity, summary, details = describe_capability_run(evidence, proposal, None, None)
            details["outcome"] = "already-implemented"
            await self._log_run(severity, summary, details)
            return {"outcome": "already-implemented"}

        resolved_mode = resolve_build_mode(proposal, self._mode)
        click.echo(
            f"[capability-scout] Build mode={self._mode} → resolved={resolved_mode} "
            f"for '{proposal.get('title')}'",
            err=True,
        )
        diff = await asyncio.to_thread(
            build_diff, proposal, mode=self._mode, repo_dir=self._repo_dir, llm_client=llm_client,
        )
        if not diff:
            click.echo(
                f"[capability-scout] Source generation produced no files for "
                f"'{proposal.get('title')}' — skipping (no docs-only PR).",
                err=True,
            )
            severity, summary, details = describe_capability_run(
                evidence, proposal, None, None,
                error="source generation returned no files",
            )
            details["build_mode"] = resolved_mode
            details["outcome"] = "source-generation-failed"
            await self._log_run(severity, summary, details)
            return {"outcome": "source-generation-failed", "build_mode": resolved_mode}

        # Label the PR from the actual diff, not the pre-generation resolve
        # (source resolve + empty generation used to open docs/proposals PRs
        # still stamped Build mode: source).
        effective_mode = (
            "docs" if all(p.replace("\\", "/").startswith("docs/proposals/") for p in diff) else "source"
        )
        gate_result = await asyncio.to_thread(run_safety_gates, proposal, diff, self._repo_dir, self._max_open_prs)

        if not gate_result["passed"]:
            failed = [g for g in gate_result["gates"] if not g["passed"]]
            failed_names = [g["name"] for g in failed]
            details_txt = "; ".join(f"{g['name']}: {g.get('detail', '')}" for g in failed)
            click.echo(
                f"[capability-scout] Proposal '{proposal['title']}' gate-blocked: "
                f"{', '.join(failed_names)} ({details_txt})",
                err=True,
            )
            severity, summary, details = describe_capability_run(evidence, proposal, gate_result, None)
            details["build_mode"] = effective_mode
            await self._log_run(severity, summary, details)
            return {"outcome": "gate-blocked"}

        pr_result = await asyncio.to_thread(self._open_pr, proposal, diff, effective_mode)
        pr_url = pr_result.get("pr_url")
        if pr_url:
            click.echo(f"[capability-scout] Opened draft PR: {pr_url}", err=True)
            severity, summary, details = describe_capability_run(evidence, proposal, gate_result, pr_url)
            details["build_mode"] = effective_mode
            await self._log_run(severity, summary, details)
            self._publisher.publish(
                "agentit-events", agent_id="capability-scout", action="capability-proposed",
                target_app=None, severity="info", summary=summary,
            )
            return {"outcome": "proposed", "pr_url": pr_url, "build_mode": effective_mode}

        click.echo(f"[capability-scout] PR creation failed: {pr_result.get('error')}", err=True)
        severity, summary, details = describe_capability_run(
            evidence, proposal, gate_result, None, error=pr_result.get("error", "PR creation failed"),
        )
        details["build_mode"] = resolved_mode
        await self._log_run(severity, summary, details)
        return {"outcome": "pr-failed"}

    def _open_pr(self, proposal: dict, diff: dict[str, str], build_mode: str = "docs") -> dict:
        """Write the diff's files to disk, branch/commit/push via
        ``git_pr.create_branch_commit_push`` (the exact mechanics
        ``self-fix --create-pr`` already uses), and open a draft PR via
        ``git_pr.open_draft_pr``. Runs off the event loop
        (``asyncio.to_thread`` at the call site) since it's genuinely
        blocking subprocess/file I/O.
        """
        from agentit.capability_scout import slugify
        from agentit.git_pr import create_branch_commit_push, open_draft_pr

        slug = slugify(proposal.get("title", "proposal"))
        branch = f"agentit/self-improve/{slug}-{int(_time.time())}"

        for path, content in diff.items():
            full = self._repo_dir / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")

        if build_mode == "source" and not all(p.startswith("docs/proposals/") for p in diff):
            commit_message = (
                f"feat(self-improve): {proposal['title']}\n\n"
                f"Auto-generated by agentit capability-scout (source mode).\n"
                f"Risk: {proposal.get('risk', 'unknown')}"
            )
        else:
            commit_message = (
                f"docs: propose {proposal['title']}\n\n"
                f"Auto-generated by agentit capability-scout.\n"
                f"Risk: {proposal.get('risk', 'unknown')}"
            )
        branch_result = create_branch_commit_push(branch, list(diff.keys()), commit_message, cwd=self._repo_dir)
        if not branch_result.get("success"):
            return {"error": branch_result.get("error", "git branch/commit/push failed")}

        body = self._render_pr_body(proposal, build_mode)
        return open_draft_pr(
            branch=branch, title=f"[AgentIT] {proposal['title']}", body=body, cwd=self._repo_dir,
        )

    def _render_pr_body(self, proposal: dict, build_mode: str = "docs") -> str:
        target_files = "\n".join(f"- `{f}`" for f in proposal.get("target_files") or []) or "- (none specified)"
        return (
            f"## {proposal['title']}\n\n"
            f"**Risk:** {proposal.get('risk', 'unknown')}\n"
            f"**Build mode:** `{build_mode}`\n\n"
            f"### Gap\n{proposal.get('gap_description', '')}\n\n"
            f"### Evidence\n{proposal.get('evidence', '')}\n\n"
            f"### Suggested target files\n{target_files}\n\n"
            f"### Test plan\n{proposal.get('test_plan', '')}\n\n"
            "> Proposed by AgentIT's capability-scout — see docs/self-improvement-for-agentit.md"
        )

    async def _log_run(self, severity: str, summary: str, details: dict) -> None:
        """Durable, queryable trace of this tick's outcome — best-effort, a
        store failure must never crash the watcher's main loop."""
        if self._store is None:
            return
        from agentit.capability_scout import CAPABILITY_RUN_ACTION
        try:
            await self._store.log_event(
                "capability-scout", CAPABILITY_RUN_ACTION, None, severity, summary, details=details,
            )
        except Exception:
            logger.warning("Failed to log capability-run event", exc_info=True)

    async def run(self) -> None:
        """Main loop: optional startup grace, then research, sleep.

        Startup grace avoids racing an in-flight Argo Rollouts canary on the
        same commit that just enabled/redeployed this watcher — git/`gh`/
        pytest gates need a settled pod + credentials, and dogfood cadence
        is ruined when the first tick fails for environmental reasons that
        clear 90s later.
        """
        from agentit.watchers import sleep_with_heartbeat

        click.echo(f"Starting capability-scout (interval={self._interval}s)...", err=True)
        if self._startup_grace_seconds > 0 and not self._startup_grace_done:
            click.echo(
                f"[capability-scout] Startup grace {self._startup_grace_seconds}s "
                "(avoids first-tick race with canary rollout)...",
                err=True,
            )
            try:
                await sleep_with_heartbeat(self._startup_grace_seconds)
            except KeyboardInterrupt:
                click.echo("capability-scout stopped.", err=True)
                return
            self._startup_grace_done = True
        while True:
            try:
                await self.research_once()
                Path("/tmp/heartbeat").touch()
                await record_tick(self._store, "capability-scout", success=True)
            except KeyboardInterrupt:
                click.echo("capability-scout stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("capability-scout tick failed")
                click.echo(f"[capability-scout] Error: {exc}", err=True)
                await record_tick(self._store, "capability-scout", success=False, error=str(exc))

            try:
                await sleep_with_heartbeat(self._interval)
            except KeyboardInterrupt:
                click.echo("capability-scout stopped.", err=True)
                break
