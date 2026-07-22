"""Decision card enrichment (ADR 0007)."""
from __future__ import annotations

from agentit.portal.pr_tracking import enrich_decision_card


def test_enrich_decision_card_high_confidence_with_contract():
    record = {
        "category": "scaling",
        "target_findings": ["scaling"],
        "contract_lines": ["Clears scaling by adding HPA"],
        "dry_run_warnings": [],
        "source": "delivery",
    }
    enrich_decision_card(record)
    assert record["confidence"] == "high"
    assert "dry-run" in record["dry_run_label"].lower() or "SSA" in record["dry_run_label"]
    assert record["evidence_lines"]
    assert "scaling" in record["decision_why"]


def test_enrich_decision_card_low_without_targets():
    record = {"category": "onboarding", "source": "onboarding"}
    enrich_decision_card(record)
    assert record["confidence"] == "low"
    assert record["dry_run_label"]
