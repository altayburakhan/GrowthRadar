"""Disposable inbox support for email-verification steps during registration.

`TempEmailProvider` is a small Protocol so the registration flow never depends on
a specific service. `MailTmProvider` is one concrete, key-free implementation
(mail.tm's public REST API). All requests are best-effort: network failures are
logged and return None/empty rather than raising, since registration must
continue even when verification can't be completed.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from growthradar.identity import generate_password

logger = logging.getLogger(__name__)

_MAILTM_BASE = "https://api.mail.tm"
_POLL_INTERVAL_SECONDS = 2.0
_VERIFICATION_LINK_RE = re.compile(r'href=["\'](https?://[^"\'>\s]+)["\']', re.IGNORECASE)


@dataclass(frozen=True)
class TempInbox:
    address: str
    provider_data: dict[str, str]


class TempEmailProvider(Protocol):
    def create_inbox(self) -> TempInbox | None: ...

    def wait_for_verification_link(
        self, inbox: TempInbox, *, timeout: float = 60.0, keyword: str = "verify"
    ) -> str | None: ...


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            result: dict[str, Any] = json.load(response)
            return result
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning("temp-email request failed (%s %s): %s", method, url, exc)
        return None


class MailTmProvider:
    """Disposable inboxes via mail.tm's free, key-free public API."""

    def create_inbox(self) -> TempInbox | None:
        domains = _request_json(f"{_MAILTM_BASE}/domains")
        members = (domains or {}).get("hydra:member") or []
        if not members:
            logger.warning("mail.tm: no domains available")
            return None

        address = f"{secrets.token_hex(6)}@{members[0]['domain']}"
        password = generate_password()

        account = _request_json(
            f"{_MAILTM_BASE}/accounts",
            method="POST",
            payload={"address": address, "password": password},
        )
        if account is None:
            return None

        token_response = _request_json(
            f"{_MAILTM_BASE}/token",
            method="POST",
            payload={"address": address, "password": password},
        )
        token = (token_response or {}).get("token")
        if not token:
            logger.warning("mail.tm: account created but token request failed")
            return None

        return TempInbox(address=address, provider_data={"token": token})

    def wait_for_verification_link(
        self, inbox: TempInbox, *, timeout: float = 60.0, keyword: str = "verify"
    ) -> str | None:
        headers = {"Authorization": f"Bearer {inbox.provider_data.get('token', '')}"}
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            messages = _request_json(f"{_MAILTM_BASE}/messages", headers=headers)
            for summary in (messages or {}).get("hydra:member") or []:
                message_id = summary.get("id")
                if not message_id:
                    continue
                detail = _request_json(f"{_MAILTM_BASE}/messages/{message_id}", headers=headers)
                link = _extract_verification_link(detail, keyword)
                if link:
                    return link
            time.sleep(_POLL_INTERVAL_SECONDS)

        logger.warning("no verification email received within %.0fs for %s", timeout, inbox.address)
        return None


def _extract_verification_link(detail: dict[str, Any] | None, keyword: str) -> str | None:
    if not detail:
        return None
    html_parts = detail.get("html") or []
    html = " ".join(html_parts) if isinstance(html_parts, list) else str(html_parts)
    text = detail.get("text", "")
    body = f"{html} {text}"
    if keyword.lower() not in body.lower():
        return None
    match = _VERIFICATION_LINK_RE.search(html)
    return match.group(1) if match else None
