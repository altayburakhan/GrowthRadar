"""Breadth-first exploration engine: the orchestrator that ties together the
browser engine (GRO-6), structured logger (GRO-7), evidence store (GRO-8),
screenshot capture (GRO-9), and DOM collection (GRO-10) into an autonomous crawl
that behaves like a real first-time user.

Visits pages level by level (breadth-first), ranking same-level candidates by
how closely they match priority sections (Sign Up, Login, Dashboard, Settings,
Help Center, Documentation, etc.) before going deeper. Bounded by max_pages and
max_depth, and a visited-set guarantees no page is ever processed twice.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse

from growthradar.browser import BrowserSession
from growthradar.config import Config
from growthradar.dom import DomSnapshot, NavItem, collect_and_record, collect_dom_snapshot
from growthradar.event_log import RunLogger
from growthradar.evidence import EvidenceStore
from growthradar.hidden_nav import discover_hidden_navigation
from growthradar.js_network import collect_and_record as collect_js_network_and_record
from growthradar.js_network import detect_tools
from growthradar.onboarding import record_onboarding_detection
from growthradar.page_classifier import classify_and_record
from growthradar.screenshot import ScreenshotKind, capture_and_record

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 3

# Highest-priority tier: registration/trial CTAs. These must win any tie-break
# against generic content pages (blog, docs, help, login, ...) -- otherwise a
# limited page budget can be spent entirely on content pages while the actual
# signup link (often just one of a dozen equally-"relevant" links on a
# landing page) never gets visited, and registration.py never gets a page to
# fill (see the 100hires.com case: /auth/login got visited but the real
# /auth/create-account trial link, tied at the old flat priority, did not).
_SIGNUP_KEYWORDS: tuple[str, ...] = (
    "sign up",
    "signup",
    "free trial",
    "trial",
    "start trial",
    "create account",
    "try for free",
    "try free",
    "get started",
    "register",
)

# Priority sections from Linear.md's "Exploration Behavior" and "Exploration Strategy".
PRIORITY_KEYWORDS: tuple[str, ...] = (
    "log in",
    "login",
    "sign in",
    "dashboard",
    "settings",
    "navigation",
    "projects",
    "help center",
    "help",
    "documentation",
    "docs",
    "resource center",
    "product updates",
    "release notes",
    "changelog",
    "blog",
    "analytics",
    "reports",
    "billing",
    "notifications",
    "team",
    "admin",
    "footer",
    # A blog/changelog listing page is only a summary -- the actual evidence
    # (feature mentions, tool names, onboarding practices) is in the full
    # article these link to, so following them is worth the same priority as
    # discovering the listing page itself.
    "read more",
    "continue reading",
    "read the full",
    "read article",
    "view post",
    "keep reading",
    "learn more",
)

_KIND_KEYWORDS: tuple[tuple[ScreenshotKind, tuple[str, ...]], ...] = (
    # Same keyword set exploration priority treats as a signup/trial CTA
    # (_SIGNUP_KEYWORDS, defined below) -- a link worth prioritizing during
    # the crawl is exactly the kind of link registration.py should later
    # recognize as "the registration page".
    (ScreenshotKind.REGISTRATION, _SIGNUP_KEYWORDS),
    (ScreenshotKind.LOGIN, ("log in", "login", "sign in")),
    # A step reached a click or two into post-registration exploration (see
    # ExplorationEngine.run's initial_kind, which only covers depth==0) --
    # e.g. bettermode.com's "Verify your email" gate, or a "let's get you set
    # up" wizard step beyond it.
    (
        ScreenshotKind.ONBOARDING,
        ("onboarding", "welcome to", "getting started", "verify your email", "set up your account"),
    ),
    (ScreenshotKind.DASHBOARD, ("dashboard",)),
    (ScreenshotKind.HELP_CENTER, ("help center", "help", "support")),
    (
        ScreenshotKind.PRODUCT_UPDATES,
        ("product updates", "release notes", "changelog", "what's new"),
    ),
)

_SKIP_HREF_PREFIXES = ("javascript:", "mailto:", "tel:")

# A "Continue with Google/LinkedIn" link is often served from the target
# site's own (sub)domain (e.g. app.100hires.com/auth/oauth/google/connect),
# so `_in_scope` alone doesn't filter it out -- but following it takes the
# agent off the product entirely and into a third-party sign-in page, whose
# DOM then gets misread as evidence about the target site (observed:
# 100hires.com scored "hot" partly from Google's/LinkedIn's own UI). These
# are never part of the product being evaluated, so they're excluded from
# candidates outright rather than merely deprioritized.
_OAUTH_LINK_KEYWORDS: tuple[str, ...] = (
    "oauth",
    "google",
    "microsoft",
    "apple",
    "github",
    "gitlab",
    "linkedin",
    "facebook",
    "twitter",
    "sso",
    "saml",
    "okta",
)


def _is_oauth_link(text: str, url: str) -> bool:
    haystack = f"{text} {url}".lower()
    return any(keyword in haystack for keyword in _OAUTH_LINK_KEYWORDS)


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    text: str
    priority: float
    depth: int


@dataclass(frozen=True)
class VisitedPage:
    url: str
    depth: int
    success: bool
    title: str | None = None


@dataclass(frozen=True)
class ExplorationResult:
    run_id: str
    start_url: str
    visited: list[VisitedPage]
    stopped_reason: str


class ExplorationEngine:
    """Breadth-first crawl of a single site, recording evidence as it goes."""

    def __init__(
        self,
        session: BrowserSession,
        store: EvidenceStore,
        run_logger: RunLogger,
        *,
        max_pages: int = 8,
        max_depth: int = DEFAULT_MAX_DEPTH,
        crawl_delay: float = 0.0,
    ) -> None:
        self.session = session
        self.store = store
        self.run_logger = run_logger
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.crawl_delay = crawl_delay
        self._visited: set[str] = set()
        self._enqueued: set[str] = set()
        self._initial_kind: ScreenshotKind | None = None

    @classmethod
    def from_config(
        cls,
        session: BrowserSession,
        store: EvidenceStore,
        run_logger: RunLogger,
        config: Config,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> ExplorationEngine:
        return cls(
            session,
            store,
            run_logger,
            max_pages=config.max_pages,
            max_depth=max_depth,
            crawl_delay=config.crawl_delay,
        )

    def run(
        self, start_url: str, *, initial_kind: ScreenshotKind | None = None
    ) -> ExplorationResult:
        """`initial_kind` overrides `_screenshot_kind_for`'s depth==0 -> LANDING
        default for `start_url` only. Needed for the orchestrator's
        post-registration crawl: its `start_url` is whatever page registration
        just landed on (e.g. bettermode.com's "Verify your email" step) --
        genuinely the onboarding experience, not a second "landing page" --
        but `_screenshot_kind_for` has no way to know this crawl didn't begin
        at the site's actual entry point."""
        visited_pages: list[VisitedPage] = []
        frontier: list[DiscoveredLink] = [
            DiscoveredLink(url=start_url, text="start", priority=1.0, depth=0)
        ]
        self._enqueued.add(_normalize_url(start_url))
        self._initial_kind = initial_kind

        while frontier and len(visited_pages) < self.max_pages:
            frontier.sort(key=lambda link: (link.depth, -link.priority))
            link = frontier.pop(0)

            normalized = _normalize_url(link.url)
            if normalized in self._visited:
                continue
            self._visited.add(normalized)

            visited_page, snapshot, hidden_nav_items = self._visit(link)
            visited_pages.append(visited_page)

            if visited_page.success and snapshot is not None and link.depth < self.max_depth:
                candidates = self._extract_candidates(
                    snapshot, link.depth + 1, extra_nav_items=hidden_nav_items
                )
                new_candidates = [
                    c for c in candidates if _normalize_url(c.url) not in self._enqueued
                ]
                for candidate in new_candidates:
                    self._enqueued.add(_normalize_url(candidate.url))
                    frontier.append(candidate)
                if new_candidates:
                    self.run_logger.discovery(
                        f"found {len(new_candidates)} new link(s) on {link.url}",
                        count=len(new_candidates),
                    )

            if self.crawl_delay:
                time.sleep(self.crawl_delay)

        stopped_reason = (
            "max_pages_reached" if len(visited_pages) >= self.max_pages else "frontier_exhausted"
        )
        self.run_logger.decision(
            f"exploration finished: {len(visited_pages)} page(s) visited ({stopped_reason})",
            evidence_refs=[vp.url for vp in visited_pages if vp.success],
        )
        return ExplorationResult(
            run_id=self.run_logger.run_id,
            start_url=start_url,
            visited=visited_pages,
            stopped_reason=stopped_reason,
        )

    def _visit(self, link: DiscoveredLink) -> tuple[VisitedPage, DomSnapshot | None, list[NavItem]]:
        self.run_logger.action("goto", url=link.url, depth=link.depth)
        ok = self.session.goto(link.url)
        page = self.session.page

        if not ok or page is None:
            self.run_logger.error(f"failed to load {link.url}")
            self.store.add(self.run_logger.run_id, f"failed to load {link.url}", url=link.url)
            return VisitedPage(url=link.url, depth=link.depth, success=False), None, []

        snapshot = collect_dom_snapshot(page)
        title = snapshot.title or None
        self.run_logger.page_visited(link.url, title=title)

        if link.depth == 0 and self._initial_kind is not None:
            kind = self._initial_kind
        else:
            kind = _screenshot_kind_for(link.text, link.url, link.depth)
        screenshot_evidence = capture_and_record(
            page, self.store, self.run_logger.run_id, kind, f"screenshot: {title or link.url}"
        )
        collect_and_record(
            page,
            self.store,
            self.run_logger.run_id,
            f"dom: {title or link.url}",
            snapshot=snapshot,
        )

        tool_detections = detect_tools(page, self.session.requests)
        collect_js_network_and_record(
            page,
            self.store,
            self.run_logger.run_id,
            f"js/network: {title or link.url}",
            requests=list(self.session.requests),
            detections=tool_detections,
        )
        onboarding_evidence = record_onboarding_detection(
            self.store,
            self.run_logger.run_id,
            f"onboarding heuristics: {title or link.url}",
            dom=snapshot,
            tool_detections=tool_detections,
            screenshot=screenshot_evidence.screenshot,
        )
        if onboarding_evidence.confidence is not None and onboarding_evidence.confidence >= 0.75:
            self.run_logger.decision(
                f"onboarding signals detected on {link.url}",
                confidence=onboarding_evidence.confidence,
                evidence_refs=[str(onboarding_evidence.id)] if onboarding_evidence.id else [],
            )

        classify_and_record(
            self.store,
            self.run_logger.run_id,
            f"page category: {title or link.url}",
            url=link.url,
            dom=snapshot,
        )

        # Only worth the interaction cost if we're actually going to expand
        # this page's children (see the depth check around _extract_candidates).
        hidden_nav_items: list[NavItem] = []
        if link.depth < self.max_depth:
            hidden_nav_items = discover_hidden_navigation(page)
            if hidden_nav_items:
                self.run_logger.discovery(
                    f"found {len(hidden_nav_items)} hidden navigation link(s) on {link.url}",
                    count=len(hidden_nav_items),
                )

        visited_page = VisitedPage(url=link.url, depth=link.depth, success=True, title=title)
        return visited_page, snapshot, hidden_nav_items

    def _extract_candidates(
        self,
        snapshot: DomSnapshot,
        depth: int,
        *,
        extra_nav_items: list[NavItem] | None = None,
    ) -> list[DiscoveredLink]:
        base_url = snapshot.url
        pairs = [(item.text, item.href) for item in snapshot.navigation if item.href]
        pairs += [(item.text, item.href) for item in snapshot.interactive_elements if item.href]
        pairs += [(item.text, item.href) for item in (extra_nav_items or []) if item.href]

        candidates: dict[str, DiscoveredLink] = {}
        for text, href in pairs:
            assert href is not None
            resolved = _resolve_url(base_url, href)
            if resolved is None or not _in_scope(base_url, resolved):
                continue
            if _is_oauth_link(text, resolved):
                continue
            key = _normalize_url(resolved)
            priority = _priority_score(text, resolved)
            existing = candidates.get(key)
            # The same URL is often linked twice with different text (e.g. a
            # terse "Try it free" in the header navbar and a keyword-rich
            # "Start 14-Day Free Trial" hero CTA pointing at the same signup
            # URL). Keep whichever occurrence scores higher instead of
            # whichever was merely encountered first; on a tie, prefer the
            # more descriptive (longer) text, since that's also what
            # _screenshot_kind_for later uses to classify the page as
            # "registration" -- a terse duplicate must not shadow that either.
            if existing is not None and (
                existing.priority > priority
                or (existing.priority == priority and len(existing.text) >= len(text))
            ):
                continue
            candidates[key] = DiscoveredLink(
                url=resolved, text=text, priority=priority, depth=depth
            )
        return list(candidates.values())


def _priority_score(text: str, url: str) -> float:
    # Normalize URL-slug punctuation ("create-account", "free_trial") to
    # spaces so keyword matching isn't defeated by a link with no descriptive
    # text (icon-only buttons) whose only signal is its URL path.
    haystack = f"{text} {url}".lower().replace("-", " ").replace("_", " ")
    if any(keyword in haystack for keyword in _SIGNUP_KEYWORDS):
        return 2.0
    return 1.0 if any(keyword in haystack for keyword in PRIORITY_KEYWORDS) else 0.0


def _screenshot_kind_for(text: str, url: str, depth: int) -> ScreenshotKind:
    if depth == 0:
        return ScreenshotKind.LANDING
    # Same normalization as _priority_score -- a URL path like "create-account"
    # must match "create account" here too, or a link discovered by its URL
    # alone (no descriptive text) would be classified as a generic PAGE and
    # never picked up as the registration page for registration.py to fill.
    haystack = f"{text} {url}".lower().replace("-", " ").replace("_", " ")
    for kind, keywords in _KIND_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return kind
    return ScreenshotKind.PAGE


def _resolve_url(base: str, href: str) -> str | None:
    href = href.strip()
    if not href or href.startswith("#") or href.lower().startswith(_SKIP_HREF_PREFIXES):
        return None
    try:
        resolved = urljoin(base, href)
    except ValueError:
        return None
    return resolved or None


def _registrable_domain(netloc: str) -> str:
    host = netloc.split(":", 1)[0]
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _in_scope(base_url: str, candidate_url: str) -> bool:
    """Same registrable domain for http(s) (covers help./docs./blog. subdomains);
    exact scheme+netloc match otherwise (e.g. data: URLs used in tests)."""
    base = urlparse(base_url)
    candidate = urlparse(candidate_url)
    if base.scheme != candidate.scheme:
        return False
    if base.scheme not in ("http", "https"):
        return base.netloc == candidate.netloc
    return _registrable_domain(base.netloc) == _registrable_domain(candidate.netloc)


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))
