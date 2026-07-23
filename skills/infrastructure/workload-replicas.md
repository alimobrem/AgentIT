---
name: workload-replicas
domain: infrastructure
version: 1
triggers:
  - replicas
  - multi-replica
  - redundancy
outputs:
  - Deployment
delivery: source
property: "Application Deployment/Rollout runs at least 2 replicas"
mode: template
---

# Workload Replicas — Multi-Replica Source Patch

## Property
The app's Deployment/Rollout declares `replicas: 2` (or higher) in **repo
YAML**, clearing the `replicas` analyzer finding (`ha_dr.py`).

## Why not PDB
`availability` → PodDisruptionBudget is for voluntary disruption. A
**single-replica** finding needs a replica count bump — opening a PDB PR
does not fix redundancy.

## Constraints
- Patch existing Deployment/Rollout when present (snapshot / delivery read)
- Never invent a competing second Deployment alongside a real one when a
  path is known — bump `spec.replicas` only
- Greenfield (no workload YAML): emit a minimal Deployment with
  `replicas: 2`
- Clear-evidence `workload_replicas`: staged file has
  `kind: Deployment|Rollout` and `replicas >= 2`
- Refuse companions: `pod-delete`, `pdb` (wrong fix for this finding)

## Delivery
Source-repo PR against the app. Re-Assess after merge clears `replicas`.

## Verification
- `rg -n 'replicas:\s*[2-9]' deploy/ chart/` (or equivalent)
- Re-Assess: `replicas` finding resolved
