---
name: containerfile
domain: security
version: 1
triggers:
  - container
  - dockerfile
  - containerfile
  - image
  - base_image
outputs:
  - Containerfile
property: "Application runs in a secure, minimal container image"
mode: llm
---

# Containerfile — Secure Container Image

## Property
The application is packaged in a multi-stage container image using a
minimal UBI base, runs as a non-root user, and includes a HEALTHCHECK
instruction for runtime liveness.

## Constraints
- Multi-stage build: builder stage for compilation/dependencies, runtime stage for execution
- Base image must be UBI (registry.access.redhat.com/ubi9/ubi-minimal or ubi9-micro)
- Final stage runs as non-root USER (uid 1001)
- HEALTHCHECK instruction present
- No secrets or credentials baked into the image
- .dockerignore excludes .git, .env, __pycache__, node_modules

## Key decisions
The LLM must detect the application language from the assessment and generate
the appropriate Containerfile:

- **Go**: `ubi9/go-toolset` builder, static binary, `ubi9-micro` runtime
- **Python**: `ubi9/python-311` builder, pip install into virtualenv, `ubi9/ubi-minimal` runtime
- **Java**: `ubi9/openjdk-17` builder, maven/gradle build, `ubi9/openjdk-17-runtime` runtime
- **Node.js**: `ubi9/nodejs-18` builder, npm ci --production, `ubi9/nodejs-18-minimal` runtime

All variants must:
1. Copy only built artifacts to the runtime stage
2. Set `USER 1001` before `CMD`
3. Expose the application port
4. Include `HEALTHCHECK --interval=30s --timeout=3s CMD curl -f http://localhost:PORT/healthz || exit 1`

## Verification
- podman build -t test . — builds without errors
- podman run --user 1001 test — starts successfully as non-root
- podman inspect test | jq '.[0].Config.Healthcheck' — HEALTHCHECK is present
- podman history test — no secrets in layer history
