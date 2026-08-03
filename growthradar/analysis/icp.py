from __future__ import annotations

from growthradar.core.models import CompanyEvidence, DimensionScore


def score_icp_fit(evidence: CompanyEvidence) -> DimensionScore:
    # Rule-based rather than LLM-judged: self-serve-B2B-SaaS-or-not is answerable
    # from structural signals alone, so keeping it deterministic makes this half
    # of the score fully reproducible and leaves the LLM to judge only the
    # genuinely subjective dimensions (product experience, onboarding opportunity).
    signals = evidence.signals
    score = 0.0
    reasons: list[str] = []

    if signals.has_free_trial_cta or signals.has_signup_cta:
        score += 30
        reasons.append("Self-serve signup or free trial detected, typical of product-led SaaS.")
    elif signals.has_demo_cta:
        score += 12
        reasons.append("Only a 'book a demo' CTA found; sales-led motion, less product-led.")

    if signals.pricing_tier_count >= 2:
        score += 20
        reasons.append(
            f"Public pricing page shows an estimated {signals.pricing_tier_count} tier(s), "
            "indicating a structured SaaS pricing model."
        )
    elif signals.pricing_tier_count == 1:
        score += 8

    if signals.b2b_keyword_hits > signals.b2c_keyword_hits and signals.b2b_keyword_hits >= 3:
        score += 20
        reasons.append(
            f"B2B language dominates ({signals.b2b_keyword_hits} B2B vs {signals.b2c_keyword_hits} B2C keyword hits)."
        )
    elif signals.b2c_keyword_hits > signals.b2b_keyword_hits:
        score -= 15
        reasons.append("Consumer-facing language dominates over B2B/SaaS language, reducing fit.")

    if signals.has_careers_page:
        score += 10
        reasons.append("Active careers page found, suggesting the company is hiring/growing.")

    if signals.has_blog:
        score += 10
        reasons.append("Company maintains a blog, a sign of ongoing marketing/growth investment.")

    if evidence.has_technology_category("payment"):
        score += 10
        reasons.append("Self-serve payment processor detected, confirming an online billing/SaaS model.")

    home = evidence.pages.get("home")
    if home is None or not home.fetched_ok:
        score -= 30
        reasons.append("Homepage could not be reached; ICP signals are unreliable.")

    score = max(0.0, min(100.0, score))
    if not reasons:
        reasons.append("Insufficient public signals found to establish ICP fit.")

    return DimensionScore(name="icp_fit", score=score, reasoning=" ".join(reasons), evidence=reasons)
