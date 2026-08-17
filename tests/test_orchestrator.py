from pathlib import Path
from urllib.parse import quote

import pytest

import growthradar.orchestrator as orchestrator_module
from growthradar.config import Config
from growthradar.evidence import EvidenceStore
from growthradar.history import RunHistoryStore
from growthradar.orchestrator import (
    _find_registration_page_url,
    _normalize_target_url,
    run_growthradar_batch,
    run_growthradar_session,
)


def _data_url(html: str) -> str:
    return "data:text/html," + quote(html)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("GROWTHRADAR_DB_PATH", str(tmp_path / "growthradar.db"))
    monkeypatch.setenv("GROWTHRADAR_MAX_PAGES", "5")
    monkeypatch.setenv("GROWTHRADAR_CRAWL_DELAY", "0")
    return Config.from_env(env_path="/nonexistent/.env")


def test_normalize_target_url_adds_https_scheme_when_missing() -> None:
    assert _normalize_target_url("100hires.com") == "https://100hires.com"
    assert _normalize_target_url("  100hires.com  ") == "https://100hires.com"
    assert _normalize_target_url("https://100hires.com") == "https://100hires.com"
    assert _normalize_target_url("http://100hires.com") == "http://100hires.com"
    assert _normalize_target_url("data:text/html,<p>x</p>") == "data:text/html,<p>x</p>"


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
