from pathlib import Path

import pytest
from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import Page

from growthradar.browser import BrowserSession, RequestRecord
from growthradar.config import Config
from growthradar.evidence import EvidenceStore
from growthradar.js_network import ToolCategory, collect_and_record, detect_tools


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


@pytest.fixture
def page(config: Config):
    with BrowserSession(config) as session:
        p = session.start()
        # Synthetic <script src="..."> tags reference fake external URLs -- abort
        # them so tests never touch the real network. We only need the `src`
        # attribute text (set synchronously as part of the HTML), not a real load.
        p.route("**/*", lambda route: route.abort())
        yield p


def test_detects_tool_with_all_three_signals(page: Page) -> None:
    page.set_content(
        "<html><head>"
        "<script src='https://cdn.userguiding.com/embed.js'></script>"
        "<script>window.UserGuiding = {};</script>"
        "</head><body></body></html>"
    )
    requests = [
        RequestRecord(
            url="https://cdn.userguiding.com/embed.js", resource_type="script", method="GET"
        )
    ]

    detections = detect_tools(page, requests)

    [ug] = [d for d in detections if d.name == "UserGuiding"]
    assert ug.signal_count == 3
    assert ug.confidence == 0.95


def test_single_signal_gets_low_confidence(page: Page) -> None:
    page.set_content(
        "<html><head><script src='https://cdn.pendo.io/agent.js'></script></head></html>"
    )

    detections = detect_tools(page, [])

    [pendo] = [d for d in detections if d.name == "Pendo"]
    assert pendo.signal_count == 1
    assert pendo.confidence == 0.4


def test_two_signals_gets_solid_confidence(page: Page) -> None:
    page.set_content(
        "<html><head>"
        "<script src='https://fast.appcues.com/loader.js'></script>"
        "<script>window.Appcues = {};</script>"
        "</head></html>"
    )

    detections = detect_tools(page, [])

    [appcues] = [d for d in detections if d.name == "Appcues"]
    assert appcues.signal_count == 2
    assert appcues.confidence == 0.75


def test_detects_userpilot_whatfix_and_userflow(page: Page) -> None:
    page.set_content(
        "<html><head>"
        "<script src='https://js.userpilot.io/sdk.js'></script>"
        "<script src='https://cdn.whatfix.com/loader.js'></script>"
        "<script src='https://js.userflow.com/userflow.js'></script>"
        "</head></html>"
    )

    detections = {d.name: d for d in detect_tools(page, [])}

    assert "Userpilot" in detections
    assert "Whatfix" in detections
    assert "Userflow" in detections
    assert all(d.category == ToolCategory.ONBOARDING for d in detections.values())


def test_no_signals_means_no_detection(page: Page) -> None:
    page.set_content("<html><body><h1>Nothing here</h1></body></html>")

    detections = detect_tools(page, [])

    assert detections == []


def test_network_only_signal_is_detected(page: Page) -> None:
    page.set_content("<html><body></body></html>")
    requests = [
        RequestRecord(url="https://api.intercom.io/messages", resource_type="xhr", method="POST")
    ]

    detections = detect_tools(page, requests)

    [intercom] = [d for d in detections if d.name == "Intercom"]
    assert intercom.signal_count == 1
    assert intercom.matched_network == ("https://api.intercom.io/messages",)


def test_onboarding_tools_are_tagged_onboarding_category(page: Page) -> None:
    page.set_content(
        "<html><head><script src='https://cdn.pendo.io/agent.js'></script></head></html>"
    )

    [pendo] = [d for d in detect_tools(page, []) if d.name == "Pendo"]

    assert pendo.category == ToolCategory.ONBOARDING


def test_google_analytics_is_detected_as_analytics_category(page: Page) -> None:
    page.set_content(
        "<html><head>"
        "<script src='https://www.googletagmanager.com/gtag/js'></script>"
        "<script>window.gtag = function(){};</script>"
        "</head></html>"
    )

    detections = detect_tools(page, [])

    [ga] = [d for d in detections if d.name == "Google Analytics"]
    assert ga.category == ToolCategory.ANALYTICS
    assert ga.signal_count == 2


def test_drift_is_detected_as_ai_assistant_category(page: Page) -> None:
    page.set_content(
        "<html><head>"
        "<script src='https://js.driftt.com/include/1234/abc.js'></script>"
        "<script>window.drift = {};</script>"
        "</head></html>"
    )

    detections = detect_tools(page, [])

    [drift] = [d for d in detections if d.name == "Drift"]
    assert drift.category == ToolCategory.AI_ASSISTANT
    assert drift.signal_count == 2


def test_ai_assistant_tools_are_excluded_from_onboarding_by_default_category_check() -> None:
    # Sanity check that the new category is distinct from onboarding/analytics
    # -- scoring.py's category != "onboarding" filter relies on this.
    assert ToolCategory.AI_ASSISTANT != ToolCategory.ONBOARDING
    assert ToolCategory.AI_ASSISTANT != ToolCategory.ANALYTICS


def test_collect_and_record_serializes_category(tmp_path: Path, page: Page) -> None:
    page.set_content(
        "<html><head><script src='https://cdn.mxpnl.com/libs/mixpanel.js'></script>"
        "<script>window.mixpanel = {};</script></head></html>"
    )

    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        evidence = collect_and_record(page, store, "run-1", "js/network check")

        [mixpanel] = [t for t in evidence.javascript["detected_tools"] if t["name"] == "Mixpanel"]
        assert mixpanel["category"] == "analytics"


class _FailingPage:
    url = "https://broken.example.com"

    def evaluate(self, script: str, *args: object, **kwargs: object) -> None:
        raise PlaywrightError("evaluation context destroyed")


def test_detect_tools_never_raises_on_evaluation_failure() -> None:
    detections = detect_tools(_FailingPage(), [])  # type: ignore[arg-type]
    assert detections == []


def test_collect_and_record_writes_evidence(tmp_path: Path, page: Page) -> None:
    page.set_content(
        "<html><head>"
        "<script src='https://cdn.userguiding.com/embed.js'></script>"
        "<script>window.UserGuiding = {};</script>"
        "</head></html>"
    )
    requests = [
        RequestRecord(
            url="https://cdn.userguiding.com/embed.js", resource_type="script", method="GET"
        )
    ]

    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        evidence = collect_and_record(page, store, "run-1", "js/network check", requests=requests)

        tools = evidence.javascript["detected_tools"]
        [ug] = [t for t in tools if t["name"] == "UserGuiding"]
        assert ug["signal_count"] == 3
        assert evidence.network["request_count"] == 1
        assert evidence.confidence == 0.95


def test_collect_and_record_handles_no_detections(tmp_path: Path, page: Page) -> None:
    page.set_content("<html><body>Plain page</body></html>")

    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        evidence = collect_and_record(page, store, "run-1", "js/network check")

        assert evidence.javascript["detected_tools"] == []
        assert evidence.confidence is None
