---
name: cleanup-task
domain: retirement
version: 1
triggers:
  - cleanup
  - delete
  - resources
  - decommission
outputs:
  - Task
property: "Resources are cleaned up via automated Tekton Task"
mode: template
---

# Cleanup Task — Resource Decommission

## Property
Application resources are cleaned up via an automated Tekton Task that
deletes deployments, services, configmaps, secrets, and other resources
in the app namespace, ensuring no orphaned infrastructure remains.

## Constraints
- Tekton Task runs with a ServiceAccount that has delete permissions
- Deletes resources in a safe order: workloads first, then services, then config
- Logs every deletion for audit trail
- Skips resources with a `retain: "true"` label
- Waits for graceful termination before moving to next resource type

## Template

```yaml
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: {{app_name}}-cleanup
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: retirement
spec:
  params:
    - name: NAMESPACE
      type: string
      description: Namespace to clean up
      default: "{{app_name}}"
    - name: APP_NAME
      type: string
      description: Application name for label selector
      default: "{{app_name}}"
    - name: DRY_RUN
      type: string
      description: Set to "true" for dry-run mode
      default: "false"
  steps:
    - name: validate
      image: registry.access.redhat.com/ubi9/ubi-minimal:latest
      script: |
        #!/usr/bin/env sh
        set -e
        echo "=== Cleanup Task for $(params.APP_NAME) in $(params.NAMESPACE) ==="
        echo "Dry run: $(params.DRY_RUN)"
        echo "Started at: $(date -u)"
    - name: delete-workloads
      image: bitnami/kubectl:latest
      script: |
        #!/usr/bin/env sh
        set -e
        DRY=""
        if [ "$(params.DRY_RUN)" = "true" ]; then DRY="--dry-run=client"; fi
        SELECTOR="app.kubernetes.io/name=$(params.APP_NAME),retain!=true"
        echo "--- Deleting Rollouts ---"
        kubectl delete rollouts -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found || true
        echo "--- Deleting Deployments ---"
        kubectl delete deployments -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found
        echo "--- Deleting StatefulSets ---"
        kubectl delete statefulsets -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found
        echo "--- Deleting Jobs ---"
        kubectl delete jobs -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found
        echo "--- Deleting CronJobs ---"
        kubectl delete cronjobs -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found
        echo "Workloads deleted"
    - name: delete-services
      image: bitnami/kubectl:latest
      script: |
        #!/usr/bin/env sh
        set -e
        DRY=""
        if [ "$(params.DRY_RUN)" = "true" ]; then DRY="--dry-run=client"; fi
        SELECTOR="app.kubernetes.io/name=$(params.APP_NAME),retain!=true"
        echo "--- Deleting Services ---"
        kubectl delete services -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found
        echo "--- Deleting Ingresses ---"
        kubectl delete ingresses -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found
        echo "--- Deleting Routes ---"
        kubectl delete routes -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found || true
        echo "Services and routes deleted"
    - name: delete-config
      image: bitnami/kubectl:latest
      script: |
        #!/usr/bin/env sh
        set -e
        DRY=""
        if [ "$(params.DRY_RUN)" = "true" ]; then DRY="--dry-run=client"; fi
        SELECTOR="app.kubernetes.io/name=$(params.APP_NAME),retain!=true"
        echo "--- Deleting ConfigMaps ---"
        kubectl delete configmaps -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found
        echo "--- Deleting Secrets ---"
        kubectl delete secrets -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found
        echo "--- Deleting PVCs ---"
        kubectl delete pvc -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found
        echo "--- Deleting ServiceAccounts ---"
        kubectl delete serviceaccounts -n $(params.NAMESPACE) -l "$SELECTOR" $DRY --ignore-not-found
        echo "Configuration resources deleted"
    - name: summary
      image: bitnami/kubectl:latest
      script: |
        #!/usr/bin/env sh
        echo "=== Remaining resources in $(params.NAMESPACE) ==="
        kubectl get all -n $(params.NAMESPACE) 2>/dev/null || echo "Namespace clean"
        echo "Cleanup completed at: $(date -u)"
```

## Verification
- tkn task describe {{app_name}}-cleanup — task exists with correct params
- Run with DRY_RUN=true — logs deletions without executing
- Run against a test namespace — all labeled resources removed
- Resources with `retain: "true"` label are preserved
