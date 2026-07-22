# ADR 0002: Postgres as the assessment store

- **Status:** Accepted
- **Date:** 2026-07
- **Brand:** AgentIT

## Context

AgentIT needs a durable store for assessments, fleet rows, events, deliveries, and watcher state. A local SQLite path existed during early development and was unsuitable for the multi-pod OpenShift deployment.

## Decision

- **Postgres** (via `AGENTIT_DB_DSN` / asyncpg) is the only supported store.
- The Helm chart can run a **bundled** Postgres for OpenShift dogfood; operators may point at an external DSN.
- One-shot `agentit migrate-sqlite-to-postgres` exists for leftover local SQLite files.

## Consequences

- CLI `agentit assess <repo>` for a single repo does **not** require Postgres.
- Portal, fleet, `--rescan`, and watchers **do** require a reachable DSN.
- Migration design notes are historical: [`../history/postgres-migration-plan.md`](../history/postgres-migration-plan.md).
