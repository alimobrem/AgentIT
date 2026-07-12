---
name: cost-labels
domain: cost
version: 1
triggers:
  - cost
  - label
  - attribution
  - chargeback
outputs:
  - ConfigMap
property: "Resources have cost attribution labels"
mode: template
---

# Cost Attribution Labels

## Property
All resources carry standardized labels for cost center, team, and
environment, enabling accurate chargeback and showback reporting.

## Template

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{app_name}}-cost-labels
  labels:
    app.kubernetes.io/name: {{app_name}}
    cost-center: "{{cost_center}}"
    team: "{{team}}"
    environment: "{{environment}}"
data:
  cost-center: "{{cost_center}}"
  team: "{{team}}"
  environment: "{{environment}}"
  project: "{{app_name}}"
```

## Notes
- All Deployments, Services, PVCs, and Jobs in the namespace should carry
  the same `cost-center`, `team`, and `environment` labels
- Label values must be DNS-compatible (lowercase, alphanumeric, hyphens)
- Pair with a policy engine (OPA/Kyverno) to enforce label presence on all resources

## Verification
- `kubectl get all -n NS --show-labels` shows cost labels on every resource
- Cost reporting tool (Kubecost, OpenCost) groups spend by these labels
