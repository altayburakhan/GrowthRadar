import json
import urllib.error
from email.message import EmailMessage
from typing import Any

import pytest

from growthradar.temp_email import (
    GmailImapProvider,
    MailTmProvider,
    TempInbox,
    _extract_verification_code,
    _extract_verification_link,
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
    html = "<a href='https://example.com/x'>link</a>"
    assert _extract_verification_link(html, "verify") is None


def test_extract_verification_link_finds_href_when_keyword_present() -> None:
    html = "please verify: <a href='https://example.com/x'>link</a>"
    assert _extract_verification_link(html, "verify") == "https://example.com/x"


def test_wait_for_verification_code_finds_code_on_first_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ("GET", "/messages"): {"hydra:member": [{"id": "msg-1"}]},
        ("GET", "/messages/msg-1"): {
            "html": ["<p>Your verification code</p>"],
            "text": "Your verification code is: 482910. It expires in 10 minutes.",
        },
    }
    monkeypatch.setattr("urllib.request.urlopen", _make_fake_urlopen(responses))

    inbox = TempInbox(address="a@b.com", provider_data={"token": "tok"})
    code = MailTmProvider().wait_for_verification_code(inbox, timeout=5.0)

    assert code == "482910"


def test_extract_verification_code_prefers_digits_near_the_word_code() -> None:
    # The "2026" copyright-year-style number sits far from "code" and must
    # lose to the actual OTP next to it.
    body = "© 2026 Acme Inc. Your code is 918273. Thanks for signing up."
    assert _extract_verification_code(body) == "918273"


def test_extract_verification_code_returns_none_when_no_digits_present() -> None:
    assert _extract_verification_code("Thanks for signing up, no code here") is None


def test_extract_verification_code_ignores_inline_style_hex_color_near_code() -> None:
    # Regression (sproutsocial.com): a design-heavy HTML-only transactional
    # email (no text/plain alternative) with an inline style="color:#040404"
    # sitting right next to the word "code" -- the raw, un-stripped HTML
    # read "040404" as the OTP instead of the real one further away, and the
    # site rejected it as incorrect.
    body = (
        '<div style="color:#040404">Verify your email</div>'
        "<p>Enter this code to continue: <b>918273</b></p>"
    )
    assert _extract_verification_code(body) == "918273"


def test_extract_verification_code_ignores_style_block_contents() -> None:
    body = (
        "<style>.code-box{color:#040404;font-size:14px}</style>"
        "<p>Your verification code is 918273.</p>"
    )
    assert _extract_verification_code(body) == "918273"


class _FakeImapConnection:
    def __init__(self, messages: dict[bytes, bytes]) -> None:
        self._messages = messages

    def __enter__(self) -> "_FakeImapConnection":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def login(self, user: str, password: str) -> None:
        pass

    def select(self, mailbox: str) -> None:
        pass

    def search(self, charset: str | None, *criteria: str) -> tuple[str, list[bytes]]:
        if not self._messages:
            return "OK", [b""]
        return "OK", [b" ".join(self._messages.keys())]

    def fetch(self, message_id: bytes, parts: str) -> tuple[str, list[Any]]:
        raw = self._messages.get(message_id)
        if raw is None:
            return "NO", [None]
        return "OK", [(b"1 (RFC822 {%d}" % len(raw), raw)]


def _build_raw_email(*, to: str, text: str = "", html: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "noreply@example.com"
    msg["To"] = to
    msg["Subject"] = "Verify your account"
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return bytes(msg.as_bytes())


def test_gmail_create_inbox_returns_a_plus_tagged_address() -> None:
    inbox = GmailImapProvider("user@gmail.com", "app-password").create_inbox()

    assert inbox is not None
    assert inbox.address.startswith("user+")
    assert inbox.address.endswith("@gmail.com")
    assert inbox.provider_data["tag"] in inbox.address


def test_gmail_create_inbox_returns_none_for_an_address_with_no_domain() -> None:
    assert GmailImapProvider("not-an-email", "app-password").create_inbox() is None


def test_gmail_wait_for_verification_link_finds_link(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _build_raw_email(
        to="user+abc123@gmail.com",
        text="Please verify your account",
        html='<p>Please verify: <a href="https://example.com/verify?token=xyz">here</a></p>',
    )
    fake = _FakeImapConnection({b"1": raw})
    monkeypatch.setattr("growthradar.temp_email.imaplib.IMAP4_SSL", lambda host: fake)

    provider = GmailImapProvider("user@gmail.com", "app-password")
    inbox = TempInbox(address="user+abc123@gmail.com", provider_data={"tag": "abc123"})
    link = provider.wait_for_verification_link(inbox, timeout=5.0)

    assert link == "https://example.com/verify?token=xyz"


def test_gmail_wait_for_verification_code_finds_code(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _build_raw_email(
        to="user+def456@gmail.com",
        text="Your verification code is: 738291. It expires soon.",
    )
    fake = _FakeImapConnection({b"1": raw})
    monkeypatch.setattr("growthradar.temp_email.imaplib.IMAP4_SSL", lambda host: fake)

    provider = GmailImapProvider("user@gmail.com", "app-password")
    inbox = TempInbox(address="user+def456@gmail.com", provider_data={"tag": "def456"})
    code = provider.wait_for_verification_code(inbox, timeout=5.0)

    assert code == "738291"


def test_gmail_wait_for_verification_link_times_out_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeImapConnection({})
    monkeypatch.setattr("growthradar.temp_email.imaplib.IMAP4_SSL", lambda host: fake)
    monkeypatch.setattr("growthradar.temp_email.time.sleep", lambda seconds: None)

    provider = GmailImapProvider("user@gmail.com", "app-password")
    inbox = TempInbox(address="user+ghi789@gmail.com", provider_data={"tag": "ghi789"})
    link = provider.wait_for_verification_link(inbox, timeout=0.05)

    assert link is None


def test_gmail_wait_for_verification_link_returns_none_on_imap_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_connection(host: str) -> _FakeImapConnection:
        raise OSError("connection refused")

    monkeypatch.setattr("growthradar.temp_email.imaplib.IMAP4_SSL", fake_connection)
    monkeypatch.setattr("growthradar.temp_email.time.sleep", lambda seconds: None)

    provider = GmailImapProvider("user@gmail.com", "app-password")
    inbox = TempInbox(address="user+jkl012@gmail.com", provider_data={"tag": "jkl012"})
    link = provider.wait_for_verification_link(inbox, timeout=0.05)

    assert link is None
