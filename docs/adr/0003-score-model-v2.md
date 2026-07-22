# ADR 0003 — Score model v2 (pass ratio + criticality weights)

## Status

Accepted (2026-07-22)

## Context

The v1 score started each dimension at 100 and subtracted fixed severity
penalties, then took an unweighted mean. That floor-clamped catastrophic
repos together, ignored criticality, and never rewarded passing controls.

## Decision

New assessments use `score_version: 2`:

1. **Per-dimension** = `100 * passed / applicable` over data-driven checks
   (plus analyzer findings as failed controls when no check rows exist).
2. **Overall** = criticality-weighted mean of dimension scores
   (`scoring.DIMENSION_WEIGHTS`).
3. Historical reports keep their stored `score_version` / scores; UI letter
   bands use `scoring.letter_grade`.
4. Shareable SVG badges are available at `/badge/{repo_name}.svg` when
   authorized (`AGENTIT_BADGE_PUBLIC`, `AGENTIT_BADGE_TOKEN`, or
   `AGENTIT_PUBLIC_BADGE_APPS`).

## Consequences

- Fleet scores on new assessments are not numerically comparable to v1
  without noting `score_version`.
- Estimated fix impact on Assessment Detail uses the active model.
