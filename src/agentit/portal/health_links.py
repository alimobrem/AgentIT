"""Resolve real ops destinations for System Health page cards.

Every href is built from a live console URL, Argo Application repoURL, or
known in-portal route. When a prerequisite cannot be resolved the card is
returned with ``href=None`` and a ``reason`` — never a guessed/mock URL.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote, urlparse

log = logging.getLogger(__name__)

# OpenShift Console host for the web UI, always prefixed on the apps domain
# the same way the in-cluster console Route is named.
_CONSOLE_HOST_PREFIX = "console-openshift-console.apps."


def console_url_from_route_host(host: str) -> str | None:
    """Derive the OpenShift console URL from an apps-domain Route host.

    ``agentit.apps.example.com`` → ``https://console-openshift-console.apps.example.com``.
    Returns None when the host is not on an ``.apps.`` domain (local/dev).
    """
    if not host or ".apps." not in host:
        return None
    _, apps_suffix = host.split(".apps.", 1)
    if not apps_suffix:
        return None
    return f"https://{_CONSOLE_HOST_PREFIX}{apps_suffix}"


def resolve_console_url() -> str | None:
    """Best-effort OpenShift console base URL (no trailing slash).

    Order: ``AGENTIT_CONSOLE_URL`` env → cluster ``Console`` CR
    ``status.consoleURL`` → derive from this app's own Route host.
    """
    override = (os.environ.get("AGENTIT_CONSOLE_URL") or "").strip().rstrip("/")
    if override:
        parsed = urlparse(override)
        if parsed.scheme in ("https", "http") and parsed.netloc:
            return override
        log.warning("Ignoring invalid AGENTIT_CONSOLE_URL=%r", override)

    try:
        from agentit import kube

        console = kube.get_custom_resource("config.openshift.io", "v1", "consoles", "cluster")
        if console:
            url = (console.get("status") or {}).get("consoleURL") or ""
            url = url.rstrip("/")
            if url:
                return url
    except Exception:
        log.debug("Could not read OpenShift Console CR for console URL", exc_info=True)

    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        try:
            from agentit import kube

            namespace = os.environ.get("AGENTIT_NAMESPACE", "agentit")
            routes = kube.list_custom_resources("route.openshift.io", "v1", "routes", namespace)
            for route in routes:
                labels = route.get("metadata", {}).get("labels") or {}
                if labels.get("app.kubernetes.io/name") != "agentit":
                    continue
                host = (route.get("spec") or {}).get("host") or ""
                derived = console_url_from_route_host(host)
                if derived:
                    return derived
        except Exception:
            log.debug("Could not derive console URL from own Route", exc_info=True)

    return None


def resolve_github_repo_url(repo_url: str | None) -> str | None:
    """Normalize an Argo/Git ``repoURL`` into an https GitHub repo root, or None."""
    if not repo_url:
        return None
    raw = repo_url.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    if raw.startswith("git@"):
        # git@github.com:org/repo → https://github.com/org/repo
        try:
            host_path = raw.split("@", 1)[1]
            host, path = host_path.split(":", 1)
            raw = f"https://{host}/{path}"
        except ValueError:
            return None
    parsed = urlparse(raw)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return None
    if "github.com" not in parsed.netloc.lower():
        # Still a usable git web UI for some hosts; keep https form when valid.
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return f"https://{parsed.netloc}{parsed.path}".rstrip("/")


def _link(href: str | None, *, title: str, external: bool = True, reason: str | None = None) -> dict[str, Any]:
    if href:
        return {"href": href, "title": title, "external": external, "reason": None}
    return {"href": None, "title": title, "external": external, "reason": reason or "Destination unavailable"}


def console_resource_url(console: str, namespace: str, api_path: str, name: str | None = None) -> str:
    """Build an OpenShift console namespaced resource URL.

    ``api_path`` examples: ``pods``, ``tekton.dev~v1~PipelineRun``,
    ``argoproj.io~v1alpha1~Application``.
    """
    base = f"{console.rstrip('/')}/k8s/ns/{quote(namespace, safe='')}/{api_path}"
    if name:
        return f"{base}/{quote(name, safe='')}"
    return base


def console_observe_metrics_url(console: str, namespace: str) -> str:
    """Admin Observe metrics query-browser scoped to the AgentIT namespace."""
    query = f'up{{namespace="{namespace}"}}'
    return f"{console.rstrip('/')}/monitoring/query-browser?query0={quote(query)}"


def build_health_card_links(
    *,
    console_url: str | None,
    github_repo_url: str | None,
    namespace: str,
    latest_pipeline_name: str | None = None,
    last_successful_ci_name: str | None = None,
    current_commit: str | None = None,
    rollout_name: str = "agentit",
    argo_app_name: str = "agentit",
    kafka_name: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Map each System Health stat-card key to a real destination (or reason)."""
    ns = namespace or "agentit"
    no_console = "OpenShift console URL not resolved (set AGENTIT_CONSOLE_URL or grant Console/Route read)"
    no_github = "GitHub repo URL not available from the agentit Argo CD Application"
    no_pipeline = "No agentit-ci PipelineRun found"
    no_commit = "Deployed commit SHA not available from Argo CD"

    cards: dict[str, dict[str, Any]] = {}

    # Platform → Observe / Prometheus query browser for this namespace
    cards["platform"] = _link(
        console_observe_metrics_url(console_url, ns) if console_url else None,
        title="Open Observe metrics for this namespace",
        reason=no_console,
    )

    # Rollout → Argo Rollouts CR in console
    cards["rollout"] = _link(
        console_resource_url(console_url, ns, "argoproj.io~v1alpha1~Rollout", rollout_name) if console_url else None,
        title="Open Argo Rollout in OpenShift console",
        reason=no_console,
    )

    # Pods → namespace pod list in console
    cards["pods"] = _link(
        console_resource_url(console_url, ns, "pods") if console_url else None,
        title="Open pods in OpenShift console",
        reason=no_console,
    )

    # Last Pipeline → latest agentit-ci PipelineRun (console preferred; portal detail fallback)
    if latest_pipeline_name and console_url:
        cards["pipeline"] = _link(
            console_resource_url(console_url, ns, "tekton.dev~v1~PipelineRun", latest_pipeline_name),
            title="Open latest PipelineRun in OpenShift console",
        )
    elif latest_pipeline_name:
        cards["pipeline"] = _link(
            f"/health/pipelines/{quote(latest_pipeline_name, safe='')}",
            title="Open PipelineRun detail in portal",
            external=False,
        )
    else:
        cards["pipeline"] = _link(None, title="Latest PipelineRun", reason=no_pipeline)

    # Deployed Commit → GitHub commit
    commit = (current_commit or "").strip()
    if github_repo_url and commit:
        cards["deployed_commit"] = _link(
            f"{github_repo_url}/commit/{quote(commit, safe='')}",
            title="View deployed commit on GitHub",
        )
    elif not github_repo_url:
        cards["deployed_commit"] = _link(None, title="Deployed commit on GitHub", reason=no_github)
    else:
        cards["deployed_commit"] = _link(None, title="Deployed commit on GitHub", reason=no_commit)

    # Last Good CI → succeeded PipelineRun in console / portal
    if last_successful_ci_name and console_url:
        cards["last_successful_ci"] = _link(
            console_resource_url(console_url, ns, "tekton.dev~v1~PipelineRun", last_successful_ci_name),
            title="Open last successful PipelineRun in OpenShift console",
        )
    elif last_successful_ci_name:
        cards["last_successful_ci"] = _link(
            f"/health/pipelines/{quote(last_successful_ci_name, safe='')}",
            title="Open last successful PipelineRun in portal",
            external=False,
        )
    else:
        cards["last_successful_ci"] = _link(None, title="Last successful CI", reason=no_pipeline)

    # Extra destinations used by tables / deploy panel (not only the top cards)
    cards["argo_app"] = _link(
        console_resource_url(console_url, "openshift-gitops", "argoproj.io~v1alpha1~Application", argo_app_name)
        if console_url
        else None,
        title="Open Argo CD Application in OpenShift console",
        reason=no_console,
    )
    cards["github_repo"] = _link(
        github_repo_url,
        title="Open GitHub repository",
        reason=no_github,
    )
    cards["github_actions"] = _link(
        f"{github_repo_url}/actions" if github_repo_url else None,
        title="Open GitHub Actions",
        reason=no_github,
    )
    if kafka_name and console_url:
        cards["kafka"] = _link(
            console_resource_url(console_url, ns, "kafka.strimzi.io~v1beta2~Kafka", kafka_name),
            title="Open Kafka CR in OpenShift console",
        )
    elif console_url:
        cards["kafka"] = _link(
            console_resource_url(console_url, ns, "kafka.strimzi.io~v1beta2~Kafka"),
            title="Open Kafka resources in OpenShift console",
        )
    else:
        cards["kafka"] = _link(None, title="Kafka in OpenShift console", reason=no_console)

    return cards


def enrich_argo_apps_with_links(
    argo_apps: list[dict[str, Any]],
    console_url: str | None,
) -> list[dict[str, Any]]:
    """Attach console Application hrefs to each Argo app row (omit when unresolved)."""
    out = []
    for app in argo_apps:
        row = dict(app)
        name = app.get("name")
        if console_url and name:
            row["href"] = console_resource_url(
                console_url, "openshift-gitops", "argoproj.io~v1alpha1~Application", name,
            )
            row["href_title"] = "Open in OpenShift console"
        else:
            row["href"] = None
            row["href_title"] = "OpenShift console URL not resolved"
        out.append(row)
    return out


def enrich_pipelines_with_links(
    pipelines: list[dict[str, Any]],
    console_url: str | None,
    namespace: str,
) -> list[dict[str, Any]]:
    """Attach Tekton console hrefs alongside the existing portal detail path."""
    out = []
    for p in pipelines:
        row = dict(p)
        name = p.get("name")
        row["portal_href"] = f"/health/pipelines/{quote(name, safe='')}" if name else None
        if console_url and name:
            row["console_href"] = console_resource_url(
                console_url, namespace, "tekton.dev~v1~PipelineRun", name,
            )
        else:
            row["console_href"] = None
        out.append(row)
    return out
