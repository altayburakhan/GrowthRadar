from __future__ import annotations

import logging

from growthradar.core.models import PageContent

logger = logging.getLogger(__name__)


class PlaywrightFetcher:
    """Fetches pages with a real headless Chromium browser, so client-side-rendered
    (SPA/React/Vue/Next.js) sites yield real content instead of an empty pre-render
    shell -- a plain HTTP GET would only see the latter."""

    def __init__(self, user_agent: str, timeout_seconds: float, max_retries: int = 1):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "The 'playwright' package is required to fetch pages. Install it with "
                "`pip install playwright && playwright install chromium`."
            ) from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context(user_agent=user_agent)
        self._timeout_ms = timeout_seconds * 1000
        self._max_retries = max_retries

    def fetch(self, url: str) -> PageContent:
        last_error: str | None = None
        for attempt in range(self._max_retries + 1):
            page = self._context.new_page()
            try:
                # domcontentloaded, not networkidle: real sites run continuous
                # analytics/tracking requests that never go quiet, which would
                # otherwise make every page wait for the full timeout. A short
                # fixed pause after DOM-ready is enough for client-side JS to
                # render its content into the page.
                response = page.goto(url, timeout=self._timeout_ms, wait_until="domcontentloaded")
                page.wait_for_timeout(500)
                status = response.status if response else None
                html = page.content() if status is not None and status < 400 else ""
                return PageContent(url=page.url, status_code=status, raw_html=html)
            except Exception as exc:  # noqa: BLE001 -- Playwright's own exception hierarchy; a fetch failure must never propagate
                last_error = str(exc)
                logger.warning("Playwright fetch attempt %s failed for %s: %s", attempt + 1, url, exc)
            finally:
                page.close()
        return PageContent(url=url, fetch_error=last_error or "unknown fetch error")

    def close(self) -> None:
        self._context.close()
        self._browser.close()
        self._playwright.stop()

    def __enter__(self) -> "PlaywrightFetcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
