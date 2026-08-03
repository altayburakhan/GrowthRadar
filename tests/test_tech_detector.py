from growthradar.collection.tech_detector import detect_technologies
from growthradar.core.models import PageContent


def _page(html: str) -> PageContent:
    return PageContent(url="https://example.com", status_code=200, raw_html=html)


def test_detects_userguiding_as_existing_customer():
    page = _page('<script src="https://static.userguiding.com/ug.js"></script>')
    names = {t.name for t in detect_technologies([page])}
    assert "UserGuiding" in names


def test_detects_competitor_onboarding_tool():
    page = _page("<script>window.Appcues = {};</script>")
    detected = detect_technologies([page])
    assert any(t.name == "Appcues" and t.category == "onboarding_adoption" for t in detected)


def test_no_false_positive_on_unrelated_html():
    page = _page("<html><body><h1>Hello world</h1></body></html>")
    assert detect_technologies([page]) == []


def test_deduplicates_multiple_matches_of_same_tool():
    page = _page(
        '<script src="https://js.stripe.com/v3/"></script>'
        '<script src="https://js.stripe.com/v3/"></script>'
    )
    stripe_matches = [t for t in detect_technologies([page]) if t.name == "Stripe"]
    assert len(stripe_matches) == 1
