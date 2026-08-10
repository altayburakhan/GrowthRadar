from pathlib import Path
from typing import Any

import pytest

from growthradar.config import Config
from growthradar.evidence import Evidence, EvidenceStore
from growthradar.scoring import score_and_record, score_run


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


_next_id = 0


def _evidence(
    label: str,
    *,
    url: str | None = None,
    screenshot: str | None = None,
    dom: Any = None,
    javascript: Any = None,
    network: Any = None,
    visible_ui: Any = None,
    confidence: float | None = None,
    run_id: str = "run-1",
) -> Evidence:
    global _next_id
    _next_id += 1
    return Evidence(
        id=_next_id,
        run_id=run_id,
        seq=_next_id,
        timestamp="2026-01-01T00:00:00+00:00",
        label=label,
        url=url,
        screenshot=screenshot,
        dom=dom,
        javascript=javascript,
        network=network,
        visible_ui=visible_ui,
        confidence=confidence,
    )


def _dom_evidence(url: str, interactive_count: int = 6) -> Evidence:
    return _evidence(
        f"dom: {url}",
        url=url,
        dom={
            "title": "Page",
            "html": "<html></html>",
            "navigation": [],
            "interactive_elements": [{"tag": "a"} for _ in range(interactive_count)],
            "truncated": False,
        },
    )


def _screenshot_evidence(kind: str, url: str, *, success: bool = True) -> Evidence:
    return _evidence(
        f"screenshot: {url}",
        url=url,
        screenshot=f"screenshots/{kind}.png" if success else None,
        visible_ui={"screenshot_kind": kind, "success": success, "error": None},
    )


def _js_evidence(
    url: str, tool_name: str, tool_confidence: float = 0.95, *, category: str = "onboarding"
) -> Evidence:
    return _evidence(
        f"js/network: {url}",
        url=url,
        javascript={
            "detected_tools": [
                {
                    "name": tool_name,
                    "category": category,
                    "signal_count": 3,
                    "confidence": tool_confidence,
                    "matched_scripts": [f"https://cdn.{tool_name.lower()}.com/x.js"],
                    "matched_globals": [],
                    "matched_network": [],
                }
            ]
        },
        network={"request_count": 1},
    )


def _registration_evidence(*, submitted: bool) -> Evidence:
    return _evidence(
        "registration attempt",
        visible_ui={
            "steps_completed": 2,
            "submitted": submitted,
            "verification_link_opened": False,
            "email": "test@example.com",
            "company_name": "Test Co",
        },
    )


def test_no_evidence_is_all_cold(config: Config) -> None:
    result = score_run("run-1", [], config)

    assert result.icp_fit.score == 0.0
    assert result.onboarding_opportunity.score == 0.0
    assert result.product_experience.score == 0.0
    assert result.overall_score == 0.0
    assert result.verdict == "cold"


def test_icp_fit_scales_with_signal_count(config: Config) -> None:
    single_signal = [_registration_evidence(submitted=True)]
    result = score_run("run-1", single_signal, config)
    assert result.icp_fit.score == 25.0
    assert result.icp_fit.signal_count == 1

    all_signals = [
        _registration_evidence(submitted=True),
        _screenshot_evidence("dashboard", "https://x.com/dashboard"),
        _screenshot_evidence("help_center", "https://x.com/help"),
        _dom_evidence("https://x.com/a"),
        _dom_evidence("https://x.com/b"),
        _dom_evidence("https://x.com/c"),
    ]
    result = score_run("run-1", all_signals, config)
    assert result.icp_fit.score == 100.0
    assert result.icp_fit.signal_count == 4


def test_onboarding_opportunity_rewards_competitor_tool(config: Config) -> None:
    evidence = [_js_evidence("https://x.com", "Pendo")]
    result = score_run("run-1", evidence, config)

    assert result.onboarding_opportunity.score == pytest.approx(100 / 3, abs=0.1)
    assert result.onboarding_opportunity.signal_count == 1


def test_onboarding_opportunity_treats_missing_category_as_onboarding(config: Config) -> None:
    # Evidence recorded before GRO-28 has no "category" key at all -- must
    # still be treated as an onboarding-tool signal, not silently dropped.
    evidence = [
        _evidence(
            "js/network: https://x.com",
            url="https://x.com",
            javascript={
                "detected_tools": [
                    {
                        "name": "Pendo",
                        "signal_count": 3,
                        "confidence": 0.95,
                        "matched_scripts": ["https://cdn.pendo.io/x.js"],
                        "matched_globals": [],
                        "matched_network": [],
                    }
                ]
            },
        )
    ]

    result = score_run("run-1", evidence, config)

    assert result.onboarding_opportunity.signal_count == 1


def test_onboarding_opportunity_ignores_analytics_category_tools(config: Config) -> None:
    # A confidently-detected Google Analytics is not an onboarding-tool signal
    # -- GRO-28 must exclude analytics-category detections from this score.
    evidence = [_js_evidence("https://x.com", "Google Analytics", category="analytics")]

    result = score_run("run-1", evidence, config)

    assert result.onboarding_opportunity.score == 0.0
    assert result.onboarding_opportunity.signal_count == 0


def test_onboarding_opportunity_ignores_ai_assistant_category_tools(config: Config) -> None:
    evidence = [_js_evidence("https://x.com", "Drift", category="ai_assistant")]

    result = score_run("run-1", evidence, config)

    assert result.onboarding_opportunity.score == 0.0
    assert result.onboarding_opportunity.signal_count == 0


def test_onboarding_opportunity_forced_low_when_userguiding_detected_via_js(
    config: Config,
) -> None:
    evidence = [
        _js_evidence("https://x.com", "UserGuiding"),
        _js_evidence("https://x.com", "Pendo"),  # should be irrelevant once disqualified
    ]
    result = score_run("run-1", evidence, config)

    assert result.onboarding_opportunity.score == 5.0
    assert "not a prospect" in result.onboarding_opportunity.notes[0]


def test_onboarding_opportunity_ignores_low_confidence_tool_detections(config: Config) -> None:
    evidence = [_js_evidence("https://x.com", "Pendo", tool_confidence=0.4)]
    result = score_run("run-1", evidence, config)

    assert result.onboarding_opportunity.score == 0.0


def test_product_experience_penalized_by_high_error_rate(config: Config) -> None:
    evidence = [
        _dom_evidence("https://x.com/a", interactive_count=1),
        _evidence("failed to load https://x.com/b", url="https://x.com/b"),
        _evidence("failed to load https://x.com/c", url="https://x.com/c"),
    ]
    result = score_run("run-1", evidence, config)

    assert result.product_experience.signal_count == 0


def test_product_experience_rewards_completed_registration_and_rich_dom(config: Config) -> None:
    evidence = [
        _registration_evidence(submitted=True),
        _dom_evidence("https://x.com/a", interactive_count=10),
        _screenshot_evidence("dashboard", "https://x.com/a", success=True),
    ]
    result = score_run("run-1", evidence, config)

    assert result.product_experience.signal_count >= 3


def test_overall_score_uses_configured_weights(config: Config) -> None:
    evidence = [_registration_evidence(submitted=True)]
    result = score_run("run-1", evidence, config)

    expected = round(
        result.icp_fit.score * config.weight_icp_fit
        + result.onboarding_opportunity.score * config.weight_onboarding_opportunity
        + result.product_experience.score * config.weight_product_experience,
        1,
    )
    assert result.overall_score == expected


def test_verdict_respects_hot_and_warm_thresholds(config: Config) -> None:
    full_signals = [
        _registration_evidence(submitted=True),
        _screenshot_evidence("dashboard", "https://x.com/dashboard"),
        _screenshot_evidence("help_center", "https://x.com/help"),
        _dom_evidence("https://x.com/a", interactive_count=10),
        _dom_evidence("https://x.com/b", interactive_count=10),
        _dom_evidence("https://x.com/c", interactive_count=10),
        _js_evidence("https://x.com", "Pendo"),
        _screenshot_evidence("product_updates", "https://x.com/changelog"),
    ]
    result = score_run("run-1", full_signals, config)
    assert result.overall_score >= config.hot_threshold
    assert result.verdict == "hot"

    no_signals_result = score_run("run-1", [], config)
    assert no_signals_result.verdict == "cold"


def test_dimension_scores_cite_evidence_ids(config: Config) -> None:
    reg = _registration_evidence(submitted=True)
    result = score_run("run-1", [reg], config)

    assert reg.id in result.icp_fit.evidence_ids


def test_score_and_record_writes_evidence(tmp_path: Path, config: Config) -> None:
    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        store.add("run-1", "registration attempt", visible_ui={"submitted": True})
        store.add(
            "run-1",
            "screenshot: https://x.com/dashboard",
            url="https://x.com/dashboard",
            visible_ui={"screenshot_kind": "dashboard", "success": True, "error": None},
        )

        result = score_and_record(store, "run-1", config)

        all_evidence = store.for_run("run-1")
        final_score_rows = [e for e in all_evidence if e.label == "final score"]
        assert len(final_score_rows) == 1
        assert final_score_rows[0].visible_ui["verdict"] == result.verdict
        assert final_score_rows[0].confidence == result.overall_score / 100.0
