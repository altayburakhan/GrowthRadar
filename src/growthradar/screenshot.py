"""Timestamped screenshot capture, tied to the evidence store.

Required capture points (per Linear.md "Screenshot Policy"): landing page,
registration, login, dashboard, onboarding experience, product updates, help
center/guide pages, every popup, empty states, errors -- not every page
visited during exploration (see exploration.py's `_visit`, which skips the
capture for pages that don't classify into one of these kinds).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import Page

from growthradar.evidence import Evidence, EvidenceStore

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


class ScreenshotKind(StrEnum):
    LANDING = "landing"
    REGISTRATION = "registration"
    LOGIN = "login"
    DASHBOARD = "dashboard"
    PAGE = "page"
    POPUP = "popup"
    ONBOARDING = "onboarding"
    EMPTY_STATE = "empty_state"
    PRODUCT_UPDATES = "product_updates"
    HELP_CENTER = "help_center"
    ERROR = "error"


@dataclass(frozen=True)
class ScreenshotResult:
    kind: ScreenshotKind
    url: str
    timestamp: str
    path: str | None
    success: bool
    error: str | None = None


def _slugify(value: str, max_len: int = 40) -> str:
    slug = _SLUG_RE.sub("-", value).strip("-").lower()
    return slug[:max_len] or "page"


def capture_screenshot(
    page: Page,
    run_id: str,
    kind: ScreenshotKind,
    *,
    screenshot_dir: str | Path = "screenshots",
    label: str | None = None,
) -> ScreenshotResult:
    """Capture a full-page screenshot. Never raises -- failures are logged and reported."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    url = page.url
    slug = _slugify(label or url)
    path = Path(screenshot_dir) / run_id / f"{kind.value}__{timestamp}__{slug}.png"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=True)
    except (PlaywrightError, OSError) as exc:
        logger.warning("screenshot capture failed (kind=%s, url=%s): %s", kind.value, url, exc)
        return ScreenshotResult(
            kind=kind, url=url, timestamp=timestamp, path=None, success=False, error=str(exc)
        )

    logger.info("screenshot captured: %s", path)
    return ScreenshotResult(kind=kind, url=url, timestamp=timestamp, path=str(path), success=True)


def capture_and_record(
    page: Page,
    store: EvidenceStore,
    run_id: str,
    kind: ScreenshotKind,
    label: str,
    *,
    screenshot_dir: str | Path = "screenshots",
    confidence: float | None = None,
    extra: dict[str, Any] | None = None,
) -> Evidence:
    """Capture a screenshot and record it as Evidence -- even when the capture failed."""
    result = capture_screenshot(page, run_id, kind, screenshot_dir=screenshot_dir, label=label)
    visible_ui: dict[str, Any] = {
        "screenshot_kind": kind.value,
        "success": result.success,
        "error": result.error,
        **(extra or {}),
    }
    return store.add(
        run_id,
        label,
        url=result.url,
        screenshot=result.path,
        visible_ui=visible_ui,
        confidence=confidence,
    )
