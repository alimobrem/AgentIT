---
name: data-archive-job
domain: retirement
version: 1
triggers:
  - archive
  - data
  - export
  - retirement
outputs:
  - Job
property: "Application data is archived before decommission"
mode: template
---

# Data Archive Job

## Property
Application data is archived before decommission using a Kubernetes Job
that runs pg_dump for PostgreSQL databases or a generic data export for
other storage backends, ensuring data preservation for compliance and
recovery purposes.

## Constraints
- Job runs to completion with backoffLimit for retry on transient failure
- Database credentials referenced from existing Secrets
- Archive output stored to a PVC or S3-compatible bucket
- Supports PostgreSQL (pg_dump) with fallback for generic data export
- Job has a deadline to prevent indefinite hangs
- Compressed output to minimize storage cost

## Template

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{app_name}}-data-archive
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: data-archive
spec:
  backoffLimit: 3
  activeDeadlineSeconds: 3600
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {{app_name}}
        app.kubernetes.io/component: data-archive
    spec:
      restartPolicy: OnFailure
      containers:
        - name: archive
          image: registry.access.redhat.com/rhel9/postgresql-15:latest
          env:
            - name: PGHOST
              valueFrom:
                secretKeyRef:
                  name: {{app_name}}-db-credentials
                  key: host
            - name: PGPORT
              valueFrom:
                secretKeyRef:
                  name: {{app_name}}-db-credentials
                  key: port
                  optional: true
            - name: PGDATABASE
              valueFrom:
                secretKeyRef:
                  name: {{app_name}}-db-credentials
                  key: database
            - name: PGUSER
              valueFrom:
                secretKeyRef:
                  name: {{app_name}}-db-credentials
                  key: username
            - name: PGPASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{app_name}}-db-credentials
                  key: password
          command:
            - /bin/bash
            - -c
            - |
              set -euo pipefail
              TIMESTAMP=$(date +%Y%m%d-%H%M%S)
              ARCHIVE_DIR="/archive/{{app_name}}"
              mkdir -p "$ARCHIVE_DIR"

              echo "=== Data Archive for {{app_name}} ==="
              echo "Started: $(date -u)"

              if command -v pg_dump &>/dev/null && [ -n "${PGHOST:-}" ]; then
                echo "PostgreSQL detected — running pg_dump"
                pg_dump \
                  --format=custom \
                  --compress=9 \
                  --verbose \
                  --file="$ARCHIVE_DIR/{{app_name}}-${TIMESTAMP}.dump"
                echo "Database archived: $ARCHIVE_DIR/{{app_name}}-${TIMESTAMP}.dump"
                pg_dump \
                  --schema-only \
                  --file="$ARCHIVE_DIR/{{app_name}}-schema-${TIMESTAMP}.sql"
                echo "Schema archived: $ARCHIVE_DIR/{{app_name}}-schema-${TIMESTAMP}.sql"
              else
                echo "No PostgreSQL detected — running generic export"
                echo "Archiving PVC data"
                tar czf "$ARCHIVE_DIR/{{app_name}}-data-${TIMESTAMP}.tar.gz" \
                  /data/ 2>/dev/null || echo "No /data directory found"
              fi

              echo "=== Archive manifest ==="
              ls -lah "$ARCHIVE_DIR/"
              echo "Completed: $(date -u)"
          volumeMounts:
            - name: archive-storage
              mountPath: /archive
            - name: app-data
              mountPath: /data
              readOnly: true
          resources:
            requests:
              cpu: 250m
              memory: 512Mi
            limits:
              cpu: "1"
              memory: 1Gi
      volumes:
        - name: archive-storage
          persistentVolumeClaim:
            claimName: {{app_name}}-archive-pvc
        - name: app-data
          persistentVolumeClaim:
            claimName: {{app_name}}-data
            readOnly: true
```

## Verification
- Job completes successfully: kubectl get job {{app_name}}-data-archive
- Archive file exists on the PVC: kubectl exec into a debug pod and ls /archive/
- For PostgreSQL: pg_restore --list on the dump file confirms tables archived
- Job logs show archive manifest with file sizes
- Job cleans up after TTL (24h)
