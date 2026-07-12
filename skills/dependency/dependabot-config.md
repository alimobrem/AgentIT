---
name: dependabot-config
domain: dependency
version: 1
triggers:
  - dependency
  - update
  - dependabot
  - github
outputs:
  - DependabotConfig
property: "Dependencies are scanned by Dependabot"
mode: template
---

# Dependabot Configuration

## Property
GitHub Dependabot monitors all dependency ecosystems in the repository,
opens PRs for updates on a weekly schedule, and flags security advisories.

## Template

```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
    open-pull-requests-limit: 5
    labels:
      - "dependencies"
      - "python"
    commit-message:
      prefix: "deps"

  - package-ecosystem: "npm"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
    open-pull-requests-limit: 5
    labels:
      - "dependencies"
      - "javascript"
    commit-message:
      prefix: "deps"

  - package-ecosystem: "docker"
    directory: "/"
    schedule:
      interval: "weekly"
    labels:
      - "dependencies"
      - "docker"
    commit-message:
      prefix: "deps"

  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
    labels:
      - "dependencies"
      - "ci"
    commit-message:
      prefix: "deps"
```

## Notes
- Place as `.github/dependabot.yml` in the repository
- Add/remove `package-ecosystem` entries to match the project's actual stacks
- Security updates are always enabled by default in GitHub — this config covers version updates
- Pair with branch protection rules requiring CI to pass before merge

## Verification
- `Settings > Code security > Dependabot` shows enabled status
- PRs appear weekly with the `dependencies` label
- Security alerts surface in the Security tab
