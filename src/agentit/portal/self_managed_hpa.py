"""Self-managed AgentIT chart awareness for HorizontalPodAutoscaler delivery.

Helm-shaped HPA YAML is necessary but not sufficient: the AgentIT chart's
workload is an Argo Rollout named ``{{ .Release.Name }}`` (when
``rollout.enabled``, the chart default), and the data PVC is ReadWriteOnce.
An HPA that targets ``Deployment`` / ``{{ .Release.Name }}-agentit`` or sets
``maxReplicas: 10`` against that RWO volume is mergeable-looking junk — refuse
it (fail closed → ``needs_attention``) rather than open a PR that clears
``hpa-exists`` without working.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# scaleTargetRef.name must be exactly the release name template (quoted or not).
_RELEASE_NAME_ONLY = re.compile(
    r"""^["']?\{\{\s*\.Release\.Name\s*\}\}["']?$""",
)
# Common LLM mistakes: append -agentit / use app_name placeholder.
_WRONG_RELEASE_NAME_SUFFIX = re.compile(
    r"""\{\{\s*\.Release\.Name\s*\}\}["']?-agentit""",
)


@dataclass(frozen=True)
class SelfManagedChartHints:
    """Workload facts inferred from ``chart/`` for HPA correctness checks."""

    uses_rollout: bool = False
    has_rwo_workload_pvc: bool = False


def default_chart_dir() -> Path:
    """Repo ``chart/`` next to the installed package (portal → agentit → src → root)."""
    return Path(__file__).resolve().parents[3] / "chart"


def inspect_self_managed_chart(chart_dir: Path | None = None) -> SelfManagedChartHints:
    """Detect Rollout workload + RWO data PVC from chart templates/values."""
    root = chart_dir if chart_dir is not None else default_chart_dir()
    templates = root / "templates"
    values = root / "values.yaml"
    uses_rollout = False
    has_rwo = False

    if values.is_file():
        try:
            vals = yaml.safe_load(values.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            vals = {}
        if isinstance(vals, dict):
            rollout = vals.get("rollout") or {}
            if isinstance(rollout, dict) and rollout.get("enabled") is True:
                uses_rollout = True

    if templates.is_dir():
        for path in templates.rglob("*"):
            if not path.is_file() or path.suffix not in (".yaml", ".yml", ".tpl"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if re.search(r"(?m)^\s*kind:\s*Rollout\s*$", text):
                uses_rollout = True
            if "ReadWriteOnce" in text and (
                "kind: PersistentVolumeClaim" in text
                or "persistentVolumeClaim:" in text
            ):
                has_rwo = True

    return SelfManagedChartHints(
        uses_rollout=uses_rollout,
        has_rwo_workload_pvc=has_rwo,
    )


def _parse_docs(content: str) -> list[dict]:
    try:
        return [d for d in yaml.safe_load_all(content or "") if isinstance(d, dict)]
    except yaml.YAMLError:
        return []


def _max_replicas(doc: dict) -> int | None:
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    raw = spec.get("maxReplicas")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def self_managed_hpa_correctness_reason(
    content: str,
    *,
    hints: SelfManagedChartHints | None = None,
) -> str | None:
    """Why an HPA must not open a self-managed chart PR, or ``None`` if OK.

    Non-HPA content returns ``None`` (other gates handle it).
    """
    docs = _parse_docs(content)
    hpas = [d for d in docs if (d.get("kind") or "") == "HorizontalPodAutoscaler"]
    if not hpas:
        return None

    chart = hints if hints is not None else inspect_self_managed_chart()

    for doc in hpas:
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        ref = spec.get("scaleTargetRef") if isinstance(spec.get("scaleTargetRef"), dict) else {}
        kind = str(ref.get("kind") or "")
        name = str(ref.get("name") or "").strip()
        api = str(ref.get("apiVersion") or "")

        if chart.uses_rollout:
            if kind != "Rollout":
                return (
                    "HPA scaleTargetRef.kind must be Rollout (argoproj.io) when the "
                    f"chart uses Argo Rollouts; got {kind or '(missing)'} — refusing "
                    "Deployment-targeted HPA that would not attach"
                )
            if api and "argoproj.io" not in api:
                return (
                    "HPA scaleTargetRef.apiVersion must be argoproj.io/* for Rollout "
                    f"targets; got {api!r}"
                )
            if _WRONG_RELEASE_NAME_SUFFIX.search(name) or (
                name and not _RELEASE_NAME_ONLY.match(name)
            ):
                return (
                    "HPA scaleTargetRef.name must be {{ .Release.Name }} (chart "
                    f"workload metadata.name); got {name!r} — refusing "
                    "{{ .Release.Name }}-agentit / wrong release pattern"
                )
        else:
            # Deployment charts: still refuse the AgentIT-shaped wrong suffix.
            if _WRONG_RELEASE_NAME_SUFFIX.search(name):
                return (
                    "HPA scaleTargetRef.name must not append -agentit to "
                    "{{ .Release.Name }}; use the workload's metadata.name"
                )

        if chart.has_rwo_workload_pvc:
            max_r = _max_replicas(doc)
            if max_r is None or max_r > 1:
                return (
                    "chart mounts a ReadWriteOnce data PVC — HPA maxReplicas>1 is "
                    "unsafe (Multi-Attach); skip HPA or use maxReplicas: 1 with an "
                    "explicit RWO explanation rather than opening a lying PR"
                )

    return None
