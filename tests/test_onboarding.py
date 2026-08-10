from pathlib import Path

from growthradar.dom import DomSnapshot
from growthradar.evidence import EvidenceStore
from growthradar.js_network import ToolCategory, ToolDetection
from growthradar.onboarding import detect_onboarding_signals, record_onboarding_detection


def _dom(
    *, visible_text: str = "", html: str = "", url: str = "https://example.com"
) -> DomSnapshot:
    return DomSnapshot(
        url=url,
        title="Test",
        html=html,
        visible_text=visible_text,
        navigation=[],
        interactive_elements=[],
        truncated=False,
    )


def _tool(
    name: str = "UserGuiding",
    *,
    scripts: tuple[str, ...] = (),
    globals_: tuple[str, ...] = (),
    network: tuple[str, ...] = (),
) -> ToolDetection:
    return ToolDetection(
        name=name,
        category=ToolCategory.ONBOARDING,
        signal_count=sum(bool(x) for x in (scripts, globals_, network)),
        matched_scripts=scripts,
        matched_globals=globals_,
        matched_network=network,
    )


def test_no_signals_means_zero_confidence() -> None:
    result = detect_onboarding_signals(_dom(), [])

    assert result.matched_categories == ()
    assert result.signal_count == 0
    assert result.confidence == 0.0


def test_dom_text_match_alone_is_low_confidence() -> None:
    result = detect_onboarding_signals(_dom(visible_text="Take a tour of your new dashboard"), [])

    assert "Product Tour" in result.matched_categories
    assert result.dom_signal is True
    assert result.js_signal is False
    assert result.network_signal is False
    assert result.signal_count == 1
    assert result.confidence == 0.4


def test_dom_html_marker_also_triggers_dom_signal() -> None:
    result = detect_onboarding_signals(_dom(html="<div class='tooltip-container'>Hi</div>"), [])

    assert "Tooltip" in result.matched_categories
    assert result.dom_signal is True


def test_dom_plus_js_signal_reaches_solid_confidence() -> None:
    dom = _dom(visible_text="Complete your setup checklist")
    tools = [_tool(scripts=("https://cdn.userguiding.com/x.js",))]

    result = detect_onboarding_signals(dom, tools)

    assert result.dom_signal is True
    assert result.js_signal is True
    assert result.network_signal is False
    assert result.signal_count == 2
    assert result.confidence == 0.65


def test_all_three_signals_reach_strong_confidence() -> None:
    dom = _dom(visible_text="Welcome! Here is your onboarding checklist")
    tools = [
        _tool(
            scripts=("https://cdn.userguiding.com/x.js",),
            network=("https://api.userguiding.com/events",),
        )
    ]

    result = detect_onboarding_signals(dom, tools)

    assert result.signal_count == 3
    assert result.confidence == 0.85
    assert result.detected_tools == ("UserGuiding",)


def test_network_only_signal_counts_independently() -> None:
    dom = _dom()
    tools = [_tool(network=("https://api.pendo.io/track",))]

    result = detect_onboarding_signals(dom, tools)

    assert result.dom_signal is False
    assert result.js_signal is False
    assert result.network_signal is True
    assert result.signal_count == 1
    assert result.confidence == 0.4


def test_matched_categories_can_include_multiple_patterns() -> None:
    dom = _dom(visible_text="This checklist has a tooltip explaining each step")

    result = detect_onboarding_signals(dom, [])

    assert "Checklist" in result.matched_categories
    assert "Tooltip" in result.matched_categories


def test_record_onboarding_detection_writes_evidence(tmp_path: Path) -> None:
    dom = _dom(visible_text="Take a tour", url="https://example.com/dashboard")

    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        evidence = record_onboarding_detection(
            store,
            "run-1",
            "onboarding heuristics",
            dom=dom,
            tool_detections=[],
            screenshot="screenshots/run-1/dashboard.png",
        )

        assert evidence.url == "https://example.com/dashboard"
        assert evidence.screenshot == "screenshots/run-1/dashboard.png"
        assert evidence.confidence == 0.4
        assert "Product Tour" in evidence.visible_ui["matched_categories"]

        [stored] = store.for_run("run-1")
        assert stored.id == evidence.id
