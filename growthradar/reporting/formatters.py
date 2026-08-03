from __future__ import annotations

from growthradar.core.models import LeadScoreResult


def to_json(result: LeadScoreResult) -> str:
    return result.model_dump_json(indent=2)


def to_markdown(result: LeadScoreResult) -> str:
    lines = [
        f"# Lead Score: {result.domain}",
        "",
        f"**Overall score:** {result.overall_score}/100 -- **Tier:** {result.tier.value.upper()}",
        f"**Provider:** {result.provider_used} (confidence {result.confidence})",
        "",
    ]

    if result.disqualifiers:
        lines.append("## Disqualifiers")
        lines.extend(f"- {d}" for d in result.disqualifiers)
        lines.append("")

    lines.append("## Dimensions")
    for dimension in result.dimensions:
        lines.append(f"### {dimension.name.replace('_', ' ').title()} -- {dimension.score}/100")
        lines.append(dimension.reasoning)
        lines.append("")

    lines.append("## Recommended pitch angle")
    lines.append(result.recommended_pitch_angle)
    return "\n".join(lines)


def to_table(result: LeadScoreResult) -> str:
    rows = [
        f"{result.domain}  |  score={result.overall_score}  |  tier={result.tier.value}  |  "
        f"provider={result.provider_used}"
    ]
    for dimension in result.dimensions:
        rows.append(f"  - {dimension.name}: {dimension.score}")
    if result.disqualifiers:
        rows.append(f"  ! disqualifiers: {'; '.join(result.disqualifiers)}")
    return "\n".join(rows)


FORMATTERS = {"json": to_json, "markdown": to_markdown, "table": to_table}
