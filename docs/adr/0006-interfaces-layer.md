# ADR 0006 — Interfaces layer for import direction

## Status

Accepted (2026-07-22)

## Context

Several packages formed 2-cycles (`models`↔`scoring`, `llm`↔`portal`,
`kube`↔`portal`). Cycles make typing, testing, and layering harder and
encourage portal helpers to become a dumping ground for shared primitives.

## Decision

Introduce `agentit.interfaces` as the shared floor:

| Module | Holds |
| --- | --- |
| `interfaces/breakers.py` | `CircuitBreaker`, `llm_breaker`, `kube_breaker` |
| `interfaces/score_aggregate.py` | criticality-weighted overall score |
| `interfaces/protocols.py` | `AnalyzerProto`, `AssessmentStoreProto` |

Layer direction: **models → analyzers → engine → portal**. Leaves
(`llm`, `kube`, `models`) import interfaces; portal may re-export for
backward compatibility.

Actionable `except Exception` paths should narrow to specific types;
best-effort background / badge refresh paths may keep broad catches.

## Consequences

Import cycles for breakers and score aggregation are gone. Call sites that
imported breakers from `portal.helpers` still work via re-export.
