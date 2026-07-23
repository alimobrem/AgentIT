---
name: workload-health-probes
domain: infrastructure
version: 1
triggers:
  - workload-probes
  - health-probes-source
outputs:
  - Deployment
delivery: source
property: "Application Deployment/Rollout containers declare liveness and readiness probes"
mode: template
---

# Workload Health Probes — Source Patch

## Property
Repo Deployment/Rollout YAML includes `livenessProbe` and `readinessProbe`
on containers — the same shapes `ha_dr.py` scans for the `health` finding.

## Why not Kyverno-only
Assess clears on **repo text**. A Kyverno mutate Policy may fix live pods
after admission, but re-Assess still fails until probes appear in YAML.
`health-probes-policy` remains an optional companion (refused as the
primary clear path).

## Constraints
- Inject `tcpSocket` probes on an already-declared `containerPort`
  (default 8080) — never guess HTTP paths
- Idempotent when probes already present
- Clear-evidence `workload_probes`: staged Deployment/Rollout has both
  probe keys
- Refuse companion: `health-probes-policy`

## Delivery
Source-repo PR. Re-Assess after merge clears `health`.

## Verification
- Staged YAML contains `livenessProbe` and `readinessProbe`
- Re-Assess: `health` finding resolved
