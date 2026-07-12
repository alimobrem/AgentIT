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
- Output is markdown, not YAML — this skill reasons about the app's
  specific infrastructure rather than emitting a static template
- Reference only resources and services detected in the assessment
- Do not assume data stores or integrations not present in the codebase
- Flag compliance-sensitive data that may require extended retention
