---
name: dependency-cronjob
domain: dependency
version: 1
triggers:
  - dependency
  - scan
  - schedule
  - weekly
outputs:
  - CronJob
property: "Dependencies are scanned weekly"
mode: template
---

# Weekly Dependency Scan CronJob

## Property
A CronJob runs weekly to scan container images and application dependencies
for known vulnerabilities, producing a report for the security team.

## Template

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{app_name}}-dependency-scan
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: dependency-scanning
spec:
  schedule: "0 6 * * 1"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 4
  failedJobsHistoryLimit: 2
  jobTemplate:
    spec:
      backoffLimit: 2
      activeDeadlineSeconds: 3600
      template:
        metadata:
          labels:
            app.kubernetes.io/name: {{app_name}}
            app.kubernetes.io/component: dependency-scanning
        spec:
          restartPolicy: OnFailure
          serviceAccountName: {{app_name}}-scanner
          containers:
            - name: dependency-scan
              image: {{scanner_image}}
              resources:
                requests:
                  cpu: 200m
                  memory: 512Mi
                limits:
                  cpu: "1"
                  memory: 1Gi
              env:
                - name: NAMESPACE
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.namespace
                - name: SCAN_SCOPE
                  value: "images,packages"
                - name: SEVERITY_THRESHOLD
                  value: "HIGH"
```

## Notes
- Schedule `0 6 * * 1` = every Monday at 06:00 UTC
- The ServiceAccount needs read access to list pods and pull image metadata
- `SEVERITY_THRESHOLD` controls which findings are flagged (CRITICAL, HIGH, MEDIUM, LOW)
- Replace `{{scanner_image}}` with Trivy, Grype, or your organization's scanner

## Verification
- `kubectl get cronjob {{app_name}}-dependency-scan -n NS` shows schedule and last run
- `kubectl logs job/{{app_name}}-dependency-scan-<id> -n NS` shows scan results
