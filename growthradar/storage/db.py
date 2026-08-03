from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lead_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    overall_score REAL NOT NULL,
    tier TEXT NOT NULL,
    provider_used TEXT NOT NULL,
    confidence REAL NOT NULL,
    result_json TEXT NOT NULL,
    evidence_json TEXT,
    generated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lead_scores_domain ON lead_scores(domain);
"""


@contextmanager
def get_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Adds columns introduced after a database's initial creation, so older
    on-disk databases keep working without the user having to delete them."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lead_scores)")}
    if "evidence_json" not in columns:
        conn.execute("ALTER TABLE lead_scores ADD COLUMN evidence_json TEXT")
