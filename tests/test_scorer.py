from growthradar.config import ScoringWeights, Settings
from growthradar.core.models import DimensionScore
from growthradar.scoring.scorer import compute_lead_score


def _settings(**overrides) -> Settings:
    base = Settings()
    return Settings(**{**base.__dict__, **overrides})


def test_weighted_average_and_tiering():
    dims = [
        DimensionScore(name="icp_fit", score=80, reasoning="r"),
        DimensionScore(name="onboarding_opportunity", score=90, reasoning="r"),
        DimensionScore(name="product_experience", score=60, reasoning="r"),
    ]
    settings = _settings(weights=ScoringWeights(icp_fit=0.3, onboarding_opportunity=0.45, product_experience=0.25))
    result = compute_lead_score("example.com", dims, [], "pitch", "heuristic", 0.8, settings)

    expected = 80 * 0.3 + 90 * 0.45 + 60 * 0.25
    assert abs(result.overall_score - round(expected, 1)) < 0.5
    assert result.tier.value == "hot"


def test_disqualifiers_cap_score_and_exclude():
    dims = [DimensionScore(name="icp_fit", score=95, reasoning="r")]
    settings = _settings(weights=ScoringWeights(icp_fit=1.0, onboarding_opportunity=0.0, product_experience=0.0))
    result = compute_lead_score("example.com", dims, ["already a customer"], "pitch", "heuristic", 0.8, settings)

    assert result.tier.value == "excluded"
    assert result.overall_score <= 15
