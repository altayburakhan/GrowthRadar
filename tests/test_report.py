import json
from pathlib import Path
from typing import Any

import pytest

from growthradar.config import Config
from growthradar.evidence import Evidence, EvidenceStore
from growthradar.report import generate_and_record, generate_report, to_json, to_markdown
from growthradar.scoring import score_run

_next_id = 0


def _evidence(
    label: str,
    *,
    url: str | None = None,
    screenshot: str | None = None,
    dom: Any = None,
    javascript: Any = None,
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
        network=None,
        visible_ui=visible_ui,
        confidence=confidence,
    )


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


def test_company_name_prefers_registration_evidence(config: Config) -> None:
    evidence = [
        _evidence(
            "registration attempt",
            visible_ui={"submitted": True, "company_name": "Acme Analytics"},
        ),
        _evidence("dom: https://acme.com", url="https://acme.com"),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.company == "Acme Analytics"
    assert report.registration_completed is True


def test_company_name_falls_back_to_domain(config: Config) -> None:
    evidence = [
        _evidence(
            "screenshot: https://www.acme.io",
            url="https://www.acme.io",
            visible_ui={"screenshot_kind": "landing", "success": True},
        ),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.company == "Acme"
    assert report.product_url == "https://www.acme.io"


def test_explored_pages_lists_dom_evidence_urls(config: Config) -> None:
    evidence = [
        _evidence("dom: https://acme.com/a", url="https://acme.com/a"),
        _evidence("dom: https://acme.com/b", url="https://acme.com/b"),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert set(report.explored_pages) == {"https://acme.com/a", "https://acme.com/b"}
    assert report.evidence_collected == 2


def test_trial_available_detected_from_dom_html(config: Config) -> None:
    evidence = [
        _evidence(
            "dom: https://acme.com",
            url="https://acme.com",
            dom={"html": "<button>Start your free trial today</button>"},
        )
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.trial_available is True


def test_trial_not_claimed_without_evidence(config: Config) -> None:
    evidence = [
        _evidence("dom: https://acme.com", url="https://acme.com", dom={"html": "<p>hi</p>"})
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.trial_available is False


def test_technologies_and_product_updates_and_help_center(config: Config) -> None:
    evidence = [
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={
                "detected_tools": [
                    {"name": "Pendo", "confidence": 0.95},
                    {"name": "Intercom", "confidence": 0.3},  # below threshold, excluded
                ]
            },
        ),
        _evidence(
            "screenshot: https://acme.com/changelog",
            url="https://acme.com/changelog",
            visible_ui={"screenshot_kind": "product_updates", "success": True},
        ),
        _evidence(
            "screenshot: https://acme.com/help",
            url="https://acme.com/help",
            visible_ui={"screenshot_kind": "help_center", "success": True},
        ),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.technologies_detected == ("Pendo",)
    assert report.product_update_pages == ("https://acme.com/changelog",)
    assert report.help_center_url == "https://acme.com/help"


def test_technologies_detected_includes_analytics_tools(config: Config) -> None:
    # GRO-28: analytics providers enrich this field even though they never
    # influence onboarding_opportunity/ICP-fit scoring.
    evidence = [
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={
                "detected_tools": [
                    {"name": "Pendo", "category": "onboarding", "confidence": 0.95},
                    {"name": "Google Analytics", "category": "analytics", "confidence": 0.95},
                ]
            },
        ),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert set(report.technologies_detected) == {"Pendo", "Google Analytics"}


def test_technologies_detected_includes_ai_assistant_tools(config: Config) -> None:
    evidence = [
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={
                "detected_tools": [
                    {"name": "Drift", "category": "ai_assistant", "confidence": 0.95},
                ]
            },
        ),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert "Drift" in report.technologies_detected


def test_onboarding_detected_requires_confident_evidence(config: Config) -> None:
    low_confidence = [
        _evidence("onboarding heuristics: https://acme.com", url="https://acme.com", confidence=0.4)
    ]
    score = score_run("run-1", low_confidence, config)
    report = generate_report("run-1", low_confidence, score)
    assert report.onboarding_detected is False

    high_confidence = [
        _evidence(
            "onboarding heuristics: https://acme.com", url="https://acme.com", confidence=0.85
        )
    ]
    score2 = score_run("run-1", high_confidence, config)
    report2 = generate_report("run-1", high_confidence, score2)
    assert report2.onboarding_detected is True


def test_recommendation_reflects_hot_verdict(config: Config) -> None:
    evidence = [
        _evidence("registration attempt", visible_ui={"submitted": True}),
        _evidence(
            "screenshot: https://acme.com/dashboard",
            url="https://acme.com/dashboard",
            visible_ui={"screenshot_kind": "dashboard", "success": True},
        ),
        _evidence(
            "screenshot: https://acme.com/help",
            url="https://acme.com/help",
            visible_ui={"screenshot_kind": "help_center", "success": True},
        ),
        _evidence("dom: https://acme.com/a", url="https://acme.com/a"),
        _evidence("dom: https://acme.com/b", url="https://acme.com/b"),
        _evidence("dom: https://acme.com/c", url="https://acme.com/c"),
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={"detected_tools": [{"name": "Pendo", "confidence": 0.95}]},
        ),
        _evidence(
            "screenshot: https://acme.com/changelog",
            url="https://acme.com/changelog",
            visible_ui={"screenshot_kind": "product_updates", "success": True},
        ),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.verdict == "hot"
    assert "pursue outreach" in report.final_recommendation.lower()


def test_recommendation_flags_existing_userguiding_customer(config: Config) -> None:
    evidence = [
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={"detected_tools": [{"name": "UserGuiding", "confidence": 0.95}]},
        ),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert "not a sales prospect" in report.final_recommendation


def test_competitor_tools_detected_lists_rival_onboarding_tools(config: Config) -> None:
    evidence = [
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={
                "detected_tools": [
                    {"name": "Pendo", "category": "onboarding", "confidence": 0.95},
                    {"name": "WalkMe", "category": "onboarding", "confidence": 0.95},
                    # Excluded: analytics category, not an onboarding-tool signal.
                    {"name": "Google Analytics", "category": "analytics", "confidence": 0.95},
                ]
            },
        ),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.competitor_tools_detected == ("Pendo", "WalkMe")
    assert report.already_userguiding_customer is False


def test_competitor_tools_detected_excludes_userguiding_itself(config: Config) -> None:
    evidence = [
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={"detected_tools": [{"name": "UserGuiding", "confidence": 0.95}]},
        ),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.competitor_tools_detected == ()
    assert report.already_userguiding_customer is True


def test_to_markdown_shows_competitor_callout(config: Config) -> None:
    evidence = [
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={"detected_tools": [{"name": "Pendo", "confidence": 0.95}]},
        ),
    ]
    score = score_run("run-1", evidence, config)
    report = generate_report("run-1", evidence, score)

    markdown = to_markdown(report)

    assert "Competitor tool(s) detected: Pendo" in markdown
    assert "switching/replacing" in markdown


def test_to_markdown_shows_existing_customer_callout(config: Config) -> None:
    evidence = [
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={"detected_tools": [{"name": "UserGuiding", "confidence": 0.95}]},
        ),
    ]
    score = score_run("run-1", evidence, config)
    report = generate_report("run-1", evidence, score)

    markdown = to_markdown(report)

    assert "Already a UserGuiding customer" in markdown
    assert "Competitor tool(s) detected" not in markdown


def test_to_markdown_contains_key_fields(config: Config) -> None:
    evidence = [_evidence("registration attempt", visible_ui={"submitted": True})]
    score = score_run("run-1", evidence, config)
    report = generate_report("run-1", evidence, score)

    markdown = to_markdown(report)

    assert "# GrowthRadar Report" in markdown
    assert "Final recommendation" in markdown
    assert "Score breakdown" in markdown


def test_llm_summary_defaults_to_none(config: Config) -> None:
    evidence = [_evidence("registration attempt", visible_ui={"submitted": True})]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.llm_summary is None
    assert "AI summary" not in to_markdown(report)


def test_llm_summary_is_read_back_from_evidence_and_rendered(config: Config) -> None:
    evidence = [
        _evidence("registration attempt", visible_ui={"submitted": True}),
        _evidence(
            "llm summary", visible_ui={"summary": "Strong prospect, has no onboarding tool."}
        ),
    ]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.llm_summary == "Strong prospect, has no onboarding tool."
    markdown = to_markdown(report)
    assert "## AI summary" in markdown
    assert "Strong prospect, has no onboarding tool." in markdown


def test_report_is_not_partial_by_default(config: Config) -> None:
    evidence = [_evidence("registration attempt", visible_ui={"submitted": True})]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score)

    assert report.partial_run is False
    assert report.run_errors == ()
    assert "Partial run" not in to_markdown(report)


def test_report_flags_partial_run_with_errors(config: Config) -> None:
    evidence = [_evidence("registration attempt", visible_ui={"submitted": True})]
    score = score_run("run-1", evidence, config)

    report = generate_report("run-1", evidence, score, run_errors=("exploration: boom",))

    assert report.partial_run is True
    assert report.run_errors == ("exploration: boom",)
    markdown = to_markdown(report)
    assert "Partial run" in markdown
    assert "exploration: boom" in markdown


def test_to_json_roundtrips(config: Config) -> None:
    evidence = [_evidence("registration attempt", visible_ui={"submitted": True})]
    score = score_run("run-1", evidence, config)
    report = generate_report("run-1", evidence, score)

    payload = json.loads(to_json(report))

    assert payload["run_id"] == "run-1"
    assert payload["verdict"] == report.verdict
    assert payload["score"]["icp_fit"]["score"] == report.score.icp_fit.score


def test_generate_and_record_end_to_end(tmp_path: Path, config: Config) -> None:
    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        store.add("run-1", "registration attempt", visible_ui={"submitted": True})
        store.add(
            "run-1",
            "screenshot: https://acme.com/dashboard",
            url="https://acme.com/dashboard",
            visible_ui={"screenshot_kind": "dashboard", "success": True},
        )

        report = generate_and_record(store, "run-1", config)

        all_evidence = store.for_run("run-1")
        report_rows = [e for e in all_evidence if e.label == "final report"]
        assert len(report_rows) == 1
        assert report_rows[0].visible_ui["verdict"] == report.verdict
        # generate_and_record should not also write a separate "final score" row.
        assert not any(e.label == "final score" for e in all_evidence)
