---
name: image-scan-task
domain: security
version: 1
triggers:
  - scanning
  - vulnerability
  - cve
  - image
  - scan
outputs:
  - Task
property: "Container images are scanned for vulnerabilities before deployment"
mode: template
---

# Image Scan Task — Vulnerability Scanning

## Property
Container images are scanned for known vulnerabilities using Trivy
before deployment, with findings reported to the AgentIT webhook
for tracking and audit.

## Constraints
- Uses Trivy scanner (aquasec/trivy) for CVE detection
- Scans for HIGH and CRITICAL vulnerabilities by default
- Fails the pipeline if CRITICAL vulnerabilities are found
- Posts scan findings to the AgentIT webhook endpoint
- Runs as a Tekton Task with workspace for shared data

## Template

```yaml
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: image-scan
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  params:
    - name: IMAGE
      type: string
      description: Full image reference to scan
    - name: WEBHOOK_URL
      type: string
      description: AgentIT webhook URL for findings
      default: "http://agentit.agentit.svc:8080/api/scan-results"
    - name: SEVERITY
      type: string
      default: "HIGH,CRITICAL"
      description: Minimum severity to report
  steps:
    - name: scan
      image: aquasec/trivy:latest
      script: |
        #!/usr/bin/env sh
        set -e
        trivy image \
          --severity $(params.SEVERITY) \
          --format json \
          --output /tmp/scan-results.json \
          $(params.IMAGE)
        trivy image \
          --severity $(params.SEVERITY) \
          --exit-code 1 \
          $(params.IMAGE) || SCAN_FAILED=true
        echo "Scan complete for $(params.IMAGE)"
        if [ "$SCAN_FAILED" = "true" ]; then
          echo "CRITICAL vulnerabilities found"
        fi
    - name: report
      image: registry.access.redhat.com/ubi9/ubi-minimal:latest
      script: |
        #!/usr/bin/env sh
        set -e
        if [ -f /tmp/scan-results.json ]; then
          curl -s -X POST \
            -H "Content-Type: application/json" \
            -d @/tmp/scan-results.json \
            $(params.WEBHOOK_URL)
          echo "Findings posted to webhook"
        fi
    - name: gate
      image: aquasec/trivy:latest
      script: |
        #!/usr/bin/env sh
        trivy image \
          --severity CRITICAL \
          --exit-code 1 \
          $(params.IMAGE)
```

## Verification
- tkn task describe image-scan — task exists with correct params
- Run task against a known-vulnerable image — should fail on CRITICAL CVEs
- Run task against a clean image — should pass
- Check webhook endpoint received scan results JSON
