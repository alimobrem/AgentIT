---
name: multi-replica-deployment
domain: ha_dr
version: 2
mode: detect
triggers: []
outputs: []
severity: high
category: replicas
description: No multi-replica deployment found -- no redundancy
recommendation: Set replicas >= 2 for high availability
rule:
  type: file_contains
  pattern: "replicas: 2"
status: active
source: manual
---

# Multi-Replica Deployment Check

## Property
Every application should run at least 2 replicas so a single pod
restart/eviction never causes a full outage.

## Rule
Fires unless some file in the repo contains the literal string
`replicas: 2` (legacy narrow match preserved). Authoritative analyzer
(`ha_dr.py`) accepts `replicas: [2-9]` / multi-digit and emits category
`replicas` (not `availability` — that is PDB-only).

## Constraints
- Detection-only skill (`mode: detect`).
- Remediation: `workload-replicas` (source) via `SOLUTION_CONTRACTS.replicas`.
- PDB remediations must not claim to clear this finding.

## Verification
- `kubectl get deployment <name> -o jsonpath='{.spec.replicas}'` shows
  `2` or more.
- Re-Assess: `replicas` finding resolved
