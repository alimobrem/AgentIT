---
name: containerfile
domain: security
version: 2
triggers:
  - container
  - dockerfile
  - containerfile
  - image
  - base_image
# Source-repo patch: analyzer findings (No Dockerfile / :latest / no USER /
# no HEALTHCHECK) are cleared only when the app repo has a real
# Dockerfile/Containerfile. delivery: source → CATEGORY_SOURCE_PATCH.
outputs:
  - Dockerfile
delivery: source
property: "Application runs in a secure, minimal container image"
mode: template
---

# Containerfile — Secure Container Image (source patch)

## Property
The application is packaged in a container image using a UBI base (not
`:latest`), runs as a non-root user, and includes a HEALTHCHECK.

## Constraints
- Base image must be UBI (`registry.access.redhat.com/ubi9/...`)
- Pin image tags (never `:latest` — use `:1` stream or a digest)
- Final stage runs as non-root `USER 1001`
- `HEALTHCHECK` instruction present
- No secrets baked into the image
- **Pin/harden on existing files:** when a Dockerfile/Containerfile already
  exists, pin `:latest` → `:1` / digest and **additively** apply USER /
  HEALTHCHECK / UBI FROM when the finding asks — never gut the body into a
  greenfield stub (#165 class / same bar as migration #163).

## Delivery
This skill opens a **source-repo PR** against the app's `Dockerfile` (or
`Containerfile`) — not a gitops BuildConfig. Re-Assess after merge clears
the `container` finding. Clear-evidence refuses destructive rewrites when
the existing file is known, binds each finding to its file path (pinning
`Dockerfile` alone cannot clear `Dockerfile.deps` / `.fast`), and requires
matching USER / HEALTHCHECK / UBI evidence for those subtypes.

## Template
Deterministic **greenfield** baseline (no Dockerfile yet). When a file
already exists, delivery applies pin-only — this template is not used as a
replacement:

```dockerfile
FROM registry.access.redhat.com/ubi9/ubi-minimal:1
WORKDIR /opt/app-root/src
COPY . .
RUN chown -R 1001:0 /opt/app-root/src
USER 1001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD curl -f http://localhost:8080/healthz || exit 1
ENTRYPOINT ["/bin/sh"]
```

## Verification
- Existing file: diff is FROM-only (body preserved)
- Greenfield: `podman build -t test .` builds; uid 1001; HEALTHCHECK; no `:latest`
