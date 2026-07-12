---
name: cost-cronjob
domain: cost
version: 1
triggers:
  - cost
  - report
  - schedule
  - weekly
outputs:
  - CronJob
property: "Cost reports are generated weekly"
mode: template
---

# Weekly Cost Report CronJob

## Property
A CronJob runs weekly to collect resource usage data and generate a cost
analysis report, providing continuous visibility into cluster spend.

## Template

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{app_name}}-cost-report
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: cost-reporting
spec:
  schedule: "0 8 * * 1"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 4
  failedJobsHistoryLimit: 2
  jobTemplate:
    spec:
      backoffLimit: 2
      activeDeadlineSeconds: 1800
      template:
        metadata:
          labels:
            app.kubernetes.io/name: {{app_name}}
            app.kubernetes.io/component: cost-reporting
        spec:
          restartPolicy: OnFailure
          serviceAccountName: {{app_name}}-cost-reporter
          containers:
            - name: cost-report
              image: {{cost_report_image}}
              resources:
                requests:
                  cpu: 100m
                  memory: 256Mi
                limits:
                  cpu: 500m
                  memory: 512Mi
              env:
                - name: NAMESPACE
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.namespace
                - name: REPORT_FORMAT
                  value: "json"
```

## Notes
- Schedule `0 8 * * 1` = every Monday at 08:00 UTC
- The ServiceAccount needs read access to pod metrics and resource usage
- Adjust `successfulJobsHistoryLimit` to retain more historical reports

## Verification
- `kubectl get cronjob {{app_name}}-cost-report -n NS` shows next scheduled run
- `kubectl get jobs -l app.kubernetes.io/component=cost-reporting -n NS` shows completed runs
