# ADR 0007 — Unified decision card

## Status

Accepted (2026-07-22)

## Context

Ledger, Assessment Ledger, and Findings needed the same human decision
surface for open Scan PRs. A thin stub showed why/impact only.

## Decision

One Jinja macro (`pr_action_card` → `.decision-card`) shows:

1. **Why** — category + target findings  
2. **Confidence** — derived from solution contract + dry-run signals  
3. **Dry-run** — Scan gate / SSA summary (never invented)  
4. **Evidence** — contract lines  
5. **Approve / Reject** — Merge PR / Close with reason  

Enrichment lives in `pr_tracking.enrich_decision_card()`. Findings tab
embeds the same card when an open PR targets that finding category.

## Consequences

Humans see one decision shape everywhere. Confidence is coarse
(high/medium/low) from real signals — not a second LLM score.
