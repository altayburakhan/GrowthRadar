import json
from dataclasses import replace
from typing import Any

import pytest
from patchright.sync_api import Page

from growthradar.browser import BrowserSession
from growthradar.config import Config
from growthradar.vision_fallback import (
    _candidate_texts,
    _parse_choice,
    describe_screenshot,
    suggest_click_target,
)


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


@pytest.fixture
def page(config: Config):
    with BrowserSession(config) as session:
        yield session.start()


def _vision_config(config: Config, **overrides: Any) -> Config:
    return replace(
        config,
        groq_api_key="gsk-test-123",
        groq_vision_model="qwen/qwen3.6-27b",
        **overrides,
    )


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _groq_response(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


_CANDIDATES_PAGE = """
<html><body>
<button>Companies</button>
<button>Advisors</button>
<button style="display:none">Hidden Option</button>
<button>Companies</button>
<a href="#">A link with an extremely long label that exceeds the sixty character cap we enforce</a>
</body></html>
"""


def test_candidate_texts_dedupes_skips_hidden_and_overlong(page: Page) -> None:
    page.set_content(_CANDIDATES_PAGE)

    texts = _candidate_texts(page, 'button, a, [role="button"]')

    assert texts == ["Companies", "Advisors"]


_CANDIDATES_PAGE_WITH_DEMO_BUTTON = """
<html><body>
<button>Sign up</button>
<button>Book a Demo</button>
<button>Request a demo</button>
</body></html>
"""


def test_candidate_texts_excludes_demo_booking_buttons(page: Page) -> None:
    # "Book a Demo"/"Request a demo" lead to scheduling a live sales call,
    # not a self-serve trial signup -- never something vision should be
    # offered as a click target (digifabster.com/getstarted/ has exactly
    # this button next to the real "Start a free trial" one).
    page.set_content(_CANDIDATES_PAGE_WITH_DEMO_BUTTON)

    texts = _candidate_texts(page, 'button, a, [role="button"]')

    assert texts == ["Sign up"]


def test_parse_choice_strips_think_block_and_extracts_json() -> None:
    content = (
        "<think>The user wants me to pick an option. I'll pick Companies.</think>\n"
        '{"choice": "Companies"}'
    )
    assert _parse_choice(content, ["Companies", "Advisors"]) == "Companies"


def test_parse_choice_rejects_hallucinated_option_not_in_candidates() -> None:
    content = '{"choice": "Something Not On The Page"}'
    assert _parse_choice(content, ["Companies", "Advisors"]) is None


def test_parse_choice_returns_none_for_unparseable_content() -> None:
    assert _parse_choice("I cannot help with that.", ["Companies", "Advisors"]) is None


def test_suggest_click_target_returns_none_without_vision_model_configured(
    config: Config, page: Page
) -> None:
    page.set_content(_CANDIDATES_PAGE)
    assert suggest_click_target(page, config) is None


def test_suggest_click_target_returns_none_when_nothing_clickable(
    config: Config, page: Page
) -> None:
    page.set_content("<html><body><p>Nothing here</p></body></html>")
    assert suggest_click_target(page, _vision_config(config)) is None


def test_suggest_click_target_returns_matched_choice(
    config: Config, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float = 30):  # noqa: ANN001
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(_groq_response('{"choice": "Companies"}'))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    page.set_content(_CANDIDATES_PAGE)

    choice = suggest_click_target(page, _vision_config(config))

    assert choice == "Companies"
    assert captured["body"]["model"] == "qwen/qwen3.6-27b"
    image_content = captured["body"]["messages"][0]["content"][1]
    assert image_content["type"] == "image_url"
    assert image_content["image_url"]["url"].startswith("data:image/png;base64,")


def test_suggest_click_target_returns_none_on_network_failure(
    config: Config, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.error

    def fake_urlopen(request, timeout: float = 30):  # noqa: ANN001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    page.set_content(_CANDIDATES_PAGE)

    assert suggest_click_target(page, _vision_config(config)) is None


def test_suggest_click_target_returns_none_when_model_picks_option_not_offered(
    config: Config, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_urlopen(request, timeout: float = 30):  # noqa: ANN001
        return _FakeResponse(_groq_response('{"choice": "A Button That Does Not Exist"}'))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    page.set_content(_CANDIDATES_PAGE)

    assert suggest_click_target(page, _vision_config(config)) is None


# --- describe_screenshot (llm_summary.py's narrative-enrichment path) -------


def test_describe_screenshot_returns_description(
    tmp_path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"not-a-real-png-but-bytes-are-all-that-matters-here")

    def fake_urlopen(request, timeout: float = 30):  # noqa: ANN001
        return _FakeResponse(_groq_response("A checklist-style onboarding tour is visible."))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    description = describe_screenshot(str(image_path), "onboarding", _vision_config(config))

    assert description == "A checklist-style onboarding tour is visible."


def test_describe_screenshot_strips_think_block(
    tmp_path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"bytes")

    def fake_urlopen(request, timeout: float = 30):  # noqa: ANN001
        return _FakeResponse(
            _groq_response("<think>reasoning about the image</think>\nAn empty dashboard state.")
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    description = describe_screenshot(str(image_path), "dashboard", _vision_config(config))

    assert description == "An empty dashboard state."


def test_describe_screenshot_returns_none_without_vision_model_configured(
    tmp_path, config: Config
) -> None:
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"bytes")

    assert describe_screenshot(str(image_path), "dashboard", config) is None


def test_describe_screenshot_returns_none_when_file_missing(config: Config) -> None:
    assert (
        describe_screenshot("/nonexistent/path/shot.png", "dashboard", _vision_config(config))
        is None
    )


def test_describe_screenshot_returns_none_on_network_failure(
    tmp_path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.error

    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"bytes")

    def fake_urlopen(request, timeout: float = 30):  # noqa: ANN001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert describe_screenshot(str(image_path), "dashboard", _vision_config(config)) is None
