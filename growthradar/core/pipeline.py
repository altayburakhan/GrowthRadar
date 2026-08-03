from __future__ import annotations

import logging

from growthradar.analysis.disqualifiers import find_disqualifiers
from growthradar.analysis.evidence_builder import build_evidence
from growthradar.analysis.icp import score_icp_fit
from growthradar.analysis.llm.anthropic_provider import AnthropicProvider
from growthradar.analysis.llm.base import LLMProvider, LLMProviderError
from growthradar.analysis.llm.groq_provider import GroqProvider
from growthradar.analysis.llm.heuristic_provider import HeuristicProvider
from growthradar.collection.playwright_fetcher import PlaywrightFetcher
from growthradar.config import Settings
from growthradar.config import settings as default_settings
from growthradar.core.models import CompanyEvidence, DimensionScore, LeadScoreResult
from growthradar.scoring.scorer import compute_lead_score

logger = logging.getLogger(__name__)


def resolve_provider(settings: Settings) -> LLMProvider:
    choice = settings.llm_provider

    if choice == "heuristic":
        return HeuristicProvider()

    if choice == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMProviderError("GROWTHRADAR_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
        return AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model)

    if choice == "groq":
        if not settings.groq_api_key:
            raise LLMProviderError("GROWTHRADAR_LLM_PROVIDER=groq but GROQ_API_KEY is not set.")
        return GroqProvider(settings.groq_api_key, settings.groq_model)

    # auto (default): prefer Claude, then Groq, then stay fully local/offline.
    if settings.anthropic_api_key:
        return AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model)
    if settings.groq_api_key:
        return GroqProvider(settings.groq_api_key, settings.groq_model)
    return HeuristicProvider()


def analyze_company(raw_input: str, settings: Settings | None = None) -> tuple[LeadScoreResult, CompanyEvidence]:
    """Runs the full pipeline for one company: collect evidence, score ICP fit
    deterministically, run LLM-assisted qualitative analysis, combine into a
    single explainable Lead Score. This is the one function a future API or UI
    layer needs to call -- everything else is an internal implementation detail.

    Returns both the score and the underlying CompanyEvidence so a caller can
    persist or display the raw collected data (page text, detected tech,
    signals), not just the final numbers.
    """
    settings = settings or default_settings

    with PlaywrightFetcher(settings.user_agent, settings.request_timeout_seconds) as fetcher:
        evidence = build_evidence(
            raw_input,
            fetcher,
            settings.max_pages_per_company,
            settings.user_agent,
            settings.request_timeout_seconds,
            settings.crawl_delay_seconds,
        )

    icp_dimension = score_icp_fit(evidence)
    disqualifiers = find_disqualifiers(evidence)

    provider = resolve_provider(settings)
    try:
        assessment = provider.assess(evidence)
        provider_used = provider.name
    except LLMProviderError as exc:
        logger.warning("Provider '%s' failed (%s); falling back to heuristic provider.", provider.name, exc)
        fallback = HeuristicProvider()
        assessment = fallback.assess(evidence)
        provider_used = f"{fallback.name} (fallback after {provider.name} error)"

    dimensions = [
        icp_dimension,
        DimensionScore(
            name="product_experience",
            score=assessment.product_experience_score,
            reasoning=assessment.product_experience_reasoning,
        ),
        DimensionScore(
            name="onboarding_opportunity",
            score=assessment.onboarding_opportunity_score,
            reasoning=assessment.onboarding_opportunity_reasoning,
        ),
    ]

    result = compute_lead_score(
        domain=evidence.domain,
        dimensions=dimensions,
        disqualifiers=disqualifiers,
        recommended_pitch_angle=assessment.recommended_pitch_angle,
        provider_used=provider_used,
        confidence=assessment.confidence,
        settings=settings,
    )
    return result, evidence
