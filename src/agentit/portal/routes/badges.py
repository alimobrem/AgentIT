"""Shareable score badges (SVG) for README embeds."""
from __future__ import annotations

import html
import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from agentit.portal.helpers import get_store
from agentit.scoring import letter_grade, score_band

router = APIRouter()


def _badge_authorized(repo_name: str, token: str | None) -> bool:
    """Allow when public badges are enabled, or a shared token matches."""
    if os.environ.get("AGENTIT_BADGE_PUBLIC", "").strip().lower() in ("1", "true", "yes"):
        return True
    expected = os.environ.get("AGENTIT_BADGE_TOKEN", "").strip()
    if expected and token and token == expected:
        return True
    allow = {
        a.strip() for a in os.environ.get("AGENTIT_PUBLIC_BADGE_APPS", "").split(",") if a.strip()
    }
    return repo_name in allow


def _svg(label: str, value: str, color: str) -> str:
    label_esc = html.escape(label)
    value_esc = html.escape(value)
    # Approximate widths for a compact shields-style pill.
    lw = 6 * len(label) + 20
    vw = 6 * len(value) + 20
    total = lw + vw
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{label_esc}: {value_esc}">
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <mask id="m"><rect width="{total}" height="20" rx="3" fill="#fff"/></mask>
  <g mask="url(#m)">
    <rect width="{lw}" height="20" fill="#555"/>
    <rect x="{lw}" width="{vw}" height="20" fill="{color}"/>
    <rect width="{total}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="{lw/2}" y="15">{label_esc}</text>
    <text x="{lw + vw/2}" y="15">{value_esc}</text>
  </g>
</svg>
"""


_BAND_COLOR = {
    "good": "#2ecc71",
    "ok": "#f0a500",
    "poor": "#e94560",
}


@router.get("/badge/{repo_name}.svg")
async def score_badge(
    request: Request,
    repo_name: str,
    token: str | None = Query(default=None),
) -> Response:
    """SVG score badge for an assessed app (latest fleet report).

    Auth: ``AGENTIT_BADGE_PUBLIC=1``, or ``?token=`` matching
    ``AGENTIT_BADGE_TOKEN``, or ``repo_name`` listed in
    ``AGENTIT_PUBLIC_BADGE_APPS`` (comma-separated).
    """
    if not _badge_authorized(repo_name, token):
        raise HTTPException(404, "Badge not available")

    s = await get_store()
    fleet = await s.get_fleet_data()
    row = next((r for r in fleet if r.get("repo_name") == repo_name), None)
    if row is None:
        raise HTTPException(404, "No assessment for this app")
    report = await s.get(row["id"])
    if report is None:
        raise HTTPException(404, "No assessment for this app")

    score = int(round(float(row.get("latest_score", report.overall_score))))
    band = score_band(score)
    color = _BAND_COLOR[band]
    grade = letter_grade(score)
    body = _svg("agentit", f"{score}/100 ({grade})", color)
    return Response(
        content=body,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )
