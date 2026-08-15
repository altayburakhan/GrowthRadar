from pathlib import Path
from urllib.parse import quote

import pytest
from patchright.sync_api import Page

from growthradar.browser import BrowserSession
from growthradar.config import Config
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


def _data_url(html: str) -> str:
    return "data:text/html," + quote(html)


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


@pytest.fixture
def page(config: Config):
    with BrowserSession(config) as session:
        yield session.start()


def _engine(
    page: Page, tmp_path: Path, **kwargs
) -> tuple[ExplorationEngine, EvidenceStore, RunLogger]:
    class _StubSession:
        def __init__(self, real_page: Page) -> None:
            self.page = real_page
            self.requests: list = []

        def goto(self, url: str) -> bool:
            self.page.goto(url)
            return True

    store = EvidenceStore(db_path=tmp_path / "e.db")
    run_logger = RunLogger(run_id="run-1", log_dir=tmp_path)
    engine = ExplorationEngine(
        _StubSession(page),  # type: ignore[arg-type]
        store,
        run_logger,
        crawl_delay=0,
        **kwargs,
    )
    return engine, store, run_logger


# --- pure helper unit tests -------------------------------------------------


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
    # A limited page budget must not be spent entirely on blog/docs/login pages
    # while the actual signup/trial CTA -- often just one of many "relevant"
    # links on a landing page -- loses the tie-break and never gets visited.
    assert _priority_score("Start 14-Day Free Trial", "https://x.com/auth/create-account") == 2.0
    assert _priority_score("Sign up", "https://x.com/signup") == 2.0
    assert _priority_score("Login", "https://x.com/login") < 2.0
    assert _priority_score("Blog", "https://x.com/blog") < 2.0


def test_priority_score_ranks_product_updates_above_generic_content_links() -> None:
    # Reliably reachable within a tight page budget even when several
    # equal-tier content links (blog, docs, footer, ...) are competing for
    # the same remaining spots.
    assert _priority_score("Product Updates", "https://x.com/product-updates") == 1.5
    assert _priority_score("Changelog", "https://x.com/changelog") == 1.5
    assert _priority_score("Release Notes", "https://x.com/release-notes") == 1.5
    assert _priority_score("Blog", "https://x.com/blog") < 1.5


def test_priority_score_prioritizes_read_more_article_links() -> None:
    # A blog/changelog listing page is only a summary -- the crawler must
    # actually follow "Read more"-style links into the full article, or
    # content-based evidence (feature mentions, tool names) never gets seen.
    assert _priority_score("Read more", "https://x.com/blog/post-1") == 1.0
    assert _priority_score("Continue reading", "https://x.com/blog/post-2") == 1.0
    assert _priority_score("Something Unrelated", "https://x.com/random") == 0.0


def test_extract_candidates_prefers_higher_scoring_duplicate_link_text(
    tmp_path: Path, page: Page
) -> None:
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
    engine, store, _ = _engine(page, tmp_path, max_pages=1, max_depth=1)

    candidates = engine._extract_candidates(snapshot, 1)

    [candidate] = [c for c in candidates if c.url == signup_url]
    assert candidate.text == "Start 14-Day Free Trial"
    assert candidate.priority == 2.0
    store.close()


def test_extract_candidates_excludes_oauth_provider_links(tmp_path: Path, page: Page) -> None:
    # Regression: "Continue with Google/LinkedIn" links are often served from
    # the target site's own (sub)domain, so `_in_scope` alone doesn't filter
    # them -- following them takes the crawl off the product entirely and
    # into a third-party sign-in page.
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
                tag="a",
                role=None,
                text="Continue with LinkedIn",
                href="https://app.x.com/auth/oauth/linkedin/connect",
                name=None,
                type=None,
            ),
            InteractiveElement(
                tag="a", role=None, text="Settings", href=real_page_url, name=None, type=None
            ),
        ],
        truncated=False,
    )
    engine, store, _ = _engine(page, tmp_path, max_pages=1, max_depth=1)

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


# --- engine integration tests (real headless browser, data: URL link graph) --


def test_breadth_first_order_and_priority_within_level(tmp_path: Path, page: Page) -> None:
    dashboard_url = _data_url("<html><body><h1>Dashboard</h1></body></html>")
    login_url = _data_url(
        f"<html><body><h1>Login</h1><a href='{dashboard_url}'>Go to Dashboard</a></body></html>"
    )
    docs_url = _data_url(
        f"<html><body><h1>Docs</h1><a href='{login_url}'>Back to Login</a></body></html>"
    )
    random_url = _data_url("<html><body><h1>Random</h1></body></html>")
    home_url = _data_url(
        "<html><body><h1>Home</h1><nav>"
        f"<a href='{login_url}'>Login</a>"
        f"<a href='{docs_url}'>Documentation</a>"
        f"<a href='{random_url}'>Something Else</a>"
        "</nav></body></html>"
    )

    engine, store, run_logger = _engine(page, tmp_path, max_pages=10, max_depth=3)
    result = engine.run(home_url)

    urls_in_order = [v.url for v in result.visited]
    # login_url here embeds dashboard_url's own HTML as its child link (an
    # artifact of how these tests build a small link graph out of data:
    # URLs), so its "url" half of the priority haystack literally contains
    # the word "Dashboard" and still scores 1.0 despite login itself being
    # deprioritized (see test_priority_score_deprioritizes_login, which uses
    # plain, non-nested URLs where that leak can't happen). login and docs
    # stay tied at 1.0 here, so original nav order (login before docs) wins
    # the tie-break -- this test's actual point either way.
    assert urls_in_order == [home_url, login_url, docs_url, random_url, dashboard_url]
    # docs links back to an already-visited page (login) -- must not be revisited.
    assert urls_in_order.count(login_url) == 1
    assert result.stopped_reason == "frontier_exhausted"

    store.close()


def test_max_pages_limits_visited_count(tmp_path: Path, page: Page) -> None:
    a_url = _data_url("<html><body>A</body></html>")
    b_url = _data_url("<html><body>B</body></html>")
    home_url = _data_url(
        f"<html><body><nav><a href='{a_url}'>A</a><a href='{b_url}'>B</a></nav></body></html>"
    )

    engine, store, run_logger = _engine(page, tmp_path, max_pages=2, max_depth=3)
    result = engine.run(home_url)

    assert len(result.visited) == 2
    assert result.stopped_reason == "max_pages_reached"
    store.close()


def test_max_depth_prevents_deeper_expansion(tmp_path: Path, page: Page) -> None:
    deep_url = _data_url("<html><body>Deep</body></html>")
    mid_url = _data_url(f"<html><body><a href='{deep_url}'>Deep</a></body></html>")
    home_url = _data_url(f"<html><body><a href='{mid_url}'>Mid</a></body></html>")

    engine, store, run_logger = _engine(page, tmp_path, max_pages=10, max_depth=1)
    result = engine.run(home_url)

    urls = {v.url for v in result.visited}
    assert home_url in urls
    assert mid_url in urls
    assert deep_url not in urls
    store.close()


def test_failed_page_does_not_stop_exploration(tmp_path: Path, page: Page) -> None:
    ok_url = _data_url("<html><body>OK</body></html>")
    bad_url = _data_url("<html><body>Should fail</body></html>")
    home_url = _data_url(
        f"<html><body><a href='{bad_url}'>Broken</a><a href='{ok_url}'>OK</a></body></html>"
    )

    class _PartialFailSession:
        def __init__(self, real_page: Page) -> None:
            self.page = real_page
            self.requests: list = []

        def goto(self, url: str) -> bool:
            if url == bad_url:
                return False
            self.page.goto(url)
            return True

    store = EvidenceStore(db_path=tmp_path / "e.db")
    run_logger = RunLogger(run_id="run-1", log_dir=tmp_path)
    engine = ExplorationEngine(
        _PartialFailSession(page),  # type: ignore[arg-type]
        store,
        run_logger,
        max_pages=5,
        max_depth=2,
        crawl_delay=0,
    )

    result = engine.run(home_url)

    by_url = {v.url: v for v in result.visited}
    assert by_url[bad_url].success is False
    assert by_url[ok_url].success is True
    store.close()


def test_visited_pages_generate_evidence_and_log_entries(tmp_path: Path, page: Page) -> None:
    home_url = _data_url("<html><body><h1>Solo Page</h1></body></html>")

    engine, store, run_logger = _engine(page, tmp_path, max_pages=5, max_depth=2)
    result = engine.run(home_url)

    evidence = store.for_run("run-1")
    # one screenshot + one dom + one js/network + one onboarding-heuristics
    # evidence record for the single visited page
    assert len(evidence) == 4
    assert any(e.screenshot is not None for e in evidence)
    assert any(e.dom is not None for e in evidence)
    assert any(e.javascript is not None for e in evidence)
    assert any(e.visible_ui and "matched_categories" in e.visible_ui for e in evidence)

    log_entries = run_logger.read_all()
    event_types = {entry.event_type.value for entry in log_entries}
    assert "page_visited" in event_types
    assert "decision" in event_types
    assert result.run_id == "run-1"

    store.close()


# --- initial_kind override (GRO-36: post-registration page mislabeled "landing") --


def test_screenshot_kind_for_matches_onboarding_keywords() -> None:
    assert (
        _screenshot_kind_for("Verify your email", "https://x.com/account/verify", 1)
        == ScreenshotKind.ONBOARDING
    )
    assert (
        _screenshot_kind_for("Welcome to Acme", "https://x.com/welcome", 2)
        == ScreenshotKind.ONBOARDING
    )


def test_depth_zero_defaults_to_landing_without_initial_kind(tmp_path: Path, page: Page) -> None:
    home_url = _data_url("<html><body><h1>Solo Page</h1></body></html>")
    engine, store, run_logger = _engine(page, tmp_path, max_pages=1, max_depth=0)

    engine.run(home_url)

    evidence = store.for_run("run-1")
    kinds = {
        e.visible_ui["screenshot_kind"]
        for e in evidence
        if e.visible_ui and "screenshot_kind" in e.visible_ui
    }
    assert kinds == {"landing"}
    store.close()


def test_initial_kind_overrides_depth_zero_classification(tmp_path: Path, page: Page) -> None:
    # Mirrors the orchestrator's post-registration crawl: its start_url is
    # wherever registration left the browser (e.g. a "Verify your email"
    # step), not the site's actual landing page -- _screenshot_kind_for's
    # unconditional depth==0 -> LANDING default would otherwise mislabel it.
    start_url = _data_url("<html><body><h1>Verify your email</h1></body></html>")
    engine, store, run_logger = _engine(page, tmp_path, max_pages=1, max_depth=0)

    engine.run(start_url, initial_kind=ScreenshotKind.ONBOARDING)

    evidence = store.for_run("run-1")
    kinds = {
        e.visible_ui["screenshot_kind"]
        for e in evidence
        if e.visible_ui and "screenshot_kind" in e.visible_ui
    }
    assert kinds == {"onboarding"}
    store.close()


def test_hidden_navigation_menu_reveals_additional_pages(tmp_path: Path, page: Page) -> None:
    hidden_url = _data_url("<html><body><h1>Hidden Page</h1></body></html>")
    home_url = _data_url(
        "<html><body>"
        "<button class='hamburger-menu' "
        "onclick=\"document.getElementById('m').style.display='block'\">"
        "☰</button>"
        f"<nav id='m' style='display:none'><a href='{hidden_url}'>Hidden Page</a></nav>"
        "</body></html>"
    )

    engine, store, run_logger = _engine(page, tmp_path, max_pages=5, max_depth=2)
    result = engine.run(home_url)

    visited_urls = {v.url for v in result.visited}
    assert hidden_url in visited_urls

    log_entries = run_logger.read_all()
    assert any(
        "hidden navigation" in entry.message
        for entry in log_entries
        if entry.event_type.value == "discovery"
    )

    store.close()
