"""Code Change Agent — uses LLM to generate source-level patches for assessment findings.

This is the agent that modifies application code (not just infrastructure manifests).
It handles config-level and simple source-level changes:
- Dockerfile fixes (USER, HEALTHCHECK, base image)
- Health endpoint addition
- OpenTelemetry auto-instrumentation config
- .gitignore additions
- Secrets externalization patterns

All changes go through LLM classification before being proposed as a PR
to the application repo.
"""

from __future__ import annotations

import json
import logging
import textwrap
from pathlib import Path

from pydantic import BaseModel

from agentit.agents.base import GeneratedFile, _sanitize_name
from agentit.models import AssessmentReport, Finding, Severity

logger = logging.getLogger(__name__)

_CHANGE_SYSTEM_PROMPT = """\
You are a senior platform engineer generating a code patch for an application.
Given a finding from an enterprise readiness assessment, generate the MINIMAL
change needed to fix it. Return ONLY valid JSON with this schema:

{"file_path": "path/to/file", "action": "create|modify|append", "content": "full file content or patch", "explanation": "one sentence why"}

Rules:
- Prefer config changes over source changes
- Never remove existing functionality
- Never add dependencies without explaining why
- For Dockerfiles: use UBI base images, add USER, add HEALTHCHECK
- For health endpoints: use the framework already in use
- Keep changes surgical — one finding, one file
"""

_CHANGE_USER_TEMPLATE = """\
App: {repo_name}
Stack: {stack}
Framework: {framework}

Finding ({severity}): {description}
Recommendation: {recommendation}
{file_context}
Generate the minimal fix.
"""

# Findings the code agent can handle (category keywords)
_SUPPORTED_CATEGORIES = {
    "secrets", "gitignore", "instrumentation", "otel",
    "opentelemetry", "tracing", "logging", "structured",
}


class CodeChange(BaseModel):
    file_path: str
    action: str  # create, modify, append
    content: str
    explanation: str
    finding: str


class CodeChangeResult(BaseModel):
    files: list[GeneratedFile]
    changes: list[CodeChange]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.changes)
        self.summary = f"Generated {count} code change{'s' if count != 1 else ''}."


class CodeChangeAgent:
    """Generates source-level patches using LLM analysis.

    Unlike infrastructure agents (which generate new YAML files), this agent
    modifies existing application files. It requires an LLM client and the
    cloned repo path for file context.
    """

    def __init__(
        self,
        report: AssessmentReport,
        output_dir: Path,
        repo_path: Path | None = None,
        llm_client: object | None = None,
    ) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)
        self._repo_path = repo_path
        self._llm = llm_client

    def _primary_language(self) -> str:
        if not self.report.stack.languages:
            return "unknown"
        top = max(self.report.stack.languages, key=lambda l: l.percentage)
        return top.name.lower()

    def _primary_framework(self) -> str:
        if not self.report.stack.frameworks:
            return "none"
        return self.report.stack.frameworks[0].name

    def _get_file_context(self, finding: Finding) -> str:
        """Read relevant file content from the cloned repo for LLM context."""
        if self._repo_path is None or finding.file_path is None:
            return ""
        target = self._repo_path / finding.file_path
        if not target.is_file():
            return ""
        try:
            content = target.read_text(errors="ignore")[:3000]
            return f"\nExisting file ({finding.file_path}):\n```\n{content}\n```"
        except Exception:
            return ""

    def _actionable_findings(self) -> list[Finding]:
        """Filter findings to those the code agent can handle."""
        results: list[Finding] = []
        for score in self.report.scores:
            for f in score.findings:
                cat = f.category.lower()
                if any(kw in cat for kw in _SUPPORTED_CATEGORIES):
                    results.append(f)
        return results

    def _write(self, filename: str, content: str) -> None:
        path = self.output_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def run(self) -> CodeChangeResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        findings = self._actionable_findings()

        if not findings:
            return CodeChangeResult(files=[], changes=[])

        changes: list[CodeChange] = []
        generated: list[GeneratedFile] = []

        if self._llm is not None:
            for finding in findings[:10]:
                change = self._generate_change_with_llm(finding)
                if change is not None:
                    changes.append(change)
                    filename = f"patch-{len(changes):02d}-{Path(change.file_path).name}"
                    self._write(filename, change.content)
                    generated.append(GeneratedFile(
                        path=filename,
                        content=change.content,
                        description=change.explanation,
                        finding_addressed=change.finding,
                    ))
        else:
            for finding in findings[:10]:
                change = self._generate_change_deterministic(finding)
                if change is not None:
                    changes.append(change)
                    filename = f"patch-{len(changes):02d}-{Path(change.file_path).name}"
                    self._write(filename, change.content)
                    generated.append(GeneratedFile(
                        path=filename,
                        content=change.content,
                        description=change.explanation,
                        finding_addressed=change.finding,
                    ))

        # Write a summary of all changes
        if changes:
            summary = self._write_change_summary(changes)
            generated.append(summary)

        return CodeChangeResult(files=generated, changes=changes)

    def _generate_change_with_llm(self, finding: Finding) -> CodeChange | None:
        """Use LLM to generate a code change for a finding."""
        user_msg = _CHANGE_USER_TEMPLATE.format(
            repo_name=self.report.repo_name,
            stack=self._primary_language(),
            framework=self._primary_framework(),
            severity=finding.severity.name,
            description=finding.description,
            recommendation=finding.recommendation,
            file_context=self._get_file_context(finding),
        )

        raw = self._llm._chat(_CHANGE_SYSTEM_PROMPT, user_msg)
        if raw is None:
            logger.warning("LLM returned no response for finding: %s", finding.description)
            return None

        try:
            parsed = json.loads(raw)
            return CodeChange(
                file_path=str(parsed["file_path"]),
                action=str(parsed["action"]),
                content=str(parsed["content"]),
                explanation=str(parsed["explanation"]),
                finding=finding.description,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("LLM returned unparseable code change: %s — %s", raw[:200], exc)
            return None

    def _generate_change_deterministic(self, finding: Finding) -> CodeChange | None:
        """Generate deterministic code changes without LLM (fallback)."""
        cat = finding.category.lower()
        lang = self._primary_language()

        if "dockerfile" in cat or "container" in cat:
            return self._fix_dockerfile(finding)
        if "health" in cat:
            return self._add_health_endpoint(finding, lang)
        if "gitignore" in cat:
            return self._fix_gitignore(finding)
        if any(kw in cat for kw in ("otel", "opentelemetry", "tracing", "instrumentation")):
            return self._add_otel_config(finding, lang)

        return None

    def _fix_dockerfile(self, finding: Finding) -> CodeChange:
        name = self._name
        lang = self._primary_language()

        base_image = {
            "python": "registry.access.redhat.com/ubi9/python-312:latest",
            "go": "registry.access.redhat.com/ubi9/go-toolset:latest",
            "java": "registry.access.redhat.com/ubi9/openjdk-21:latest",
            "node": "registry.access.redhat.com/ubi9/nodejs-20:latest",
            "javascript": "registry.access.redhat.com/ubi9/nodejs-20:latest",
            "typescript": "registry.access.redhat.com/ubi9/nodejs-20:latest",
        }.get(lang, "registry.access.redhat.com/ubi9/ubi-minimal:latest")

        port = 3000 if lang in ("node", "javascript", "typescript") else 8080

        content = textwrap.dedent(f"""\
            FROM {base_image}

            WORKDIR /app
            COPY . .

            USER 1001

            EXPOSE {port}

            HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\
              CMD curl -f http://localhost:{port}/ || exit 1
        """)

        return CodeChange(
            file_path="Dockerfile",
            action="create",
            content=content,
            explanation=f"UBI9 Dockerfile with non-root user and health check ({finding.description})",
            finding=finding.description,
        )

    def _add_health_endpoint(self, finding: Finding, lang: str) -> CodeChange | None:
        framework = self._primary_framework().lower()

        if framework in ("flask", "fastapi"):
            content = textwrap.dedent("""\
                # Health check endpoint — add to your main app file
                @app.get("/healthz")
                def healthz():
                    return {"status": "ok"}

                @app.get("/readyz")
                def readyz():
                    return {"status": "ready"}
            """)
            return CodeChange(
                file_path="healthz.py",
                action="create",
                content=content,
                explanation="Health and readiness endpoints for Kubernetes probes",
                finding=finding.description,
            )
        elif framework in ("express", "next.js") or lang in ("node", "javascript"):
            content = textwrap.dedent("""\
                // Health check endpoint — add to your Express app
                app.get('/healthz', (req, res) => res.json({ status: 'ok' }));
                app.get('/readyz', (req, res) => res.json({ status: 'ready' }));
            """)
            return CodeChange(
                file_path="healthz.js",
                action="create",
                content=content,
                explanation="Health and readiness endpoints for Kubernetes probes",
                finding=finding.description,
            )
        elif framework in ("gin", "echo", "fiber") or lang == "go":
            content = textwrap.dedent("""\
                package main

                import "net/http"

                func healthz(w http.ResponseWriter, r *http.Request) {
                    w.Header().Set("Content-Type", "application/json")
                    w.Write([]byte(`{"status":"ok"}`))
                }

                func readyz(w http.ResponseWriter, r *http.Request) {
                    w.Header().Set("Content-Type", "application/json")
                    w.Write([]byte(`{"status":"ready"}`))
                }

                // Register in main: http.HandleFunc("/healthz", healthz)
            """)
            return CodeChange(
                file_path="healthz.go",
                action="create",
                content=content,
                explanation="Health and readiness endpoints for Kubernetes probes",
                finding=finding.description,
            )

        return None

    def _fix_gitignore(self, finding: Finding) -> CodeChange:
        content = textwrap.dedent("""\
            # Secrets and credentials
            .env
            .env.*
            *.pem
            *.key
            credentials.json
            service-account.json

            # IDE
            .idea/
            .vscode/
            *.swp

            # Build artifacts
            __pycache__/
            *.pyc
            node_modules/
            dist/
            build/
            target/
        """)
        return CodeChange(
            file_path=".gitignore",
            action="append",
            content=content,
            explanation="Add standard ignores for secrets, IDE files, and build artifacts",
            finding=finding.description,
        )

    def _add_otel_config(self, finding: Finding, lang: str) -> CodeChange | None:
        if lang == "python":
            content = textwrap.dedent("""\
                # OpenTelemetry auto-instrumentation for Python
                # Install: pip install opentelemetry-distro opentelemetry-exporter-otlp
                # Run with: opentelemetry-instrument python app.py

                # Or add to your app startup:
                from opentelemetry import trace
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

                provider = TracerProvider()
                processor = BatchSpanProcessor(OTLPSpanExporter())
                provider.add_span_processor(processor)
                trace.set_tracer_provider(provider)
            """)
            return CodeChange(
                file_path="otel_setup.py",
                action="create",
                content=content,
                explanation="OpenTelemetry auto-instrumentation setup for Python",
                finding=finding.description,
            )
        elif lang in ("node", "javascript", "typescript"):
            content = textwrap.dedent("""\
                // OpenTelemetry auto-instrumentation for Node.js
                // Install: npm install @opentelemetry/auto-instrumentations-node @opentelemetry/sdk-node
                // Run with: node --require ./otel-setup.js app.js

                const { NodeSDK } = require('@opentelemetry/sdk-node');
                const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');

                const sdk = new NodeSDK({
                  instrumentations: [getNodeAutoInstrumentations()],
                });
                sdk.start();
            """)
            return CodeChange(
                file_path="otel-setup.js",
                action="create",
                content=content,
                explanation="OpenTelemetry auto-instrumentation setup for Node.js",
                finding=finding.description,
            )
        elif lang == "go":
            content = textwrap.dedent("""\
                // OpenTelemetry setup for Go
                // Add to go.mod: go.opentelemetry.io/otel
                // See: https://opentelemetry.io/docs/languages/go/getting-started/

                package main

                import (
                    "go.opentelemetry.io/otel"
                    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
                    "go.opentelemetry.io/otel/sdk/trace"
                )

                func initTracer() func() {
                    exporter, _ := otlptracegrpc.New(context.Background())
                    tp := trace.NewTracerProvider(trace.WithBatcher(exporter))
                    otel.SetTracerProvider(tp)
                    return func() { tp.Shutdown(context.Background()) }
                }
            """)
            return CodeChange(
                file_path="otel_setup.go",
                action="create",
                content=content,
                explanation="OpenTelemetry setup for Go",
                finding=finding.description,
            )

        return None

    def _write_change_summary(self, changes: list[CodeChange]) -> GeneratedFile:
        lines = [
            f"# Code Changes: {self.report.repo_name}",
            "",
            f"Generated {len(changes)} code change(s) for enterprise readiness.",
            "",
            "## Changes",
            "",
        ]
        for i, c in enumerate(changes, 1):
            lines.append(f"### {i}. `{c.file_path}` ({c.action})")
            lines.append(f"**Finding:** {c.finding}")
            lines.append(f"**Fix:** {c.explanation}")
            lines.append("")

        lines.extend([
            "## Next Steps",
            "",
            "1. Review each change carefully",
            "2. Integrate into your application",
            "3. Run tests to verify no regressions",
            "4. Re-assess to verify score improvement",
        ])

        content = "\n".join(lines)
        self._write("code-changes-summary.md", content)
        return GeneratedFile(
            path="code-changes-summary.md",
            content=content,
            description="Summary of all code changes generated by the Code Change Agent.",
            finding_addressed="Documentation of source-level remediations.",
        )
