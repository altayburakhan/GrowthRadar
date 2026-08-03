from __future__ import annotations

import json

from growthradar.core.models import CompanyEvidence, LeadScoreResult
from growthradar.storage.db import get_connection


def _serialize_evidence(evidence: CompanyEvidence) -> str:
    """Drops raw_html before persisting -- it's only needed transiently for tech
    detection during a run, not for later inspection, and would otherwise bloat
    the database with page source that's already been reduced to extracted text."""
    data = evidence.model_dump(mode="json")
    for page in data.get("pages", {}).values():
        page.pop("raw_html", None)
    return json.dumps(data)


def save_result(db_path: str, result: LeadScoreResult, evidence: CompanyEvidence | None = None) -> None:
    """Appends the result rather than upserting, so score history per domain
    is preserved and trends over time (e.g. after a company adds onboarding
    tooling) become queryable later. Optionally stores the underlying collected
    evidence (page text, detected technologies, signals) alongside it, so the
    raw data behind a score can be inspected later, not just the final numbers."""
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO lead_scores "
            "(domain, overall_score, tier, provider_used, confidence, result_json, evidence_json, generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.domain,
                result.overall_score,
                result.tier.value,
                result.provider_used,
                result.confidence,
                result.model_dump_json(),
                _serialize_evidence(evidence) if evidence else None,
                result.generated_at.isoformat(),
            ),
        )


def list_results(db_path: str, tier: str | None = None, limit: int = 20) -> list[LeadScoreResult]:
    query = "SELECT result_json FROM lead_scores"
    params: tuple = ()
    if tier:
        query += " WHERE tier = ?"
        params = (tier,)
    query += " ORDER BY id DESC LIMIT ?"
    params = params + (limit,)

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [LeadScoreResult.model_validate_json(row[0]) for row in rows]


def get_latest_for_domain(db_path: str, domain: str) -> LeadScoreResult | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT result_json FROM lead_scores WHERE domain = ? ORDER BY id DESC LIMIT 1",
            (domain,),
        ).fetchone()
    return LeadScoreResult.model_validate_json(row[0]) if row else None


def get_evidence_for_domain(db_path: str, domain: str) -> CompanyEvidence | None:
    """Returns the collected evidence (page text, detected tech, signals) behind
    the most recent score for a domain, or None if that run predates this feature
    or evidence storage was skipped for it."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT evidence_json FROM lead_scores WHERE domain = ? ORDER BY id DESC LIMIT 1",
            (domain,),
        ).fetchone()
    if not row or not row[0]:
        return None
    return CompanyEvidence.model_validate_json(row[0])
