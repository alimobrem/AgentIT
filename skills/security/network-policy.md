---
name: network-policy
domain: security
version: 1
triggers:
  - network
  - firewall
  - ingress
  - egress
  - isolation
outputs:
  - NetworkPolicy
property: "No unauthorized network access between pods"
mode: llm
---

# Network Isolation

## Property
No pod accepts traffic from sources it didn't explicitly allow.
No pod sends traffic to destinations it didn't explicitly allow.
This is the zero-trust networking foundation.

## Key decisions for the LLM
- Start with deny-all (ingress + egress) as the default posture
- Detect the app's ports from the stack info — do NOT hardcode 8080
- Detect database dependencies and allow egress to their standard ports:
  - PostgreSQL: 5432
  - MySQL: 3306
  - Redis: 6379
  - MongoDB: 27017
- If the assessment shows multiple services, create per-service policies
- If the cluster has a service mesh (check platform context for Istio/Linkerd CRDs),
  note that mesh-level policies may be more appropriate

## Constraints
- Use networking.k8s.io/v1 API
- Labels must include app.kubernetes.io/name
- Namespace must not be hardcoded — use the deployment namespace

## Template
Deterministic baseline used when no LLM is available: deny-all ingress/egress
plus an explicit allow policy for the app's own port and the common database
ports called out above, and DNS so pods can still resolve names. The LLM
enhancement replaces the hardcoded port list with ports actually detected in
the assessment.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{app_name}}-deny-all
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: {{app_name}}
  policyTypes:
    - Ingress
    - Egress
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{app_name}}-allow-common
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: {{app_name}}
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - ports:
        - protocol: TCP
          port: 8080
  egress:
    - ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    - ports:
        - protocol: TCP
          port: 5432
        - protocol: TCP
          port: 3306
        - protocol: TCP
          port: 6379
        - protocol: TCP
          port: 27017
```

## Verification
- From another namespace: curl APP_IP:PORT → connection refused
- From an allowed pod: curl APP_IP:PORT → 200 OK
- kubectl get networkpolicy -n NS → shows deny-all + allow rules

## Examples

### Simple web app with PostgreSQL
A deny-all policy plus allow ingress on port 8080 and allow egress to port 5432.

### Microservices
Each service gets its own policy. Frontend allows ingress from the router,
backend allows ingress only from frontend, database allows ingress only from backend.
