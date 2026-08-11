from dataclasses import replace
from pathlib import Path

from growthradar.config import Config
from growthradar.evidence import EvidenceStore
from growthradar.web import _run_from_evidence, _screenshots_for_run


def _config(tmp_path: Path) -> Config:
    return replace(Config.from_env(env_path="/nonexistent/.env"), db_path=str(tmp_path / "e.db"))


def test_screenshots_for_run_includes_non_screenshot_prefixed_labels(tmp_path: Path) -> None:
    # Regression: registration.py's own captures ("registration form",
    # "registration form after N step(s)", "registration blocked by
    # anti-bot challenge (captcha)") never started with "screenshot:" and
    # were previously invisible in the dashboard entirely -- only
    # exploration's "screenshot: {title}"-labeled captures ever showed up.
    config = _config(tmp_path)
    with EvidenceStore.from_config(config) as store:
        store.add(
            "run-1",
            "screenshot: Landing",
            url="https://example.com",
            screenshot="screenshots/run-1/landing.png",
            visible_ui={"screenshot_kind": "landing", "success": True, "error": None},
        )
        store.add(
            "run-1",
            "registration blocked by anti-bot challenge (captcha)",
            url="https://example.com/signup",
            screenshot="screenshots/run-1/captcha.png",
            visible_ui={"screenshot_kind": "registration", "success": True, "error": None},
        )
        # No screenshot captured (capture failed) -- must stay excluded.
        store.add(
            "run-1",
            "screenshot: Broken",
            url="https://example.com/broken",
            screenshot=None,
            visible_ui={"screenshot_kind": "page", "success": False, "error": "boom"},
        )

    shots = _screenshots_for_run(config, "run-1")

    kinds = {s["kind"] for s in shots}
    assert kinds == {"landing", "registration"}
    assert len(shots) == 2


def test_run_from_evidence_reconstructs_a_past_run_not_in_the_job_registry(
    tmp_path: Path,
) -> None:
    # A run started before the current server process (a previous dashboard
    # session, or any run kicked off via the CLI) is never in the in-memory
    # `_jobs` registry -- history entries must still be viewable by
    # recomputing the report straight from persisted evidence.
    config = _config(tmp_path)
    with EvidenceStore.from_config(config) as store:
        store.add(
            "run-1",
            "screenshot: Landing",
            url="https://example.com",
            screenshot="screenshots/run-1/landing.png",
            visible_ui={"screenshot_kind": "landing", "success": True, "error": None},
        )
        store.add(
            "run-1",
            "registration attempt",
            url="https://example.com/signup",
            visible_ui={"submitted": True},
        )

    payload = _run_from_evidence(config, "run-1")

    assert payload is not None
    assert payload["run_id"] == "run-1"
    assert payload["status"] == "done"
    assert payload["report"]["registration_completed"] is True
    assert len(payload["screenshots"]) == 1


def test_run_from_evidence_returns_none_when_no_evidence_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with EvidenceStore.from_config(config):
        pass  # ensure the db file exists, but write nothing for this run_id

    assert _run_from_evidence(config, "missing-run") is None
