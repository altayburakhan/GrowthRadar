from pathlib import Path

from growthradar.dom import DomSnapshot, InteractiveElement, NavItem
from growthradar.event_log import RunLogger
from growthradar.evidence import EvidenceStore
from growthradar.exploration import (
    ExplorationEngine,
    _in_scope,
    _normalize_url,
    _priority_score,
    _resolve_url,
    _screenshot_kind_for,
)
from growthradar.screenshot import ScreenshotKind


class _StubSession:
    """No real browser needed: the tests using this only exercise
    ExplorationEngine._extract_candidates, which never touches session.page."""

    def __init__(self) -> None:
        self.page = None
        self.requests: list = []

    def goto(self, url: str) -> bool:
        raise NotImplementedError


def _engine(tmp_path: Path, **kwargs) -> tuple[ExplorationEngine, EvidenceStore, RunLogger]:
    store = EvidenceStore(db_path=tmp_path / "e.db")
    run_logger = RunLogger(run_id="run-1", log_dir=tmp_path)
    engine = ExplorationEngine(
        _StubSession(),  # type: ignore[arg-type]
        store,
        run_logger,
        crawl_delay=0,
        **kwargs,
    )
    return engine, store, run_logger


def test_priority_score_matches_target_keywords() -> None:
    assert _priority_score("Documentation", "https://x.com/docs") == 1.0
    assert _priority_score("Random Thing", "https://x.com/random") == 0.0


def test_priority_score_deprioritizes_login() -> None:
    # A separate login visit is redundant once registration succeeds -- that
    # already lands on the onboarding experience directly (see
    # orchestrator.py's post-registration crawl) -- so login must not compete
    # for a spot in a tight page budget the way other content pages do.
    assert _priority_score("Login", "https://x.com/login") == 0.0
    assert _priority_score("Log in", "https://x.com/login") == 0.0
    assert _priority_score("Sign in", "https://x.com/signin") == 0.0


def test_priority_score_ranks_signup_trial_cta_above_generic_content_links() -> None:
    assert _priority_score("Start 14-Day Free Trial", "https://x.com/auth/create-account") == 2.0
    assert _priority_score("Sign up", "https://x.com/signup") == 2.0
    assert _priority_score("Login", "https://x.com/login") < 2.0
    assert _priority_score("Blog", "https://x.com/blog") < 2.0


def test_priority_score_ranks_product_updates_above_generic_content_links() -> None:
    assert _priority_score("Product Updates", "https://x.com/product-updates") == 1.5
    assert _priority_score("Changelog", "https://x.com/changelog") == 1.5
    assert _priority_score("Blog", "https://x.com/blog") < 1.5


def test_priority_score_prioritizes_read_more_article_links() -> None:
    assert _priority_score("Read more", "https://x.com/blog/post-1") == 1.0
    assert _priority_score("Something Unrelated", "https://x.com/random") == 0.0


def test_extract_candidates_prefers_higher_scoring_duplicate_link_text(tmp_path: Path) -> None:
    # Regression: 100hires.com links the same signup URL twice -- a terse
    # "Try It, Free" in the header nav (visited first) and a keyword-rich
    # "Start 14-Day Free Trial" hero CTA (visited second, same URL). The
    # low-signal nav occurrence must not shadow the high-signal one.
    signup_url = "https://x.com/auth/create-account"
    snapshot = DomSnapshot(
        url="https://x.com",
        title="Home",
        html="",
        visible_text="",
        navigation=[NavItem(text="Try It, Free", href=signup_url)],
        interactive_elements=[
            InteractiveElement(
                tag="a",
                role=None,
                text="Start 14-Day Free Trial",
                href=signup_url,
                name=None,
                type=None,
            )
        ],
        truncated=False,
    )
    engine, store, _ = _engine(tmp_path, max_pages=1, max_depth=1)

    candidates = engine._extract_candidates(snapshot, 1)

    [candidate] = [c for c in candidates if c.url == signup_url]
    assert candidate.text == "Start 14-Day Free Trial"
    assert candidate.priority == 2.0
    store.close()


def test_extract_candidates_excludes_oauth_provider_links(tmp_path: Path) -> None:
    # Regression: "Continue with Google/LinkedIn" links are often served from
    # the target site's own (sub)domain, so `_in_scope` alone doesn't filter
    # them -- following them takes the crawl off the product entirely.
    real_page_url = "https://app.x.com/settings"
    snapshot = DomSnapshot(
        url="https://app.x.com/auth/create-account",
        title="Sign up",
        html="",
        visible_text="",
        navigation=[],
        interactive_elements=[
            InteractiveElement(
                tag="a",
                role=None,
                text="Continue with Google",
                href="https://app.x.com/auth/oauth/google/connect",
                name=None,
                type=None,
            ),
            InteractiveElement(
                tag="a", role=None, text="Settings", href=real_page_url, name=None, type=None
            ),
        ],
        truncated=False,
    )
    engine, store, _ = _engine(tmp_path, max_pages=1, max_depth=1)

    candidates = engine._extract_candidates(snapshot, 1)

    urls = {c.url for c in candidates}
    assert urls == {real_page_url}
    store.close()


def test_resolve_url_filters_non_navigable_hrefs() -> None:
    assert _resolve_url("https://x.com", "#") is None
    assert _resolve_url("https://x.com", "javascript:void(0)") is None
    assert _resolve_url("https://x.com", "mailto:a@b.com") is None
    assert _resolve_url("https://x.com/page", "/pricing") == "https://x.com/pricing"


def test_in_scope_allows_subdomains_blocks_external_domains() -> None:
    assert _in_scope("https://userguiding.com", "https://docs.userguiding.com/x") is True
    assert _in_scope("https://userguiding.com", "https://userguiding.com/pricing") is True
    assert _in_scope("https://userguiding.com", "https://twitter.com/userguiding") is False


def test_normalize_url_strips_fragment_and_trailing_slash() -> None:
    assert _normalize_url("https://x.com/page/#section") == _normalize_url("https://x.com/page")


def test_screenshot_kind_for_matches_onboarding_keywords() -> None:
    assert (
        _screenshot_kind_for("Verify your email", "https://x.com/account/verify", 1)
        == ScreenshotKind.ONBOARDING
    )
    assert (
        _screenshot_kind_for("Welcome to Acme", "https://x.com/welcome", 2)
        == ScreenshotKind.ONBOARDING
    )
