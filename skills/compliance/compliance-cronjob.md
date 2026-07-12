---
name: compliance-cronjob
domain: compliance
version: 1
triggers:
  - compliance
  - rescan
  - periodic
  - schedule
outputs:
  - CronJob
property: "Compliance posture is re-assessed monthly"
mode: template
---

# Compliance Re-assessment CronJob

## Property
A monthly CronJob runs `agentit assess` against the application,
ensuring compliance posture is continuously validated and drift
from desired state is detected.

## Constraints
- Schedule: monthly (1st of each month at 02:00 UTC)
- Runs agentit assess targeting the application's namespace
- Retains last 3 successful and 1 failed job for debugging
- Uses a ServiceAccount with read access to the namespace

## Template

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{app_name}}-compliance-rescan
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  schedule: "0 2 1 * *"
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        metadata:
          labels:
            app.kubernetes.io/name: {{app_name}}
            app.kubernetes.io/component: compliance-rescan
        spec:
          serviceAccountName: {{app_name}}-compliance-sa
          restartPolicy: OnFailure
          containers:
            - name: assess
              image: agentit:latest
              command: ["agentit", "assess"]
              args:
                - "--namespace"
                - "{{namespace}}"
                - "--output"
                - "/results/report.json"
              volumeMounts:
                - name: results
                  mountPath: /results
          volumes:
            - name: results
              emptyDir: {}
```

## Verification
- `oc get cronjob {{app_name}}-compliance-rescan` shows the job with correct schedule
- After manual trigger (`oc create job --from=cronjob/{{app_name}}-compliance-rescan test-run`), job completes and produces a report
