from __future__ import annotations

from growthradar.config import Settings
from growthradar.core.models import DimensionScore, LeadScoreResult, Tier


def compute_tier(score: float, hot_threshold: float, warm_threshold: float) -> Tier:
    if score >= hot_threshold:
        return Tier.HOT
    if score >= warm_threshold:
        return Tier.WARM
    return Tier.COLD


def compute_lead_score(
    domain: str,
    dimensions: list[DimensionScore],
    disqualifiers: list[str],
    recommended_pitch_angle: str,
    provider_used: str,
    confidence: float,
    settings: Settings,
) -> LeadScoreResult:
    """Deterministically combines dimension scores into a single Lead Score.

    Weights are applied here, in plain code, rather than left to the LLM to
    freehand -- the qualitative judgment happens per-dimension (analysis layer),
    but how much each dimension counts toward the final number is a business
    decision that must stay consistent and auditable across every run.
    """
    weights = settings.weights.as_dict()
    weighted_sum = 0.0
    total_weight = 0.0
    for dimension in dimensions:
        weight = weights.get(dimension.name, 0.0)
        weighted_sum += dimension.score * weight
        total_weight += weight

    overall = weighted_sum / total_weight if total_weight > 0 else 0.0

    if disqualifiers:
        overall = min(overall, 15.0)
        tier = Tier.EXCLUDED
    else:
        tier = compute_tier(overall, settings.hot_threshold, settings.warm_threshold)

    return LeadScoreResult(
        domain=domain,
        overall_score=round(overall, 1),
        tier=tier,
        dimensions=dimensions,
        disqualifiers=disqualifiers,
        recommended_pitch_angle=recommended_pitch_angle,
        provider_used=provider_used,
        confidence=confidence,
    )
