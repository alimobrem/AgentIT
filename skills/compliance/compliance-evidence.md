---
name: compliance-evidence
domain: compliance
version: 1
triggers:
  - compliance
  - evidence
  - report
  - attestation
  - soc2
outputs:
  - ComplianceEvidence
property: "Compliance evidence maps controls to implementations"
mode: llm
---

# Compliance Evidence Document

## Property
A compliance evidence document maps security controls (SOC 2, ISO 27001,
NIST 800-53, or custom frameworks) to their concrete implementations
in the application's infrastructure and deployment configuration.

## Instructions
Generate a markdown compliance evidence document for the assessed application.
The document must:

1. **Identify applicable controls** from the assessment results and the
   application's architecture (e.g., access control, encryption in transit,
   audit logging, change management, availability).

2. **Map each control to its implementation**, referencing specific generated
   manifests (NetworkPolicy, SecurityContext, ServiceMonitor, audit policy,
   RBAC, PDB, HPA, etc.).

3. **State the evidence** — what artifact proves the control is satisfied
   (e.g., "NetworkPolicy {{app_name}}-netpol restricts ingress to port 8080
   from the ingress namespace only").

4. **Flag gaps** — controls that are not yet implemented or only partially
   covered by the current configuration.

5. **Use this structure:**
   - Header with app name, date, assessor version
   - Summary table: Control | Status (Met / Partial / Gap) | Evidence
   - Detailed sections per control with implementation specifics
   - Gap analysis with remediation recommendations

## Constraints
- The LLM-tailored output is markdown, not YAML — this skill reasons about
  the app's specific architecture rather than emitting a static template
- Reference only manifests that were actually generated in the assessment
- Do not fabricate controls or evidence — if a control cannot be verified
  from the generated artifacts, mark it as a gap

## Template
Deterministic baseline used when no LLM is available: maps controls to the
skill outputs that *would* satisfy them (network-policy, rbac, audit-policy,
image-registry-policy, resource-limits/hpa) without confirming, per finding,
that those skills actually fired for this app — only `{{app_name}}` is
substituted, there's no placeholder for "which skills matched." Delivered as
a ConfigMap so the skill engine — which only ever writes a single `.yaml`
file per skill — produces a real, applyable K8s object instead of a bare
markdown file. The LLM enhancement replaces this with evidence tied to the
manifests actually generated for {{app_name}} in this specific assessment,
and marks anything unconfirmed as a gap rather than assuming coverage.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{app_name}}-compliance-evidence
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: compliance-evidence
data:
  compliance-evidence.md: |
    # Compliance Evidence Report — {{app_name}}

    This is a template-generated baseline. It lists the skill outputs that
    would satisfy each control area — it does not confirm those skills
    actually matched findings for this assessment. Re-run with an LLM
    connection for evidence tied to the manifests genuinely generated for
    {{app_name}}.

    | Control Area | Expected Evidence | Status |
    |---|---|---|
    | Network Isolation | network-policy skill: deny-all NetworkPolicy plus explicit allow rules | Unconfirmed |
    | Access Control (RBAC) | rbac skill: ServiceAccount, Role, RoleBinding scoped to {{app_name}} | Unconfirmed |
    | Audit Logging | audit-policy skill: audit.k8s.io Policy logging writes on core/RBAC resources | Unconfirmed |
    | Image Provenance | image-registry-policy skill: Kyverno Policy restricting images to trusted registries | Unconfirmed |
    | Resource Governance | resource-limits / hpa skills: ResourceQuota, LimitRange, HorizontalPodAutoscaler | Unconfirmed |
    | Encryption in Transit | No skill in this assessment generates automated evidence | Gap |
    | Encryption at Rest | No skill in this assessment generates automated evidence | Gap |

    ## Gap Analysis
    - Every row above is "Unconfirmed" until cross-checked against the
      manifests actually produced for {{app_name}} in this run.
    - Encryption in transit/at rest have no corresponding skill output at
      all — these require either a dedicated skill or manual attestation.
```

## Verification
- `kubectl get configmap {{app_name}}-compliance-evidence -o jsonpath='{.data}'` — evidence document is present
- Every row marked "Met" in the LLM-tailored version cross-references a manifest that was actually generated in this assessment
- No row claims a control is satisfied by a skill that did not match any finding
