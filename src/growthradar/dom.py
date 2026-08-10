"""Typed DOM collection: URL, title, cleaned HTML, visible text, navigation,
and interactive elements for the current page. Resilient to UI differences --
extraction runs in-browser against broad selectors (roles, semantic tags, common
attributes) rather than assuming a specific site's markup.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from growthradar.evidence import Evidence, EvidenceStore

logger = logging.getLogger(__name__)

MAX_HTML_CHARS = 200_000
MAX_TEXT_CHARS = 20_000
MAX_NAV_ITEMS = 100
MAX_INTERACTIVE_ELEMENTS = 200

# Runs in-browser: broad, framework-agnostic selectors so it holds up across UI differences.
_COLLECT_JS = """
() => {
  function cleanHtml() {
    const clone = document.documentElement.cloneNode(true);
    clone.querySelectorAll('script, style, noscript').forEach((el) => el.remove());
    return clone.outerHTML;
  }

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0
      && style.visibility !== 'hidden' && style.display !== 'none';
  }

  function collectNav() {
    const roots = document.querySelectorAll('nav, [role="navigation"], header, footer');
    const seen = new Set();
    const items = [];
    roots.forEach((root) => {
      root.querySelectorAll('a[href]').forEach((a) => {
        const text = (a.innerText || a.textContent || '').trim();
        const href = a.getAttribute('href');
        const key = text + '|' + href;
        if (text && !seen.has(key)) {
          seen.add(key);
          items.push({ text, href });
        }
      });
    });
    return items;
  }

  function collectInteractive() {
    const selector =
      'a[href], button, input, select, textarea, ' +
      '[role="button"], [role="link"], [role="menuitem"], [onclick]';
    const elements = Array.from(document.querySelectorAll(selector)).slice(0, 500);
    return elements.filter(isVisible).map((el) => ({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role'),
      text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 120),
      href: el.getAttribute('href'),
      name: el.getAttribute('name'),
      type: el.getAttribute('type'),
    }));
  }

  return {
    title: document.title,
    html: cleanHtml(),
    visibleText: document.body ? document.body.innerText : '',
    navigation: collectNav(),
    interactiveElements: collectInteractive(),
  };
}
"""


@dataclass(frozen=True)
class NavItem:
    text: str
    href: str | None


@dataclass(frozen=True)
class InteractiveElement:
    tag: str
    role: str | None
    text: str
    href: str | None
    name: str | None
    type: str | None


@dataclass(frozen=True)
class DomSnapshot:
    url: str
    title: str
    html: str
    visible_text: str
    navigation: list[NavItem]
    interactive_elements: list[InteractiveElement]
    truncated: bool


def _cap_str(value: str, limit: int) -> tuple[str, bool]:
    if len(value) > limit:
        return value[:limit], True
    return value, False


def _safe_title(page: Page) -> str:
    try:
        return page.title()
    except PlaywrightError:
        return ""


def _safe_url(page: Page) -> str:
    try:
        return page.url
    except PlaywrightError:
        return ""


def collect_dom_snapshot(page: Page) -> DomSnapshot:
    """Collect a typed DOM snapshot of the current page. Never raises."""
    url = _safe_url(page)
    try:
        raw: dict[str, Any] = page.evaluate(_COLLECT_JS)
    except PlaywrightError as exc:
        logger.warning("DOM collection failed on %s: %s", url, exc)
        return DomSnapshot(
            url=url,
            title=_safe_title(page),
            html="",
            visible_text="",
            navigation=[],
            interactive_elements=[],
            truncated=False,
        )

    html, html_truncated = _cap_str(raw.get("html") or "", MAX_HTML_CHARS)
    visible_text, text_truncated = _cap_str(raw.get("visibleText") or "", MAX_TEXT_CHARS)

    nav_raw = raw.get("navigation") or []
    nav_truncated = len(nav_raw) > MAX_NAV_ITEMS
    navigation = [
        NavItem(text=item.get("text", ""), href=item.get("href"))
        for item in nav_raw[:MAX_NAV_ITEMS]
    ]

    interactive_raw = raw.get("interactiveElements") or []
    interactive_truncated = len(interactive_raw) > MAX_INTERACTIVE_ELEMENTS
    interactive_elements = [
        InteractiveElement(
            tag=item.get("tag", ""),
            role=item.get("role"),
            text=item.get("text", ""),
            href=item.get("href"),
            name=item.get("name"),
            type=item.get("type"),
        )
        for item in interactive_raw[:MAX_INTERACTIVE_ELEMENTS]
    ]

    return DomSnapshot(
        url=url,
        title=raw.get("title") or _safe_title(page),
        html=html,
        visible_text=visible_text,
        navigation=navigation,
        interactive_elements=interactive_elements,
        truncated=html_truncated or text_truncated or nav_truncated or interactive_truncated,
    )


def collect_and_record(
    page: Page,
    store: EvidenceStore,
    run_id: str,
    label: str,
    *,
    confidence: float | None = None,
    snapshot: DomSnapshot | None = None,
) -> Evidence:
    """Collect a DOM snapshot (or reuse one already collected) and record it as Evidence."""
    snapshot = snapshot or collect_dom_snapshot(page)
    return store.add(
        run_id,
        label,
        url=snapshot.url,
        dom={
            "title": snapshot.title,
            "html": snapshot.html,
            "navigation": [asdict(item) for item in snapshot.navigation],
            "interactive_elements": [asdict(item) for item in snapshot.interactive_elements],
            "truncated": snapshot.truncated,
        },
        visible_ui=snapshot.visible_text,
        confidence=confidence,
    )
