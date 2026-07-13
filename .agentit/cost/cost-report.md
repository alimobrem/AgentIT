# Cost Optimization Report: AgentIT

## Detected Stack

- **Primary language:** python
- **Frameworks:** fastapi
- **Databases:** none detected
- **Architecture:** monolith
- **Service count:** 1

## Resource Right-Sizing

Estimated deployment tier: **small**

| Resource | Recommended |
|----------|-------------|
| CPU request | 250m |
| Memory request | 256Mi |
| Replicas | 1 |

## Idle Resource Detection

- Review workloads with CPU utilization below 10% over 7 days.
- Check for PVCs not mounted to any running pod.
- Identify Services with zero endpoint traffic.

## Reserved Capacity Suggestions

- Criticality: **high**
- For sustained workloads, commit to 1-year reserved instances to save 30-40%.
- For variable workloads, use spot/preemptible nodes for non-critical tiers.

## Estimated Monthly Cost

| Size | Replicas | Estimate |
|------|----------|----------|
| small | 1 | $15-30 |
| medium | 2 | $30-80 |
| large | 3 | $80-200 |

**Selected tier (small):** $15-30/month per replica x 1 replicas
