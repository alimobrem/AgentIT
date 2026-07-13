"""Skill learner agent — periodically researches CVEs via LLM and drafts new skills.

This is the automatic counterpart to the manual `agentit learn` CLI command
and the portal's "Research CVEs & Generate Skills" button on the Capabilities
page. All three call the same `agentit.learning_agent` functions; this one
just runs on an interval instead of waiting for a human or a click.

Generated skills are always saved with `status: draft` (set by the LLM
itself, per the prompt in `learning_agent.generate_skill_from_research`) and
require human review via `agentit activate-skill` before the skill engine
will match them against real assessments — this loop drafts, it never
auto-activates.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from agentit.events import EventPublisher
from agentit.watchers import record_tick

logger = logging.getLogger(__name__)


class SkillLearner:
    """Long-lived agent that periodically researches recent CVEs via the LLM
    and drafts new skills for any that aren't covered yet.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        llm_model: str | None = None,
        interval: int = 86400,
        limit: int = 3,
        skills_dir: Path | None = None,
        store: object | None = None,
    ) -> None:
        self._publisher = publisher
        self._llm_model = llm_model
        self._interval = interval
        self._limit = limit
        self._skills_dir = skills_dir or Path("skills")
        self._store = store

    def research_once(self) -> tuple[list[str], list[str]]:
        """Research CVEs and draft skills for any gaps. Returns (saved, skipped)."""
        try:
            from agentit.llm import LLMClient
            llm_client = LLMClient(model=self._llm_model)
        except Exception as exc:
            click.echo(f"[skill-learn] LLM unavailable this cycle: {exc}", err=True)
            return [], []

        from agentit.learning_agent import (
            check_skill_exists,
            generate_skill_from_research,
            research_cves,
            save_skill,
        )

        items = research_cves(llm_client, limit=self._limit)
        click.echo(f"[skill-learn] Researched {len(items)} CVE(s)", err=True)

        saved: list[str] = []
        skipped: list[str] = []
        for item in items:
            item_name = item.get("id") or item.get("title") or item.get("name", "")
            if item_name and check_skill_exists(self._skills_dir, item_name, "security"):
                skipped.append(item_name)
                continue
            content = generate_skill_from_research(llm_client, item, domain="security")
            if not content:
                continue
            path = save_skill(content, self._skills_dir, domain="security")
            if path:
                saved.append(path.stem)
                click.echo(f"[skill-learn] Drafted new skill: {path}", err=True)

        if saved:
            self._publisher.publish(
                "agentit-events",
                agent_id="skill-learner",
                action="skills-generated",
                target_app=None,
                severity="info",
                summary=f"Drafted {len(saved)} new skill(s) for review: {', '.join(saved)}",
            )
        else:
            click.echo("[skill-learn] No new skills this cycle", err=True)

        return saved, skipped

    async def run(self) -> None:
        """Main loop: research, sleep.

        ``research_once`` is unconverted synchronous code this pass (see
        docs/postgres-migration-plan.md's Phase 3 progress notes), so
        it's dispatched via ``asyncio.to_thread`` to avoid blocking the
        event loop for the tick's full duration.
        """
        click.echo(f"Starting skill learner (interval={self._interval}s)...", err=True)
        while True:
            try:
                await asyncio.to_thread(self.research_once)
                Path("/tmp/heartbeat").touch()
                record_tick(self._store, "skill-learner", success=True)
            except KeyboardInterrupt:
                click.echo("Skill learner stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("skill-learn tick failed")
                click.echo(f"[skill-learn] Error: {exc}", err=True)
                record_tick(self._store, "skill-learner", success=False, error=str(exc))

            try:
                await asyncio.sleep(self._interval)
            except KeyboardInterrupt:
                click.echo("Skill learner stopped.", err=True)
                break
