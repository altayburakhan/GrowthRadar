"""Confidence scoring & decision engine.

Aggregates a run's evidence (screenshot/DOM/JS-network/onboarding-heuristics
-- from GRO-9/10/13/14) into three weighted dimensions -- ICP fit,
onboarding opportunity, product experience -- and a Hot/Warm/Cold verdict,
using the `.env`-configured weights and thresholds (GROWTHRADAR_WEIGHT_*,
GROWTHRADAR_HOT_THRESHOLD, GROWTHRADAR_WARM_THRESHOLD).

Each dimension is itself evidence-grounded: `score_run` is a pure function (no
I/O), so it's fully unit-testable, and every DimensionScore cites the Evidence
ids it was computed from for traceability. A dimension only reaches its higher
bands once multiple independent signals within it agree (Linear.md: "never
conclude from one signal") -- except one deliberate override: if the product
already appears to use UserGuiding itself, onboarding_opportunity is forced
low, since an existing customer isn't a sales prospect.

This is a first-pass heuristic model, not a precise formula -- the configured
weights make each dimension's contribution to the overall score tunable
without code changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from growthradar.config import Config
from growthradar.evidence import Evidence, EvidenceStore

_ALREADY_USERGUIDING_SCORE = 5.0
# Matches GRO-14/15's "2+ independent signals agreed" confidence band.
_CONFIDENT_THRESHOLD = 0.65


@dataclass(frozen=True)
class DimensionScore:
    name: str
    score: float
    signal_count: int
    max_signals: int
    evidence_ids: tuple[int, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoreResult:
    run_id: str
    icp_fit: DimensionScore
    onboarding_opportunity: DimensionScore
    product_experience: DimensionScore
    overall_score: float
    verdict: str


def _by_label_prefix(evidence: list[Evidence], *prefixes: str) -> list[Evidence]:
    return [e for e in evidence if any(e.label.startswith(p) for p in prefixes)]


def _ids(evidence: list[Evidence]) -> tuple[int, ...]:
    return tuple(e.id for e in evidence if e.id is not None)


def _screenshot_kind(e: Evidence) -> str | None:
    if isinstance(e.visible_ui, dict):
        kind = e.visible_ui.get("screenshot_kind")
        return kind if isinstance(kind, str) else None
    return None


def _dimension_score(name: str, checks: list[tuple[bool, tuple[int, ...], str]]) -> DimensionScore:
    hits = [c for c in checks if c[0]]
    evidence_ids = tuple(sorted({eid for _, ids, _ in hits for eid in ids}))
    notes = tuple(note for _, _, note in hits)
    max_signals = len(checks)
    signal_count = len(hits)
    score = (signal_count / max_signals) * 100 if max_signals else 0.0
    return DimensionScore(
        name=name,
        score=round(score, 1),
        signal_count=signal_count,
        max_signals=max_signals,
        evidence_ids=evidence_ids,
        notes=notes,
    )


def _score_icp_fit(evidence: list[Evidence]) -> DimensionScore:
    registration = _by_label_prefix(evidence, "registration attempt")
    screenshots = _by_label_prefix(evidence, "screenshot:")
    dom_pages = _by_label_prefix(evidence, "dom:")

    dashboard_shots = [e for e in screenshots if _screenshot_kind(e) == "dashboard"]
    help_shots = [e for e in screenshots if _screenshot_kind(e) == "help_center"]

    checks = [
        (bool(registration), _ids(registration), "signup/registration flow found"),
        (bool(dashboard_shots), _ids(dashboard_shots), "dashboard page found"),
        (bool(help_shots), _ids(help_shots), "help/docs page found"),
        (len(dom_pages) >= 3, _ids(dom_pages), f"{len(dom_pages)} distinct page(s) explored"),
    ]
    return _dimension_score("icp_fit", checks)


def _detected_tool_evidence(
    evidence: list[Evidence], *, name_is_userguiding: bool
) -> list[Evidence]:
    """Onboarding-category tools only (GRO-28): general analytics/tracking
    providers (Google Analytics, Segment, ...) say nothing about onboarding
    budget or intent, so they must never count as a "competitor tool" here.
    Evidence recorded before GRO-28 has no "category" key -- treated as
    onboarding, since that's all that existed at the time.
    """
    matches = []
    for e in evidence:
        if not isinstance(e.javascript, dict):
            continue
        for tool in e.javascript.get("detected_tools", []):
            if tool.get("category", "onboarding") != "onboarding":
                continue
            is_ug = tool.get("name") == "UserGuiding"
            if is_ug != name_is_userguiding:
                continue
            if tool.get("confidence", 0) >= _CONFIDENT_THRESHOLD:
                matches.append(e)
                break
    return matches


def _already_userguiding(evidence: list[Evidence]) -> tuple[bool, tuple[int, ...]]:
    js_evidence = _by_label_prefix(evidence, "js/network:")
    ug_js = _detected_tool_evidence(js_evidence, name_is_userguiding=True)
    return bool(ug_js), _ids(ug_js)


def _score_onboarding_opportunity(evidence: list[Evidence]) -> DimensionScore:
    already, already_ids = _already_userguiding(evidence)
    if already:
        return DimensionScore(
            name="onboarding_opportunity",
            score=_ALREADY_USERGUIDING_SCORE,
            signal_count=0,
            max_signals=3,
            evidence_ids=already_ids,
            notes=("product already appears to use UserGuiding -- not a prospect",),
        )

    onboarding_evidence = _by_label_prefix(evidence, "onboarding heuristics:")
    js_evidence = _by_label_prefix(evidence, "js/network:")
    screenshots = _by_label_prefix(evidence, "screenshot:")

    onboarding_ui_found = [
        e for e in onboarding_evidence if (e.confidence or 0) >= _CONFIDENT_THRESHOLD
    ]
    competitor_tools_found = _detected_tool_evidence(js_evidence, name_is_userguiding=False)
    product_updates_found = [e for e in screenshots if _screenshot_kind(e) == "product_updates"]

    checks = [
        (
            bool(onboarding_ui_found),
            _ids(onboarding_ui_found),
            "onboarding UI detected on at least one page",
        ),
        (
            bool(competitor_tools_found),
            _ids(competitor_tools_found),
            "competitor onboarding tool detected",
        ),
        (
            bool(product_updates_found),
            _ids(product_updates_found),
            "product updates/changelog page found",
        ),
    ]
    return _dimension_score("onboarding_opportunity", checks)


def _average_interactive_elements(dom_evidence: list[Evidence]) -> float:
    counts = [
        len(e.dom.get("interactive_elements", [])) for e in dom_evidence if isinstance(e.dom, dict)
    ]
    return sum(counts) / len(counts) if counts else 0.0


def _majority_succeeded(screenshots: list[Evidence]) -> bool:
    successes = sum(
        1
        for e in screenshots
        if isinstance(e.visible_ui, dict) and e.visible_ui.get("success") is True
    )
    return bool(screenshots) and successes >= len(screenshots) / 2


def _score_product_experience(evidence: list[Evidence]) -> DimensionScore:
    registration = _by_label_prefix(evidence, "registration attempt")
    failed_pages = _by_label_prefix(evidence, "failed to load ")
    dom_pages = _by_label_prefix(evidence, "dom:")
    screenshots = _by_label_prefix(evidence, "screenshot:")

    completed_registration = [
        e
        for e in registration
        if isinstance(e.visible_ui, dict) and e.visible_ui.get("submitted") is True
    ]
    total_attempts = len(dom_pages) + len(failed_pages)
    low_error_rate = total_attempts > 0 and (len(failed_pages) / total_attempts) < 0.2
    rich_navigation = bool(dom_pages) and _average_interactive_elements(dom_pages) >= 5

    checks = [
        (
            bool(completed_registration),
            _ids(completed_registration),
            "registration completed successfully",
        ),
        (
            low_error_rate,
            _ids(dom_pages),
            f"{len(failed_pages)}/{total_attempts} page load(s) failed",
        ),
        (rich_navigation, _ids(dom_pages), "rich navigation/interactive UI found"),
        (
            _majority_succeeded(screenshots),
            _ids(screenshots),
            "most screenshots captured successfully",
        ),
    ]
    return _dimension_score("product_experience", checks)


def score_run(run_id: str, evidence: list[Evidence], config: Config) -> ScoreResult:
    """Combine a run's evidence into ICP fit / onboarding opportunity /
    product experience scores and a Hot/Warm/Cold verdict.

    Pure function: no I/O, deterministic given its inputs.
    """
    icp = _score_icp_fit(evidence)
    opportunity = _score_onboarding_opportunity(evidence)
    experience = _score_product_experience(evidence)

    overall = round(
        icp.score * config.weight_icp_fit
        + opportunity.score * config.weight_onboarding_opportunity
        + experience.score * config.weight_product_experience,
        1,
    )

    if overall >= config.hot_threshold:
        verdict = "hot"
    elif overall >= config.warm_threshold:
        verdict = "warm"
    else:
        verdict = "cold"

    return ScoreResult(
        run_id=run_id,
        icp_fit=icp,
        onboarding_opportunity=opportunity,
        product_experience=experience,
        overall_score=overall,
        verdict=verdict,
    )


def _dimension_to_dict(dimension: DimensionScore) -> dict[str, Any]:
    return {
        "score": dimension.score,
        "signal_count": dimension.signal_count,
        "max_signals": dimension.max_signals,
        "evidence_ids": list(dimension.evidence_ids),
        "notes": list(dimension.notes),
    }


def score_and_record(store: EvidenceStore, run_id: str, config: Config) -> ScoreResult:
    """Score a run's accumulated evidence and record the result as Evidence."""
    result = score_run(run_id, store.for_run(run_id), config)

    store.add(
        run_id,
        "final score",
        visible_ui={
            "overall_score": result.overall_score,
            "verdict": result.verdict,
            "icp_fit": _dimension_to_dict(result.icp_fit),
            "onboarding_opportunity": _dimension_to_dict(result.onboarding_opportunity),
            "product_experience": _dimension_to_dict(result.product_experience),
        },
        confidence=min(max(result.overall_score / 100.0, 0.0), 1.0),
    )
    return result
