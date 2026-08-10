"""Structured, reproducible event logging for a single exploration run.

Every domain event (page visited, action taken, screenshot captured, discovery made,
error, retry, decision) is appended as one JSON line to `logs/<run_id>.jsonl`, so any
run can be replayed or consumed by downstream modules (Evidence store, reports).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class EventType(StrEnum):
    PAGE_VISITED = "page_visited"
    ACTION = "action"
    SCREENSHOT = "screenshot"
    DISCOVERY = "discovery"
    ERROR = "error"
    RETRY = "retry"
    DECISION = "decision"
    INFO = "info"


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    run_id: str
    seq: int
    level: str
    event_type: EventType
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "seq": self.seq,
            "level": self.level,
            "event_type": self.event_type.value,
            "message": self.message,
            "data": self.data,
        }
        return json.dumps(payload, default=str, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> LogEntry:
        payload = json.loads(line)
        return cls(
            timestamp=payload["timestamp"],
            run_id=payload["run_id"],
            seq=payload["seq"],
            level=payload["level"],
            event_type=EventType(payload["event_type"]),
            message=payload["message"],
            data=payload.get("data", {}),
        )


def read_log(path: str | Path) -> Iterator[LogEntry]:
    """Read back a run's log file in order. Yields nothing if the file doesn't exist."""
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield LogEntry.from_json(line)


def configure_console_logging(level: str = "INFO") -> None:
    """Wire stdlib `logging` (used for low-level diagnostics) to the console. Call once."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def _generate_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


class RunLogger:
    """Appends structured events for one exploration run to a JSONL file + console."""

    def __init__(
        self,
        run_id: str | None = None,
        log_dir: str | Path = "logs",
        console_level: str = "INFO",
    ) -> None:
        self.run_id = run_id or _generate_run_id()
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self._log_dir / f"{self.run_id}.jsonl"
        self._seq = 0
        self._console = logging.getLogger(f"growthradar.run.{self.run_id}")
        self._console.setLevel(getattr(logging, console_level.upper(), logging.INFO))

    def page_visited(self, url: str, *, title: str | None = None, **extra: Any) -> LogEntry:
        return self._write(
            "INFO", EventType.PAGE_VISITED, f"visited {url}", url=url, title=title, **extra
        )

    def action(self, name: str, **extra: Any) -> LogEntry:
        return self._write("INFO", EventType.ACTION, name, **extra)

    def screenshot(self, path: str, **extra: Any) -> LogEntry:
        return self._write(
            "INFO", EventType.SCREENSHOT, f"screenshot saved: {path}", path=path, **extra
        )

    def discovery(self, description: str, **extra: Any) -> LogEntry:
        return self._write("INFO", EventType.DISCOVERY, description, **extra)

    def error(self, message: str, **extra: Any) -> LogEntry:
        return self._write("ERROR", EventType.ERROR, message, **extra)

    def retry(self, action_name: str, *, attempt: int, max_attempts: int, reason: str) -> LogEntry:
        return self._write(
            "WARNING",
            EventType.RETRY,
            f"retry {attempt}/{max_attempts} for {action_name}: {reason}",
            action=action_name,
            attempt=attempt,
            max_attempts=max_attempts,
            reason=reason,
        )

    def decision(
        self,
        decision: str,
        *,
        confidence: float | None = None,
        evidence_refs: list[str] | None = None,
        **extra: Any,
    ) -> LogEntry:
        return self._write(
            "INFO",
            EventType.DECISION,
            decision,
            confidence=confidence,
            evidence_refs=evidence_refs or [],
            **extra,
        )

    def read_all(self) -> list[LogEntry]:
        return list(read_log(self.path))

    def _write(self, level: str, event_type: EventType, message: str, **data: Any) -> LogEntry:
        self._seq += 1
        entry = LogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            run_id=self.run_id,
            seq=self._seq,
            level=level,
            event_type=event_type,
            message=message,
            data=data,
        )
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(entry.to_json() + "\n")
        except OSError as exc:
            self._console.error("failed to write log entry to %s: %s", self.path, exc)

        self._console.log(
            getattr(logging, level, logging.INFO),
            "[%s] %s",
            event_type.value,
            message,
        )
        return entry
