"""Cumulative, append-only evidence store for exploration runs (SQLite-backed).

Every discovery becomes an Evidence record combining whatever signals support it
(screenshot, URL, DOM, JavaScript, network, visible UI, confidence). Records are
never updated or deleted -- only added -- so a run's evidence trail is always complete
and reproducible. Consumed by later modules: screenshot capture (GRO-9), DOM
collection (GRO-10), JS/network inspection (GRO-13), onboarding detection (GRO-14).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from growthradar.config import Config


@dataclass(frozen=True)
class Evidence:
    id: int | None
    run_id: str
    seq: int
    timestamp: str
    label: str
    url: str | None = None
    screenshot: str | None = None
    dom: Any = None
    javascript: Any = None
    network: Any = None
    visible_ui: Any = None
    confidence: float | None = None


class EvidenceStore:
    """SQLite-backed, append-only evidence store. Records are never updated or deleted."""

    def __init__(self, db_path: str | Path = "growthradar.db") -> None:
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    @classmethod
    def from_config(cls, config: Config) -> EvidenceStore:
        return cls(db_path=config.db_path)

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                label TEXT NOT NULL,
                url TEXT,
                screenshot TEXT,
                dom TEXT,
                javascript TEXT,
                network TEXT,
                visible_ui TEXT,
                confidence REAL,
                UNIQUE (run_id, seq)
            )
            """)
        self._conn.commit()

    def add(
        self,
        run_id: str,
        label: str,
        *,
        url: str | None = None,
        screenshot: str | None = None,
        dom: Any = None,
        javascript: Any = None,
        network: Any = None,
        visible_ui: Any = None,
        confidence: float | None = None,
    ) -> Evidence:
        if not run_id:
            raise ValueError("run_id must not be empty")
        if not label:
            raise ValueError("label must not be empty")
        if confidence is not None and not (0.0 <= confidence <= 1.0):
            raise ValueError(f"confidence must be within [0.0, 1.0], got {confidence}")

        seq = self._next_seq(run_id)
        timestamp = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            """
            INSERT INTO evidence
                (run_id, seq, timestamp, label, url, screenshot,
                 dom, javascript, network, visible_ui, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                seq,
                timestamp,
                label,
                url,
                screenshot,
                _dump(dom),
                _dump(javascript),
                _dump(network),
                _dump(visible_ui),
                confidence,
            ),
        )
        self._conn.commit()
        return Evidence(
            id=cursor.lastrowid,
            run_id=run_id,
            seq=seq,
            timestamp=timestamp,
            label=label,
            url=url,
            screenshot=screenshot,
            dom=dom,
            javascript=javascript,
            network=network,
            visible_ui=visible_ui,
            confidence=confidence,
        )

    def for_run(self, run_id: str) -> list[Evidence]:
        rows = self._conn.execute(
            "SELECT * FROM evidence WHERE run_id = ? ORDER BY seq ASC",
            (run_id,),
        ).fetchall()
        return [_row_to_evidence(row) for row in rows]

    def all(self) -> list[Evidence]:
        rows = self._conn.execute("SELECT * FROM evidence ORDER BY run_id, seq ASC").fetchall()
        return [_row_to_evidence(row) for row in rows]

    def close(self) -> None:
        self._conn.close()

    def _next_seq(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM evidence WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["next_seq"])

    def __enter__(self) -> EvidenceStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str, ensure_ascii=False)


def _load(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _row_to_evidence(row: sqlite3.Row) -> Evidence:
    return Evidence(
        id=row["id"],
        run_id=row["run_id"],
        seq=row["seq"],
        timestamp=row["timestamp"],
        label=row["label"],
        url=row["url"],
        screenshot=row["screenshot"],
        dom=_load(row["dom"]),
        javascript=_load(row["javascript"]),
        network=_load(row["network"]),
        visible_ui=_load(row["visible_ui"]),
        confidence=row["confidence"],
    )
