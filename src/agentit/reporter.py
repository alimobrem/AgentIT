from __future__ import annotations

from agentit.models import AssessmentReport


def render_json_report(report: AssessmentReport) -> str:
    return report.model_dump_json(indent=2)


def render_terminal_report(report: AssessmentReport) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append(f"  {report.repo_name.upper()} — ENTERPRISE READINESS ASSESSMENT")
    lines.append("  " + "=" * 55)
    lines.append("")

    if report.stack.languages:
        lang_str = " / ".join(
            f"{l.name.capitalize()} {l.version or ''}" for l in report.stack.languages
        )
        lines.append(f"  Stack: {lang_str.strip()}")
    if report.stack.frameworks:
        fw_str = " / ".join(f.name for f in report.stack.frameworks)
        lines.append(f"  Frameworks: {fw_str}")
    if report.stack.databases:
        db_str = " / ".join(d.name.capitalize() for d in report.stack.databases)
        lines.append(f"  Databases: {db_str}")
    lines.append(f"  Architecture: {report.architecture.architecture_style} ({report.architecture.service_count} service(s))")
    if report.architecture.auth_mechanism:
        lines.append(f"  Auth: {report.architecture.auth_mechanism}")
    else:
        lines.append("  Auth: None detected")
    lines.append("")

    lines.append("  SCORES:")
    for score in sorted(report.scores, key=lambda s: s.score):
        bar_filled = score.score // 10
        bar_empty = 10 - bar_filled
        bar = "█" * bar_filled + "░" * bar_empty
        dim_name = score.dimension.replace("_", " ").ljust(22)
        lines.append(f"    {dim_name} {score.score:>3}/100  {bar}")
    lines.append("")
    lines.append(f"  OVERALL: {report.overall_score:.0f}/100")
    lines.append("")

    critical_findings = [
        f for s in report.scores for f in s.findings if f.severity.name == "critical"
    ]
    high_findings = [
        f for s in report.scores for f in s.findings if f.severity.name == "high"
    ]

    if critical_findings or high_findings:
        lines.append("  CRITICAL & HIGH FINDINGS:")
        for f in critical_findings:
            lines.append(f"    [{f.severity.name.upper()}] {f.description}")
        for f in high_findings:
            lines.append(f"    [{f.severity.name.upper()}] {f.description}")
        lines.append("")

    if report.remediation_plan:
        lines.append(f"  REMEDIATION PLAN: {len(report.remediation_plan)} items")
        for item in report.remediation_plan[:10]:
            lines.append(f"    {item.priority}. [{item.dimension}] {item.description} ({item.estimated_effort})")
        if len(report.remediation_plan) > 10:
            lines.append(f"    ... and {len(report.remediation_plan) - 10} more")
        lines.append("")

    return "\n".join(lines)
