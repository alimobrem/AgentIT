---
name: sbom-task
domain: compliance
version: 1
triggers:
  - sbom
  - bom
  - software
  - bill
outputs:
  - Task
property: "Software Bill of Materials is generated for every build"
mode: template
---

# SBOM Generation Task

> **Does not clear compliance `sbom`.** Use `sbom-artifact` for source PRs.

## Property
Every container image build produces a CycloneDX Software Bill of
Materials, enabling license compliance checks and vulnerability
correlation against known CVE databases.

## Constraints
- Uses syft for SBOM generation (anchore/syft — pinned tag, not `:latest`)
- Output format: CycloneDX JSON
- Runs against the built container image (not source tree)
- SBOM artifact is stored as a Tekton result for downstream consumption

## Template

```yaml
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: {{app_name}}-sbom
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  params:
    - name: IMAGE
      description: Container image reference to scan
      type: string
  results:
    - name: SBOM_PATH
      description: Path to the generated SBOM file
  steps:
    - name: generate-sbom
      image: anchore/syft:v1.48.0
      args:
        - $(params.IMAGE)
        - --output
        - cyclonedx-json=/workspace/sbom.json
      volumeMounts:
        - name: workspace
          mountPath: /workspace
    - name: report-result
      image: registry.access.redhat.com/ubi9-minimal:latest
      script: |
        #!/usr/bin/env sh
        echo -n "/workspace/sbom.json" > $(results.SBOM_PATH.path)
        echo "SBOM generated: $(wc -c < /workspace/sbom.json) bytes"
      volumeMounts:
        - name: workspace
          mountPath: /workspace
  volumes:
    - name: workspace
      emptyDir: {}
```

## Verification
- Task run completes successfully and SBOM_PATH result is set
- `cat sbom.json | jq '.bomFormat'` returns `"CycloneDX"`
- SBOM contains component entries for application dependencies
