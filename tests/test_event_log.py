from pathlib import Path

from growthradar.event_log import EventType, LogEntry, RunLogger, read_log


def test_run_logger_writes_jsonl_file(tmp_path: Path) -> None:
    logger = RunLogger(run_id="test-run-1", log_dir=tmp_path)

    logger.page_visited("https://example.com", title="Example")
    logger.action("click_signup")
    logger.screenshot("screenshots/example.png")
    logger.discovery("found pricing page")
    logger.error("navigation failed")
    logger.retry("goto", attempt=1, max_attempts=3, reason="timeout")
    logger.decision("likely UserGuiding prospect", confidence=0.82, evidence_refs=["e1", "e2"])

    assert logger.path == tmp_path / "test-run-1.jsonl"
    lines = logger.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 7


def test_entries_are_sequential_and_tagged_with_run_id(tmp_path: Path) -> None:
    logger = RunLogger(run_id="test-run-2", log_dir=tmp_path)
    logger.action("first")
    logger.action("second")

    entries = logger.read_all()
    assert [e.seq for e in entries] == [1, 2]
    assert all(e.run_id == "test-run-2" for e in entries)


def test_event_types_are_covered(tmp_path: Path) -> None:
    logger = RunLogger(run_id="test-run-3", log_dir=tmp_path)
    logger.page_visited("https://example.com")
    logger.action("noop")
    logger.screenshot("shot.png")
    logger.discovery("thing")
    logger.error("boom")
    logger.retry("action", attempt=1, max_attempts=2, reason="x")
    logger.decision("done")

    entries = logger.read_all()
    assert [e.event_type for e in entries] == [
        EventType.PAGE_VISITED,
        EventType.ACTION,
        EventType.SCREENSHOT,
        EventType.DISCOVERY,
        EventType.ERROR,
        EventType.RETRY,
        EventType.DECISION,
    ]


def test_log_entry_roundtrips_through_json() -> None:
    entry = LogEntry(
        timestamp="2026-08-05T10:00:00+00:00",
        run_id="run-x",
        seq=1,
        level="INFO",
        event_type=EventType.DECISION,
        message="hot prospect",
        data={"confidence": 0.9, "evidence_refs": ["a", "b"]},
    )

    restored = LogEntry.from_json(entry.to_json())

    assert restored == entry


def test_read_log_is_consumable_by_other_modules(tmp_path: Path) -> None:
    logger = RunLogger(run_id="test-run-4", log_dir=tmp_path)
    logger.page_visited("https://example.com")
    logger.decision("warm prospect", confidence=0.5)

    entries = list(read_log(logger.path))

    assert len(entries) == 2
    assert entries[1].data["confidence"] == 0.5


def test_read_log_returns_empty_for_missing_file(tmp_path: Path) -> None:
    assert list(read_log(tmp_path / "does-not-exist.jsonl")) == []


def test_run_id_defaults_to_generated_value(tmp_path: Path) -> None:
    logger = RunLogger(log_dir=tmp_path)
    assert logger.run_id
    assert logger.path.parent == tmp_path
