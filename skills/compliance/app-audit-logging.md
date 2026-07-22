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
access — a real source module **inside the app package** that is
**imported and called** from the API entrypoint (compliance requires
packaged module + usage evidence).

## Why not cluster audit-policy
`audit-policy` delivers an apiserver Policy as an advisory ConfigMap. That
is cluster-admin configuration and **does not** clear the analyzer finding
"No audit logging implementation detected", which scans app source.

## Constraints
- Emit structured JSON audit events (action, actor, resource, outcome)
- Language-matched module **in the app package** — never leave an orphan
  `audit.py` / `audit.ts` at the repo root (that is theater; clear-evidence
  `audit_wired` refuses it)
- Wire into FastAPI/Express middleware (mutating methods) so a call site exists
- No secrets in audit payloads

## Delivery
Source-repo PR. The skill template may emit a root-path stub; **delivery
must relocate** the module next to `app.py` / Express entry and patch that
entrypoint **before** clear-evidence simulation. After merge, re-Assess
clears `audit`. Already-wired repos (packaged module + usage) drop the
orphan stub — no theater PR.

## Verification
- Audit module exists under the app package (e.g. `apps/api/src/<pkg>/audit.py`
  or `src/<pkg>/audit.py`) — not repo-root only
- Entrypoint imports and calls the helper (middleware is sufficient)
