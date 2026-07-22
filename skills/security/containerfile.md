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
# BuildConfig (not "Containerfile"): SkillEngine.has_api() gates on outputs
# against the live apiserver. "Containerfile" is not a K8s kind, so a set
# platform previously skipped this skill entirely (no file → quality filter
# never saw a container remediation). The template/LLM emit an OpenShift
# BuildConfig that embeds the Dockerfile — a real applyable kind.
outputs:
  - BuildConfig
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

## Template
Deterministic baseline used when no LLM is available: a generic UBI-minimal
single-stage build with a non-root user and a HEALTHCHECK, wrapped in an
OpenShift `BuildConfig` so the inline Dockerfile is a real, applyable K8s
manifest rather than a bare text file the engine has no way to emit. The LLM
enhancement replaces the generic `dockerfile:` content with the multi-stage,
language-specific variant described above (Go/Python/Java/Node) once the
app's stack is known.

```yaml
apiVersion: build.openshift.io/v1
kind: BuildConfig
metadata:
  name: {{app_name}}
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  source:
    type: Dockerfile
    dockerfile: |
      FROM registry.access.redhat.com/ubi9/ubi-minimal:latest
      WORKDIR /opt/app-root/src
      COPY . .
      RUN chown -R 1001:0 /opt/app-root/src
      USER 1001
      HEALTHCHECK --interval=30s --timeout=3s CMD curl -f http://localhost:8080/healthz || exit 1
      ENTRYPOINT ["/bin/sh"]
  strategy:
    type: Docker
    dockerStrategy: {}
  output:
    to:
      kind: ImageStreamTag
      name: {{app_name}}:latest
```

## Verification
- podman build -t test . — builds without errors
- podman run --user 1001 test — starts successfully as non-root
- podman inspect test | jq '.[0].Config.Healthcheck' — HEALTHCHECK is present
- podman history test — no secrets in layer history
