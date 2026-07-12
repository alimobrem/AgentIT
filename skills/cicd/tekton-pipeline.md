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

## Verification
- tkn pipeline describe APP — pipeline exists with all steps
- tkn pipeline start APP — runs without errors on valid source
- Each step produces expected artifacts (binary, image, SBOM, scan report)
- Failed tests cause pipeline failure (non-zero exit propagates)
