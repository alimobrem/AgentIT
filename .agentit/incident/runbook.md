# Incident Response Runbook — agentit

## Triage Steps

1. Confirm the alert source and affected service.
2. Verify pod status: `kubectl get pods -l app=agentit`
3. Check recent logs: `kubectl logs -l app=agentit --tail=100`

## Common Failure Modes

| Mode | Symptom | Likely Cause |
|------|---------|--------------|
| OOMKilled | Pod restarts, exit code 137 | Memory limit too low or leak |
| CrashLoopBackOff | Repeated restarts | Missing config, bad image, startup error |
| ImagePullBackOff | Pod stuck Pending | Bad image ref or missing pull secret |

## Escalation Contacts

| Role | Contact |
|------|---------|
| On-call engineer | `TODO` |
| Service owner | `TODO` |
| Platform team | `TODO` |

## Recovery Procedures

1. Rolling restart: `kubectl rollout restart deployment/agentit`
2. Scale up: `kubectl scale deployment/agentit --replicas=3`
3. Rollback: `kubectl rollout undo deployment/agentit`
