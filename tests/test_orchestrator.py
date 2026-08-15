from pathlib import Path
from urllib.parse import quote

import pytest

import growthradar.orchestrator as orchestrator_module
from growthradar.browser import BrowserSession
from growthradar.config import Config
from growthradar.evidence import EvidenceStore
from growthradar.history import RunHistoryStore
from growthradar.orchestrator import (
    _find_registration_page_url,
    _normalize_target_url,
    run_growthradar_batch,
    run_growthradar_session,
)

_FAKE_BASE = "https://fake.growthradar.test"


def _data_url(html: str) -> str:
    return "data:text/html," + quote(html)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("GROWTHRADAR_DB_PATH", str(tmp_path / "growthradar.db"))
    monkeypatch.setenv("GROWTHRADAR_MAX_PAGES", "5")
    monkeypatch.setenv("GROWTHRADAR_CRAWL_DELAY", "0")
    return Config.from_env(env_path="/nonexistent/.env")


def test_full_pipeline_on_a_simple_single_page_site(tmp_path: Path, config: Config) -> None:
    url = _data_url("<html><body><h1>Just a landing page</h1></body></html>")

    outcome = run_growthradar_session(url, config=config, run_id="run-1", log_dir=tmp_path)

    assert outcome.run_id == "run-1"
    assert outcome.exploration is not None
    assert len(outcome.exploration.visited) == 1
    assert outcome.exploration.visited[0].success is True
    assert outcome.registration is None  # no registration page discovered
    assert outcome.post_registration_exploration is None
    assert outcome.report.evidence_collected > 0
    assert outcome.errors == ()


def test_full_pipeline_completes_registration_when_signup_page_is_found(
    tmp_path: Path, config: Config
) -> None:
    signup_html = (
        "<html><body>"
        "<input name='email' type='email' />"
        "<input name='password' type='password' />"
        '<button onclick="document.body.insertAdjacentHTML('
        "'beforeend', '<div id=done>Welcome!</div>')\">Sign up</button>"
        "</body></html>"
    )
    signup_url = _data_url(signup_html)
    home_url = _data_url(f"<html><body><nav><a href='{signup_url}'>Sign up</a></nav></body></html>")

    outcome = run_growthradar_session(home_url, config=config, run_id="run-2", log_dir=tmp_path)

    assert outcome.registration is not None
    assert outcome.registration.submitted is True
    assert outcome.report.registration_completed is True
    assert len(outcome.exploration.visited) == 2  # type: ignore[union-attr]


def _routed_browser_session_class(pages: dict[str, str]) -> type[BrowserSession]:
    """A BrowserSession subclass that fulfills requests to _FAKE_BASE from `pages`
    instead of touching the real network -- lets a clicked <a href> perform a
    real (non-data:) navigation, which data: URLs cannot do once a page has
    already loaded (Chromium blocks renderer-initiated navigation *to* a new
    data: URL after the first navigation, even from a genuine link click)."""

    def handler(route):  # noqa: ANN001
        body = pages.get(route.request.url, "<html><body>missing</body></html>")
        route.fulfill(status=200, content_type="text/html", body=body)

    class _RoutedBrowserSession(BrowserSession):
        def start(self):  # type: ignore[override]
            page = super().start()
            page.route(f"{_FAKE_BASE}/**", handler)
            return page

    return _RoutedBrowserSession


def test_post_registration_exploration_discovers_the_authenticated_app(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_url = f"{_FAKE_BASE}/"
    signup_url = f"{_FAKE_BASE}/signup"
    dashboard_url = f"{_FAKE_BASE}/dashboard"
    settings_url = f"{_FAKE_BASE}/settings"

    pages = {
        home_url: f"<html><body><nav><a href='{signup_url}'>Sign up</a></nav></body></html>",
        signup_url: (
            "<html><body>"
            "<input name='email' type='email' />"
            "<input name='password' type='password' />"
            f"<a href='{dashboard_url}'>Sign up</a>"
            "</body></html>"
        ),
        dashboard_url: (
            "<html><body><h1>Dashboard</h1>"
            "<div class='onboarding-checklist'>Welcome! Here is your onboarding checklist</div>"
            f"<nav><a href='{settings_url}'>Settings</a></nav>"
            "</body></html>"
        ),
        settings_url: "<html><body><h1>Settings</h1><p>Manage your account</p></body></html>",
    }

    monkeypatch.setattr(
        "growthradar.orchestrator.BrowserSession", _routed_browser_session_class(pages)
    )

    # max_depth=1 bounds the *pre-registration* crawl to home + signup only, so
    # dashboard/settings (reachable one hop past signup) are provably only
    # found via the new post-registration pass, not leftover pre-reg crawling.
    outcome = run_growthradar_session(
        home_url, config=config, run_id="run-post-reg", log_dir=tmp_path, max_depth=1
    )

    pre_registration_urls = {v.url for v in outcome.exploration.visited}  # type: ignore[union-attr]
    assert dashboard_url not in pre_registration_urls
    assert settings_url not in pre_registration_urls

    assert outcome.registration is not None
    assert outcome.registration.submitted is True

    assert outcome.post_registration_exploration is not None
    post_registration_urls = {
        v.url for v in outcome.post_registration_exploration.visited if v.success
    }
    assert dashboard_url in post_registration_urls
    assert settings_url in post_registration_urls

    assert dashboard_url in outcome.report.explored_pages
    assert settings_url in outcome.report.explored_pages


def test_post_registration_exploration_is_skipped_when_browser_ends_up_off_site(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression (100hires.com): a signup form's submit button can accidentally
    # trigger a redirect to a third-party domain (OAuth or otherwise). Post-
    # registration exploration must not then wander that unrelated site and
    # contaminate onboarding/scoring evidence with it -- it should just skip.
    home_url = f"{_FAKE_BASE}/"
    signup_url = f"{_FAKE_BASE}/signup"
    offsite_url = "https://oauth-provider.test/callback"

    pages = {
        home_url: f"<html><body><nav><a href='{signup_url}'>Sign up</a></nav></body></html>",
        signup_url: (
            "<html><body>"
            "<input name='email' type='email' />"
            "<input name='password' type='password' />"
            f"<a href='{offsite_url}'>Sign up</a>"
            "</body></html>"
        ),
        offsite_url: "<html><body><h1>Sign in with Provider</h1></body></html>",
    }

    def handler(route):  # noqa: ANN001
        body = pages.get(route.request.url, "<html><body>missing</body></html>")
        route.fulfill(status=200, content_type="text/html", body=body)

    class _AnyDomainRoutedBrowserSession(BrowserSession):
        def start(self):  # type: ignore[override]
            page = super().start()
            page.route("**/*", handler)
            return page

    monkeypatch.setattr("growthradar.orchestrator.BrowserSession", _AnyDomainRoutedBrowserSession)

    outcome = run_growthradar_session(
        home_url, config=config, run_id="run-offsite", log_dir=tmp_path, max_depth=1
    )

    assert outcome.registration is not None
    assert outcome.registration.submitted is True
    assert outcome.post_registration_exploration is None
    assert outcome.errors == ()


def test_post_registration_exploration_respects_its_own_page_budget(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_url = f"{_FAKE_BASE}/"
    signup_url = f"{_FAKE_BASE}/signup"
    dashboard_url = f"{_FAKE_BASE}/dashboard"
    settings_url = f"{_FAKE_BASE}/settings"

    pages = {
        home_url: f"<html><body><nav><a href='{signup_url}'>Sign up</a></nav></body></html>",
        signup_url: (
            "<html><body>"
            "<input name='email' type='email' /><input name='password' type='password' />"
            f"<a href='{dashboard_url}'>Sign up</a>"
            "</body></html>"
        ),
        dashboard_url: f"<html><body><a href='{settings_url}'>Settings</a></body></html>",
        settings_url: "<html><body>Settings</body></html>",
    }
    monkeypatch.setattr(
        "growthradar.orchestrator.BrowserSession", _routed_browser_session_class(pages)
    )

    outcome = run_growthradar_session(
        home_url,
        config=config,
        run_id="run-budget",
        log_dir=tmp_path,
        max_depth=1,
        max_post_registration_pages=1,
    )

    assert outcome.post_registration_exploration is not None
    assert len(outcome.post_registration_exploration.visited) == 1
    assert outcome.post_registration_exploration.stopped_reason == "max_pages_reached"


def test_normalize_target_url_adds_https_scheme_when_missing() -> None:
    assert _normalize_target_url("100hires.com") == "https://100hires.com"
    assert _normalize_target_url("  100hires.com  ") == "https://100hires.com"
    assert _normalize_target_url("https://100hires.com") == "https://100hires.com"
    assert _normalize_target_url("http://100hires.com") == "http://100hires.com"
    assert _normalize_target_url("data:text/html,<p>x</p>") == "data:text/html,<p>x</p>"


def test_schemeless_url_is_normalized_and_still_explores(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A URL typed into the dashboard without "https://" (e.g. "100hires.com")
    # must not be rejected by Playwright's navigation -- it should behave
    # exactly like typing it into a browser address bar.
    home_url = f"{_FAKE_BASE}/"
    pages = {home_url: "<html><body><h1>Home</h1></body></html>"}
    monkeypatch.setattr(
        "growthradar.orchestrator.BrowserSession", _routed_browser_session_class(pages)
    )
    bare_url = home_url.removeprefix("https://")

    outcome = run_growthradar_session(
        bare_url,
        config=config,
        run_id="run-normalize",
        log_dir=tmp_path,
        attempt_registration=False,
    )

    assert outcome.exploration is not None
    assert outcome.exploration.visited[0].success is True
    assert outcome.exploration.visited[0].url == home_url
    assert outcome.report.company != "Unknown"
    assert outcome.report.product_url == home_url


def test_report_is_persisted_and_readable_after_run(tmp_path: Path, config: Config) -> None:
    url = _data_url("<html><body><p>Hello</p></body></html>")

    outcome = run_growthradar_session(url, config=config, run_id="run-3", log_dir=tmp_path)

    with EvidenceStore(db_path=config.db_path) as reopened:
        rows = reopened.for_run("run-3")
        report_rows = [e for e in rows if e.label == "final report"]
        assert len(report_rows) == 1
        assert report_rows[0].visible_ui["verdict"] == outcome.report.verdict


def test_run_history_is_recorded_and_queryable_across_runs(tmp_path: Path, config: Config) -> None:
    url = _data_url("<html><body><p>Hello</p></body></html>")

    outcome = run_growthradar_session(url, config=config, run_id="run-hist", log_dir=tmp_path)

    assert outcome.history is not None
    assert outcome.history.run_id == "run-hist"
    assert outcome.history.verdict == outcome.report.verdict

    with RunHistoryStore(db_path=config.db_path) as reopened:
        recent = reopened.recent(limit=5)
        assert any(r.run_id == "run-hist" for r in recent)


def test_browser_failure_is_isolated_and_still_produces_a_report(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _ExplodingBrowserSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "_ExplodingBrowserSession":
            raise RuntimeError("browser boom")

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr("growthradar.orchestrator.BrowserSession", _ExplodingBrowserSession)

    outcome = run_growthradar_session(
        "https://example.com", config=config, run_id="run-4", log_dir=tmp_path
    )

    assert outcome.exploration is None
    assert outcome.registration is None
    assert any(e.startswith("browser:") for e in outcome.errors)
    assert outcome.report is not None
    # No page evidence was collected -- the browser never started -- but the
    # llm_summary phase still runs (independent of the browser) and leaves its
    # own "skipped" marker row (no Groq key configured in the test config).
    assert outcome.report.evidence_collected == 1
    assert outcome.report.partial_run is True
    assert outcome.report.run_errors == outcome.errors


def test_llm_summary_failure_is_isolated_and_still_produces_a_report(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a, **kw):  # noqa: ANN001
        raise RuntimeError("groq exploded")

    monkeypatch.setattr("growthradar.orchestrator.record_llm_summary", boom)

    url = _data_url("<html><body><p>Hello</p></body></html>")
    outcome = run_growthradar_session(url, config=config, run_id="run-llm-fail", log_dir=tmp_path)

    assert any(e.startswith("llm summary:") for e in outcome.errors)
    assert outcome.report is not None
    assert outcome.report.partial_run is True
    assert outcome.report.llm_summary is None


def test_registration_falls_back_to_modal_entry_point_when_no_distinct_url_found(
    tmp_path: Path, config: Config
) -> None:
    # GRO-32: mirrors allevents.in -- no distinct signup URL anywhere on the
    # site, only a "Sign in" button that reveals a client-side email-then-name
    # modal. _find_registration_page_url finds nothing (the crawl never sees
    # a page classified "registration"), so registration must fall back to
    # open_registration_entry_point on the landing page instead of skipping.
    home_html = (
        "<html><body>"
        "<button onclick=\"document.getElementById('modal').style.display='block';\">"
        "Sign in</button>"
        "<div id='modal' style='display:none'>"
        "<input type='email' placeholder='Email' />"
        '<button onclick="'
        "document.getElementById('modal').style.display='none';"
        "document.getElementById('step2').style.display='block';"
        '">Continue</button>'
        "</div>"
        "<div id='step2' style='display:none'>"
        "<input type='text' placeholder='First Name' />"
        "<input type='text' placeholder='Last Name' />"
        "<button onclick=\"document.title='registered';\">Register</button>"
        "</div>"
        "</body></html>"
    )
    home_url = _data_url(home_html)

    outcome = run_growthradar_session(home_url, config=config, run_id="run-modal", log_dir=tmp_path)

    assert outcome.registration is not None
    assert outcome.registration.submitted is True
    assert outcome.report.registration_completed is True
    assert outcome.errors == ()


def test_find_registration_page_url_prefers_the_actual_signup_form_over_a_pricing_page(
    config: Config,
) -> None:
    # Regression (statusbrew.com): a "Free Trial" nav link classified
    # /pricing as "registration" -- a plan-comparison page with no form on
    # it at all -- while the real signup form (space.statusbrew.com/
    # get-started, titled "Sign up | Statusbrew") was visited moments later
    # and, before this fix, was never picked because the first match always
    # won. The fix must prefer whichever candidate's own URL+title most
    # directly names the signup flow, regardless of visit order.
    with EvidenceStore(db_path=config.db_path) as store:
        store.add(
            "run-1",
            "screenshot: Statusbrew Pricing - Find Out How Much Statusbrew Costs?",
            url="https://statusbrew.com/pricing",
            visible_ui={"screenshot_kind": "registration", "success": True},
        )
        store.add(
            "run-1",
            "screenshot: Sign up | Statusbrew",
            url="https://space.statusbrew.com/get-started",
            visible_ui={"screenshot_kind": "registration", "success": True},
        )

        assert (
            _find_registration_page_url(store, "run-1")
            == "https://space.statusbrew.com/get-started"
        )


def test_find_registration_page_url_falls_back_to_the_only_candidate(config: Config) -> None:
    with EvidenceStore(db_path=config.db_path) as store:
        store.add(
            "run-1",
            "screenshot: Sign up",
            url="https://example.com/signup",
            visible_ui={"screenshot_kind": "registration", "success": True},
        )

        assert _find_registration_page_url(store, "run-1") == "https://example.com/signup"


def test_find_registration_page_url_returns_none_without_a_registration_candidate(
    config: Config,
) -> None:
    with EvidenceStore(db_path=config.db_path) as store:
        store.add(
            "run-1",
            "screenshot: Home",
            url="https://example.com",
            visible_ui={"screenshot_kind": "landing", "success": True},
        )

        assert _find_registration_page_url(store, "run-1") is None


def test_run_id_is_auto_generated_when_not_provided(tmp_path: Path, config: Config) -> None:
    url = _data_url("<html><body>Hi</body></html>")

    outcome = run_growthradar_session(url, config=config, log_dir=tmp_path)

    assert outcome.run_id
    assert outcome.report.run_id == outcome.run_id


def test_batch_processes_every_url_and_returns_one_outcome_each(
    tmp_path: Path, config: Config
) -> None:
    urls = [
        _data_url("<html><body>Site A</body></html>"),
        _data_url("<html><body>Site B</body></html>"),
    ]

    outcomes = run_growthradar_batch(urls, config=config, log_dir=tmp_path)

    assert len(outcomes) == 2
    assert all(o.report is not None for o in outcomes)
    assert len({o.run_id for o in outcomes}) == 2  # each got its own auto run_id


def test_batch_continues_after_one_url_totally_fails(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    good_url = _data_url("<html><body>Fine</body></html>")
    bad_url = "https://this-url-triggers-the-fake-crash.example"

    real_session = orchestrator_module.run_growthradar_session

    def flaky(url: str, **kwargs: object) -> object:
        if url == bad_url:
            raise RuntimeError("totally unexpected crash")
        return real_session(url, **kwargs)

    monkeypatch.setattr("growthradar.orchestrator.run_growthradar_session", flaky)

    outcomes = run_growthradar_batch([good_url, bad_url, good_url], config=config, log_dir=tmp_path)

    assert len(outcomes) == 3
    assert outcomes[0].report.evidence_collected > 0
    assert outcomes[1].report.partial_run is True
    assert any("totally unexpected crash" in e for e in outcomes[1].errors)
    assert outcomes[2].report.evidence_collected > 0
