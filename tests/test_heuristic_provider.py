from growthradar.analysis.llm.heuristic_provider import HeuristicProvider
from growthradar.core.models import CompanyEvidence, CompanySignals, DetectedTech


def test_no_competitor_tool_and_self_serve_scores_high_opportunity():
    evidence = CompanyEvidence(
        domain="example.com",
        signals=CompanySignals(has_free_trial_cta=True, pricing_tier_count=3),
    )
    assessment = HeuristicProvider().assess(evidence)
    assert assessment.onboarding_opportunity_score >= 70
    assert "no onboarding" in assessment.onboarding_opportunity_reasoning.lower()


def test_existing_competitor_tool_lowers_but_does_not_zero_opportunity():
    evidence = CompanyEvidence(
        domain="example.com",
        signals=CompanySignals(has_free_trial_cta=True),
        detected_technologies=[DetectedTech(name="Appcues", category="onboarding_adoption", matched_pattern="appcues")],
    )
    assessment = HeuristicProvider().assess(evidence)
    assert "appcues" in assessment.recommended_pitch_angle.lower()
    assert assessment.onboarding_opportunity_score > 0


def test_deterministic_output_for_same_input():
    evidence = CompanyEvidence(domain="example.com", signals=CompanySignals(has_signup_cta=True, pricing_tier_count=2))
    provider = HeuristicProvider()
    assert provider.assess(evidence) == provider.assess(evidence)
