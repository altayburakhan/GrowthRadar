import json
from pathlib import Path
from urllib.parse import quote

import pytest

from growthradar.cli import main
from growthradar.orchestrator import RunOutcome
from growthradar.report import FinalReport
from growthradar.scoring import DimensionScore, ScoreResult


def _data_url(html: str) -> str:
    return "data:text/html," + quote(html)


def _fake_outcome(run_id: str = "run-1", *, errors: tuple[str, ...] = ()) -> RunOutcome:
    dim = DimensionScore(name="x", score=50.0, signal_count=1, max_signals=2, evidence_ids=(1,))
    score = ScoreResult(
        run_id=run_id,
        icp_fit=dim,
        onboarding_opportunity=dim,
        product_experience=dim,
        overall_score=50.0,
        verdict="warm",
    )
    report = FinalReport(
        run_id=run_id,
        company="Acme",
        product_url="https://acme.com",
        explored_pages=("https://acme.com",),
        registration_completed=False,
        trial_available=False,
        onboarding_detected=False,
        evidence_collected=3,
        technologies_detected=(),
        product_update_pages=(),
        help_center_url=None,
        confidence_score=50.0,
        verdict="warm",
        final_recommendation="Moderate prospect.",
        score=score,
    )
    return RunOutcome(
        run_id=run_id,
        report=report,
        exploration=None,
        registration=None,
        post_registration_exploration=None,
        history=None,
        errors=errors,
    )


def test_main_prints_markdown_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("growthradar.cli.run_growthradar_session", lambda *a, **kw: _fake_outcome())

    exit_code = main(["https://acme.com"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "# GrowthRadar Report -- Acme" in out


def test_main_prints_json_when_requested(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("growthradar.cli.run_growthradar_session", lambda *a, **kw: _fake_outcome())

    exit_code = main(["https://acme.com", "--output", "json"])

    out = capsys.readouterr().out
    assert exit_code == 0
    payload = json.loads(out)
    assert payload["company"] == "Acme"


def test_main_writes_to_output_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("growthradar.cli.run_growthradar_session", lambda *a, **kw: _fake_outcome())
    out_file = tmp_path / "report.md"

    exit_code = main(["https://acme.com", "--output-file", str(out_file)])

    assert exit_code == 0
    assert out_file.exists()
    assert "GrowthRadar Report" in out_file.read_text(encoding="utf-8")


def test_main_passes_max_pages_override_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_run(url, *, config, **kwargs):  # noqa: ANN001
        captured["max_pages"] = config.max_pages
        return _fake_outcome()

    monkeypatch.setattr("growthradar.cli.run_growthradar_session", fake_run)

    main(["https://acme.com", "--max-pages", "3"])

    assert captured["max_pages"] == 3


def test_main_reports_config_error_with_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GROWTHRADAR_LLM_PROVIDER", "not-a-real-provider")

    exit_code = main(["https://acme.com"])

    assert exit_code == 2
    assert "Configuration error" in capsys.readouterr().err


def test_main_handles_unexpected_orchestrator_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*a, **kw):  # noqa: ANN001
        raise RuntimeError("kaboom")

    monkeypatch.setattr("growthradar.cli.run_growthradar_session", boom)

    exit_code = main(["https://acme.com"])

    assert exit_code == 1
    assert "GrowthRadar run failed" in capsys.readouterr().err


def test_main_prints_warnings_for_partial_run_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "growthradar.cli.run_growthradar_session",
        lambda *a, **kw: _fake_outcome(errors=("exploration: boom",)),
    )

    exit_code = main(["https://acme.com"])

    assert exit_code == 0
    assert "exploration: boom" in capsys.readouterr().err


def test_main_end_to_end_with_real_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GROWTHRADAR_DB_PATH", str(tmp_path / "growthradar.db"))
    monkeypatch.setenv("GROWTHRADAR_MAX_PAGES", "2")
    monkeypatch.setenv("GROWTHRADAR_CRAWL_DELAY", "0")
    monkeypatch.chdir(tmp_path)

    url = _data_url("<html><body><h1>Real page</h1></body></html>")

    exit_code = main([url, "--output", "json"])

    out = capsys.readouterr().out
    assert exit_code == 0
    payload = json.loads(out)
    assert payload["evidence_collected"] > 0
