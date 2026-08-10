"""Hidden navigation discovery: best-effort clicking of common menu triggers
(hamburger icons, aria-expanded/aria-haspopup buttons, profile/avatar
dropdowns, "Menu"/"More" buttons) to reveal navigation links that don't exist
-- or aren't visible -- in the DOM until a user interacts with a trigger.

Mirrors browser.py's `dismiss_overlays`: try several common patterns, never
raise, and simply find nothing when a site has no such triggers.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from growthradar.dom import NavItem

logger = logging.getLogger(__name__)

_TRIGGER_SELECTORS: tuple[str, ...] = (
    '[aria-haspopup="true"]',
    '[aria-haspopup="menu"]',
    "button[aria-expanded]",
    '[class*="hamburger" i]',
    '[class*="menu-toggle" i]',
    '[class*="nav-toggle" i]',
    '[class*="mobile-menu" i]',
    '[class*="avatar" i]',
    '[class*="profile-menu" i]',
)

_TRIGGER_LABELS: tuple[str, ...] = (
    "Menu",
    "More",
    "Open menu",
    "Toggle navigation",
    "Toggle menu",
    "Account",
    "Profile",
    "My account",
    "User menu",
)

_MAX_TRIGGERS = 8
_CLICK_TIMEOUT_MS = 1500

_VISIBLE_LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href]'))
  .filter((a) => {
    const rect = a.getBoundingClientRect();
    const style = window.getComputedStyle(a);
    return rect.width > 0 && rect.height > 0
      && style.visibility !== 'hidden' && style.display !== 'none';
  })
  .map((a) => ({ text: (a.innerText || a.textContent || '').trim(), href: a.getAttribute('href') }))
"""


def _visible_links(page: Page) -> list[NavItem]:
    try:
        raw: list[dict[str, str]] = page.evaluate(_VISIBLE_LINKS_JS)
    except PlaywrightError as exc:
        logger.warning("hidden-nav link scan failed on %s: %s", getattr(page, "url", "?"), exc)
        return []
    return [NavItem(text=item.get("text", ""), href=item.get("href")) for item in raw]


def _candidate_triggers(page: Page) -> list[Locator]:
    triggers = [page.locator(selector) for selector in _TRIGGER_SELECTORS]
    triggers += [page.get_by_role("button", name=label, exact=False) for label in _TRIGGER_LABELS]
    return triggers


def discover_hidden_navigation(page: Page, *, max_triggers: int = _MAX_TRIGGERS) -> list[NavItem]:
    """Click common menu/profile triggers to reveal hidden nav links.

    Returns only links that weren't already visible before any interaction --
    i.e. genuinely new discoveries. Never raises: an unmatched or unclickable
    trigger is simply skipped.
    """
    before = {(item.text, item.href) for item in _visible_links(page)}
    discovered: dict[tuple[str, str | None], NavItem] = {}
    attempts = 0

    for locator in _candidate_triggers(page):
        if attempts >= max_triggers:
            break
        try:
            if locator.count() == 0 or not locator.first.is_visible():
                continue
            locator.first.click(timeout=_CLICK_TIMEOUT_MS)
        except PlaywrightError:
            continue
        attempts += 1

        for item in _visible_links(page):
            key = (item.text, item.href)
            if item.href and key not in before:
                discovered[key] = item

    return list(discovered.values())
