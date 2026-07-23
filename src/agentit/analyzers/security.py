from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from agentit.analyzers.base import calculate_score, iter_text_files, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity
from agentit.secret_classify_cache import snippet_hash

if TYPE_CHECKING:
    from agentit.secret_classify_cache import SecretClassifyCache

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("password", re.compile(r"""(?:password|passwd|pwd)\s*[:=]\s*['"]?[^\s'"]{8,}""", re.IGNORECASE)),
    ("api_key", re.compile(r"""(?:api[_-]?key|apikey)\s*[:=]\s*['"]?[a-zA-Z0-9_\-]{16,}""", re.IGNORECASE)),
    ("secret_key", re.compile(r"""(?:secret[_-]?key|secret)\s*[:=]\s*['"]?[a-zA-Z0-9_\-]{16,}""", re.IGNORECASE)),
    ("token", re.compile(r"""(?:auth[_-]?token|access[_-]?token|bearer)\s*[:=]\s*['"]?[a-zA-Z0-9_\-]{16,}""", re.IGNORECASE)),
    ("sk_prefix", re.compile(r"""['"]sk-[a-zA-Z0-9]{10,}['"]""")),
    ("aws_key", re.compile(r"""AKIA[0-9A-Z]{16}""")),
    ("private_key", re.compile(r"""-----BEGIN (?:RSA |EC )?PRIVATE KEY-----""")),
]

IGNORED_SECRET_PATHS = {
    "test", "tests", "spec", "specs", "fixtures", "testdata",
    "mock", "mocks", "__tests__", "evals",
}

IGNORED_SECRET_SUFFIXES = {
    "_test.go", "_test.py", ".test.ts", ".test.js",
    ".spec.ts", ".spec.js", ".stories.tsx",
}

HELM_TEMPLATE_RE = re.compile(r"\{\{.*\}\}")
# Templating / pinky-style placeholders (e.g. __PINKY_OAUTH_SECRET__) — not credentials.
PLACEHOLDER_TOKEN_RE = re.compile(r"__\w+__")
# Prometheus/alert expressions that reference a secret *name* as a label value,
# not a credential (e.g. secret="github-webhook-secret").
SECRET_NAME_LABEL_RE = re.compile(r"""secret\s*=\s*["'][\w-]+["']""", re.IGNORECASE)

# Fail-conservative drop threshold — must stay aligned with `_check_secrets`.
_DROP_CONFIDENCE = 0.7

# Action name for the durable event `classify_secret` decisions are persisted
# under (see `agentit.llm_decisions`) -- mirrors `capability_scout
# .CAPABILITY_RUN_ACTION`'s convention of a module-owned constant the
# decision-audit layer imports rather than duplicating the literal string.
SECRET_CLASSIFY_ACTION = "secret-classify"


class SecurityAnalyzer:
    dimension = "security"

    def __init__(
        self,
        llm_client: object | None = None,
        secret_decisions_out: list[dict] | None = None,
        app_name: str | None = None,
        secret_classify_cache: SecretClassifyCache | None = None,
    ) -> None:
        """``secret_decisions_out``, if given, is populated (in place) with one
        ``{"file_path", "secret_type", "is_secret", "confidence", "reason", "kept"}``
        row per *new or flipped* `classify_secret` verdict -- mirrors
        `runner.run_assessment`'s `check_results_out` convention. Cache hits
        for a prior confident drop (or kept) skip the LLM and do not append
        here, so Decisions stays an audit of first sight / outcome flips.
        The caller (once it has an `assessment_id`) persists these via
        `llm_decisions.build_secret_classify_events()` + `store.log_event()`.
        """
        self._llm = llm_client
        self._secret_decisions_out = secret_decisions_out
        self._app_name = app_name
        self._classify_cache = secret_classify_cache

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        findings.extend(self._check_secrets(repo_path))
        findings.extend(self._check_dockerfile(repo_path))
        findings.extend(self._check_network_policies(repo_path))
        findings.extend(self._check_scanning(repo_path))
        findings.extend(self._check_base_image(repo_path))

        return DimensionScore(
            dimension="security",
            score=calculate_score(findings),
            max_score=100,
            findings=findings,
        )

    def _check_secrets(self, repo_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for file_path, content in iter_text_files(repo_path):
            rel_path = str(file_path.relative_to(repo_path))

            if _is_secret_scan_excluded(file_path, rel_path):
                continue

            for secret_type, pattern in SECRET_PATTERNS:
                match = pattern.search(content)
                if match and not _is_false_positive(content, match):
                    matched_line = _get_match_line(content, match.start())
                    line_hash = snippet_hash(matched_line)

                    cached = self._cache_lookup(rel_path, line_hash)
                    if cached is not None:
                        if cached["outcome"] == "dropped" and float(cached["confidence"]) > _DROP_CONFIDENCE:
                            self._cache_touch(rel_path, line_hash)
                            continue
                        if cached["outcome"] == "kept":
                            self._cache_touch(rel_path, line_hash)
                            findings.append(_secret_finding(rel_path, secret_type))
                            continue

                    if self._llm is not None:
                        context_lines = _get_context_lines(content, match.start(), radius=3)
                        verdict = self._llm.classify_secret(rel_path, matched_line, context_lines)
                        if verdict is not None:
                            dropped = (
                                not verdict["is_secret"]
                                and float(verdict["confidence"]) > _DROP_CONFIDENCE
                            )
                            outcome = "dropped" if dropped else "kept"
                            should_log = (
                                cached is None or cached.get("outcome") != outcome
                            )
                            self._cache_upsert(
                                rel_path, line_hash, secret_type, outcome,
                                float(verdict["confidence"]), str(verdict["reason"]),
                            )
                            if should_log and self._secret_decisions_out is not None:
                                self._secret_decisions_out.append({
                                    "file_path": rel_path,
                                    "secret_type": secret_type,
                                    "is_secret": verdict["is_secret"],
                                    "confidence": verdict["confidence"],
                                    "reason": verdict["reason"],
                                    "kept": not dropped,
                                })
                            if dropped:
                                continue
                    findings.append(_secret_finding(rel_path, secret_type))
        return findings

    def _cache_lookup(self, file_path: str, line_hash: str) -> dict | None:
        if self._classify_cache is None or not self._app_name:
            return None
        return self._classify_cache.lookup(self._app_name, file_path, line_hash)

    def _cache_touch(self, file_path: str, line_hash: str) -> None:
        if self._classify_cache is None or not self._app_name:
            return
        self._classify_cache.touch(self._app_name, file_path, line_hash)

    def _cache_upsert(
        self,
        file_path: str,
        line_hash: str,
        secret_type: str,
        outcome: str,
        confidence: float,
        reason: str,
    ) -> None:
        if self._classify_cache is None or not self._app_name:
            return
        self._classify_cache.upsert(
            self._app_name, file_path, line_hash, secret_type,
            outcome, confidence, reason, source="llm",
        )

    def _check_dockerfile(self, repo_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        dockerfiles = list(repo_path.glob("Dockerfile*")) + list(repo_path.glob("Containerfile*"))

        if not dockerfiles:
            findings.append(Finding(
                category="container",
                severity=Severity.medium,
                description="No Dockerfile or Containerfile found",
                recommendation="Create a multi-stage Containerfile using UBI base image",
                source="analyzer:security",
            ))
            return findings

        for df in dockerfiles:
            try:
                content = df.read_text(errors="ignore")
            except OSError:
                continue
            rel_path = str(df.relative_to(repo_path))

            if not re.search(r"^\s*USER\s+", content, re.MULTILINE):
                findings.append(Finding(
                    category="container",
                    severity=Severity.high,
                    description=f"Container runs as root (no USER directive) in {rel_path}",
                    file_path=rel_path,
                    recommendation="Add USER 1001 directive to run as non-root",
                    source="analyzer:security",
                ))

            if not re.search(r"^\s*HEALTHCHECK\s+", content, re.MULTILINE):
                findings.append(Finding(
                    category="container",
                    severity=Severity.medium,
                    description=f"No HEALTHCHECK defined in {rel_path}",
                    file_path=rel_path,
                    recommendation="Add HEALTHCHECK for container orchestration readiness probes",
                    source="analyzer:security",
                ))

            if re.search(r":latest\b", content):
                findings.append(Finding(
                    category="container",
                    severity=Severity.medium,
                    description=f"Using :latest tag in base image in {rel_path}",
                    file_path=rel_path,
                    recommendation="Pin base image to specific version for reproducible builds",
                    source="analyzer:security",
                ))

        return findings

    def _check_network_policies(self, repo_path: Path) -> list[Finding]:
        seen_yaml = False
        for _, content in iter_yaml_files(repo_path):
            seen_yaml = True
            if "NetworkPolicy" in content:
                return []

        if not seen_yaml:
            return []

        return [Finding(
            category="network",
            severity=Severity.high,
            description="No NetworkPolicy manifests found",
            recommendation="Add deny-all default NetworkPolicy with explicit allow rules",
            source="analyzer:security",
        )]

    def _check_scanning(self, repo_path: Path) -> list[Finding]:
        scan_indicators = ["trivy", "grype", "snyk", "stackrox", "acs", "clair", "anchore"]
        for ci_file in list(repo_path.rglob(".github/workflows/*.yml")) + list(repo_path.rglob(".gitlab-ci.yml")) + list(repo_path.rglob("Jenkinsfile")):
            try:
                content = ci_file.read_text(errors="ignore").lower()
                if any(s in content for s in scan_indicators):
                    return []
            except OSError:
                continue
        return [Finding(
            category="scanning",
            severity=Severity.high,
            description="No container or dependency vulnerability scanning detected in CI",
            recommendation=(
                "Add ACS (StackRox) or Trivy scanning to the CI pipeline; "
                "enable automated dependency updates (Renovate/Dependabot) via skills"
            ),
            source="analyzer:security",
        )]

    def _check_base_image(self, repo_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for df in list(repo_path.glob("Dockerfile*")) + list(repo_path.glob("Containerfile*")):
            try:
                content = df.read_text(errors="ignore")
            except OSError:
                continue
            if re.search(r"^\s*FROM\s+", content, re.MULTILINE) and not re.search(r"FROM\s+.*(?:ubi|redhat|registry\.access\.redhat)", content, re.IGNORECASE):
                findings.append(Finding(
                    category="container",
                    severity=Severity.low,
                    description=f"Base image is not UBI (Red Hat Universal Base Image) in {df.name}",
                    file_path=str(df.relative_to(repo_path)),
                    recommendation="Use registry.access.redhat.com/ubi9/ubi-minimal for supported, secure base image",
                    source="analyzer:security",
                ))
        return findings


def _is_secret_scan_excluded(file_path: Path, rel_path: str) -> bool:
    parts = set(file_path.parts)
    if parts & IGNORED_SECRET_PATHS:
        return True
    name_lower = file_path.name.lower()
    if any(name_lower.endswith(s) for s in IGNORED_SECRET_SUFFIXES):
        return True
    if name_lower.endswith(".md") or name_lower.endswith(".txt"):
        return True
    if "hook" in rel_path.lower() or "pre-commit" in rel_path.lower():
        return True
    return False


def _is_false_positive(content: str, match: re.Match) -> bool:
    matched_line = _get_match_line(content, match.start())
    if HELM_TEMPLATE_RE.search(matched_line):
        return True
    if PLACEHOLDER_TOKEN_RE.search(matched_line):
        return True
    if SECRET_NAME_LABEL_RE.search(matched_line):
        return True
    placeholder_patterns = [
        "example", "placeholder", "changeme", "your_", "xxx", "<",
        "REPLACE", "TODO", "${", "$(", "os.environ", "os.getenv",
        "process.env", "vault:", "secretKeyRef",
        "from-literal", "from-file", "create secret", "kubectl create",
        "dry-run", "os.urandom",
    ]
    if any(p in matched_line for p in placeholder_patterns):
        return True
    stripped = matched_line.lstrip()
    if stripped.startswith(("#", "//", "*", "echo", "grep", "if ", "elif ")):
        return True
    if re.search(r'[A-Z_]+="\$\{', matched_line):
        return True
    if re.search(r'[A-Z_]+="\$\(', matched_line):
        return True
    return False


def _secret_finding(rel_path: str, secret_type: str) -> Finding:
    return Finding(
        category="secrets",
        severity=Severity.critical,
        description=f"Potential {secret_type} found in {rel_path}",
        file_path=rel_path,
        recommendation="Migrate to ExternalSecrets Operator or HashiCorp Vault",
        source="analyzer:security",
    )


def _get_match_line(content: str, pos: int) -> str:
    line_start = content.rfind("\n", 0, pos) + 1
    line_end = content.find("\n", pos)
    if line_end == -1:
        line_end = len(content)
    return content[line_start:line_end]


def _get_context_lines(content: str, pos: int, radius: int = 3) -> list[str]:
    lines = content.splitlines()
    # determine which line index pos falls on
    current = 0
    target_idx = 0
    for i, line in enumerate(lines):
        end = current + len(line) + 1  # +1 for newline
        if pos < end:
            target_idx = i
            break
        current = end
    start = max(0, target_idx - radius)
    end = min(len(lines), target_idx + radius + 1)
    return lines[start:end]
