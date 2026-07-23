"""CI-based SBOM generation detection (product path for compliance ``sbom``).

Assess clears when the app repo's CI generates an SBOM — GitHub Action
``anchore/sbom-action`` / Syft workflow step, or a Tekton Pipeline that
wires an sbom task — **not** a committed static ``sbom.cdx.json``.

Bare cluster ``sbom-task`` (``kind: Task`` with syft, no Pipeline wiring)
does **not** clear — same wrong-layer class as audit-policy vs app-audit.
"""
from __future__ import annotations

import re
from pathlib import Path

from agentit.analyzers.base import is_ignored

# GHA / GitLab / Jenkins CI paths we scan for SBOM generation steps.
_CI_PATH_HINTS = (
    ".github/workflows/",
    ".gitlab-ci.yml",
    "jenkinsfile",
)

_GHA_SBOM_ACTION = re.compile(r"anchore/sbom-action\b", re.IGNORECASE)
# Syft generate step in CI (not merely a prose mention).
_SYFT_GENERATE = re.compile(
    r"\bsyft\b[\s\S]{0,200}(?:cyclonedx|spdx|-o\s)",
    re.IGNORECASE,
)
_KIND_PIPELINE = re.compile(r"^\s*kind:\s*Pipeline\s*$", re.IGNORECASE | re.MULTILINE)
_KIND_TASK = re.compile(r"^\s*kind:\s*(Cluster)?Task\s*$", re.IGNORECASE | re.MULTILINE)
# Pipeline wires an SBOM step (task name or taskRef).
_PIPELINE_SBOM_WIRE = re.compile(
    r"(?:^\s*-\s*name:\s*sbom-generate\s*$)"
    r"|(?:taskRef:\s*\n(?:[ \t]+[^\n]+\n)*?[ \t]+name:\s*[\"']?[^\s\"']*sbom)"
    r"|(?:^\s*name:\s*[\"']?[^\s\"']*-sbom[\"']?\s*$)",
    re.IGNORECASE | re.MULTILINE,
)


def _norm_path(path: str) -> str:
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lower()


def is_ci_config_path(path: str) -> bool:
    """True when ``path`` looks like GHA / GitLab CI / Jenkins config."""
    p = _norm_path(path)
    name = p.rsplit("/", 1)[-1]
    if p == ".gitlab-ci.yml" or name == "jenkinsfile":
        return True
    return ".github/workflows/" in p and name.endswith((".yml", ".yaml"))


def content_has_gha_or_ci_sbom(content: str) -> bool:
    """True when CI config text generates an SBOM (action or syft)."""
    body = content or ""
    if _GHA_SBOM_ACTION.search(body):
        return True
    if _SYFT_GENERATE.search(body):
        return True
    return False


def content_has_tekton_pipeline_sbom(content: str) -> bool:
    """True when YAML is a Pipeline that wires an SBOM task/step.

    Bare ``kind: Task`` with syft does **not** count.
    """
    body = content or ""
    if not _KIND_PIPELINE.search(body):
        return False
    return bool(_PIPELINE_SBOM_WIRE.search(body) or _SYFT_GENERATE.search(body))


def content_is_bare_sbom_task(content: str) -> bool:
    """True when content is a Tekton Task (not Pipeline) that mentions SBOM/syft."""
    body = content or ""
    if _KIND_PIPELINE.search(body):
        return False
    if not _KIND_TASK.search(body):
        return False
    low = body.lower()
    return "syft" in low or "sbom" in low or "cyclonedx" in low


def staged_has_ci_sbom(files: list[dict]) -> tuple[bool, str]:
    """Clear-evidence: staged files include CI SBOM generation (not static BOM)."""
    bare_task_paths: list[str] = []
    for entry in files:
        path = str(entry.get("target_path") or entry.get("path") or "")
        content = str(entry.get("content") or "")
        if not content.strip():
            continue
        if is_ci_config_path(path) and content_has_gha_or_ci_sbom(content):
            if _GHA_SBOM_ACTION.search(content):
                return True, f"GitHub Action anchore/sbom-action in {path}"
            return True, f"CI Syft SBOM generation in {path}"
        if content_has_tekton_pipeline_sbom(content):
            return True, f"Tekton Pipeline wires SBOM generation in {path}"
        if content_is_bare_sbom_task(content):
            bare_task_paths.append(path or "?")
        # Static CycloneDX/SPDX artifacts do not clear (wrong product shape).
        name = _norm_path(path).rsplit("/", 1)[-1]
        if ("sbom" in name or "bom" in name) and (
            "bomformat" in content.lower() or "spdx" in content.lower()
        ):
            return False, (
                f"{path}: static SBOM artifact does not clear — need CI "
                "generation (anchore/sbom-action / syft workflow or Tekton "
                "Pipeline sbom step)"
            )
    if bare_task_paths:
        return False, (
            f"{bare_task_paths[0]}: bare Tekton sbom-task does not clear "
            "(wire into a Pipeline that runs on the app, or add GHA "
            "anchore/sbom-action)"
        )
    return False, (
        "no CI SBOM generation in staged files "
        "(need anchore/sbom-action, syft in workflow, or Pipeline sbom step)"
    )


def repo_has_ci_sbom_generation(repo_path: Path) -> bool:
    """True when the repo's CI already generates an SBOM."""
    try:
        root = repo_path.resolve()
    except OSError:
        root = repo_path

    # GitHub Actions workflows
    workflows = repo_path / ".github" / "workflows"
    if workflows.is_dir():
        for fp in list(workflows.glob("*.yml")) + list(workflows.glob("*.yaml")):
            if not fp.is_file() or is_ignored(fp, repo_path):
                continue
            try:
                if content_has_gha_or_ci_sbom(fp.read_text(encoding="utf-8", errors="ignore")):
                    return True
            except OSError:
                continue

    for rel in (".gitlab-ci.yml", "Jenkinsfile"):
        fp = repo_path / rel
        if fp.is_file() and not is_ignored(fp, repo_path):
            try:
                if content_has_gha_or_ci_sbom(fp.read_text(encoding="utf-8", errors="ignore")):
                    return True
            except OSError:
                pass

    # Tekton Pipelines in-repo that wire SBOM (not bare Task alone).
    for fp in repo_path.rglob("*"):
        if not fp.is_file() or is_ignored(fp, repo_path):
            continue
        try:
            if not fp.resolve().is_relative_to(root):
                continue
        except (OSError, ValueError):
            continue
        if fp.suffix.lower() not in {".yaml", ".yml"}:
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if content_has_tekton_pipeline_sbom(content):
            return True
    return False


def default_gha_sbom_workflow() -> str:
    """Minimal workflow that generates a CycloneDX SBOM via anchore/sbom-action."""
    return (
        "name: SBOM\n"
        "on:\n"
        "  push:\n"
        "    branches: [main, master]\n"
        "  pull_request:\n"
        "\n"
        "permissions:\n"
        "  contents: read\n"
        "\n"
        "jobs:\n"
        "  sbom:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: Generate SBOM\n"
        "        uses: anchore/sbom-action@v0.24.0\n"
        "        with:\n"
        "          artifact-name: sbom.cyclonedx.json\n"
        "          format: cyclonedx-json\n"
    )


def gha_sbom_step_snippet() -> str:
    """YAML fragment to append into an existing workflow job steps list."""
    return (
        "      - name: Generate SBOM\n"
        "        uses: anchore/sbom-action@v0.24.0\n"
        "        with:\n"
        "          artifact-name: sbom.cyclonedx.json\n"
        "          format: cyclonedx-json\n"
    )
