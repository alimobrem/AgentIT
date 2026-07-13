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

**Known gap (partially addressed):** this watcher runs in its own pod,
separate from the portal. Drafts it writes go to whatever `skills_dir` this
process was given -- by default `Path("skills")` inside *this* pod's own
container filesystem, which is neither the portal pod's filesystem (so
drafts never show up on the Capabilities page for review) nor persisted
across this pod's own restarts. Setting `AGENTIT_SKILLS_DIR` to a mounted
PVC (`chart/templates/agents/skill-learner.yaml`, gated behind
`agents.skillLearner.persistence.enabled`, default on) fixes the
restart-survival half of that; making drafts visible to the portal still
needs either shared/RWX storage across both Deployments (risky with the
default RWO storage class -- see that chart file's comments) or a
git-write-back pipeline, neither of which ships here. `run()` below logs a
loud warning every cycle so this isn't silent. Until one of those lands,
prefer the portal's own "Research CVEs & Generate Skills" button or
`agentit learn`/`learn-for` run against the portal's own data volume --
those run in-process, so drafts are immediately visible and persisted.
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
        """Research low-effectiveness skills first, falling back to a generic
        CVE sweep when nothing's flagged. Returns (saved, skipped).

        This is the wiring that actually closes the self-improvement loop:
        before this, the research cycle only ever swept CVEs on its own
        schedule, blind to which of its own already-shipped skills humans
        keep rejecting (``skill_effectiveness``, populated by
        ``skill_engine.record_skill_outcomes`` from the real onboarding
        apply/gate/auto-mode paths -- see ``docs/postgres-migration-plan.md``
        and this repo's README for the full loop). A skill flagged low this
        cycle is prioritized; CVE research only runs when there's nothing to
        improve.
        """
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
            research_skill_improvement,
            save_skill,
        )

        saved: list[str] = []
        skipped: list[str] = []

        flagged = self._get_flagged_skills()
        if flagged:
            click.echo(f"[skill-learn] {len(flagged)} low-effectiveness skill(s) flagged -- "
                       "prioritizing improvement research over the generic CVE sweep this cycle.", err=True)
            from agentit.skill_engine import load_all_skills
            by_name = {s.name: s for s in load_all_skills(self._skills_dir)}

            for entry in flagged[: self._limit]:
                skill_name = entry["skill"]
                skill = by_name.get(skill_name)
                if skill is None:
                    click.echo(f"[skill-learn] Skipping improvement research for "
                               f"'{skill_name}' -- skill no longer found on disk", err=True)
                    skipped.append(skill_name)
                    continue
                item = research_skill_improvement(llm_client, skill.name, skill.domain, entry)
                if not item:
                    skipped.append(skill_name)
                    continue
                content = generate_skill_from_research(llm_client, item, domain=skill.domain)
                if not content:
                    skipped.append(skill_name)
                    continue
                # Deliberately skip check_skill_exists() here -- the point
                # is a replacement for an existing (underperforming) skill,
                # so a name/domain match against that same skill is
                # expected, not a duplicate to reject.
                path = save_skill(content, self._skills_dir, domain=skill.domain)
                if path:
                    saved.append(path.stem)
                    click.echo(f"[skill-learn] Drafted improvement for '{skill_name}': {path}", err=True)
                    if self._store is not None:
                        try:
                            self._store.log_event(
                                "skill-learner", "skill-improvement-drafted", None, "info",
                                f"Drafted {path.stem} to improve low-effectiveness skill "
                                f"'{skill_name}' ({entry.get('approval_rate', 0):.0%} approval)",
                            )
                        except Exception:
                            logger.warning("Failed to log skill-improvement-drafted event", exc_info=True)

            if saved or skipped:
                if saved:
                    self._publisher.publish(
                        "agentit-events",
                        agent_id="skill-learner",
                        action="skills-generated",
                        target_app=None,
                        severity="info",
                        summary=f"Drafted {len(saved)} skill improvement(s) for review: {', '.join(saved)}",
                    )
                else:
                    click.echo("[skill-learn] No usable improvement drafts this cycle", err=True)
                return saved, skipped

        items = research_cves(llm_client, limit=self._limit)
        click.echo(f"[skill-learn] Researched {len(items)} CVE(s)", err=True)

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

    def _get_flagged_skills(self) -> list[dict]:
        """Low-effectiveness skills from the store, or ``[]`` if there's no
        store (e.g. ``agentit learn`` CLI invocations without a DB path) or
        the lookup fails -- never lets this new prioritization block the
        existing CVE-sweep fallback."""
        if self._store is None or not hasattr(self._store, "get_low_effectiveness_skills"):
            return []
        try:
            return self._store.get_low_effectiveness_skills()
        except Exception:
            logger.warning("Failed to fetch low-effectiveness skills", exc_info=True)
            return []

    async def run(self) -> None:
        """Main loop: research, sleep.

        ``research_once`` is unconverted synchronous code this pass (see
        docs/postgres-migration-plan.md's Phase 3 progress notes), so
        it's dispatched via ``asyncio.to_thread`` to avoid blocking the
        event loop for the tick's full duration.
        """
        click.echo(f"Starting skill learner (interval={self._interval}s)...", err=True)
        click.echo(
            f"[skill-learn] Writing drafts to {self._skills_dir} in THIS pod. "
            "Drafts are not synced to the portal's Capabilities page and are lost on "
            "pod restart unless this path is a mounted persistent volume "
            "(see agents.skillLearner.persistence in chart/values.yaml). "
            "Review drafts via `agentit test-skill`/`activate-skill` against this same "
            "path, or prefer the portal's own 'Research CVEs & Generate Skills' button "
            "(runs in-process, immediately visible and persisted).",
            err=True,
        )
        logger.warning(
            "skill-learner writing drafts to %s (this pod only -- not visible to the "
            "portal's Capabilities page without shared storage)", self._skills_dir,
        )
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
