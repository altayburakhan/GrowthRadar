"""Multi-run / company research history.

A separate, append-only SQLite table (`run_history`) alongside the evidence
table (GRO-8) -- same database file, same style -- recording one summary row
per completed run: company, target URL, verdict, overall score, and
timestamp. Lets repeated runs against the same company be compared over time
and supports simple queries ("last N runs", "highest-scoring companies")
without touching the evidence schema.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from growthradar.config import Config
from growthradar.report import FinalReport


@dataclass(frozen=True)
class RunSummary:
    id: int | None
    run_id: str
    company: str
    product_url: str
    verdict: str
    overall_score: float
    recorded_at: str


class RunHistoryStore:
    """Append-only store of run summaries. Records are never updated or deleted."""

    def __init__(self, db_path: str | Path = "growthradar.db") -> None:
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    @classmethod
    def from_config(cls, config: Config) -> RunHistoryStore:
        return cls(db_path=config.db_path)

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS run_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                company TEXT NOT NULL,
                product_url TEXT NOT NULL,
                verdict TEXT NOT NULL,
                overall_score REAL NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """)
        self._conn.commit()

    def record(self, report: FinalReport) -> RunSummary:
        recorded_at = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            """
            INSERT INTO run_history
                (run_id, company, product_url, verdict, overall_score, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                report.run_id,
                report.company,
                report.product_url,
                report.verdict,
                report.confidence_score,
                recorded_at,
            ),
        )
        self._conn.commit()
        return RunSummary(
            id=cursor.lastrowid,
            run_id=report.run_id,
            company=report.company,
            product_url=report.product_url,
            verdict=report.verdict,
            overall_score=report.confidence_score,
            recorded_at=recorded_at,
        )

    def for_company(self, company: str) -> list[RunSummary]:
        """All past runs for `company`, oldest first -- for trend comparison."""
        rows = self._conn.execute(
            "SELECT * FROM run_history WHERE company = ? ORDER BY recorded_at ASC",
            (company,),
        ).fetchall()
        return [_row_to_summary(r) for r in rows]

    def recent(self, limit: int = 10) -> list[RunSummary]:
        """The most recently recorded runs, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM run_history ORDER BY recorded_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_summary(r) for r in rows]

    def top_scoring(self, limit: int = 10) -> list[RunSummary]:
        """The highest-scoring companies recorded so far."""
        rows = self._conn.execute(
            "SELECT * FROM run_history ORDER BY overall_score DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_summary(r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> RunHistoryStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _row_to_summary(row: sqlite3.Row) -> RunSummary:
    return RunSummary(
        id=row["id"],
        run_id=row["run_id"],
        company=row["company"],
        product_url=row["product_url"],
        verdict=row["verdict"],
        overall_score=row["overall_score"],
        recorded_at=row["recorded_at"],
    )
