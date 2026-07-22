---
name: eol-upgrade
domain: infrastructure
version: 1
triggers:
  - eol
  - end-of-life
  - end of life
  - outdated
outputs:
  - .node-version
delivery: source
property: "Language runtimes and base images are within vendor support"
mode: template
---

# EOL Upgrade (source patch)

## Property
Pinned language runtimes and container base images are within vendor
support windows. Past-EOL Node/Python/base-image findings clear after the
pin lands in the app repo.

## Constraints
- Prefer `.node-version` / `.python-version` pins over rewriting
  `package.json` / lockfiles wholesale
- Never invent unsupported version numbers — bump to the next maintained
  LTS/stream known to the EOL table
- Dockerfile base images: pin away from `:latest` and past-EOL tags

## Delivery
Source-repo PR. Re-Assess clears the `eol` finding when the pin is
non-EOL (analyzer prefers `.node-version` over `engines.node`).

## Verification
- `.node-version` or `.python-version` declares a supported major
- Re-Assess no longer reports the prior EOL finding
