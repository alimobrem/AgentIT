from __future__ import annotations

import json
import textwrap
from pathlib import Path

import yaml
from pydantic import BaseModel

from agentit.agents.base import GeneratedFile, _sanitize_name
from agentit.models import AssessmentReport, Severity


class DependencyResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} dependency artifact{'s' if count != 1 else ''}."
        )


# Known risky packages with version thresholds
_KNOWN_CVES: dict[str, str] = {
    "log4j": "Any version of log4j may be affected by CVE-2021-44228",
    "lodash": "Versions < 4.17.21 affected by prototype pollution",
    "requests": "Versions < 2.31.0 affected by CVE-2023-32681",
    "urllib3": "Versions < 2.0.7 affected by CVE-2023-45803",
    "setuptools": "Versions < 65.5.1 affected by CVE-2022-40897",
}

_DEPRECATED_PACKAGES: set[str] = {
    "nose", "pycrypto", "optparse", "imp",
    "request", "querystring", "domain", "punycode",
}

# Map language names to dependabot/renovate ecosystem identifiers
_LANG_TO_ECOSYSTEM: dict[str, str] = {
    "python": "pip",
    "javascript": "npm",
    "typescript": "npm",
    "node": "npm",
    "go": "gomod",
    "java": "maven",
    "kotlin": "maven",
    "ruby": "bundler",
    "rust": "cargo",
    "php": "composer",
    "csharp": "nuget",
    "c#": "nuget",
}

_PKG_MGR_TO_ECOSYSTEM: dict[str, str] = {
    "pip": "pip",
    "pipenv": "pip",
    "poetry": "pip",
    "npm": "npm",
    "yarn": "npm",
    "pnpm": "npm",
    "go": "gomod",
    "gomod": "gomod",
    "maven": "maven",
    "gradle": "maven",
    "bundler": "bundler",
    "cargo": "cargo",
    "composer": "composer",
    "nuget": "nuget",
}


class DependencyAgent:
    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)

    def run(self) -> DependencyResult:
        """Generate dependency lifecycle artifacts."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.extend(self._generate_dependency_report())
        generated.extend(self._generate_renovate_config())
        generated.extend(self._generate_dependabot_config())

        return DependencyResult(files=generated)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _findings_for(self, *categories: str) -> list[str]:
        """Return descriptions of findings whose category contains any keyword."""
        hits: list[str] = []
        for score in self.report.scores:
            for f in score.findings:
                if any(kw in f.category.lower() for kw in categories):
                    hits.append(f.description)
        return hits

    def _detect_ecosystems(self) -> list[str]:
        """Determine package ecosystems from stack info."""
        ecosystems: set[str] = set()

        for lang in self.report.stack.languages:
            eco = _LANG_TO_ECOSYSTEM.get(lang.name.lower())
            if eco:
                ecosystems.add(eco)

        for pm in self.report.stack.package_managers:
            eco = _PKG_MGR_TO_ECOSYSTEM.get(pm.lower())
            if eco:
                ecosystems.add(eco)

        return sorted(ecosystems)

    def _collect_dependency_risks(self) -> list[dict[str, str]]:
        """Scan findings for dependency-related risks."""
        risks: list[dict[str, str]] = []
        dep_findings = self._findings_for("dependency", "vulnerability", "cve", "outdated", "deprecated", "license")

        for desc in dep_findings:
            desc_lower = desc.lower()
            risk_type = "general"
            if any(w in desc_lower for w in ("outdated", "old", "upgrade", "update")):
                risk_type = "outdated"
            elif any(w in desc_lower for w in ("cve", "vulnerability", "vulnerable")):
                risk_type = "vulnerability"
            elif any(w in desc_lower for w in ("deprecated", "end-of-life", "eol")):
                risk_type = "deprecated"
            elif any(w in desc_lower for w in ("license", "gpl", "agpl")):
                risk_type = "license"
            risks.append({"type": risk_type, "description": desc})

        # Check known CVE packages against external_dependencies
        for dep in self.report.architecture.external_dependencies:
            dep_lower = dep.lower()
            for pkg, advisory in _KNOWN_CVES.items():
                if pkg in dep_lower:
                    risks.append({"type": "vulnerability", "description": f"{dep}: {advisory}"})
            if dep_lower in _DEPRECATED_PACKAGES:
                risks.append({"type": "deprecated", "description": f"{dep} is deprecated"})

        return risks

    def _write(self, filename: str, content: str) -> None:
        path = self.output_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    # ------------------------------------------------------------------
    # generators
    # ------------------------------------------------------------------

    def _generate_dependency_report(self) -> list[GeneratedFile]:
        ecosystems = self._detect_ecosystems()
        risks = self._collect_dependency_risks()

        lines: list[str] = [
            f"# Dependency Report: {self.report.repo_name}",
            "",
            "## Detected Ecosystems",
            "",
        ]

        if ecosystems:
            for eco in ecosystems:
                lines.append(f"- {eco}")
        else:
            lines.append("- No package ecosystems detected")
        lines.append("")

        lines.append("## Languages")
        lines.append("")
        for lang in self.report.stack.languages:
            version_str = f" ({lang.version})" if lang.version else ""
            lines.append(f"- {lang.name}{version_str}: {lang.percentage:.0f}%")
        lines.append("")

        if self.report.stack.package_managers:
            lines.append("## Package Managers")
            lines.append("")
            for pm in self.report.stack.package_managers:
                lines.append(f"- {pm}")
            lines.append("")

        lines.append("## Risk Indicators")
        lines.append("")

        if risks:
            for risk in risks:
                icon = {
                    "vulnerability": "CRITICAL",
                    "outdated": "HIGH",
                    "deprecated": "MEDIUM",
                    "license": "MEDIUM",
                    "general": "LOW",
                }.get(risk["type"], "LOW")
                lines.append(f"- **[{icon}]** ({risk['type']}): {risk['description']}")
        else:
            lines.append("- No specific dependency risks detected in findings")
        lines.append("")

        lines.append("## Recommendations")
        lines.append("")
        lines.append("1. Enable automated dependency updates (Renovate or Dependabot)")
        lines.append("2. Run `npm audit` / `pip-audit` / `govulncheck` regularly")
        lines.append("3. Pin dependency versions in production manifests")
        lines.append("4. Review license compatibility before adding new dependencies")
        lines.append("")

        content = "\n".join(lines)
        self._write("dependency-report.md", content)

        finding_text = "; ".join(r["description"] for r in risks) if risks else "Baseline dependency inventory"
        return [
            GeneratedFile(
                path="dependency-report.md",
                content=content,
                description="Dependency risk report with ecosystem detection and known CVE checks.",
                finding_addressed=finding_text,
            ),
        ]

    def _generate_renovate_config(self) -> list[GeneratedFile]:
        ecosystems = self._detect_ecosystems()
        if not ecosystems:
            return []

        config: dict = {
            "$schema": "https://docs.renovatebot.com/renovate-schema.json",
            "extends": ["config:recommended"],
            "labels": ["dependencies"],
            "packageRules": [
                {
                    "matchUpdateTypes": ["patch"],
                    "automerge": True,
                    "automergeType": "pr",
                    "description": "Auto-merge patch updates",
                },
                {
                    "matchUpdateTypes": ["minor"],
                    "groupName": "minor-updates",
                    "description": "Group minor updates together",
                },
                {
                    "matchUpdateTypes": ["major"],
                    "labels": ["dependencies", "breaking"],
                    "description": "Label major updates as breaking",
                },
            ],
            "vulnerabilityAlerts": {
                "enabled": True,
                "labels": ["security", "priority"],
            },
        }

        # Add ecosystem-specific manager config
        manager_map = {
            "pip": "pip_requirements",
            "npm": "npm",
            "gomod": "gomod",
            "maven": "maven",
            "bundler": "bundler",
            "cargo": "cargo",
            "composer": "composer",
            "nuget": "nuget",
        }
        enabled_managers = [manager_map[e] for e in ecosystems if e in manager_map]
        if enabled_managers:
            config["enabledManagers"] = enabled_managers

        content = json.dumps(config, indent=2) + "\n"
        self._write("renovate.json", content)

        return [
            GeneratedFile(
                path="renovate.json",
                content=content,
                description=f"Renovate config for ecosystems: {', '.join(ecosystems)}.",
                finding_addressed="Automated dependency update management.",
            ),
        ]

    def _generate_dependabot_config(self) -> list[GeneratedFile]:
        ecosystems = self._detect_ecosystems()
        if not ecosystems:
            return []

        updates: list[dict] = []
        for eco in ecosystems:
            updates.append({
                "package-ecosystem": eco,
                "directory": "/",
                "schedule": {"interval": "weekly"},
                "open-pull-requests-limit": 10,
                "labels": ["dependencies"],
            })

        config = {
            "version": 2,
            "registries": {},
            "updates": updates,
        }

        content = yaml.dump(config, default_flow_style=False, sort_keys=False)
        filename = ".github/dependabot.yml"
        self._write(filename, content)

        return [
            GeneratedFile(
                path=filename,
                content=content,
                description=f"Dependabot config for ecosystems: {', '.join(ecosystems)}.",
                finding_addressed="GitHub-native dependency update automation.",
            ),
        ]
