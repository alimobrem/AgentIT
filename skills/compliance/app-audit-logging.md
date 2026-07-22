---
name: app-audit-logging
domain: compliance
version: 1
triggers:
  - audit
  - logging
  - compliance
  - governance
outputs:
  - audit.py
delivery: source
property: "Application emits structured audit logs for privileged actions"
mode: template
---

# Application Audit Logging (source patch)

## Property
The application implements audit logging for privileged actions and data
access — a real source module the compliance analyzer detects
(`audit.py` / `audit.ts` / `audit.go`).

## Why not cluster audit-policy
`audit-policy` delivers an apiserver Policy as an advisory ConfigMap. That
is cluster-admin configuration and **does not** clear the analyzer finding
"No audit logging implementation detected", which scans app source.

## Constraints
- Emit structured JSON audit events (action, actor, resource, outcome)
- Language-matched module at repo root (`audit.py`, `audit.ts`, or `audit.go`)
- No secrets in audit payloads

## Delivery
Source-repo PR. After merge, re-Assess clears the `audit` finding.

## Verification
- File `audit.py` / `audit.ts` / `audit.go` exists in the app repo
- Module exports a callable audit log helper
