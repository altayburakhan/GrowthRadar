import json
import urllib.error
from typing import Any

import pytest

from growthradar.temp_email import MailTmProvider, TempInbox, _extract_verification_link


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _make_fake_urlopen(responses: dict[tuple[str, str], dict[str, Any]]):
    def fake_urlopen(request, timeout: float = 10):  # noqa: ANN001
        method = request.get_method()
        for (want_method, path_suffix), payload in responses.items():
            if method == want_method and request.full_url.endswith(path_suffix):
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected request: {method} {request.full_url}")

    return fake_urlopen


def test_create_inbox_success(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        ("GET", "/domains"): {"hydra:member": [{"domain": "mailtm.example"}]},
        ("POST", "/accounts"): {"id": "acc-1"},
        ("POST", "/token"): {"token": "tok-123"},
    }
    monkeypatch.setattr("urllib.request.urlopen", _make_fake_urlopen(responses))

    inbox = MailTmProvider().create_inbox()

    assert inbox is not None
    assert inbox.address.endswith("@mailtm.example")
    assert inbox.provider_data["token"] == "tok-123"


def test_create_inbox_returns_none_when_no_domains_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {("GET", "/domains"): {"hydra:member": []}}
    monkeypatch.setattr("urllib.request.urlopen", _make_fake_urlopen(responses))

    assert MailTmProvider().create_inbox() is None


def test_create_inbox_returns_none_on_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout: float = 10):  # noqa: ANN001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert MailTmProvider().create_inbox() is None


def test_create_inbox_returns_none_when_token_request_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ("GET", "/domains"): {"hydra:member": [{"domain": "mailtm.example"}]},
        ("POST", "/accounts"): {"id": "acc-1"},
        ("POST", "/token"): {},  # no token in response
    }
    monkeypatch.setattr("urllib.request.urlopen", _make_fake_urlopen(responses))

    assert MailTmProvider().create_inbox() is None


def test_wait_for_verification_link_finds_link_on_first_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ("GET", "/messages"): {"hydra:member": [{"id": "msg-1"}]},
        ("GET", "/messages/msg-1"): {
            "html": ["<p>Click <a href='https://example.com/verify?token=abc'>here</a></p>"],
            "text": "please verify your account",
        },
    }
    monkeypatch.setattr("urllib.request.urlopen", _make_fake_urlopen(responses))

    inbox = TempInbox(address="a@b.com", provider_data={"token": "tok"})
    link = MailTmProvider().wait_for_verification_link(inbox, timeout=5.0)

    assert link == "https://example.com/verify?token=abc"


def test_wait_for_verification_link_times_out_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {("GET", "/messages"): {"hydra:member": []}}
    monkeypatch.setattr("urllib.request.urlopen", _make_fake_urlopen(responses))
    monkeypatch.setattr("growthradar.temp_email.time.sleep", lambda seconds: None)

    inbox = TempInbox(address="a@b.com", provider_data={"token": "tok"})
    link = MailTmProvider().wait_for_verification_link(inbox, timeout=0.05)

    assert link is None


def test_extract_verification_link_requires_keyword_match() -> None:
    detail = {"html": ["<a href='https://example.com/x'>link</a>"], "text": "unrelated content"}
    assert _extract_verification_link(detail, "verify") is None


def test_extract_verification_link_returns_none_for_missing_detail() -> None:
    assert _extract_verification_link(None, "verify") is None
