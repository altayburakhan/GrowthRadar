"""Thin, resilient wrapper around Playwright for the exploration agent."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from patchright.sync_api import Dialog, Page, Playwright, Request, sync_playwright
from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import TimeoutError as PlaywrightTimeoutError

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
    # Each retried goto() attempt can itself take a full request_timeout
    # (10s default) to fail before the next one starts -- 3 attempts plus
    # wait_for_stable's own domcontentloaded/networkidle waits made one slow
    # page cost up to ~50s (seen live on squareup.com). 2 still gives a
    # genuinely transient failure one more chance without stacking a third
    # full timeout on top for a page that's reliably slow/unreachable.
    retries: int = 2,
    delay: float = 0.5,
    exceptions: tuple[type[BaseException], ...] = _RETRYABLE_EXCEPTIONS,
) -> T:
    """Run `action`, retrying on `exceptions`. Re-raises the last exception if all attempts fail."""
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


# Finds the first visible button/link/role=button whose trimmed text OR
# aria-label case-insensitively exact-matches one of `texts`, entirely in
# the page's own JS engine -- see dismiss_overlays below for why this
# replaced 22 separate Locator.count() round trips (11 texts x
# has_text-filter/aria-label-selector) per frame, most of which found
# nothing on a page with no overlay at all.
_OVERLAY_SCAN_JS = """
(texts) => {
    const wanted = new Set(texts.map((t) => t.trim().toLowerCase()));
    const els = document.querySelectorAll('button, a, [role="button"]');
    for (let i = 0; i < els.length; i++) {
        const el = els[i];
        const rawText = (el.innerText || el.textContent || '').trim();
        const rawAria = (el.getAttribute('aria-label') || '').trim();
        const matched = wanted.has(rawText.toLowerCase())
            ? rawText
            : wanted.has(rawAria.toLowerCase())
                ? rawAria
                : null;
        if (matched === null) continue;
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (rect.width > 0 && rect.height > 0
            && style.visibility !== 'hidden' && style.display !== 'none') {
            return {index: i, text: matched};
        }
    }
    return null;
}
"""

# A page can show more than one overlay in sequence (a cookie banner, then a
# separate newsletter popup) -- dismiss_overlays keeps re-scanning a frame
# after each successful click, bounded so a page that keeps regenerating a
# "closeable" element (or two overlays that toggle each other back on) can't
# loop forever.
_MAX_OVERLAY_DISMISSALS_PER_FRAME = 5


def dismiss_overlays(page: Page, timeout: float = 1.2) -> bool:
    """Best-effort dismissal of cookie banners / consent modals. Never raises.

    Searches every frame, not just the main page -- some overlays (seen live
    on mirro.io: a full-viewport HubSpot marketing popup that appears ~2.5s
    after page load and blocked every click on the page beneath it,
    including a plain "Login" link) render their actual content, including
    the close control, inside a same-origin-restricted iframe served from a
    third-party domain -- a plain page.locator() never reaches those.

    Also matches by aria-label, not just visible text: that same close
    control has no text at all, just an SVG icon behind
    `role="button" aria-label="Close"`.

    A single batched `evaluate()` per scan (see _OVERLAY_SCAN_JS) instead of
    a Locator.count() per text/pattern combination -- the latter cost ~7s a
    call on a real, overlay-free page (seen live on web.hr/pricing) purely
    from 22 round trips that all came back empty, and this function runs on
    every navigation (browser.py's own goto()) plus every registration loop
    iteration, so that cost was paid over and over for the entire run.
    """
    dismissed_any = False
    timeout_ms = timeout * 1000
    texts = list(_OVERLAY_BUTTON_TEXTS)
    for frame in page.frames:
        for _ in range(_MAX_OVERLAY_DISMISSALS_PER_FRAME):
            try:
                match = frame.evaluate(_OVERLAY_SCAN_JS, texts)
            except PlaywrightError:
                break
            if match is None:
                break
            try:
                frame.locator('button, a, [role="button"]').nth(match["index"]).click(
                    timeout=timeout_ms
                )
            except (PlaywrightTimeoutError, PlaywrightError):
                break
            dismissed_any = True
            logger.info("dismissed overlay via button text=%r on %s", match["text"], frame.url)
            page.wait_for_timeout(150)
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
    requests: list[RequestRecord] = field(default_factory=list, init=False)

    _playwright: Playwright | None = field(default=None, init=False, repr=False)

    def start(self) -> Page:
        """Launch the browser and open the main page. Idempotent."""
        if self.page is not None:
            return self.page

        playwright = sync_playwright().start()
        self._playwright = playwright
        try:
            self._start_after_driver(playwright)
        except Exception:
            # A failure anywhere after the driver itself starts (e.g.
            # launch_persistent_context's SingletonLock error) must still
            # tear the driver down here: `with BrowserSession(...)` never
            # calls __exit__/close() when __enter__ (this method) raises, so
            # without this the driver's event loop + dispatcher greenlet
            # leaks forever on whatever OS thread called start(). The
            # dashboard's ThreadPoolExecutor reuses worker threads across
            # scans, so one failed launch here poisoned every later scan
            # that happened to land on the same thread with "Please use the
            # Async API instead" -- an unrelated asyncio error surfacing on
            # a completely different run.
            self.close()
            raise
        assert self.page is not None
        return self.page

    def _start_after_driver(self, playwright: Playwright) -> None:
        user_agent = getattr(self.config, "user_agent", None) or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )

        viewport = {
            "width": 1280 + random.randint(0, 120),
            "height": 720 + random.randint(0, 80),
        }

        context_args: dict[str, Any] = {
            "user_agent": user_agent,
            "viewport": viewport,
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "color_scheme": "light",
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False,
            "java_script_enabled": True,
        }

        if self.config.google_profile_dir:
            context = playwright.chromium.launch_persistent_context(
                self.config.google_profile_dir,
                channel="chrome",
                headless=self.headless,
                **context_args,
            )
        else:
            browser = playwright.chromium.launch(
                headless=self.headless,
                channel="chrome",  # gerçek Chrome kullan (önerilir)
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(**context_args)

        context.set_default_timeout(self.config.request_timeout * 1000)

        # Patchright zaten birçok şeyi patch'lediği için ekstra stealth script'e gerek yok.
        # Sadece basit bir webdriver override bırakıyoruz (zararsız).
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        context.on("page", self._handle_new_page)

        page = context.pages[0] if context.pages else context.new_page()

        page.on("dialog", self._handle_dialog)
        page.on("request", self._handle_request)

        self.page = page

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
