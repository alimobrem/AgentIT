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
- Output is markdown, not YAML — this skill reasons about the app's
  specific architecture rather than emitting a static template
- Reference only manifests that were actually generated in the assessment
- Do not fabricate controls or evidence — if a control cannot be verified
  from the generated artifacts, mark it as a gap
