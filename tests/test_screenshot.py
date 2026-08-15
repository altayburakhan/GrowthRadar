from pathlib import Path

import pytest
from patchright.sync_api import Page

from growthradar.browser import BrowserSession
from growthradar.config import Config
from growthradar.evidence import EvidenceStore
from growthradar.screenshot import ScreenshotKind, capture_and_record, capture_screenshot


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


@pytest.fixture
def page(config: Config):
    with BrowserSession(config) as session:
        p = session.start()
        p.set_content("<html><body><h1>Hello</h1></body></html>")
        yield p


def test_capture_screenshot_writes_file(tmp_path: Path, page: Page) -> None:
    result = capture_screenshot(page, "run-1", ScreenshotKind.LANDING, screenshot_dir=tmp_path)

    assert result.success is True
    assert result.error is None
    assert result.path is not None
    assert Path(result.path).exists()
    assert Path(result.path).stat().st_size > 0


def test_filename_follows_session_kind_timestamp_convention(tmp_path: Path, page: Page) -> None:
    result = capture_screenshot(
        page, "run-42", ScreenshotKind.DASHBOARD, screenshot_dir=tmp_path, label="Dashboard Home"
    )

    assert result.path is not None
    path = Path(result.path)
    assert path.parent == tmp_path / "run-42"
    assert path.name.startswith("dashboard__")
    assert "dashboard-home" in path.name


def test_capture_screenshot_never_raises_on_bad_target(tmp_path: Path, page: Page) -> None:
    # A file where a directory needs to be creates an OSError inside capture_screenshot.
    blocker = tmp_path / "run-1"
    blocker.write_text("not a directory")

    result = capture_screenshot(page, "run-1", ScreenshotKind.ERROR, screenshot_dir=tmp_path)

    assert result.success is False
    assert result.path is None
    assert result.error is not None


def test_capture_and_record_creates_evidence_on_success(tmp_path: Path, page: Page) -> None:
    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        evidence = capture_and_record(
            page,
            store,
            "run-1",
            ScreenshotKind.LOGIN,
            "login page loaded",
            screenshot_dir=tmp_path / "shots",
            confidence=0.6,
        )

        assert evidence.screenshot is not None
        assert Path(evidence.screenshot).exists()
        assert evidence.visible_ui["success"] is True
        assert evidence.confidence == 0.6

        [stored] = store.for_run("run-1")
        assert stored.id == evidence.id


def test_capture_and_record_still_records_evidence_on_failure(tmp_path: Path, page: Page) -> None:
    blocker = tmp_path / "run-1"
    blocker.write_text("not a directory")

    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        evidence = capture_and_record(
            page, store, "run-1", ScreenshotKind.ERROR, "capture failed", screenshot_dir=tmp_path
        )

        assert evidence.screenshot is None
        assert evidence.visible_ui["success"] is False
        assert evidence.visible_ui["error"] is not None
