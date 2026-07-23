---
name: db-migration-tooling
domain: data_governance
version: 2
triggers:
  - migration
  - alembic
  - flyway
  - schema
outputs:
  - alembic.ini
  - alembic/versions/0001_baseline.py
delivery: source
property: "Database schema changes are versioned with migration tooling"
mode: template
---

# Database Migration Tooling (source patch)

## Property
The application has a real schema-evolution path: formal migration tooling
(Alembic, Flyway, golang-migrate, goose) **or** hand-rolled idempotent DDL
(`CREATE TABLE IF NOT EXISTS` / additive `ALTER … IF NOT EXISTS` in the app
store layer, as AgentIT does per ADR 0002).

## Constraints
- Prefer the stack's idiomatic tool (Alembic for Python, golang-migrate
  for Go, versioned SQL under `migrations/` for Node)
- Do not invent connection strings or credentials — read `DATABASE_URL` /
  `SQLALCHEMY_URL` / `AGENTIT_DB_DSN` at runtime
- Never open a theater stub: `SELECT 1`, empty `upgrade()`/`pass`, or
  comment-only `op.execute` — clear-evidence requires **real DDL**
  (`CREATE`/`ALTER`/`DROP` or `op.create_table`). Skip the PR when no
  schema change is needed (hand-rolled store DDL already passes)
- Do **not** propose Alembic when the repo already has hand-rolled schema
  DDL — the analyzer passes that finding with no PR

## Delivery
Source-repo PR when no migration approach is detected. Nested monorepo
layouts (`apps/api/alembic`) and embedded store DDL already satisfy the
analyzer.

## Verification
- Formal tooling: `alembic.ini` + revision with real DDL `upgrade()`, or
  Flyway / versioned SQL with `CREATE`/`ALTER`/`DROP`
- Hand-rolled: analyzer detects `SCHEMA_SQL` / multi-table idempotent DDL
- Re-Assess clears the `migration` finding
- Clear-evidence refuses `SELECT 1`, `pass`, comment-only `op.execute`, and
  `target_metadata = None` without a real DDL revision (#157 / shallow audit)
