from pathlib import Path

from growthradar.dom import DomSnapshot
from growthradar.evidence import EvidenceStore
from growthradar.page_classifier import PageCategory, classify_and_record, classify_page


def _dom(*, title: str = "", visible_text: str = "") -> DomSnapshot:
    return DomSnapshot(
        url="https://acme.com",
        title=title,
        html="",
        visible_text=visible_text,
        navigation=[],
        interactive_elements=[],
        truncated=False,
    )


def test_classifies_product_updates_when_url_and_content_agree() -> None:
    result = classify_page(
        "https://acme.com/changelog", _dom(title="Changelog", visible_text="See our release notes")
    )

    assert result is not None
    assert result.category == PageCategory.PRODUCT_UPDATES
    assert result.matched_url_pattern == "/changelog"


def test_classifies_help_center_when_url_and_content_agree() -> None:
    result = classify_page(
        "https://acme.com/help", _dom(title="Help Center", visible_text="Browse our knowledge base")
    )

    assert result is not None
    assert result.category == PageCategory.HELP_CENTER


def test_classifies_documentation_and_blog() -> None:
    docs = classify_page(
        "https://acme.com/docs", _dom(title="Documentation", visible_text="API reference guide")
    )
    assert docs is not None
    assert docs.category == PageCategory.DOCUMENTATION

    blog = classify_page(
        "https://acme.com/blog/post-1", _dom(title="Our Blog", visible_text="Posted by the team")
    )
    assert blog is not None
    assert blog.category == PageCategory.BLOG


def test_returns_none_when_only_url_matches() -> None:
    # URL looks like a help page, but the content doesn't back it up.
    result = classify_page(
        "https://acme.com/help", _dom(title="Contact Us", visible_text="Call us")
    )
    assert result is None


def test_returns_none_when_only_content_matches() -> None:
    # Content mentions "help center", but the URL gives no supporting signal.
    result = classify_page(
        "https://acme.com/random-page", _dom(title="Help Center", visible_text="help center info")
    )
    assert result is None


def test_returns_none_for_unrelated_page() -> None:
    result = classify_page(
        "https://acme.com/pricing", _dom(title="Pricing", visible_text="Choose your plan")
    )
    assert result is None


def test_classify_and_record_writes_evidence_only_when_classified(tmp_path: Path) -> None:
    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        confident = classify_and_record(
            store,
            "run-1",
            "page category: changelog",
            url="https://acme.com/changelog",
            dom=_dom(title="Changelog", visible_text="release notes for this month"),
        )
        assert confident is not None
        assert confident.visible_ui["category"] == "product_updates"
        assert confident.confidence == 0.85

        inconclusive = classify_and_record(
            store,
            "run-1",
            "page category: pricing",
            url="https://acme.com/pricing",
            dom=_dom(title="Pricing", visible_text="plans"),
        )
        assert inconclusive is None

        rows = store.for_run("run-1")
        assert len(rows) == 1  # only the confident classification was recorded
