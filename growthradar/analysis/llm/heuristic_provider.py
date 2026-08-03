from __future__ import annotations

from growthradar.analysis.llm.base import LLMProvider
from growthradar.analysis.llm.schemas import LLMAssessment
from growthradar.core.models import CompanyEvidence


class HeuristicProvider(LLMProvider):
    """Deterministic, fully offline stand-in for an LLM.

    Produces the same structured assessment shape as a real language model, using
    transparent rules over already-collected evidence. This is what GrowthRadar
    uses automatically when no ANTHROPIC_API_KEY is configured, so the platform is
    completely usable with no external API, no cost, and no data leaving the
    machine -- and it's what the pipeline falls back to if a real provider call
    fails, so a single API hiccup never breaks a batch run.
    """

    name = "heuristic"

    def assess(self, evidence: CompanyEvidence) -> LLMAssessment:
        product_score, product_reason = self._assess_product_experience(evidence)
        onboarding_score, onboarding_reason = self._assess_onboarding_opportunity(evidence)
        pitch = self._build_pitch(evidence, onboarding_score)
        fetched = sum(1 for page in evidence.pages.values() if page.fetched_ok)
        confidence = round(min(1.0, 0.3 + 0.1 * fetched), 2)

        return LLMAssessment(
            product_experience_score=product_score,
            product_experience_reasoning=product_reason,
            onboarding_opportunity_score=onboarding_score,
            onboarding_opportunity_reasoning=onboarding_reason,
            recommended_pitch_angle=pitch,
            confidence=confidence,
        )

    @staticmethod
    def _assess_product_experience(evidence: CompanyEvidence) -> tuple[float, str]:
        signals = evidence.signals
        score = 40.0
        reasons = []

        if signals.pricing_tier_count >= 3:
            score += 20
            reasons.append(
                f"{signals.pricing_tier_count} pricing tiers suggest a feature-rich product "
                "with varying complexity across plans"
            )
        if signals.has_docs_or_help_center:
            score += 15
            reasons.append("a dedicated documentation/help center indicates enough product complexity to warrant self-serve support")
        if signals.b2b_keyword_hits >= 5:
            score += 15
            reasons.append(f"heavy B2B/product terminology ({signals.b2b_keyword_hits} hits) implies a feature-dense workflow tool")
        if not reasons:
            reasons.append("limited evidence of product depth was found on the pages analyzed")

        score = max(0.0, min(100.0, score))
        return score, "Estimated product complexity from public signals: " + "; ".join(reasons) + "."

    @staticmethod
    def _assess_onboarding_opportunity(evidence: CompanyEvidence) -> tuple[float, str]:
        signals = evidence.signals
        competitor_tools = [t.name for t in evidence.detected_technologies if t.category == "onboarding_adoption"]
        score = 30.0
        reasons = []

        if not competitor_tools:
            score += 35
            reasons.append("no onboarding/adoption tool (e.g. Appcues, Pendo, WalkMe) was detected on the site")
        else:
            score += 10
            reasons.append(
                f"already uses {', '.join(competitor_tools)} for onboarding, so this is a displacement "
                "opportunity rather than greenfield"
            )

        if signals.has_free_trial_cta or signals.has_signup_cta:
            score += 20
            reasons.append("offers self-serve signup/trial, where a poor first-run experience directly hurts activation")

        if signals.pricing_tier_count >= 3 and not competitor_tools:
            score += 15
            reasons.append("a multi-tier, feature-rich product with no onboarding tooling is a strong candidate for guided onboarding")

        score = max(0.0, min(100.0, score))
        return score, "Onboarding opportunity assessment: " + "; ".join(reasons) + "."

    @staticmethod
    def _build_pitch(evidence: CompanyEvidence, onboarding_score: float) -> str:
        competitor_tools = [t.name for t in evidence.detected_technologies if t.category == "onboarding_adoption"]
        if competitor_tools:
            return (
                f"They already use {competitor_tools[0]} for onboarding -- position UserGuiding as a more "
                "cost-effective, no-code alternative with faster time-to-value."
            )
        if evidence.signals.has_free_trial_cta or evidence.signals.has_signup_cta:
            return (
                "They run self-serve signup with no visible onboarding tooling -- pitch UserGuiding's "
                "onboarding checklists and product tours to reduce trial-to-paid drop-off."
            )
        if onboarding_score >= 50:
            return "No onboarding tooling detected; lead with UserGuiding's quick no-code setup for guided product tours."
        return "Limited public evidence of onboarding needs; treat as a lower-confidence lead pending more research."
