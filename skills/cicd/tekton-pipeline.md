---
name: tekton-pipeline
domain: cicd
version: 1
triggers:
  - pipeline
  - cicd
  - ci
  - tekton
  - build
outputs:
  - Pipeline
  - PipelineRun
property: "Application has automated CI/CD pipeline"
mode: llm
---

# Tekton Pipeline — CI/CD Automation

## Property
The application has a complete CI/CD pipeline that clones source,
builds, tests, creates a container image, scans it for vulnerabilities,
generates an SBOM, and deploys — all automated via Tekton.

## Constraints
- Pipeline uses tekton.dev/v1 API
- Standard step ordering: git-clone → build → test → image-build → image-push → image-scan → sbom-generate → deploy
- Uses ClusterTasks where available (git-clone, buildah)
- PipelineRun references the pipeline with appropriate params
- Workspaces for shared-data and credentials
- Pipeline must be idempotent — safe to re-run

## Key decisions
The LLM must detect the application language from the assessment and
tailor the build and test steps:

- **Go**: `go build -o app .`, `go test ./...`
- **Python**: `pip install -r requirements.txt`, `pytest`
- **Java**: `mvn package -DskipTests=false`, test step uses `mvn test`
- **Node.js**: `npm ci`, `npm test`

All variants must include:
1. **git-clone** — ClusterTask reference, writes to shared-data workspace
2. **build** — language-specific compilation/dependency install
3. **test** — language-specific test runner
4. **image-build** — buildah bud using the generated Containerfile
5. **image-push** — buildah push to the target registry
6. **image-scan** — reference the image-scan Task for Trivy scanning
7. **sbom-generate** — syft or cyclonedx to produce SBOM
8. **deploy** — kubectl apply or kustomize build | kubectl apply

PipelineRun must set:
- `pipelineRef` to the generated pipeline name
- `params` for git URL, revision, image name
- `workspaces` bound to PVCs or VolumeClaimTemplates

## Template
Deterministic baseline used when no LLM is available: clone, build the image,
scan it, generate an SBOM, and deploy — using `tekton.dev/v1` (not the
deprecated `v1beta1`). Language-specific build/test steps are intentionally
left out of the static baseline (there's no placeholder for "detected
language" — only `{{app_name}}` is substituted); the LLM enhancement inserts
a `build`/`test` task tailored to the app's stack ahead of `image-build`. The
`image-scan` and `sbom-generate` task references match the names produced by
the `image-scan-task` and `sbom-task` skills in this same assessment.

```yaml
apiVersion: tekton.dev/v1
kind: Pipeline
metadata:
  name: {{app_name}}-pipeline
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  params:
    - name: repo-url
      type: string
    - name: image-ref
      type: string
  workspaces:
    - name: shared-workspace
  tasks:
    - name: git-clone
      taskRef:
        name: git-clone
        kind: ClusterTask
      params:
        - name: url
          value: $(params.repo-url)
      workspaces:
        - name: output
          workspace: shared-workspace
    - name: image-build
      taskRef:
        name: buildah
        kind: ClusterTask
      runAfter:
        - git-clone
      params:
        - name: IMAGE
          value: $(params.image-ref)
      workspaces:
        - name: source
          workspace: shared-workspace
    - name: image-scan
      taskRef:
        name: image-scan
        kind: Task
      runAfter:
        - image-build
      params:
        - name: IMAGE
          value: $(params.image-ref)
    - name: sbom-generate
      taskRef:
        name: {{app_name}}-sbom
        kind: Task
      runAfter:
        - image-build
      params:
        - name: IMAGE
          value: $(params.image-ref)
    - name: deploy
      taskRef:
        name: kubernetes-actions
        kind: ClusterTask
      runAfter:
        - image-scan
        - sbom-generate
      params:
        - name: script
          value: kubectl rollout restart deployment/{{app_name}}
---
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  generateName: {{app_name}}-pipeline-run-
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  pipelineRef:
    name: {{app_name}}-pipeline
  params:
    - name: repo-url
      value: "{{repo_url}}"
    - name: image-ref
      value: "{{image_ref}}"
  workspaces:
    - name: shared-workspace
      volumeClaimTemplate:
        spec:
          accessModes:
            - ReadWriteOnce
          resources:
            requests:
              storage: 1Gi
```

## Verification
- tkn pipeline describe APP — pipeline exists with all steps
- tkn pipeline start APP — runs without errors on valid source
- Each step produces expected artifacts (binary, image, SBOM, scan report)
- Failed tests cause pipeline failure (non-zero exit propagates)
