from __future__ import annotations

from agentit.models import AssessmentReport, Severity


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
            f"{lang.name.capitalize()} {lang.version or ''}" for lang in report.stack.languages
        )
        lines.append(f"  Stack: {lang_str.strip()}")
    if report.stack.frameworks:
        fw_str = " / ".join(fw.name for fw in report.stack.frameworks)
        lines.append(f"  Frameworks: {fw_str}")
    if report.stack.databases:
        db_str = " / ".join(db.name.capitalize() for db in report.stack.databases)
        lines.append(f"  Databases: {db_str}")
    lines.append(f"  Architecture: {report.architecture.architecture_style} ({report.architecture.service_count} service(s))")
    lines.append(f"  Auth: {report.architecture.auth_mechanism or 'None detected'}")
    lines.append("")

    lines.append("  SCORES:")
    for score in sorted(report.scores, key=lambda s: s.score):
        bar_filled = score.score // 10
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        dim_name = score.dimension.replace("_", " ").ljust(22)
        lines.append(f"    {dim_name} {score.score:>3}/100  {bar}")
    lines.append("")
    lines.append(f"  OVERALL: {report.overall_score:.0f}/100")
    lines.append("")

    urgent = [
        f for s in report.scores for f in s.findings
        if f.severity in (Severity.critical, Severity.high)
    ]
    if urgent:
        lines.append("  CRITICAL & HIGH FINDINGS:")
        for f in urgent:
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
