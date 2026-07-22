# ADR 0004 — Fail-closed webhook authentication

## Status

Accepted (2026-07-22)

## Context

Unset `GITHUB_WEBHOOK_SECRET` / `AGENTIT_INTERNAL_WEBHOOK_TOKEN` previously
skipped verification (fail open). Misconfigured deploys silently accepted
unauthenticated webhook traffic.

## Decision

Reject when secrets are unset, unless `AGENTIT_ALLOW_UNVERIFIED_WEBHOOKS=1`
(local demos/tests only). Health → Access shows a **blocking** warning for
missing secrets without the opt-in.

## Consequences

Chart deployments already mount both secrets. Portal tests set the opt-in
in `conftest.portal_client`.
