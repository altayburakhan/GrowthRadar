"""Disposable inbox support for email-verification steps during registration.

`TempEmailProvider` is a small Protocol so the registration flow never depends on
a specific service. `MailTmProvider` is one concrete, key-free implementation
(mail.tm's public REST API); `GmailImapProvider` is another, reading a real
Gmail inbox over IMAP with an app password -- useful for sites that reject
disposable-looking mail.tm addresses as "not a work email". All requests are
best-effort: network failures are logged and return None/empty rather than
raising, since registration must continue even when verification can't be
completed.
"""

from __future__ import annotations

import email as email_module
import imaplib
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from email.message import Message
from typing import Any, Protocol

from growthradar.identity import generate_password

logger = logging.getLogger(__name__)

_MAILTM_BASE = "https://api.mail.tm"
_POLL_INTERVAL_SECONDS = 2.0
_VERIFICATION_LINK_RE = re.compile(r'href=["\'](https?://[^"\'>\s]+)["\']', re.IGNORECASE)
# A bare digit run, 4-8 digits -- covers the common OTP lengths (4, 6, 8) seen
# live (joinblink.com: 6 digits) without also matching things like a 10-digit
# phone number or a longer tracking id.
_VERIFICATION_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
# Strip <style>/<script> block contents entirely (their CSS/JS text can
# itself contain stray digit runs, e.g. a hex color), then strip every
# remaining tag -- which also removes tag *attributes* like
# style="color:#040404", the biggest source of noise since those sit
# directly inline next to real content rather than off in a footer.
_HTML_STYLE_SCRIPT_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_markup(body: str) -> str:
    """No-op on plain text; on HTML, removes markup so a stray hex color or
    other CSS/attribute value can never be mistaken for the OTP (seen live
    on sproutsocial.com: a design-heavy transactional email with no
    text/plain alternative -- an inline `style="color:#040404"` sitting near
    the word "code" in the raw HTML got read as the verification code
    itself, and the real one was never entered)."""
    body = _HTML_STYLE_SCRIPT_RE.sub(" ", body)
    return _HTML_TAG_RE.sub(" ", body)


@dataclass(frozen=True)
class TempInbox:
    address: str
    provider_data: dict[str, str]


class TempEmailProvider(Protocol):
    def create_inbox(self) -> TempInbox | None: ...

    def wait_for_verification_link(
        self, inbox: TempInbox, *, timeout: float = 60.0, keyword: str = "verify"
    ) -> str | None: ...

    def wait_for_verification_code(
        self, inbox: TempInbox, *, timeout: float = 60.0
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
        return self._poll(
            inbox, timeout, lambda html, _text: _extract_verification_link(html, keyword)
        )

    def wait_for_verification_code(self, inbox: TempInbox, *, timeout: float = 60.0) -> str | None:
        return self._poll(
            inbox, timeout, lambda html, text: _extract_verification_code(f"{text} {html}")
        )

    def _poll(
        self,
        inbox: TempInbox,
        timeout: float,
        extract: Callable[[str, str], str | None],
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
                if not detail:
                    continue
                html_parts = detail.get("html") or []
                html = " ".join(html_parts) if isinstance(html_parts, list) else str(html_parts)
                text = str(detail.get("text", ""))
                result = extract(html, text)
                if result:
                    return result
            time.sleep(_POLL_INTERVAL_SECONDS)

        logger.warning("no verification email received within %.0fs for %s", timeout, inbox.address)
        return None


def _extract_verification_link(html: str, keyword: str) -> str | None:
    if keyword.lower() not in html.lower():
        return None
    match = _VERIFICATION_LINK_RE.search(html)
    return match.group(1) if match else None


def _extract_verification_code(body: str) -> str | None:
    """Prefer the digit run closest to the word "code" over just the first
    one in the body -- emails routinely carry other numbers (a copyright
    year, an unsubscribe id, a tracking-pixel query param) that aren't the
    OTP, and some of those can still sort earlier in the text than the real
    code (e.g. a footer "© 2026" before a body "Your code is 918273")."""
    body = _strip_html_markup(body)
    idx = body.lower().find("code")
    if idx != -1:
        window_start = max(0, idx - 100)
        window = body[window_start : idx + 200]
        matches = list(_VERIFICATION_CODE_RE.finditer(window))
        if matches:
            closest = min(matches, key=lambda m: abs((window_start + m.start()) - idx))
            return closest.group(1)
    match = _VERIFICATION_CODE_RE.search(body)
    return match.group(1) if match else None


_GMAIL_IMAP_HOST = "imap.gmail.com"
# How many of the most recent matching messages to actually open per poll --
# a real inbox can accumulate several matches for the same +tag (a resend,
# a "your account was created" follow-up); checking a handful covers that
# without fetching a message backlog on every 2-second poll.
_MAX_MESSAGES_PER_POLL = 5


def _combined_body_text(msg: Message) -> str:
    parts: list[str] = []
    walkable = msg.walk() if msg.is_multipart() else [msg]
    for part in walkable:
        if part.is_multipart() or part.get_content_type() not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        with suppress(LookupError, UnicodeDecodeError):
            parts.append(payload.decode(charset, errors="replace"))
    return " ".join(parts)


class GmailImapProvider:
    """Reads a real Gmail inbox over IMAP with an app password (see
    Config.gmail_address / gmail_app_password) -- useful for sites that
    reject mail.tm's disposable-looking addresses as "not a work email".

    Never opens a browser or touches accounts.google.com, so it's
    unaffected by the automation detection that blocks google_profile_dir's
    OAuth flow (see registration.py's allow_google_oauth) -- this is plain
    IMAP, the same protocol any real mail client uses.

    Each run gets its own "+tag" alias of the real address (mail sent to
    user+tag@gmail.com still lands in user@gmail.com's inbox -- a standard
    Gmail feature, not a GrowthRadar trick), so a poll only ever matches the
    message this specific run is waiting on, never an unrelated older one.
    """

    def __init__(self, address: str, app_password: str) -> None:
        self._address = address
        self._app_password = app_password

    def create_inbox(self) -> TempInbox | None:
        local, _, domain = self._address.partition("@")
        if not domain:
            logger.warning("GROWTHRADAR_GMAIL_ADDRESS is not a valid email address")
            return None
        tag = secrets.token_hex(4)
        return TempInbox(address=f"{local}+{tag}@{domain}", provider_data={"tag": tag})

    def wait_for_verification_link(
        self, inbox: TempInbox, *, timeout: float = 60.0, keyword: str = "verify"
    ) -> str | None:
        return self._poll(inbox, timeout, lambda body: _extract_verification_link(body, keyword))

    def wait_for_verification_code(self, inbox: TempInbox, *, timeout: float = 60.0) -> str | None:
        return self._poll(inbox, timeout, _extract_verification_code)

    def _poll(
        self, inbox: TempInbox, timeout: float, extract: Callable[[str], str | None]
    ) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for body in self._fetch_recent_bodies(inbox.address):
                result = extract(body)
                if result:
                    return result
            time.sleep(_POLL_INTERVAL_SECONDS)

        logger.warning("no email received within %.0fs for %s", timeout, inbox.address)
        return None

    def _fetch_recent_bodies(self, tagged_address: str) -> list[str]:
        try:
            with imaplib.IMAP4_SSL(_GMAIL_IMAP_HOST) as imap:
                imap.login(self._address, self._app_password)
                imap.select("INBOX")
                status, data = imap.search(None, "TO", f'"{tagged_address}"')
                if status != "OK" or not data or not data[0]:
                    return []
                message_ids = data[0].split()[-_MAX_MESSAGES_PER_POLL:]
                bodies: list[str] = []
                for message_id in message_ids:
                    status, msg_data = imap.fetch(message_id, "(RFC822)")
                    if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                        continue
                    raw = msg_data[0][1]
                    if not isinstance(raw, bytes):
                        continue
                    bodies.append(_combined_body_text(email_module.message_from_bytes(raw)))
                return bodies
        except (imaplib.IMAP4.error, OSError) as exc:
            logger.warning("Gmail IMAP fetch failed for %s: %s", tagged_address, exc)
            return []
