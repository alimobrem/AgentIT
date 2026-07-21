"""``FleetMixin`` -- fleet-wide, read-only aggregate views across every app's
latest ``assessments``/``apps``/``onboarding_results`` rows: the Fleet
dashboard's one-row-per-app listing (``get_fleet_data()``) and the Insights
page's fleet-wide counters (``get_fleet_insights()``), plus the two small
onboarded-repo-set lookups both depend on (and that ``get_fleet_data()``
itself calls).

Distinct from ``assessments.py`` (which owns single-assessment/single-app
CRUD) because these methods are specifically the *fleet-wide, many-apps-
at-once* read side -- ``get_fleet_data()`` calls back into
``self.repo_urls_with_onboarding()`` (this mixin) and ``self.get_trend()``
(``assessments.py``) per row, exactly the cross-domain call pattern this
package's mixin-composition resolves via normal attribute lookup on the
combined ``AssessmentStore`` instance -- see ``store/__init__.py``'s module
docstring.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``.
"""

from __future__ import annotations

import asyncpg

from agentit.models import AssessmentReport, Severity


class FleetMixin:
    _pool: asyncpg.Pool

    async def repo_urls_with_onboarding(self) -> set[str]:
        """Repo URLs that have at least one onboarding_results row (any assessment).

        Used so Fleet can offer a single "Scan" CTA for apps that already
        generated manifests — re-assess alone would drop lifecycle back to
        assessed and force a second Onboard click.
        """
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT a.repo_url
            FROM onboarding_results o
            JOIN assessments a ON a.id = o.assessment_id
            """
        )
        return {r["repo_url"] for r in rows}

    async def repo_has_onboarding(self, repo_url: str) -> bool:
        """True if any historical assessment of this repo was onboarded."""
        row = await self._pool.fetchrow(
            """
            SELECT 1
            FROM onboarding_results o
            JOIN assessments a ON a.id = o.assessment_id
            WHERE a.repo_url = $1
            LIMIT 1
            """,
            repo_url,
        )
        return row is not None

    async def get_fleet_data(self) -> list[dict]:
        """Return one row per unique repo_url with latest assessment + trend."""
        rows = await self._pool.fetch(
            """
            SELECT a.id, a.repo_url, a.repo_name, a.assessed_at,
                   a.overall_score, a.criticality, a.report_json,
                   apps.infra_repo_url AS app_infra_repo_url
            FROM assessments a
            INNER JOIN (
                SELECT repo_url, MAX(assessed_at) AS max_at
                FROM assessments GROUP BY repo_url
            ) latest ON a.repo_url = latest.repo_url
                    AND a.assessed_at = latest.max_at
            LEFT JOIN apps ON apps.repo_url = a.repo_url
            ORDER BY a.overall_score ASC
            """
        )

        ever_onboarded = await self.repo_urls_with_onboarding()
        fleet: list[dict] = []
        for r in rows:
            report = AssessmentReport.model_validate_json(r["report_json"])
            critical_count = sum(
                1 for s in report.scores for f in s.findings
                if f.severity in (Severity.critical, Severity.high)
            )
            trend = await self.get_trend(r["repo_url"])
            fleet.append({
                "id": r["id"],
                "repo_url": r["repo_url"],
                "repo_name": r["repo_name"],
                "latest_score": r["overall_score"],
                "previous_score": trend["previous_score"],
                "delta": trend["delta"],
                "criticality": r["criticality"],
                "last_assessed": r["assessed_at"].isoformat(),
                "assessment_count": trend["assessments_count"],
                "critical_count": critical_count,
                # Read from the `apps` table (the authoritative,
                # always-current source), not this specific assessment's
                # own `report_json`.
                "infra_repo_url": r["app_infra_repo_url"],
                # Prior onboard of any assessment for this repo — drives
                # Fleet's chained "Scan" CTA (confirm-gated once true).
                "ever_onboarded": r["repo_url"] in ever_onboarded,
            })
        return fleet

    async def get_fleet_insights(self) -> dict:
        """Get fleet-wide statistics for the insights dashboard."""
        total_assessments = await self._pool.fetchval("SELECT COUNT(*) FROM assessments") or 0
        unique_apps = await self._pool.fetchval("SELECT COUNT(DISTINCT repo_url) FROM assessments") or 0
        total_onboardings = await self._pool.fetchval("SELECT COUNT(*) FROM onboarding_results") or 0
        # Real PR activity, not a hand-maintained "remediations" completion
        # flag with no link to any actual PR/delivery (see the removed
        # `remediations` table's schema comment) -- a pure DB count across
        # the two places pr_tracking.py documents a `pr_url` can land, with
        # no live GitHub call (mirrors every other stat here).
        # `delivery_pr_count`: a delivery outcome's own pr_url (every
        # category now, including the former gate-tracked cluster_config/
        # cicd_shared_namespace -- the `gates` table has been removed
        # entirely, 2026-07-19). `onboarding_pr_count`: onboarding_results.
        # pr_url, which may itself be several `|`-joined URLs (Per-Agent
        # PRs) -- split and counted individually.
        delivery_pr_count = await self._pool.fetchval(
            """
            SELECT COUNT(*) FROM deliveries d,
                jsonb_each(COALESCE(d.details_json->'outcomes', '{}'::jsonb)) AS outcome(category, value)
            WHERE value->>'pr_url' IS NOT NULL
            """
        ) or 0
        onboarding_pr_rows = await self._pool.fetch(
            "SELECT pr_url FROM onboarding_results WHERE pr_url IS NOT NULL AND pr_url != ''"
        )
        onboarding_pr_count = sum(
            len([u for u in (row["pr_url"] or "").split("|") if u.strip()])
            for row in onboarding_pr_rows
        )
        total_prs = delivery_pr_count + onboarding_pr_count
        total_events = await self._pool.fetchval("SELECT COUNT(*) FROM events") or 0

        row = await self._pool.fetchrow(
            "SELECT COUNT(*) as total, SUM(CASE WHEN action='rejected' THEN 1 ELSE 0 END) as rejections FROM agent_feedback"
        )
        total_feedback = row["total"] if row else 0
        total_rejections = (row["rejections"] or 0) if row else 0

        return {
            "total_assessments": total_assessments,
            "unique_apps": unique_apps,
            "total_onboardings": total_onboardings,
            "total_prs": total_prs,
            "total_events": total_events,
            "total_feedback": total_feedback,
            "total_rejections": total_rejections,
        }
