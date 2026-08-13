"""Thin, resilient wrapper around Playwright for the exploration agent."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field

from playwright.sync_api import Dialog, Page, Playwright, Request, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.stealth import stealth_sync

from growthradar.config import Config

logger = logging.getLogger(__name__)

_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PlaywrightTimeoutError,
    PlaywrightError,
)

_OVERLAY_BUTTON_TEXTS: tuple[str, ...] = (
    "Accept all",
    "Accept All",
    "Accept",
    "I agree",
    "I Agree",
    "Agree",
    "Got it",
    "Got It",
    "Allow all",
    "Allow All",
    "Close",
    "Dismiss",
)


def retry[T](
    action: Callable[[], T],
    *,
    retries: int = 3,
    delay: float = 0.5,
    exceptions: tuple[type[BaseException], ...] = _RETRYABLE_EXCEPTIONS,
) -> T:
    """Run `action`, retrying on `exceptions`. Re-raises the last exception if all attempts fail.

    A successful `action` can legitimately return None (e.g. Page.goto on a same-document or
    data: URL), so failure is signaled by raising rather than by a sentinel return value.
    """
    last_exc: BaseException = RuntimeError("retry called with retries <= 0")
    for attempt in range(1, retries + 1):
        try:
            return action()
        except exceptions as exc:
            last_exc = exc
            logger.warning("action failed (attempt %d/%d): %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(delay)
    logger.error("action failed after %d attempts: %s", retries, last_exc)
    raise last_exc


def wait_for_stable(page: Page, timeout: float = 10.0) -> None:
    """Wait for the page to finish loading and settle. Never raises."""
    timeout_ms = timeout * 1000
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        logger.warning("domcontentloaded timeout on %s", page.url)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        logger.warning("networkidle timeout on %s", page.url)
    with suppress(PlaywrightError):
        page.wait_for_timeout(250)


def dismiss_overlays(page: Page, timeout: float = 1.2) -> bool:
    """Best-effort dismissal of cookie banners / consent modals. Never raises.

    Uses a plain CSS locator (not `get_by_role`) so it also catches consent
    widgets rendered inside a shadow root -- seen live on influencity.com's
    "Privacy Center" modal (a Consentiam widget hosted in an open shadow DOM):
    `get_by_role("button", name="Accept")` found nothing there even though the
    button was visible and clickable, apparently failing to compute an
    accessible name for it, while a CSS `:has-text()` match (which Playwright
    pierces open shadow roots for automatically, same as any other CSS
    selector) found and clicked it fine.
    """
    dismissed_any = False
    timeout_ms = timeout * 1000
    for text in _OVERLAY_BUTTON_TEXTS:
        try:
            locator = page.locator('button, a, [role="button"]').filter(has_text=text)
            if locator.count() == 0:
                continue
            locator.first.click(timeout=timeout_ms)
            dismissed_any = True
            logger.info("dismissed overlay via button text=%r on %s", text, page.url)
            page.wait_for_timeout(150)
        except (PlaywrightTimeoutError, PlaywrightError):
            continue
    return dismissed_any


@dataclass
class DialogEvent:
    page_url: str
    dialog_type: str
    message: str


@dataclass(frozen=True)
class RequestRecord:
    url: str
    resource_type: str
    method: str


_MAX_TRACKED_REQUESTS = 500


@dataclass
class BrowserSession:
    """Manages a single browser + page lifecycle with resilient defaults."""

    config: Config
    headless: bool = True

    page: Page | None = field(default=None, init=False)
    dialog_log: list[DialogEvent] = field(default_factory=list, init=False)
    extra_pages: list[Page] = field(default_factory=list, init=False)
    # Requests observed since the last goto() call -- cleared on each navigation.
    requests: list[RequestRecord] = field(default_factory=list, init=False)

    _playwright: Playwright | None = field(default=None, init=False, repr=False)

    def start(self) -> Page:
        """Launch the browser and open the main page. Idempotent."""
        if self.page is not None:
            return self.page

        self._playwright = sync_playwright().start()
        if self.config.google_profile_dir:
            # A persistent, already-authenticated profile so "Continue with
            # Google" buttons can go through instead of being skipped (see
            # registration.py's _is_oauth_button) -- the profile must already
            # be signed in (see scripts/google_profile_bootstrap.py); this
            # never attempts to log in itself. `channel="chrome"` uses the
            # real installed Google Chrome rather than Playwright's bundled
            # Chromium, matching the actual browser the profile's Google
            # session was created in.
            context = self._playwright.chromium.launch_persistent_context(
                self.config.google_profile_dir,
                channel="chrome",
                headless=self.headless,
                user_agent=self.config.user_agent,
            )
        else:
            browser = self._playwright.chromium.launch(headless=self.headless)
            context = browser.new_context(user_agent=self.config.user_agent)
        context.set_default_timeout(self.config.request_timeout * 1000)
        context.on("page", self._handle_new_page)

        # A persistent context opens with one blank page already; a fresh
        # context has none yet.
        page = context.pages[0] if context.pages else context.new_page()
        stealth_sync(page)
        page.on("dialog", self._handle_dialog)
        page.on("request", self._handle_request)
        self.page = page
        return page

    def goto(self, url: str) -> bool:
        """Navigate the main page to `url`, waiting for stability. Returns success."""
        page = self._require_page()
        self.requests.clear()
        try:
            retry(lambda: page.goto(url, wait_until="domcontentloaded"))
        except _RETRYABLE_EXCEPTIONS:
            return False
        wait_for_stable(page, timeout=self.config.request_timeout)
        dismiss_overlays(page)
        return True

    def close(self) -> None:
        """Tear down browser resources. Safe to call multiple times."""
        for extra in self.extra_pages:
            with suppress(PlaywrightError):
                extra.close()
        self.extra_pages.clear()

        if self.page is not None:
            # A persistent context (google_profile_dir) has no separate
            # `.browser` -- closing the context alone shuts the whole thing
            # down, and `.browser` is None rather than a Browser to close.
            browser = self.page.context.browser
            with suppress(PlaywrightError):
                self.page.context.close()
            if browser is not None:
                with suppress(PlaywrightError):
                    browser.close()
        if self._playwright is not None:
            with suppress(PlaywrightError):
                self._playwright.stop()

        self.page = None
        self._playwright = None

    def _require_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("BrowserSession.start() must be called before use")
        return self.page

    def _handle_dialog(self, dialog: Dialog) -> None:
        page_url = dialog.page.url if dialog.page else "?"
        self.dialog_log.append(
            DialogEvent(page_url=page_url, dialog_type=dialog.type, message=dialog.message)
        )
        logger.info("dialog(%s) on %s: %s", dialog.type, page_url, dialog.message)
        with suppress(PlaywrightError):
            dialog.dismiss()

    def _handle_new_page(self, page: Page) -> None:
        logger.info("new page/tab opened: %s", page.url)
        self.extra_pages.append(page)

    def _handle_request(self, request: Request) -> None:
        if len(self.requests) >= _MAX_TRACKED_REQUESTS:
            return
        self.requests.append(
            RequestRecord(
                url=request.url, resource_type=request.resource_type, method=request.method
            )
        )

    def __enter__(self) -> BrowserSession:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
