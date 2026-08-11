import json
from pathlib import Path
from typing import Any

import pytest

from growthradar.config import Config
from growthradar.evidence import Evidence, EvidenceStore
from growthradar.llm_summary import record_llm_summary, summarize_evidence
from growthradar.scoring import DimensionScore, ScoreResult

_next_id = 0


def _evidence(
    label: str,
    *,
    url: str | None = None,
    screenshot: str | None = None,
    javascript: Any = None,
    visible_ui: Any = None,
    confidence: float | None = None,
) -> Evidence:
    global _next_id
    _next_id += 1
    return Evidence(
        id=_next_id,
        run_id="run-1",
        seq=_next_id,
        timestamp="2026-01-01T00:00:00+00:00",
        label=label,
        url=url,
        screenshot=screenshot,
        dom=None,
        javascript=javascript,
        network=None,
        visible_ui=visible_ui,
        confidence=confidence,
    )


def _score(verdict: str = "hot") -> ScoreResult:
    dim = DimensionScore(name="x", score=80.0, signal_count=3, max_signals=4, evidence_ids=(1,))
    return ScoreResult(
        run_id="run-1",
        icp_fit=dim,
        onboarding_opportunity=dim,
        product_experience=dim,
        overall_score=80.0,
        verdict=verdict,
    )


def _groq_config(*, groq_vision_model: str = "", **env: str) -> Config:
    base = {
        "GROWTHRADAR_LLM_PROVIDER": "groq",
        "GROQ_API_KEY": "gsk-test-123",
        "GROQ_VISION_MODEL": groq_vision_model,
        **env,
    }
    for key, value in base.items():
        __import__("os").environ[key] = value
    try:
        return Config.from_env(env_path="/nonexistent/.env")
    finally:
        for key in base:
            del __import__("os").environ[key]


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def test_summarize_evidence_returns_none_when_provider_is_not_groq() -> None:
    config = _groq_config(GROWTHRADAR_LLM_PROVIDER="heuristic")
    assert summarize_evidence([], _score(), config) is None


def test_summarize_evidence_returns_none_when_no_api_key() -> None:
    config = _groq_config(GROQ_API_KEY="")
    assert summarize_evidence([], _score(), config) is None


def test_summarize_evidence_parses_successful_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout: float = 20):  # noqa: ANN001
        return _FakeResponse(
            {"choices": [{"message": {"content": "Strong prospect based on the checklist UI."}}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    summary = summarize_evidence([], _score(), _groq_config())

    assert summary == "Strong prospect based on the checklist UI."


def test_summarize_evidence_returns_none_on_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    def fake_urlopen(request, timeout: float = 20):  # noqa: ANN001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert summarize_evidence([], _score(), _groq_config()) is None


def test_summarize_evidence_returns_none_on_empty_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout: float = 20):  # noqa: ANN001
        return _FakeResponse({"choices": []})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert summarize_evidence([], _score(), _groq_config()) is None


def test_summarize_evidence_prompt_includes_key_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float = 20):  # noqa: ANN001
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    evidence = [
        _evidence(
            "registration attempt",
            visible_ui={"submitted": True},
        ),
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={
                "detected_tools": [{"name": "Pendo", "category": "onboarding", "confidence": 0.95}]
            },
        ),
    ]

    summarize_evidence(evidence, _score(), _groq_config())

    prompt = captured["body"]["messages"][0]["content"]
    assert "Registration completed: True" in prompt
    assert "Pendo (onboarding)" in prompt
    assert "HOT" in prompt


def test_summarize_evidence_prompt_flags_competitor_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float = 20):  # noqa: ANN001
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    evidence = [
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={
                "detected_tools": [{"name": "WalkMe", "category": "onboarding", "confidence": 0.95}]
            },
        ),
    ]

    summarize_evidence(evidence, _score(), _groq_config())

    prompt = captured["body"]["messages"][0]["content"]
    assert "Already a UserGuiding customer: False" in prompt
    assert "Competitor onboarding tools detected: WalkMe" in prompt


def test_summarize_evidence_prompt_flags_existing_userguiding_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float = 20):  # noqa: ANN001
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    evidence = [
        _evidence(
            "js/network: https://acme.com",
            url="https://acme.com",
            javascript={
                "detected_tools": [
                    {"name": "UserGuiding", "category": "onboarding", "confidence": 0.95}
                ]
            },
        ),
    ]

    summarize_evidence(evidence, _score(), _groq_config())

    prompt = captured["body"]["messages"][0]["content"]
    assert "Already a UserGuiding customer: True" in prompt
    assert "Competitor onboarding tools detected: none" in prompt


def test_summarize_evidence_prompt_includes_screenshot_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard_shot = tmp_path / "dashboard.png"
    dashboard_shot.write_bytes(b"bytes")
    updates_shot = tmp_path / "updates.png"
    updates_shot.write_bytes(b"bytes")

    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float = 20):  # noqa: ANN001
        body = json.loads(request.data.decode("utf-8"))
        content = body["messages"][0]["content"]
        if isinstance(content, list):
            # A vision call (image_url content block) -- reply differently
            # per target page so the test can tell them apart in the prompt.
            is_dashboard = "dashboard" in content[0]["text"]
            reply = "A guided checklist tour is shown." if is_dashboard else "A changelog entry."
            return _FakeResponse({"choices": [{"message": {"content": reply}}]})
        captured["body"] = body
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    evidence = [
        _evidence(
            "screenshot: https://acme.com/dashboard",
            url="https://acme.com/dashboard",
            screenshot=str(dashboard_shot),
            visible_ui={"screenshot_kind": "dashboard", "success": True},
        ),
        _evidence(
            "screenshot: https://acme.com/changelog",
            url="https://acme.com/changelog",
            screenshot=str(updates_shot),
            visible_ui={"screenshot_kind": "product_updates", "success": True},
        ),
    ]

    summarize_evidence(evidence, _score(), _groq_config(groq_vision_model="qwen/qwen3.6-27b"))

    prompt = captured["body"]["messages"][0]["content"]
    assert "Dashboard/onboarding screenshot shows: A guided checklist tour is shown." in prompt
    assert "Product updates screenshot shows: A changelog entry." in prompt


def test_summarize_evidence_prompt_omits_screenshot_observations_without_vision_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float = 20):  # noqa: ANN001
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    evidence = [
        _evidence(
            "screenshot: https://acme.com/dashboard",
            url="https://acme.com/dashboard",
            screenshot="/some/path/dashboard.png",
            visible_ui={"screenshot_kind": "dashboard", "success": True},
        ),
    ]

    # No groq_vision_model set -- default _groq_config().
    summarize_evidence(evidence, _score(), _groq_config())

    prompt = captured["body"]["messages"][0]["content"]
    assert "screenshot shows" not in prompt


def test_record_llm_summary_writes_evidence_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_urlopen(request, timeout: float = 20):  # noqa: ANN001
        return _FakeResponse({"choices": [{"message": {"content": "Looks promising."}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        evidence = record_llm_summary(
            store, "run-1", "llm summary", evidence=[], score=_score(), config=_groq_config()
        )

        assert evidence.visible_ui == {"summary": "Looks promising."}


def test_record_llm_summary_writes_skip_reason_when_unavailable(tmp_path: Path) -> None:
    config = _groq_config(GROWTHRADAR_LLM_PROVIDER="heuristic")

    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        evidence = record_llm_summary(
            store, "run-1", "llm summary", evidence=[], score=_score(), config=config
        )

        assert evidence.visible_ui == {"skipped": True}
