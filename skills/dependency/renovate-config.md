---
name: renovate-config
domain: dependency
version: 1
triggers:
  - dependency
  - update
  - renovate
  - automated
outputs:
  - RenovateConfig
property: "Dependencies are automatically updated via Renovate"
mode: template
---

# Renovate Configuration

## Property
Dependencies are continuously monitored and updated by Renovate Bot,
with automatic merging for patch versions and immediate alerts for
security vulnerabilities.

## Template

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": [
    "config:recommended",
    ":automergeMinor",
    ":separateMajorReleases",
    "security:openssf-scorecard"
  ],
  "labels": ["dependencies"],
  "vulnerabilityAlerts": {
    "enabled": true,
    "labels": ["security"]
  },
  "packageRules": [
    {
      "matchUpdateTypes": ["patch"],
      "automerge": true,
      "automergeType": "pr"
    },
    {
      "matchUpdateTypes": ["major"],
      "automerge": false,
      "labels": ["breaking-change"]
    },
    {
      "matchPackagePatterns": ["*"],
      "schedule": ["before 8am on monday"]
    }
  ],
  "prConcurrentLimit": 5,
  "prHourlyLimit": 2
}
```

## Notes
- Place as `renovate.json` in the repository root
- Patch auto-merge requires passing CI — configure branch protection accordingly
- Adjust `schedule` to match team review cadence
- `prConcurrentLimit` prevents PR flood

## Verification
- Renovate opens PRs for outdated dependencies
- Patch PRs auto-merge after CI passes
- Vulnerability PRs appear with the `security` label
