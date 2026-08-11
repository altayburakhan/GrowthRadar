"""Final report generator.

Turns a run's evidence (GRO-8) plus its ScoreResult (GRO-16) into the fields
Linear.md's "Final Report" section calls for -- Company, Product, Explored
pages, Registration completed, Trial available, Onboarding detected, Evidence
collected, Technologies detected, Product update pages, Help center,
Confidence score, Final recommendation.

`generate_report` is a pure function: it only reads evidence/score already
computed by earlier stages, never fetches anything new (Linear.md: "Evidence
first. Conclusion second." / "Every conclusion must reference collected
evidence.").
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from growthradar.config import Config
from growthradar.evidence import Evidence, EvidenceStore
from growthradar.scoring import ScoreResult, score_run

# Matches GRO-14/15/16's "2+ independent signals agreed" confidence band.
_CONFIDENT_THRESHOLD = 0.65
_TRIAL_KEYWORDS = ("free trial", "start trial", "try for free", "start your trial")

_VERDICT_RECOMMENDATIONS: dict[str, str] = {
    "hot": "Strong prospect -- pursue outreach.",
    "warm": "Moderate prospect -- worth monitoring or a lower-priority outreach.",
    "cold": "Weak prospect -- deprioritize.",
}


@dataclass(frozen=True)
class FinalReport:
    run_id: str
    company: str
    product_url: str
    explored_pages: tuple[str, ...]
    registration_completed: bool
    trial_available: bool
    onboarding_detected: bool
    evidence_collected: int
    technologies_detected: tuple[str, ...]
    product_update_pages: tuple[str, ...]
    help_center_url: str | None
    confidence_score: float
    verdict: str
    final_recommendation: str
    score: ScoreResult
    partial_run: bool = False
    run_errors: tuple[str, ...] = ()
    llm_summary: str | None = None
    competitor_tools_detected: tuple[str, ...] = ()
    already_userguiding_customer: bool = False


def _by_label_prefix(evidence: list[Evidence], *prefixes: str) -> list[Evidence]:
    return [e for e in evidence if any(e.label.startswith(p) for p in prefixes)]


def _screenshot_kind(e: Evidence) -> str | None:
    if isinstance(e.visible_ui, dict):
        kind = e.visible_ui.get("screenshot_kind")
        return kind if isinstance(kind, str) else None
    return None


def _classified_pages(evidence: list[Evidence], category: str) -> list[Evidence]:
    """Pages GRO-19's page_classifier confidently categorized (URL + content agreed)."""
    return [
        e
        for e in _by_label_prefix(evidence, "page category:")
        if isinstance(e.visible_ui, dict) and e.visible_ui.get("category") == category and e.url
    ]


def _unique_urls(*groups: list[Evidence]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for group in groups:
        for e in group:
            if e.url:
                seen.setdefault(e.url, None)
    return tuple(seen.keys())


def _registrable_domain_label(url: str) -> str:
    host = urlparse(url).netloc.split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    labels = host.split(".")
    root = labels[-2] if len(labels) >= 2 else host
    return root.capitalize() if root else "Unknown"


def _find_product_url(evidence: list[Evidence]) -> str:
    screenshots = _by_label_prefix(evidence, "screenshot:")
    landing = [e for e in screenshots if _screenshot_kind(e) == "landing" and e.url]
    if landing:
        assert landing[0].url is not None
        return landing[0].url
    dom_pages = [e for e in _by_label_prefix(evidence, "dom:") if e.url]
    return dom_pages[0].url if dom_pages else ""  # type: ignore[return-value]


def _company_name(evidence: list[Evidence], product_url: str) -> str:
    for e in _by_label_prefix(evidence, "registration attempt"):
        if isinstance(e.visible_ui, dict):
            company = e.visible_ui.get("company_name")
            if company:
                return str(company)
    return _registrable_domain_label(product_url) if product_url else "Unknown"


def _trial_available(evidence: list[Evidence]) -> bool:
    for e in _by_label_prefix(evidence, "dom:"):
        if isinstance(e.dom, dict):
            html = str(e.dom.get("html", "")).lower()
            if any(kw in html for kw in _TRIAL_KEYWORDS):
                return True
    return False


def _technologies_detected(evidence: list[Evidence]) -> tuple[str, ...]:
    names: set[str] = set()
    for e in _by_label_prefix(evidence, "js/network:"):
        if not isinstance(e.javascript, dict):
            continue
        for tool in e.javascript.get("detected_tools", []):
            if tool.get("confidence", 0) >= _CONFIDENT_THRESHOLD:
                names.add(str(tool.get("name")))
    return tuple(sorted(names))


def _already_userguiding_customer(score: ScoreResult) -> bool:
    notes = score.onboarding_opportunity.notes
    return bool(notes) and "not a prospect" in notes[0]


def _competitor_tools_detected(evidence: list[Evidence]) -> tuple[str, ...]:
    """Onboarding-category tools other than UserGuiding itself -- mirrors
    scoring.py's own "competitor onboarding tool detected" signal, surfaced
    here by name (not just folded into the flat `technologies_detected`
    list) so a reader can immediately see *which* rival tool to write their
    outreach message around, without digging into the score breakdown."""
    names: set[str] = set()
    for e in _by_label_prefix(evidence, "js/network:"):
        if not isinstance(e.javascript, dict):
            continue
        for tool in e.javascript.get("detected_tools", []):
            if tool.get("category", "onboarding") != "onboarding":
                continue
            if tool.get("name") == "UserGuiding":
                continue
            if tool.get("confidence", 0) >= _CONFIDENT_THRESHOLD:
                names.add(str(tool.get("name")))
    return tuple(sorted(names))


def _llm_summary(evidence: list[Evidence]) -> str | None:
    """Read back llm_summary.py's recorded evidence, if any. Reading, not
    calling an LLM here, keeps this module's "no I/O, pure function" contract
    intact -- the summary was already generated (or skipped) before
    `generate_report` runs."""
    for e in _by_label_prefix(evidence, "llm summary"):
        if isinstance(e.visible_ui, dict):
            summary = e.visible_ui.get("summary")
            if summary:
                return str(summary)
    return None


def _recommendation(score: ScoreResult) -> str:
    if _already_userguiding_customer(score):
        return "Already appears to use UserGuiding -- not a sales prospect."
    return _VERDICT_RECOMMENDATIONS.get(score.verdict, "Insufficient evidence.")


def generate_report(
    run_id: str,
    evidence: list[Evidence],
    score: ScoreResult,
    *,
    run_errors: tuple[str, ...] = (),
) -> FinalReport:
    """Build the final report from already-computed evidence and score.

    `run_errors` carries any phase failures the orchestrator (GRO-18/GRO-21)
    recorded before generating this report -- when non-empty, the report is
    marked `partial_run=True` so a partial result is never presented as
    equivalent to a clean one (Linear.md: "Never make unsupported claims.").

    Pure function: no I/O, deterministic given its inputs.
    """
    dom_pages = [e for e in _by_label_prefix(evidence, "dom:") if e.url]
    screenshots = _by_label_prefix(evidence, "screenshot:")
    onboarding_evidence = _by_label_prefix(evidence, "onboarding heuristics:")
    registration_completed = any(
        isinstance(e.visible_ui, dict) and e.visible_ui.get("submitted") is True
        for e in _by_label_prefix(evidence, "registration attempt")
    )
    onboarding_detected = any(
        (e.confidence or 0) >= _CONFIDENT_THRESHOLD for e in onboarding_evidence
    )

    product_update_pages = _unique_urls(
        [e for e in screenshots if _screenshot_kind(e) == "product_updates"],
        _classified_pages(evidence, "product_updates"),
    )
    help_center_urls = _unique_urls(
        [e for e in screenshots if _screenshot_kind(e) == "help_center"],
        _classified_pages(evidence, "help_center"),
    )

    product_url = _find_product_url(evidence)

    return FinalReport(
        run_id=run_id,
        company=_company_name(evidence, product_url),
        product_url=product_url,
        explored_pages=_unique_urls(dom_pages),
        registration_completed=registration_completed,
        trial_available=_trial_available(evidence),
        onboarding_detected=onboarding_detected,
        evidence_collected=len(evidence),
        technologies_detected=_technologies_detected(evidence),
        competitor_tools_detected=_competitor_tools_detected(evidence),
        already_userguiding_customer=_already_userguiding_customer(score),
        product_update_pages=product_update_pages,
        help_center_url=help_center_urls[0] if help_center_urls else None,
        confidence_score=score.overall_score,
        verdict=score.verdict,
        final_recommendation=_recommendation(score),
        score=score,
        partial_run=bool(run_errors),
        run_errors=run_errors,
        llm_summary=_llm_summary(evidence),
    )


def to_dict(report: FinalReport) -> dict[str, Any]:
    return asdict(report)


def to_json(report: FinalReport, *, indent: int = 2) -> str:
    return json.dumps(to_dict(report), indent=indent, default=str, ensure_ascii=False)


def to_markdown(report: FinalReport) -> str:
    score = report.score
    lines = [
        f"# GrowthRadar Report -- {report.company}",
        "",
    ]
    if report.partial_run:
        lines += [
            "> ⚠️ **Partial run** -- one or more phases failed; this report reflects "
            "only the evidence that was successfully collected.",
        ]
        lines += [f">   - {err}" for err in report.run_errors]
        lines.append("")
    if report.already_userguiding_customer:
        lines += [
            "> 🟢 **Already a UserGuiding customer** -- not a competitive-displacement "
            "prospect; if reaching out, this is an account-management/upsell "
            "conversation, not a sales pitch.",
            "",
        ]
    elif report.competitor_tools_detected:
        lines += [
            f"> ⚠️ **Competitor tool(s) detected: {', '.join(report.competitor_tools_detected)}** "
            "-- this company already has onboarding-tool budget and buy-in. Frame outreach "
            'around switching/replacing, not "do you need this at all".',
            "",
        ]
    lines += [
        f"- **Product**: {report.product_url or 'unknown'}",
        f"- **Explored pages**: {len(report.explored_pages)}",
        f"- **Registration completed**: {'Yes' if report.registration_completed else 'No'}",
        f"- **Trial available**: {'Yes' if report.trial_available else 'No'}",
        f"- **Onboarding detected**: {'Yes' if report.onboarding_detected else 'No'}",
        f"- **Evidence collected**: {report.evidence_collected}",
        f"- **Technologies detected**: {', '.join(report.technologies_detected) or 'None'}",
        f"- **Product update pages**: {len(report.product_update_pages)}",
        f"- **Help center**: {report.help_center_url or 'Not found'}",
        f"- **Confidence score**: {report.confidence_score:.1f}/100 ({report.verdict.upper()})",
        "",
        "## Final recommendation",
        "",
        report.final_recommendation,
        "",
    ]
    if report.llm_summary:
        lines += [
            "## AI summary",
            "",
            report.llm_summary,
            "",
        ]
    lines += [
        "## Score breakdown",
        "",
        f"- ICP fit: {score.icp_fit.score:.1f} "
        f"({score.icp_fit.signal_count}/{score.icp_fit.max_signals} signals)",
        f"- Onboarding opportunity: {score.onboarding_opportunity.score:.1f} "
        f"({score.onboarding_opportunity.signal_count}/"
        f"{score.onboarding_opportunity.max_signals} signals)",
        f"- Product experience: {score.product_experience.score:.1f} "
        f"({score.product_experience.signal_count}/"
        f"{score.product_experience.max_signals} signals)",
        "",
        "## Explored pages",
        "",
    ]
    lines += [f"- {url}" for url in report.explored_pages] or ["- (none)"]
    return "\n".join(lines) + "\n"


def record_report(store: EvidenceStore, report: FinalReport) -> Evidence:
    return store.add(
        report.run_id,
        "final report",
        url=report.product_url or None,
        visible_ui=to_dict(report),
        confidence=min(max(report.confidence_score / 100.0, 0.0), 1.0),
    )


def generate_and_record(store: EvidenceStore, run_id: str, config: Config) -> FinalReport:
    """Fetch a run's evidence, score it, build the report, and record it.

    Uses `score_run` directly (not `scoring.score_and_record`) so this doesn't
    also write a separate "final score" evidence row -- the report's own
    recorded evidence already carries the full score breakdown.
    """
    evidence = store.for_run(run_id)
    score = score_run(run_id, evidence, config)
    report = generate_report(run_id, evidence, score)
    record_report(store, report)
    return report
