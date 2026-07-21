from __future__ import annotations

import logging
import time as _time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from agentit.portal.helpers import kube_breaker

logger = logging.getLogger(__name__)


class KubeError(Exception):
    """Raised when a Kubernetes API call fails (not a missing-resource 404)."""


class KubeOfflineError(KubeError):
    """Raised specifically by ``get_client()`` when ``AGENTIT_OFFLINE`` is
    set. A distinct subclass (rather than a plain ``KubeError``) so every
    real API-calling function below can tell "the explicit offline
    hard-stop fired" apart from "a real Kubernetes API call actually
    failed" -- see ``_kube_breaker_scope()``, which deliberately never
    counts this against ``kube_breaker``: offline mode is an intentional,
    explicit choice, not evidence the cluster is unhealthy.
    """


@contextmanager
def _kube_breaker_scope(*, benign_statuses: tuple[int, ...] = ()):
    """Shared low-level choke point for ``kube_breaker`` bookkeeping around
    a single real Kubernetes API call.

    Every real-API-calling function below wraps its actual client call
    with this instead of duplicating the same success/failure bookkeeping
    (and the ``AGENTIT_OFFLINE`` carve-out) in each one -- mirrors
    ``llm.py``'s ``LLMClient._chat()`` pattern (``record_success()`` on a
    clean call, ``record_failure()`` on a real exception), just
    centralized since this module has many more real call sites than
    ``llm.py``'s single one.

    ``benign_statuses`` lets a caller mark specific HTTP status codes as
    *expected, not a health signal* (e.g. 404 "not found" for a lookup,
    409 "already exists"/"conflict" for a create) -- those still
    propagate to the caller's own except-clause exactly as before, they
    just don't move the breaker.
    """
    try:
        yield
    except KubeOfflineError:
        # Explicit, intentional offline mode -- not evidence the real
        # cluster is unhealthy, so this must never move the breaker.
        raise
    except Exception as exc:
        if getattr(exc, "status", None) in benign_statuses:
            kube_breaker.record_success()
        else:
            kube_breaker.record_failure()
        raise
    else:
        kube_breaker.record_success()


_client_cache = None
_client_cache_time: float = 0
_client_cache_source: str | None = None
_CLIENT_TTL = 600  # 10 minutes


def _offline_mode_enabled() -> bool:
    """True when ``AGENTIT_OFFLINE`` is set truthy -- see ``get_client()``'s
    docstring for exactly what this guarantees and why it exists."""
    import os
    return os.environ.get("AGENTIT_OFFLINE", "").lower() in ("1", "true", "on")


def get_client():
    """Get a configured kubernetes client. Auto-detects in-cluster vs kubeconfig.

    Cached with a 10-minute TTL so bound service-account tokens (which rotate
    hourly) are picked up after expiry.

    Checks ``AGENTIT_OFFLINE`` FIRST -- before the cache lookup and before
    any config-resolution attempt -- and raises ``KubeError`` immediately if
    it's set, rather than resolving a real client. This exists because
    unsetting ``KUBECONFIG`` alone is NOT a reliable "zero cluster access"
    guarantee: two independent live reviews confirmed the Kubernetes Python
    client's default config-resolution chain (``config.load_kube_config()``)
    still falls back to the ambient default ``~/.kube/config`` regardless of
    ``KUBECONFIG`` being unset, silently connecting to whatever real cluster
    that machine's default kubeconfig happens to point at. Set
    ``AGENTIT_OFFLINE=1`` (see the README's Configuration table) for a
    genuine hard-offline guarantee during local testing/review.
    """
    if _offline_mode_enabled():
        raise KubeOfflineError(
            "AGENTIT_OFFLINE is set -- refusing to resolve a Kubernetes client "
            "(in-cluster config, kubeconfig, or otherwise). Unset AGENTIT_OFFLINE "
            "to allow real cluster access."
        )
    global _client_cache, _client_cache_time, _client_cache_source
    now = _time.monotonic()
    if _client_cache is not None and (now - _client_cache_time) < _CLIENT_TTL:
        return _client_cache
    from kubernetes import client, config

    try:
        config.load_incluster_config()
        _client_cache_source = "in-cluster"
    except config.ConfigException:
        config.load_kube_config()
        _client_cache_source = "kubeconfig"
    _client_cache = client
    _client_cache_time = now
    return client


def get_current_cluster_identity() -> dict:
    """Best-effort, side-effect-free identification of whichever cluster
    ``get_client()`` currently resolves to -- read back from the already-
    resolved client configuration, never a live call to the API server
    itself, so this is always safe to call from a request path even when
    the target cluster turns out to be completely unreachable.

    Exists so a human sees *where* (not just *what*) before approving a
    destructive action -- see ``portal/delivery.py``'s ``confirmation_text()``,
    which surfaces this in the direct-apply confirmation message. Fixes the
    incident where a customer-review agent expected zero cluster access
    after ``unset KUBECONFIG`` and instead silently hit whatever cluster the
    ambient kubeconfig happened to point at, with no on-screen indication of
    which cluster that was.

    Returns a dict:
      - "label": a short human-readable string, safe to interpolate
        directly into a confirmation message, e.g.
        ``"https://api.example.com:6443 (context: my-cluster)"``,
        ``"in-cluster (this pod's own cluster)"``, or
        ``"unknown/unreachable cluster"`` when nothing could be resolved.
      - "host": the API server URL, or ``None``.
      - "context": the active kubeconfig context name, or ``None`` -- always
        ``None`` for in-cluster config (no context concept) or when it
        couldn't be determined.
      - "in_cluster": ``True`` only when resolved via in-cluster config.
    """
    try:
        get_client()
    except Exception as exc:
        logger.warning("Could not resolve current cluster identity (no reachable cluster?): %s", exc)
        return {"label": "unknown/unreachable cluster", "host": None, "context": None, "in_cluster": False}

    in_cluster = _client_cache_source == "in-cluster"

    host = None
    try:
        from kubernetes import client as _client
        host = _client.Configuration.get_default_copy().host
    except Exception as exc:
        logger.debug("Could not read API host from client configuration: %s", exc)

    context_name = None
    if not in_cluster:
        try:
            from kubernetes import config as _config
            _, active_context = _config.list_kube_config_contexts()
            context_name = (active_context or {}).get("name")
        except Exception as exc:
            logger.debug("Could not read active kubeconfig context: %s", exc)

    if in_cluster:
        label = "in-cluster (this pod's own cluster)"
    elif host and context_name:
        label = f"{host} (context: {context_name})"
    elif host:
        label = host
    else:
        label = "unknown/unreachable cluster"

    return {"label": label, "host": host, "context": context_name, "in_cluster": in_cluster}


_dynamic_client_cache = None
_dynamic_client_cache_time: float = 0


def dynamic_client():
    """Get a cached kubernetes dynamic client (generic apiVersion/kind ->
    REST-path resolution + server-side-apply support), used by
    ``apply_yaml()`` below. Shares ``get_client()``'s 10-minute TTL so a
    refreshed in-cluster service-account token is picked up the same way.
    """
    global _dynamic_client_cache, _dynamic_client_cache_time
    now = _time.monotonic()
    if _dynamic_client_cache is not None and (now - _dynamic_client_cache_time) < _CLIENT_TTL:
        return _dynamic_client_cache
    from kubernetes import dynamic

    api_client = get_client().ApiClient()
    _dynamic_client_cache = dynamic.DynamicClient(api_client)
    _dynamic_client_cache_time = now
    return _dynamic_client_cache


def core_v1():
    return get_client().CoreV1Api()


def apps_v1():
    return get_client().AppsV1Api()


def custom_objects():
    return get_client().CustomObjectsApi()


def batch_v1():
    return get_client().BatchV1Api()


def list_pods(namespace: str, label_selector: str = "") -> list[dict]:
    """List pods in a namespace, returns simplified dicts."""
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping list_pods(%s)", namespace)
        raise KubeError("Kubernetes circuit breaker open — too many recent API failures")
    try:
        with _kube_breaker_scope():
            pods = core_v1().list_namespaced_pod(namespace, label_selector=label_selector, _request_timeout=10)
        return [
            {
                "name": p.metadata.name,
                "status": p.status.phase,
                "restarts": sum(cs.restart_count for cs in (p.status.container_statuses or [])),
                "age": p.metadata.creation_timestamp.isoformat()[:16] if p.metadata.creation_timestamp else "",
                "ready": all(cs.ready for cs in (p.status.container_statuses or [])),
                "crash_looping": any(
                    cs.state and cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff"
                    for cs in (p.status.container_statuses or [])
                ),
                "container_statuses": p.status.container_statuses or [],
                # The controlling owner's kind (e.g. "TaskRun", "ReplicaSet", "Job"),
                # or None if the pod has no controller owner. Lets callers tell a
                # one-shot Tekton task pod apart from an actual long-running
                # service pod without a second API call.
                "owner_kind": next(
                    (o.kind for o in (p.metadata.owner_references or []) if o.controller),
                    None,
                ),
            }
            for p in pods.items
        ]
    except Exception as exc:
        raise KubeError(f"Failed to list pods in {namespace}: {exc}") from exc


def get_pod_count(namespace: str) -> tuple[int, int]:
    """Returns (running_count, failed_count). Raises KubeError on API failure."""
    pods = list_pods(namespace)
    running = sum(1 for p in pods if p["status"] == "Running")
    failed = sum(1 for p in pods if p["status"] == "Failed" or p.get("crash_looping", False))
    return running, failed


def count_stale_terminal_pods(namespace: str, max_age_hours: float = 2.0) -> int:
    """Count Failed/Succeeded pods older than ``max_age_hours`` in a
    namespace -- a generic proxy for "is this namespace's terminal-pod
    cleanup actually keeping up", independent of *why* cleanup might not
    be working (a CronJob's Job can exit 0 while its own pruning logic
    silently does nothing -- see docs/cicd-stall-hardening-2026-07-17.md).
    Used by the self-health-check watcher's cleanup-effectiveness check.

    Reads raw pod timestamps directly (rather than going through
    ``list_pods()``'s simplified ``age`` string, which is truncated to
    minute precision with no timezone for display purposes) so the age
    comparison here is exact.
    """
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping count_stale_terminal_pods(%s)", namespace)
        raise KubeError("Kubernetes circuit breaker open — too many recent API failures")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    try:
        with _kube_breaker_scope():
            pods = core_v1().list_namespaced_pod(namespace, _request_timeout=10)
        count = 0
        for p in pods.items:
            if p.status.phase not in ("Failed", "Succeeded"):
                continue
            created = p.metadata.creation_timestamp
            if created is not None and created < cutoff:
                count += 1
        return count
    except Exception as exc:
        raise KubeError(f"Failed to count stale terminal pods in {namespace}: {exc}") from exc


def list_custom_resources(
    group: str, version: str, plural: str, namespace: str = "", timeout: int = 10,
) -> list[dict]:
    """List custom resources. Returns raw dicts.

    ``timeout`` is the per-call kube-apiserver deadline (seconds). Hot paths
    like the ambient deploy-status badge pass a shorter value so a wedged
    apiserver cannot pin portal workers until oauth-proxy returns 502/503.
    """
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping list_custom_resources(%s/%s %s)", group, version, plural)
        raise KubeError("Kubernetes circuit breaker open — too many recent API failures")
    try:
        with _kube_breaker_scope():
            if namespace:
                result = custom_objects().list_namespaced_custom_object(
                    group, version, namespace, plural, _request_timeout=timeout,
                )
            else:
                result = custom_objects().list_cluster_custom_object(
                    group, version, plural, _request_timeout=timeout,
                )
        return result.get("items", [])
    except Exception as exc:
        raise KubeError(f"Failed to list {group}/{version} {plural}: {exc}") from exc


def get_custom_resource(
    group: str, version: str, plural: str, name: str, namespace: str = "", timeout: int = 10,
) -> dict | None:
    """Get a single custom resource by name. Returns None if not found (404)."""
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping get_custom_resource(%s/%s %s/%s)", group, version, plural, name)
        raise KubeError("Kubernetes circuit breaker open — too many recent API failures")
    try:
        with _kube_breaker_scope(benign_statuses=(404,)):
            if namespace:
                return custom_objects().get_namespaced_custom_object(
                    group, version, namespace, plural, name, _request_timeout=timeout,
                )
            return custom_objects().get_cluster_custom_object(
                group, version, plural, name, _request_timeout=timeout,
            )
    except Exception as exc:
        if hasattr(exc, "status") and exc.status == 404:
            return None
        raise KubeError(f"Failed to get {group}/{version} {plural}/{name}: {exc}") from exc


def create_custom_resource(group: str, version: str, plural: str, namespace: str, body: dict) -> dict:
    """Create a namespaced custom resource. Raises KubeError on failure (including 'already exists')."""
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping create_custom_resource(%s/%s %s)", group, version, plural)
        raise KubeError("Kubernetes circuit breaker open — too many recent API failures")
    try:
        with _kube_breaker_scope():
            return custom_objects().create_namespaced_custom_object(group, version, namespace, plural, body, _request_timeout=15)
    except Exception as exc:
        raise KubeError(f"Failed to create {group}/{version} {plural}: {exc}") from exc


def patch_custom_resource(group: str, version: str, plural: str, name: str, namespace: str, body: dict) -> dict:
    """Merge-patch an existing namespaced custom resource. Raises KubeError on failure."""
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping patch_custom_resource(%s/%s %s/%s)", group, version, plural, name)
        raise KubeError("Kubernetes circuit breaker open — too many recent API failures")
    try:
        with _kube_breaker_scope():
            return custom_objects().patch_namespaced_custom_object(group, version, namespace, plural, name, body, _request_timeout=15)
    except Exception as exc:
        raise KubeError(f"Failed to patch {group}/{version} {plural}/{name}: {exc}") from exc


DEFAULT_FIELD_MANAGER = "agentit"


def _ssa_conflict_message(exc) -> str:
    """Best-effort human-readable message from a 409 server-side-apply
    conflict response. The apiserver's error body is JSON with a `message`
    field naming the conflicting field manager(s) and path(s)
    (e.g. `Apply failed with 1 conflict: conflict with "kubectl-client-side-apply"
    using v1: .data.foo`) -- fall back to `str(exc)` if the body isn't the
    shape we expect (defensive against apiserver version differences)."""
    import json

    try:
        body = json.loads(exc.body) if exc.body else {}
        return body.get("message") or str(exc)
    except (TypeError, ValueError, AttributeError):
        return str(exc)


def apply_yaml(
    content: str, namespace: str, *,
    field_manager: str = DEFAULT_FIELD_MANAGER, force: bool = False, dry_run: bool = False,
) -> dict:
    """Apply a YAML manifest via real per-field-manager server-side-apply.

    `content` is an arbitrary, multi-document manifest spanning both
    core/typed kinds (ConfigMap, Namespace, ...) and CRDs (Application,
    Rollout, Subscription, ...) -- each document is resolved to its REST
    resource via the kubernetes client's dynamic-client discovery, then
    applied with `content_type="application/apply-patch+yaml"` and
    `field_manager=field_manager`, matching `oc apply --server-side`'s wire
    format but through the real Python client instead of an `oc` subprocess.

    `force` defaults to `False` and is passed through as SSA's `force`
    query parameter (`force_conflicts` in the dynamic client) only when the
    caller explicitly opts in -- on a genuine field-manager conflict (HTTP
    409, `reason=Conflict`) this does NOT silently retry with force; it
    returns a structured, distinguishable result instead so callers can
    route the conflict to a human-reviewed gate rather than either
    failing silently or seizing ownership from another manager.

    `dry_run` defaults to `False` and, when `True`, passes the Kubernetes
    API's own `dryRun=All` query parameter through to the server-side-apply
    call -- the apiserver validates and admission-checks the request exactly
    as it would for a real apply (missing CRDs, RBAC denials, schema/
    admission-webhook rejections, quota, ...) but never persists it. This
    is what makes "Dry Run" in `cluster_apply.py` a real dry run instead of
    only checking that a manifest has a recognizable `kind` -- see that
    module's `apply_manifests_to_cluster()` for the caller.

    Returns a dict:
      - applied: True only if every document in `content` applied cleanly.
      - error: a short message (first failure) when `applied` is False.
      - errors: per-document failure messages (non-conflict). Callers that
        classify hard vs soft dry-run failures (Forbidden vs Bad Request)
        must use this list — a soft failure on doc 1 must not hide a hard
        failure on doc 2 if only ``error`` (first) were consulted.
      - conflict: True when the failure was purely field-manager conflict(s)
        (never true when `applied` is True, and never true alongside a
        non-conflict failure -- a hard error takes precedence for `error`/
        `conflict` since it needs attention regardless of ownership).
      - conflict_details: per-document conflict info (kind/name/namespace/
        apiserver message), empty unless `conflict` is True. One `content`
        call can contain multiple documents (see above), so this is a list,
        not a single conflict -- matching this function's existing
        per-*file* (not per-document) result granularity used throughout
        `cluster_apply.py`.
    """
    import yaml as _yaml
    from kubernetes.client.exceptions import ApiException

    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping apply_yaml(%s)", namespace)
        msg = "Kubernetes circuit breaker open — too many recent API failures"
        return {
            "applied": False,
            "error": msg,
            "errors": [msg],
            "conflict": False, "conflict_details": [],
        }

    try:
        docs = [d for d in _yaml.safe_load_all(content) if isinstance(d, dict)]
    except _yaml.YAMLError as exc:
        msg = f"invalid YAML: {exc}"
        return {
            "applied": False, "error": msg, "errors": [msg],
            "conflict": False, "conflict_details": [],
        }

    if not docs:
        return {
            "applied": True, "error": None, "errors": [],
            "conflict": False, "conflict_details": [],
        }

    with _kube_breaker_scope():
        dyn = dynamic_client()
    conflict_details: list[dict] = []
    doc_errors: list[str] = []
    first_error: str | None = None
    any_conflict = False
    any_failure = False

    for doc in docs:
        kind = doc.get("kind", "")
        api_version = doc.get("apiVersion", "")
        meta = doc.get("metadata") or {}
        name = meta.get("name", "")
        doc_namespace = meta.get("namespace") or namespace

        if not kind or not api_version or not name:
            any_failure = True
            msg = f"document missing kind/apiVersion/name (kind={kind!r}, name={name!r})"
            doc_errors.append(msg)
            first_error = first_error or msg
            continue

        try:
            with _kube_breaker_scope():
                resource = dyn.resources.get(api_version=api_version, kind=kind)
        except Exception as exc:
            any_failure = True
            msg = f"{kind} ({api_version}) not found on cluster: {exc}"
            doc_errors.append(msg)
            first_error = first_error or msg
            continue

        try:
            with _kube_breaker_scope(benign_statuses=(409,)):
                dyn.server_side_apply(
                    resource, body=doc, name=name,
                    namespace=doc_namespace if resource.namespaced else None,
                    field_manager=field_manager, force_conflicts=force,
                    dry_run="All" if dry_run else None,
                    _request_timeout=30,
                )
        except ApiException as exc:
            if exc.status == 409:
                any_conflict = True
                message = _ssa_conflict_message(exc)
                conflict_details.append({
                    "kind": kind, "name": name, "namespace": doc_namespace, "message": message,
                })
                first_error = first_error or f"{kind}/{name}: field-manager conflict -- {message}"
            else:
                any_failure = True
                msg = f"{kind}/{name}: {exc.reason or exc.body or exc}"
                doc_errors.append(msg)
                first_error = first_error or msg
        except Exception as exc:
            any_failure = True
            msg = f"{kind}/{name}: {exc}"
            doc_errors.append(msg)
            first_error = first_error or msg

    if not any_failure and not any_conflict:
        return {
            "applied": True, "error": None, "errors": [],
            "conflict": False, "conflict_details": [],
        }

    return {
        "applied": False,
        "error": first_error,
        "errors": doc_errors,
        "conflict": any_conflict and not any_failure,
        "conflict_details": conflict_details if (any_conflict and not any_failure) else [],
    }


def _get_argo_rollout(name: str, namespace: str) -> dict | None:
    """Return the Argo Rollout custom resource with this name, or None if it
    doesn't exist (or the Rollout CRD isn't installed on this cluster)."""
    try:
        with _kube_breaker_scope(benign_statuses=(404,)):
            return custom_objects().get_namespaced_custom_object(
                "argoproj.io", "v1alpha1", namespace, "rollouts", name, _request_timeout=10,
            )
    except Exception:
        return None


def rollout_undo(deployment: str, namespace: str) -> dict:
    """Roll back a deployment to its previous stable state.

    For Argo Rollouts-managed apps (kind ``Rollout``, group ``argoproj.io``),
    this patches the Rollout's ``status`` subresource with ``{"abort": true}``
    -- the same mechanism ``kubectl argo rollouts abort`` uses -- which halts
    the canary and reverts traffic to the previous stable ReplicaSet.

    For plain Deployments (no matching Rollout found), apps/v1 does not
    support ``spec.rollbackTo`` (that was extensions/v1beta1), so this falls
    back to a restart via a pod-template annotation, equivalent to
    ``kubectl rollout restart``. That is NOT a true rollback -- it re-deploys
    the current spec rather than reverting to a previous ReplicaSet -- but is
    the best available fallback for non-Rollout resources.
    """
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping rollout_undo(%s/%s)", namespace, deployment)
        return {"success": False, "message": "Kubernetes circuit breaker open — too many recent API failures"}

    if _get_argo_rollout(deployment, namespace) is not None:
        try:
            with _kube_breaker_scope():
                custom_objects().patch_namespaced_custom_object_status(
                    "argoproj.io", "v1alpha1", namespace, "rollouts", deployment,
                    body={"status": {"abort": True}}, _request_timeout=15,
                )
            return {
                "success": True,
                "message": f"Argo Rollout '{deployment}' aborted -- reverted to previous stable ReplicaSet",
            }
        except Exception as exc:
            logger.warning("Rollout abort failed for %s/%s: %s", namespace, deployment, exc)
            return {"success": False, "message": str(exc)}

    from datetime import datetime, timezone

    try:
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat(),
                        }
                    }
                }
            }
        }
        with _kube_breaker_scope():
            apps_v1().patch_namespaced_deployment(deployment, namespace, body, _request_timeout=15)
        return {
            "success": True,
            "message": f"Rollout restart initiated for {deployment} "
                        "(no Argo Rollout found -- restarted the Deployment instead of a true rollback)",
        }
    except Exception as exc:
        logger.warning("Rollout restart failed for %s/%s: %s", namespace, deployment, exc)
        return {"success": False, "message": str(exc)}


def get_api_resources() -> set[str]:
    """Get available API resource kinds across ALL API groups on the cluster.

    Queries the core v1 group (Pods, Services, ConfigMaps, ...) plus every
    named API group (apps, networking.k8s.io, autoscaling, batch, policy,
    ...) via the discovery API, so ``PlatformContext.has_api()`` correctly
    reports availability for Deployments, Ingresses, HPAs, NetworkPolicies,
    etc. -- not just core resources.
    """
    import json

    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping get_api_resources()")
        raise KubeError("Kubernetes circuit breaker open — too many recent API failures")

    try:
        resources = set()
        with _kube_breaker_scope():
            api_client = get_client().ApiClient()
            api_resources = core_v1().get_api_resources(_request_timeout=10).resources
        for api in api_resources:
            resources.add(api.kind.lower())

        with _kube_breaker_scope():
            groups = get_client().ApisApi(api_client).get_api_versions(_request_timeout=10)
        for group in groups.groups:
            version = None
            if group.preferred_version is not None:
                version = group.preferred_version.version
            elif group.versions:
                version = group.versions[0].version
            if not version:
                continue
            try:
                # auth_settings is required: without BearerToken, call_api
                # hits the apiserver as system:anonymous even when
                # load_incluster_config() succeeded (typed clients still
                # authenticate). That previously left only core v1 kinds
                # (~26) in the set, so SkillEngine gated out HPA /
                # NetworkPolicy / Deployment skills on every onboard.
                with _kube_breaker_scope():
                    resp = api_client.call_api(
                        f"/apis/{group.name}/{version}", "GET",
                        auth_settings=["BearerToken"],
                        _return_http_data_only=True, _preload_content=False,
                        _request_timeout=10,
                    )
                data = json.loads(resp.read())
                for res in data.get("resources", []):
                    kind = res.get("kind")
                    if kind:
                        resources.add(kind.lower())
            except Exception as exc:
                logger.debug("Failed to list resources for group %s/%s: %s", group.name, version, exc)

        return resources
    except Exception as exc:
        raise KubeError(f"Failed to get API resources: {exc}") from exc


def create_config_map(name: str, namespace: str, data: dict[str, str]) -> bool:
    """Create a ConfigMap. Returns True on success."""
    from kubernetes.client import V1ConfigMap, V1ObjectMeta
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping create_config_map(%s/%s)", namespace, name)
        return False
    try:
        cm = V1ConfigMap(
            metadata=V1ObjectMeta(name=name, namespace=namespace),
            data=data,
        )
        with _kube_breaker_scope():
            core_v1().create_namespaced_config_map(namespace, cm)
        return True
    except Exception as exc:
        if "already exists" in str(exc).lower():
            with _kube_breaker_scope():
                core_v1().replace_namespaced_config_map(name, namespace, V1ConfigMap(
                    metadata=V1ObjectMeta(name=name, namespace=namespace),
                    data=data,
                ))
            return True
        logger.warning("Failed to create ConfigMap %s: %s", name, exc)
        return False


def delete_config_map(name: str, namespace: str) -> None:
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping delete_config_map(%s/%s)", namespace, name)
        return
    try:
        with _kube_breaker_scope(benign_statuses=(404,)):
            core_v1().delete_namespaced_config_map(name, namespace)
    except Exception:
        logger.debug("delete_config_map %s/%s failed", namespace, name, exc_info=True)


def get_current_pod_image() -> str | None:
    """Auto-detect the image of the current pod (when running in-cluster)."""
    import os
    hostname = os.environ.get("HOSTNAME", "")
    if not hostname:
        return None
    namespace = os.environ.get("AGENTIT_NAMESPACE", "agentit")
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping get_current_pod_image()")
        return None
    try:
        with _kube_breaker_scope():
            pod = core_v1().read_namespaced_pod(hostname, namespace)
        if pod.spec.containers:
            return pod.spec.containers[0].image
    except Exception as exc:
        logger.warning("Failed to auto-detect pod image: %s", exc)
    return None


def create_job(
    name: str,
    namespace: str,
    image: str,
    command: list[str],
    config_map_name: str | None = None,
    config_map_mount: str = "/input",
    active_deadline: int = 300,
    backoff_limit: int = 1,
    labels: dict[str, str] | None = None,
    resources: dict[str, str] | None = None,
) -> bool:
    """Create a K8s Job. Returns True on success."""
    from kubernetes.client import (
        V1Job, V1JobSpec, V1ObjectMeta, V1PodTemplateSpec, V1PodSpec,
        V1Container, V1ResourceRequirements, V1SecurityContext,
        V1Volume, V1VolumeMount, V1ConfigMapVolumeSource,
        V1EnvVar,
    )

    volumes = []
    volume_mounts = []
    if config_map_name:
        volumes.append(V1Volume(
            name="input",
            config_map=V1ConfigMapVolumeSource(name=config_map_name),
        ))
        volume_mounts.append(V1VolumeMount(
            name="input", mount_path=config_map_mount, read_only=True,
        ))

    all_labels = {"app.kubernetes.io/component": "agent", "agentit/job": name}
    if labels:
        all_labels.update(labels)

    res = resources or {}
    cpu_req = res.get("cpu_req", "100m")
    cpu_lim = res.get("cpu_lim", "500m")
    mem_req = res.get("mem_req", "256Mi")
    mem_lim = res.get("mem_lim", "512Mi")

    job = V1Job(
        metadata=V1ObjectMeta(name=name, namespace=namespace, labels=all_labels),
        spec=V1JobSpec(
            active_deadline_seconds=active_deadline,
            backoff_limit=backoff_limit,
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels=all_labels),
                spec=V1PodSpec(
                    restart_policy="Never",
                    containers=[V1Container(
                        name="agent",
                        image=image,
                        command=command,
                        volume_mounts=volume_mounts or None,
                        resources=V1ResourceRequirements(
                            requests={"cpu": cpu_req, "memory": mem_req},
                            limits={"cpu": cpu_lim, "memory": mem_lim},
                        ),
                        security_context=V1SecurityContext(
                            allow_privilege_escalation=False,
                            run_as_non_root=True,
                        ),
                        env=[V1EnvVar(name="PYTHONUNBUFFERED", value="1")],
                    )],
                    volumes=volumes or None,
                ),
            ),
        ),
    )

    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping create_job(%s/%s)", namespace, name)
        return False
    try:
        with _kube_breaker_scope():
            batch_v1().create_namespaced_job(namespace, job)
        return True
    except Exception as exc:
        logger.warning("Failed to create Job %s: %s", name, exc)
        return False


def get_job_status(name: str, namespace: str) -> str:
    """Get Job status: 'active', 'succeeded', 'failed', or 'unknown'."""
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping get_job_status(%s/%s)", namespace, name)
        return "unknown"
    try:
        with _kube_breaker_scope():
            job = batch_v1().read_namespaced_job_status(name, namespace, _request_timeout=10)
        if job.status.succeeded and job.status.succeeded > 0:
            return "succeeded"
        if job.status.failed and job.status.failed > 0:
            return "failed"
        if job.status.active and job.status.active > 0:
            return "active"
        return "unknown"
    except Exception as exc:
        logger.warning("Failed to get Job status %s: %s", name, exc)
        return "unknown"


def get_job_pod_log(job_name: str, namespace: str) -> str:
    """Read logs from the pod created by a Job."""
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping get_job_pod_log(%s/%s)", namespace, job_name)
        return ""
    try:
        with _kube_breaker_scope():
            pods = core_v1().list_namespaced_pod(
                namespace, label_selector=f"agentit/job={job_name}", _request_timeout=10,
            )
        if not pods.items:
            return ""
        pod_name = pods.items[0].metadata.name
        with _kube_breaker_scope():
            return core_v1().read_namespaced_pod_log(pod_name, namespace, _request_timeout=30)
    except Exception as exc:
        logger.warning("Failed to read Job pod log %s: %s", job_name, exc)
        return ""


def delete_job(name: str, namespace: str) -> None:
    """Delete a Job and its pods."""
    from kubernetes.client import V1DeleteOptions
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping delete_job(%s/%s)", namespace, name)
        return
    try:
        with _kube_breaker_scope(benign_statuses=(404,)):
            batch_v1().delete_namespaced_job(
                name, namespace,
                body=V1DeleteOptions(propagation_policy="Background"),
            )
    except Exception:
        logger.debug("delete_job %s/%s failed", namespace, name, exc_info=True)


def list_cronjobs(namespace: str) -> list[dict]:
    """List CronJobs in a namespace, returns simplified dicts.

    Backs the self-health-check watcher's "are my own maintenance
    CronJobs actually running and succeeding" check
    (``watchers/self_health_check.py``) -- it needs to reason about *any*
    CronJob this chart currently ships (``tekton-cleanup``,
    ``secret-rotation``, the fleet-rescan CronJobs, ...) without a
    hardcoded name list that silently goes stale as new ones are added,
    so this lists everything in the namespace rather than one name at a
    time. Uses the namespace-scoped ``edit`` ClusterRole already bound to
    every watcher's ServiceAccount (``rbac.yaml``) -- no new RBAC grant.
    """
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping list_cronjobs(%s)", namespace)
        raise KubeError("Kubernetes circuit breaker open — too many recent API failures")
    try:
        with _kube_breaker_scope():
            cronjobs = batch_v1().list_namespaced_cron_job(namespace, _request_timeout=10)
        return [
            {
                "name": cj.metadata.name,
                "schedule": cj.spec.schedule,
                "suspended": bool(cj.spec.suspend),
                "last_schedule_time": cj.status.last_schedule_time.isoformat() if cj.status.last_schedule_time else None,
                "last_successful_time": cj.status.last_successful_time.isoformat() if cj.status.last_successful_time else None,
                "active_count": len(cj.status.active or []),
            }
            for cj in cronjobs.items
        ]
    except Exception as exc:
        raise KubeError(f"Failed to list CronJobs in {namespace}: {exc}") from exc


def namespace_exists(namespace: str) -> bool:
    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping namespace_exists(%s)", namespace)
        raise KubeError("Kubernetes circuit breaker open — too many recent API failures")
    try:
        with _kube_breaker_scope(benign_statuses=(404,)):
            core_v1().read_namespace(namespace, _request_timeout=5)
        return True
    except Exception as exc:
        if hasattr(exc, "status") and exc.status == 404:
            return False
        raise KubeError(f"Failed to check namespace {namespace}: {exc}") from exc


def create_namespace(namespace: str) -> None:
    from kubernetes.client import V1Namespace, V1ObjectMeta

    if kube_breaker.is_open:
        logger.warning("Kube circuit breaker open — skipping create_namespace(%s)", namespace)
        raise KubeError("Kubernetes circuit breaker open — too many recent API failures")
    try:
        with _kube_breaker_scope(benign_statuses=(409,)):
            core_v1().create_namespace(V1Namespace(metadata=V1ObjectMeta(name=namespace)), _request_timeout=10)
    except Exception as exc:
        if hasattr(exc, "status") and exc.status == 409:
            return
        raise KubeError(f"Failed to create namespace {namespace}: {exc}") from exc
