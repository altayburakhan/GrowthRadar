from pathlib import Path

import pytest

from growthradar.history import RunHistoryStore
from growthradar.report import FinalReport
from growthradar.scoring import DimensionScore, ScoreResult


def _report(run_id: str, company: str, verdict: str, overall_score: float) -> FinalReport:
    dim = DimensionScore(name="x", score=0.0, signal_count=0, max_signals=1, evidence_ids=())
    score = ScoreResult(
        run_id=run_id,
        icp_fit=dim,
        onboarding_opportunity=dim,
        product_experience=dim,
        overall_score=overall_score,
        verdict=verdict,
    )
    return FinalReport(
        run_id=run_id,
        company=company,
        product_url=f"https://{company.lower()}.com",
        explored_pages=(),
        registration_completed=False,
        trial_available=False,
        onboarding_detected=False,
        evidence_collected=0,
        technologies_detected=(),
        product_update_pages=(),
        help_center_url=None,
        confidence_score=overall_score,
        verdict=verdict,
        final_recommendation="n/a",
        score=score,
    )


def test_record_stores_a_summary_row(tmp_path: Path) -> None:
    with RunHistoryStore(db_path=tmp_path / "h.db") as history:
        summary = history.record(_report("run-1", "Acme", "hot", 82.0))

        assert summary.id is not None
        assert summary.company == "Acme"
        assert summary.verdict == "hot"
        assert summary.overall_score == 82.0


def test_for_company_returns_only_that_companys_runs_oldest_first(tmp_path: Path) -> None:
    with RunHistoryStore(db_path=tmp_path / "h.db") as history:
        history.record(_report("run-1", "Acme", "cold", 20.0))
        history.record(_report("run-2", "OtherCo", "warm", 50.0))
        history.record(_report("run-3", "Acme", "warm", 55.0))

        acme_runs = history.for_company("Acme")

        assert [r.run_id for r in acme_runs] == ["run-1", "run-3"]
        assert acme_runs[0].overall_score == 20.0
        assert acme_runs[1].overall_score == 55.0


def test_recent_returns_newest_first_limited(tmp_path: Path) -> None:
    with RunHistoryStore(db_path=tmp_path / "h.db") as history:
        for i in range(5):
            history.record(_report(f"run-{i}", f"Company{i}", "warm", float(i)))

        recent = history.recent(limit=2)

        assert [r.run_id for r in recent] == ["run-4", "run-3"]


def test_top_scoring_orders_by_score_descending(tmp_path: Path) -> None:
    with RunHistoryStore(db_path=tmp_path / "h.db") as history:
        history.record(_report("run-1", "Low", "cold", 10.0))
        history.record(_report("run-2", "High", "hot", 90.0))
        history.record(_report("run-3", "Mid", "warm", 50.0))

        top = history.top_scoring(limit=2)

        assert [r.company for r in top] == ["High", "Mid"]


def test_history_persists_across_store_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "h.db"
    with RunHistoryStore(db_path=db_path) as history:
        history.record(_report("run-1", "Acme", "hot", 75.0))

    with RunHistoryStore(db_path=db_path) as reopened:
        runs = reopened.for_company("Acme")
        assert len(runs) == 1
        assert runs[0].run_id == "run-1"


def test_recording_the_same_run_id_twice_raises(tmp_path: Path) -> None:
    with RunHistoryStore(db_path=tmp_path / "h.db") as history:
        history.record(_report("run-1", "Acme", "hot", 75.0))
        with pytest.raises(Exception):  # noqa: B017 -- sqlite3.IntegrityError, not worth importing
            history.record(_report("run-1", "Acme", "hot", 75.0))
