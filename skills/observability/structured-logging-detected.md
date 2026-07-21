---
name: structured-logging-detected
domain: observability
version: 1
mode: detect
triggers: []
outputs: []
severity: medium
category: logging
description: No structured logging library detected
recommendation: Add structured JSON logging (e.g., structlog for Python, zap for Go)
rule:
  type: file_contains
  pattern: structlog
status: active
source: manual
---

# Structured Logging Detected Check

## Property
Every application should emit structured (JSON) logs so they're
machine-parseable by a log aggregator, rather than free-text lines that
require regex scraping.

## Rule
Fires unless some file in the repo contains the substring `structlog`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/observability/structured-logging.yaml`,
  byte-for-byte the same rule (single `file_contains: structlog` pattern,
  same dimension/severity/category/description/recommendation) -- Phase 4
  of docs/extension-model-unification-plan-2026-07-18.md. Proven
  equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestStructuredLoggingDetectedParity` before the YAML file was deleted.
  observability dimension fully migrated after this port (all its
  checks now `mode: detect` skills, alongside the pre-existing
  `health-probes-check.md` from Phase 1).
- Deliberately kept as a Python-`structlog`-only literal match, not
  broadened to also recognize Go's `zap` (which the recommendation text
  mentions as an alternative) -- the original check's own narrow scope is
  preserved byte-for-byte during this port.

## Verification
- Repo's dependency manifest (`requirements.txt`/`pyproject.toml`) lists
  `structlog`, and application code imports it for logging.
