"""Tests for portal check_catalog — checks & resolutions matrix."""

from __future__ import annotations

from agentit.portal.check_catalog import (
    ANALYZER_CATEGORIES,
    badge_for_category,
    build_check_catalog,
    catalog_summary,
)
from agentit.remediation.registry import SOLUTION_CONTRACTS


def test_every_analyzer_category_has_a_row():
    rows = {r.category: r for r in build_check_catalog()}
    for cat in ANALYZER_CATEGORIES:
        assert cat in rows, f"missing catalog row for analyzer category {cat}"


def test_no_uncontracted_analyzer_categories():
    """After detect_only contracts, analyzer categories must not be bare."""
    rows = build_check_catalog()
    for row in rows:
        if row.category in ANALYZER_CATEGORIES:
            assert row.badge != "uncontracted", row.category


def test_detect_only_badges():
    assert badge_for_category("license") == "detect_only"
    assert badge_for_category("secrets") == "detect_only"
    assert badge_for_category("backup") == "detect_only"


def test_remediable_badges():
    assert badge_for_category("container") == "remediable"
    assert badge_for_category("scaling") == "remediable"
    assert badge_for_category("audit") == "remediable"


def test_summary_counts_match_contracts():
    rows = build_check_catalog()
    summary = catalog_summary(rows)
    assert summary["total"] == len(rows)
    assert summary["remediable"] == sum(1 for r in rows if r.badge == "remediable")
    assert summary["detect_only"] == sum(1 for r in rows if r.badge == "detect_only")
    # Every SOLUTION_CONTRACTS key appears in the catalog.
    cats = {r.category for r in rows}
    assert set(SOLUTION_CONTRACTS).issubset(cats)


def test_container_row_is_source_delivery():
    row = next(r for r in build_check_catalog() if r.category == "container")
    assert row.badge == "remediable"
    assert row.delivery == "source"
    assert row.skill_name == "containerfile"
    assert "image-registry-policy" in row.refuse_companions
