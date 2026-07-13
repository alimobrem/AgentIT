---
name: decommission-plan
domain: retirement
version: 1
triggers:
  - retirement
  - decommission
  - sunset
  - cleanup
  - end-of-life
outputs:
  - DecommissionPlan
property: "Abandoned applications have a 30-day sunset plan"
mode: llm
---

# Decommission Plan

## Property
Abandoned or end-of-life applications have a structured 30-day sunset
plan covering data archival, DNS cleanup, resource reclamation, and
stakeholder notification to prevent orphaned infrastructure.

## Instructions
Generate a markdown decommission plan for the assessed application. The
document must:

1. **Stakeholder notification** — identify teams consuming the app's APIs
   or data, draft notification templates for 30-day, 14-day, and 7-day
   warnings, and list communication channels based on the app's detected
   integrations.

2. **Data archival plan** — based on the app's detected data stores
   (PostgreSQL, Redis, S3, PVCs), document what to archive, retention
   periods, export commands, and target archive location. Flag any data
   subject to compliance retention requirements.

3. **DNS and routing cleanup** — list Routes, Ingresses, and Service
   entries to remove. Document external DNS records that point to the
   app and the process to update or delete them.

4. **Resource reclamation timeline** — a phased 30-day plan:
   - Day 0: Announce sunset, disable new user access
   - Day 7: Scale to minimum replicas, enable read-only mode if applicable
   - Day 14: Stop accepting traffic, begin data export
   - Day 21: Archive data, remove external DNS
   - Day 30: Delete all namespace resources, reclaim PVCs and quotas

5. **Dependency impact assessment** — identify downstream services,
   shared ConfigMaps/Secrets, and cross-namespace references that must
   be updated before deletion.

6. **Use this structure:**
   - Header with app name, sunset date placeholder, owner
   - Stakeholder notification schedule
   - Data archival checklist
   - Resource inventory table
   - 30-day phased timeline
   - Post-decommission verification

## Constraints
- The LLM-tailored output is markdown, not YAML — this skill reasons about
  the app's specific infrastructure rather than emitting a static template
- Reference only resources and services detected in the assessment
- Do not assume data stores or integrations not present in the codebase
- Flag compliance-sensitive data that may require extended retention

## Template
Deterministic baseline used when no LLM is available: a generic 30-day
sunset checklist covering the same six sections listed above, without
app-specific detail (no placeholder exists for "detected databases" —
only `{{app_name}}` is substituted). Delivered as a ConfigMap so the skill
engine — which only ever writes a single `.yaml` file per skill — produces a
real, applyable K8s object instead of a bare markdown file. The LLM
enhancement replaces the generic checklist with one referencing the app's
actual detected data stores, consumers, and integrations.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{app_name}}-decommission-plan
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: decommission-plan
data:
  decommission-plan.md: |
    # Decommission Plan — {{app_name}}

    **Sunset date:** TBD (set when retirement is approved)
    **Owner:** TBD

    ## 1. Stakeholder Notification
    - [ ] 30-day notice sent to all known API/data consumers
    - [ ] 14-day reminder sent
    - [ ] 7-day final warning sent
    - [ ] Announcement posted to the team's Slack channel(s)
    - [ ] Service catalog / registry entry marked deprecated

    ## 2. Data Archival
    - [ ] Identify all persistent data stores (databases, PVCs, object storage)
    - [ ] Export/backup data using the store's native tooling (pg_dump, mongodump, etc.)
    - [ ] Verify backup integrity and record backup location plus retention period
    - [ ] Flag any data subject to compliance retention requirements

    ## 3. DNS and Routing Cleanup
    - [ ] Remove OpenShift Routes / Ingress resources
    - [ ] Delete Service entries
    - [ ] Update or remove external DNS records
    - [ ] Remove from service mesh / API gateway configuration

    ## 4. Resource Reclamation Timeline (30-Day Sunset)
    | Day | Action |
    |-----|--------|
    | 0   | Announce sunset, disable new user access |
    | 7   | Scale to minimum replicas, enable read-only mode if applicable |
    | 14  | Stop accepting traffic, begin data export |
    | 21  | Archive data, remove external DNS |
    | 30  | Delete all namespace resources, reclaim PVCs and quotas |

    ## 5. Dependency Impact Assessment
    - [ ] Identify downstream services calling this application
    - [ ] Identify shared ConfigMaps/Secrets referenced by other namespaces
    - [ ] Confirm no cross-namespace references remain before deletion

    ## 6. Post-Decommission Verification
    - [ ] `kubectl get all -l app.kubernetes.io/name={{app_name}}` returns no resources
    - [ ] Confirm no active alerts or dashboards still reference {{app_name}}
    - [ ] Confirm namespace/quota reclaimed
```
