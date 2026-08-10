"""Onboarding detection heuristics: combines DOM (GRO-10) and JS/network
(GRO-13) signals -- already-collected data, no live Page needed -- into a
per-page verdict on whether product-adoption UI (tours, tooltips, checklists,
...) is present.

`detect_onboarding_signals` is a pure function by design (Linear.md "never
conclude from one signal" + this task's acceptance criteria ask for isolated,
testable decision logic). Confidence only reaches "solid"/"strong" once
independent signal categories (DOM content, JS tooling, network traffic)
agree -- matching Linear.md's Decision Making example (JS + visible
onboarding + network = high confidence).
"""

from __future__ import annotations

from dataclasses import dataclass

from growthradar.dom import DomSnapshot
from growthradar.evidence import Evidence, EvidenceStore
from growthradar.js_network import ToolDetection

_CONFIDENCE_BY_SIGNAL_COUNT: dict[int, float] = {
    0: 0.0,
    1: 0.4,
    2: 0.65,
    3: 0.85,
}


@dataclass(frozen=True)
class OnboardingPattern:
    category: str
    text_keywords: tuple[str, ...]
    html_markers: tuple[str, ...]


# The 10 categories from Linear.md's "Onboarding Detection" section.
ONBOARDING_PATTERNS: tuple[OnboardingPattern, ...] = (
    OnboardingPattern(
        "Product Tour",
        ("take a tour", "product tour", "start tour", "guided product tour"),
        ("product-tour", "producttour", "shepherd-", "introjs-", "driver-popover"),
    ),
    OnboardingPattern("Tooltip", ("tooltip",), ("tooltip", "tippy-", "popper-")),
    OnboardingPattern(
        "Checklist",
        ("checklist", "get started", "complete your setup", "onboarding checklist"),
        ("checklist", "onboarding-checklist", "getting-started"),
    ),
    OnboardingPattern("Hotspot", ("hotspot",), ("hotspot", "pulse-dot", "beacon")),
    OnboardingPattern("Coach Mark", ("coach mark", "coachmark"), ("coachmark", "coach-mark")),
    OnboardingPattern("Popover", ("popover",), ("popover", "shepherd-popover")),
    OnboardingPattern(
        "Guided Tour", ("guided tour", "walkthrough"), ("walkthrough", "guided-tour")
    ),
    OnboardingPattern(
        "Interactive Walkthrough",
        ("interactive walkthrough", "step 1 of", "step-by-step"),
        ("walkthrough-step", "wizard-step"),
    ),
    OnboardingPattern(
        "Empty State Guide",
        ("no data yet", "you don't have any", "create your first", "empty state"),
        ("empty-state", "emptystate"),
    ),
    OnboardingPattern(
        "Resource Center",
        ("resource center", "help center", "knowledge base"),
        ("resource-center", "resourcecenter", "help-widget"),
    ),
)


@dataclass(frozen=True)
class OnboardingHeuristicResult:
    matched_categories: tuple[str, ...]
    dom_signal: bool
    js_signal: bool
    network_signal: bool
    signal_count: int
    confidence: float
    detected_tools: tuple[str, ...]


def _dom_matches(dom: DomSnapshot) -> tuple[str, ...]:
    text = dom.visible_text.lower()
    html = dom.html.lower()
    matched = [
        pattern.category
        for pattern in ONBOARDING_PATTERNS
        if any(kw in text for kw in pattern.text_keywords)
        or any(marker in html for marker in pattern.html_markers)
    ]
    return tuple(matched)


def detect_onboarding_signals(
    dom: DomSnapshot,
    tool_detections: list[ToolDetection],
) -> OnboardingHeuristicResult:
    """Combine DOM content and JS/network tool detections into one verdict.

    Pure function: no I/O, deterministic given its inputs.
    """
    matched_categories = _dom_matches(dom)
    dom_signal = bool(matched_categories)
    js_signal = any(d.matched_scripts or d.matched_globals for d in tool_detections)
    network_signal = any(d.matched_network for d in tool_detections)

    signal_count = sum((dom_signal, js_signal, network_signal))

    return OnboardingHeuristicResult(
        matched_categories=matched_categories,
        dom_signal=dom_signal,
        js_signal=js_signal,
        network_signal=network_signal,
        signal_count=signal_count,
        confidence=_CONFIDENCE_BY_SIGNAL_COUNT.get(signal_count, 0.95),
        detected_tools=tuple(sorted({d.name for d in tool_detections})),
    )


def record_onboarding_detection(
    store: EvidenceStore,
    run_id: str,
    label: str,
    *,
    dom: DomSnapshot,
    tool_detections: list[ToolDetection],
    screenshot: str | None = None,
) -> Evidence:
    """Run the heuristic and record it as Evidence, tagged with the page URL."""
    result = detect_onboarding_signals(dom, tool_detections)
    return store.add(
        run_id,
        label,
        url=dom.url,
        screenshot=screenshot,
        visible_ui={
            "matched_categories": list(result.matched_categories),
            "dom_signal": result.dom_signal,
            "js_signal": result.js_signal,
            "network_signal": result.network_signal,
            "signal_count": result.signal_count,
            "detected_tools": list(result.detected_tools),
        },
        confidence=result.confidence,
    )
