"""Property verifier — validates that generated manifests satisfy declared security properties."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import yaml

from agentit.agents.base import GeneratedFile

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Outcome of verifying a single property against generated files."""

    property_name: str
    passed: bool
    checks: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        details = "; ".join(c.get("detail", "") for c in self.checks if not c.get("passed", True))
        base = f"[{status}] {self.property_name}"
        return f"{base}: {details}" if details else base


# ---- Individual property verifiers ----

def _verify_network_isolation(files: list[GeneratedFile]) -> VerificationResult:
    """Verify that NetworkPolicy resources exist and restrict ingress."""
    checks: list[dict] = []
    has_netpol = False
    for f in files:
        if not f.path.endswith((".yaml", ".yml")):
            continue
        try:
            for doc in yaml.safe_load_all(f.content):
                if not isinstance(doc, dict):
                    continue
                if doc.get("kind") == "NetworkPolicy":
                    has_netpol = True
                    policy_types = doc.get("spec", {}).get("policyTypes", [])
                    if "Ingress" in policy_types:
                        checks.append({"name": "ingress-restricted", "passed": True, "detail": f"NetworkPolicy in {f.path} restricts Ingress"})
                    else:
                        checks.append({"name": "ingress-restricted", "passed": False, "detail": f"NetworkPolicy in {f.path} missing Ingress policyType"})
        except yaml.YAMLError:
            checks.append({"name": "yaml-parse", "passed": False, "detail": f"Cannot parse {f.path}"})

    if not has_netpol:
        checks.append({"name": "netpol-exists", "passed": False, "detail": "No NetworkPolicy found"})

    passed = has_netpol and all(c["passed"] for c in checks)
    return VerificationResult(property_name="Network Isolation", passed=passed, checks=checks)


def _verify_rbac(files: list[GeneratedFile]) -> VerificationResult:
    """Verify RBAC resources (ServiceAccount, Role/ClusterRole, RoleBinding)."""
    checks: list[dict] = []
    has_sa = False
    has_role = False
    has_binding = False

    for f in files:
        if not f.path.endswith((".yaml", ".yml")):
            continue
        try:
            for doc in yaml.safe_load_all(f.content):
                if not isinstance(doc, dict):
                    continue
                kind = doc.get("kind", "")
                if kind == "ServiceAccount":
                    has_sa = True
                elif kind in ("Role", "ClusterRole"):
                    has_role = True
                elif kind in ("RoleBinding", "ClusterRoleBinding"):
                    has_binding = True
        except yaml.YAMLError:
            pass

    checks.append({"name": "sa-exists", "passed": has_sa, "detail": "ServiceAccount present" if has_sa else "No ServiceAccount found"})
    checks.append({"name": "role-exists", "passed": has_role, "detail": "Role/ClusterRole present" if has_role else "No Role found"})
    checks.append({"name": "binding-exists", "passed": has_binding, "detail": "RoleBinding present" if has_binding else "No RoleBinding found"})

    passed = has_sa and has_role and has_binding
    return VerificationResult(property_name="RBAC", passed=passed, checks=checks)


def _verify_autoscaling(files: list[GeneratedFile]) -> VerificationResult:
    """Verify HPA or KEDA ScaledObject is present."""
    checks: list[dict] = []
    found = False

    for f in files:
        if not f.path.endswith((".yaml", ".yml")):
            continue
        try:
            for doc in yaml.safe_load_all(f.content):
                if not isinstance(doc, dict):
                    continue
                kind = doc.get("kind", "")
                if kind in ("HorizontalPodAutoscaler", "ScaledObject"):
                    found = True
                    checks.append({"name": "autoscaler-exists", "passed": True, "detail": f"{kind} in {f.path}"})
        except yaml.YAMLError:
            pass

    if not found:
        checks.append({"name": "autoscaler-exists", "passed": False, "detail": "No HPA or ScaledObject found"})

    return VerificationResult(property_name="Autoscaling", passed=found, checks=checks)


def _verify_monitoring(files: list[GeneratedFile]) -> VerificationResult:
    """Verify ServiceMonitor or PodMonitor is present."""
    checks: list[dict] = []
    found = False

    for f in files:
        if not f.path.endswith((".yaml", ".yml")):
            continue
        try:
            for doc in yaml.safe_load_all(f.content):
                if not isinstance(doc, dict):
                    continue
                kind = doc.get("kind", "")
                if kind in ("ServiceMonitor", "PodMonitor", "PrometheusRule"):
                    found = True
                    checks.append({"name": "monitor-exists", "passed": True, "detail": f"{kind} in {f.path}"})
        except yaml.YAMLError:
            pass

    if not found:
        checks.append({"name": "monitor-exists", "passed": False, "detail": "No ServiceMonitor/PodMonitor found"})

    return VerificationResult(property_name="Monitoring", passed=found, checks=checks)


_PROBED_WORKLOAD_KINDS = ("Deployment", "Rollout", "StatefulSet", "DaemonSet")


def _workload_containers(doc: dict) -> list[dict]:
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    tspec = template.get("spec") if isinstance(template.get("spec"), dict) else {}
    containers = tspec.get("containers")
    return [c for c in containers if isinstance(c, dict)] if isinstance(containers, list) else []


def _mutate_rule_adds_probes(doc: dict) -> bool:
    """True when a Kyverno ``Policy``/``ClusterPolicy`` has a ``mutate`` rule
    whose patch content literally sets both ``livenessProbe`` and
    ``readinessProbe`` -- the shape ``skills/infrastructure/
    health-probes-policy.md`` generates. A mutate policy fixes containers it
    doesn't own the source definition of (see that skill's docstring for
    why this is the safer alternative to patching an unknown, real
    Deployment blind) -- this check recognizes it as satisfying the same
    property a directly-authored probe would, so this skill's own output
    doesn't spuriously fail the very property check its finding-fix loop
    (auto_delivery.py's ``_PROPERTY_TO_FIX_CATEGORY``) retries against.
    """
    if (doc.get("kind") or "") not in ("Policy", "ClusterPolicy"):
        return False
    rules = (doc.get("spec") or {}).get("rules")
    if not isinstance(rules, list):
        return False
    for rule in rules:
        if not isinstance(rule, dict) or "mutate" not in rule:
            continue
        blob = str(rule.get("mutate") or "")
        if "livenessProbe" in blob and "readinessProbe" in blob:
            return True
    return False


def _verify_health_probes(files: list[GeneratedFile]) -> VerificationResult:
    """Verify every container of every generated Deployment/Rollout/
    StatefulSet/DaemonSet has both ``livenessProbe`` and ``readinessProbe``
    configured -- OR a Kyverno mutate policy that adds both is present
    (``skills/infrastructure/health-probes-policy.md``'s shape).

    A batch with no workload-shaped resource *and* no such mutate policy at
    all is treated as failing (not vacuously passing) -- mirroring
    ``_verify_autoscaling``/``_verify_monitoring``'s "nothing found -> not
    satisfied" posture, so an app whose ``health`` finding is genuinely
    still open (nothing in this batch addresses it) is correctly reported
    as not-yet-fixed rather than silently skipped.
    """
    checks: list[dict] = []
    found_workload = False
    found_policy = False
    all_ok = True

    for f in files:
        if not f.path.endswith((".yaml", ".yml")):
            continue
        try:
            for doc in yaml.safe_load_all(f.content):
                if not isinstance(doc, dict):
                    continue
                kind = doc.get("kind", "")
                if kind in _PROBED_WORKLOAD_KINDS:
                    found_workload = True
                    containers = _workload_containers(doc)
                    ok = bool(containers) and all(
                        c.get("livenessProbe") and c.get("readinessProbe") for c in containers
                    )
                    checks.append({
                        "name": "workload-has-probes",
                        "passed": ok,
                        "detail": (
                            f"{kind} in {f.path} has probes on all containers" if ok
                            else f"{kind} in {f.path} is missing livenessProbe/readinessProbe on >=1 container"
                        ),
                    })
                    if not ok:
                        all_ok = False
                elif _mutate_rule_adds_probes(doc):
                    found_policy = True
                    checks.append({
                        "name": "mutate-policy-adds-probes",
                        "passed": True,
                        "detail": f"Kyverno mutate policy in {f.path} adds livenessProbe/readinessProbe",
                    })
        except yaml.YAMLError:
            checks.append({"name": "yaml-parse", "passed": False, "detail": f"Cannot parse {f.path}"})

    if not found_workload and not found_policy:
        checks.append({
            "name": "health-fix-exists",
            "passed": False,
            "detail": "No probed workload manifest and no health-probes mutate policy found",
        })
        return VerificationResult(property_name="Health Probes", passed=False, checks=checks)

    passed = all_ok and (found_workload or found_policy)
    return VerificationResult(property_name="Health Probes", passed=passed, checks=checks)


# Registry of property verifiers
PROPERTY_VERIFIERS: dict[str, Callable[[list[GeneratedFile]], VerificationResult]] = {
    "network-isolation": _verify_network_isolation,
    "rbac": _verify_rbac,
    "autoscaling": _verify_autoscaling,
    "monitoring": _verify_monitoring,
    "health-probes": _verify_health_probes,
}


def verify_all_properties(files: list[GeneratedFile]) -> list[VerificationResult]:
    """Run all registered property verifiers against generated files."""
    results: list[VerificationResult] = []
    for name, verifier in PROPERTY_VERIFIERS.items():
        result = verifier(files)
        results.append(result)
        logger.info("Property %s: %s", name, result.summary())
    return results
