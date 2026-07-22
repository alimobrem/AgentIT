---
name: db-migration-tooling
domain: data_governance
version: 1
triggers:
  - migration
  - alembic
  - flyway
  - schema
outputs:
  - alembic.ini
delivery: source
property: "Database schema changes are versioned with migration tooling"
mode: template
---

# Database Migration Tooling (source patch)

## Property
The application has versioned database migration tooling (Alembic,
Flyway, golang-migrate, or a `migrations/` directory) so schema changes
are reproducible across environments.

## Constraints
- Prefer the stack's idiomatic tool (Alembic for Python, golang-migrate
  for Go, `migrations/` for Node)
- Do not invent connection strings or credentials
- Scaffold only — humans wire `sqlalchemy.url` / models

## Delivery
Source-repo PR. Nested monorepo layouts (`apps/api/alembic`) already
satisfy the analyzer; this skill runs when none are present.

## Verification
- `alembic.ini` + `alembic/` or `migrations/` exists after merge
- Re-Assess clears the `migration` finding
