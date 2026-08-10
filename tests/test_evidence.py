from pathlib import Path

import pytest

from growthradar.config import Config
from growthradar.evidence import EvidenceStore


def test_add_assigns_incrementing_seq_per_run(tmp_path: Path) -> None:
    with EvidenceStore(db_path=tmp_path / "evidence.db") as store:
        e1 = store.add("run-1", "landing page loaded", url="https://example.com")
        e2 = store.add("run-1", "signup form found", url="https://example.com/signup")

        assert e1.seq == 1
        assert e2.seq == 2
        assert e1.id is not None and e2.id is not None
        assert e1.id != e2.id


def test_seq_is_isolated_per_run(tmp_path: Path) -> None:
    with EvidenceStore(db_path=tmp_path / "evidence.db") as store:
        store.add("run-1", "first in run 1")
        e = store.add("run-2", "first in run 2")

        assert e.seq == 1


def test_for_run_returns_only_that_runs_evidence_in_order(tmp_path: Path) -> None:
    with EvidenceStore(db_path=tmp_path / "evidence.db") as store:
        store.add("run-1", "a")
        store.add("run-2", "x")
        store.add("run-1", "b")

        results = store.for_run("run-1")

        assert [e.label for e in results] == ["a", "b"]
        assert all(e.run_id == "run-1" for e in results)


def test_structured_fields_round_trip_through_json(tmp_path: Path) -> None:
    with EvidenceStore(db_path=tmp_path / "evidence.db") as store:
        evidence = store.add(
            "run-1",
            "onboarding checklist detected",
            dom={"tag": "div", "class": "checklist"},
            javascript={"detected": ["UserGuiding", "Pendo"]},
            network=[{"url": "https://cdn.userguiding.com/x.js"}],
            visible_ui="a checklist widget is visible in the bottom-right corner",
            confidence=0.85,
        )
        [stored] = store.for_run("run-1")

        assert evidence.dom == {"tag": "div", "class": "checklist"}
        assert stored.dom == {"tag": "div", "class": "checklist"}
        assert stored.javascript == {"detected": ["UserGuiding", "Pendo"]}
        assert stored.network == [{"url": "https://cdn.userguiding.com/x.js"}]
        assert stored.visible_ui == "a checklist widget is visible in the bottom-right corner"
        assert stored.confidence == 0.85


def test_confidence_out_of_range_raises(tmp_path: Path) -> None:
    with EvidenceStore(db_path=tmp_path / "evidence.db") as store:
        with pytest.raises(ValueError):
            store.add("run-1", "bad confidence", confidence=1.5)
        with pytest.raises(ValueError):
            store.add("run-1", "bad confidence", confidence=-0.1)


def test_empty_run_id_or_label_raises(tmp_path: Path) -> None:
    with EvidenceStore(db_path=tmp_path / "evidence.db") as store:
        with pytest.raises(ValueError):
            store.add("", "label")
        with pytest.raises(ValueError):
            store.add("run-1", "")


def test_evidence_persists_across_store_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "evidence.db"
    with EvidenceStore(db_path=db_path) as store:
        store.add("run-1", "first discovery")

    with EvidenceStore(db_path=db_path) as reopened:
        results = reopened.for_run("run-1")
        assert len(results) == 1
        assert results[0].label == "first discovery"


def test_from_config_uses_configured_db_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROWTHRADAR_DB_PATH", str(tmp_path / "configured.db"))
    config = Config.from_env(env_path="/nonexistent/.env")

    with EvidenceStore.from_config(config) as store:
        store.add("run-1", "via config")

    assert (tmp_path / "configured.db").exists()


def test_all_returns_evidence_across_runs(tmp_path: Path) -> None:
    with EvidenceStore(db_path=tmp_path / "evidence.db") as store:
        store.add("run-1", "a")
        store.add("run-2", "b")

        assert {e.run_id for e in store.all()} == {"run-1", "run-2"}
