import json
from dataclasses import replace
from typing import Any

import pytest

from growthradar.config import Config
from growthradar.vision_fallback import _parse_choice, _parse_field_values, describe_screenshot


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


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


def test_parse_field_values_maps_numbered_keys_to_zero_based_index() -> None:
    content = '{"1": "Marketing Manager", "2": "10-50"}'
    assert _parse_field_values(content, 2) == {0: "Marketing Manager", 1: "10-50"}


def test_parse_field_values_strips_think_block() -> None:
    content = '<think>picking values</think>\n{"1": "Founder"}'
    assert _parse_field_values(content, 1) == {0: "Founder"}


def test_parse_field_values_drops_out_of_range_and_empty_entries() -> None:
    content = '{"1": "Founder", "2": "", "5": "out of range"}'
    assert _parse_field_values(content, 2) == {0: "Founder"}


def test_parse_field_values_returns_none_for_unparseable_content() -> None:
    assert _parse_field_values("I cannot help with that.", 2) is None


def test_parse_field_values_returns_none_when_all_entries_invalid() -> None:
    assert _parse_field_values('{"1": ""}', 1) is None


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
