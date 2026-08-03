from growthradar.analysis.icp import score_icp_fit
from growthradar.core.models import CompanyEvidence, CompanySignals, PageContent


def _evidence(signals: CompanySignals, home_ok: bool = True) -> CompanyEvidence:
    home = PageContent(
        url="https://example.com/",
        status_code=200 if home_ok else None,
        fetch_error=None if home_ok else "timeout",
    )
    return CompanyEvidence(domain="example.com", pages={"home": home}, signals=signals)


def test_strong_plg_saas_signals_score_high():
    signals = CompanySignals(
        has_free_trial_cta=True,
        pricing_tier_count=3,
        has_careers_page=True,
        has_blog=True,
        b2b_keyword_hits=8,
        b2c_keyword_hits=0,
    )
    result = score_icp_fit(_evidence(signals))
    assert result.score >= 70


def test_consumer_site_scores_low():
    signals = CompanySignals(b2b_keyword_hits=0, b2c_keyword_hits=5, pricing_tier_count=0)
    result = score_icp_fit(_evidence(signals))
    assert result.score < 30


def test_unreachable_homepage_penalized():
    signals = CompanySignals(has_free_trial_cta=True, pricing_tier_count=2, b2b_keyword_hits=5)
    result_ok = score_icp_fit(_evidence(signals, home_ok=True))
    result_bad = score_icp_fit(_evidence(signals, home_ok=False))
    assert result_bad.score < result_ok.score
