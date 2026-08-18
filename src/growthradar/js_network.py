"""JavaScript and network inspection: detects known onboarding/product-adoption
tools (UserGuiding, Pendo, Appcues, WalkMe, Chameleon, Product Fruits, Userpilot,
Whatfix, Userflow, Shepherd, Intro.js, Driver.js), general analytics/tracking
providers (Google
Analytics, Segment, Mixpanel, Amplitude, Hotjar, FullStory -- GRO-28), *and*
AI chat/assistant widgets (Intercom, Drift, Zendesk Chat, Ada, Crisp, Tidio, LiveChat,
HubSpot Chat, Freshchat, Tawk.to) by combining multiple independent signals --
script URLs, window globals, and observed network requests -- rather than a
single name match. Reads passively from BrowserSession.requests (GRO-6),
which records traffic since the last navigation.

Detection here means "this site embeds a chat/assistant widget", not
"the widget is confirmed AI-powered" -- from an unauthenticated crawl we
can't tell whether a given Intercom/Zendesk instance is staffed by humans,
an AI agent, or both, since the same embed script serves either. Most major
platforms ship AI-assisted replies by default as of this writing, so
presence is still a meaningful, evidence-grounded signal.

Analytics- and AI-assistant-category tools are informational only: they
enrich the Final Report's "Technologies detected" field but are deliberately
excluded from `scoring.py`'s onboarding_opportunity/ICP-fit calculations
(neither says anything about onboarding-tool budget or intent) -- see
`ToolCategory` and how scoring.py filters on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import Page

from growthradar.browser import RequestRecord
from growthradar.evidence import Evidence, EvidenceStore

logger = logging.getLogger(__name__)


class ToolCategory(StrEnum):
    ONBOARDING = "onboarding"
    ANALYTICS = "analytics"
    AI_ASSISTANT = "ai_assistant"


_SCRIPT_URLS_JS = "Array.from(document.querySelectorAll('script[src]')).map((s) => s.src)"

_GLOBALS_CHECK_JS = """
(globalNames) => globalNames.filter((name) => {
  try {
    return typeof window[name] !== 'undefined';
  } catch (e) {
    return false;
  }
})
"""


@dataclass(frozen=True)
class ToolSignature:
    name: str
    script_patterns: tuple[str, ...]
    global_vars: tuple[str, ...]
    # Falls back to script_patterns when empty -- most tools serve their widget
    # and their tracking/API traffic from the same domain.
    network_patterns: tuple[str, ...] = ()
    category: ToolCategory = ToolCategory.ONBOARDING


_ONBOARDING_TOOLS: tuple[ToolSignature, ...] = (
    ToolSignature("UserGuiding", ("userguiding.com",), ("userGuiding", "UserGuiding")),
    ToolSignature("Pendo", ("pendo.io",), ("pendo",)),
    ToolSignature("Appcues", ("appcues.com",), ("Appcues",)),
    ToolSignature("WalkMe", ("walkme.com",), ("WalkMeAPI",)),
    ToolSignature("Chameleon", ("trychameleon.com",), ("chmln",)),
    ToolSignature("Product Fruits", ("productfruits.com",), ("productFruits",)),
    # Not yet verified against a live site actually running these three
    # (unlike every other signature here) -- script domains/globals are
    # taken from each vendor's own published embed-script documentation, so
    # treat as best-effort until confirmed the way UserGuiding/Pendo/etc were.
    ToolSignature(
        "Userpilot", ("js.userpilot.io", "userpilot.io"), ("Userpilot", "userpilotSettings")
    ),
    ToolSignature("Whatfix", ("whatfix.com",), ("Whatfix", "_wfx_environment")),
    ToolSignature("Userflow", ("js.userflow.com",), ("userflow", "Userflow")),
    ToolSignature("Shepherd", ("shepherdjs.dev", "shepherd.js"), ("Shepherd",)),
    ToolSignature("Intro.js", ("introjs.com", "intro.js"), ("introJs",)),
    ToolSignature(
        "Driver.js", ("driverjs.com", "driver.js.iife.js", "driver.js.umd.js"), ("Driver",)
    ),
)

# Informational only -- see module docstring: excluded from onboarding scoring.
_ANALYTICS_TOOLS: tuple[ToolSignature, ...] = (
    ToolSignature(
        "Google Analytics",
        ("google-analytics.com", "googletagmanager.com"),
        ("gtag", "ga", "dataLayer"),
        category=ToolCategory.ANALYTICS,
    ),
    ToolSignature(
        "Segment",
        ("cdn.segment.com",),
        ("analytics",),
        category=ToolCategory.ANALYTICS,
    ),
    ToolSignature(
        "Mixpanel",
        ("cdn.mxpnl.com",),
        ("mixpanel",),
        category=ToolCategory.ANALYTICS,
    ),
    ToolSignature(
        "Amplitude",
        ("cdn.amplitude.com",),
        ("amplitude",),
        category=ToolCategory.ANALYTICS,
    ),
    ToolSignature(
        "Hotjar",
        ("static.hotjar.com",),
        ("hj",),
        category=ToolCategory.ANALYTICS,
    ),
    ToolSignature(
        "FullStory",
        ("edge.fullstory.com", "fullstory.com/s/fs.js"),
        ("FS",),
        category=ToolCategory.ANALYTICS,
    ),
)

# Informational only -- see module docstring: presence, not confirmed-AI,
# and excluded from onboarding scoring same as _ANALYTICS_TOOLS.
_AI_ASSISTANT_TOOLS: tuple[ToolSignature, ...] = (
    # Intercom's core product is customer messaging/support chat, not a
    # UserGuiding-style product tour/checklist tool -- it was previously
    # miscategorized under _ONBOARDING_TOOLS, which made scoring.py treat
    # every Intercom-using site (an extremely common live-chat widget,
    # unrelated to onboarding-tool budget) as having a "competitor onboarding
    # tool", and report.py/the dashboard would list it as a "Rakip araç" even
    # on runs where the same site was flagged as an existing UserGuiding
    # customer (seen live on 17hats.com).
    ToolSignature(
        "Intercom",
        ("intercom.io", "intercomcdn.com"),
        ("Intercom", "intercomSettings"),
        category=ToolCategory.AI_ASSISTANT,
    ),
    ToolSignature(
        "Drift",
        ("js.driftt.com", "js.drift.com"),
        ("drift", "driftt"),
        category=ToolCategory.AI_ASSISTANT,
    ),
    ToolSignature(
        "Zendesk Chat",
        ("zdassets.com", "zopim.com"),
        ("zE", "zEmbed"),
        category=ToolCategory.AI_ASSISTANT,
    ),
    ToolSignature(
        "Ada",
        ("static.ada.support",),
        ("adaEmbed", "adaSettings"),
        category=ToolCategory.AI_ASSISTANT,
    ),
    ToolSignature(
        "Crisp",
        ("client.crisp.chat",),
        ("$crisp", "CRISP_WEBSITE_ID"),
        category=ToolCategory.AI_ASSISTANT,
    ),
    ToolSignature(
        "Tidio",
        ("code.tidio.co",),
        ("tidioChatApi",),
        category=ToolCategory.AI_ASSISTANT,
    ),
    ToolSignature(
        "LiveChat",
        ("cdn.livechatinc.com",),
        ("LC_API", "__lc"),
        category=ToolCategory.AI_ASSISTANT,
    ),
    ToolSignature(
        "HubSpot Chat",
        ("js.hs-scripts.com", "js.usemessages.com"),
        ("HubSpotConversations",),
        category=ToolCategory.AI_ASSISTANT,
    ),
    ToolSignature(
        "Freshchat",
        ("wchat.freshchat.com",),
        ("fcWidget",),
        category=ToolCategory.AI_ASSISTANT,
    ),
    ToolSignature(
        "Tawk.to",
        ("embed.tawk.to",),
        ("Tawk_API",),
        category=ToolCategory.AI_ASSISTANT,
    ),
)

KNOWN_TOOLS: tuple[ToolSignature, ...] = _ONBOARDING_TOOLS + _ANALYTICS_TOOLS + _AI_ASSISTANT_TOOLS

_CONFIDENCE_BY_SIGNAL_COUNT: dict[int, float] = {0: 0.0, 1: 0.4, 2: 0.75, 3: 0.95}


@dataclass(frozen=True)
class ToolDetection:
    name: str
    category: ToolCategory
    signal_count: int
    matched_scripts: tuple[str, ...]
    matched_globals: tuple[str, ...]
    matched_network: tuple[str, ...]

    @property
    def confidence(self) -> float:
        return _CONFIDENCE_BY_SIGNAL_COUNT.get(self.signal_count, 0.95)


def _script_urls(page: Page) -> list[str]:
    try:
        urls: list[str] = page.evaluate(_SCRIPT_URLS_JS)
        return urls
    except PlaywrightError as exc:
        logger.warning("script URL inspection failed on %s: %s", page.url, exc)
        return []


def _check_globals(page: Page, global_names: list[str]) -> set[str]:
    try:
        # patchright's evaluate() defaults to an isolated execution context
        # (a separate `window` from the page's own scripts, done to avoid
        # detection via Runtime.enable) -- a tool's own `window.<Name> = ...`
        # assignment is invisible there, so every global check came back
        # empty. isolated_context=False runs this in the page's real main
        # world instead, where those assignments actually land.
        found: list[str] = page.evaluate(_GLOBALS_CHECK_JS, global_names, isolated_context=False)
        return set(found)
    except PlaywrightError as exc:
        logger.warning("global variable inspection failed on %s: %s", page.url, exc)
        return set()


def _safe_url(page: Page) -> str:
    try:
        return page.url
    except PlaywrightError:
        return ""


def detect_tools(page: Page, requests: list[RequestRecord]) -> list[ToolDetection]:
    """Detect known tools from script URLs, window globals, and network requests.

    Never raises. A tool only appears in the result once at least one signal
    matches; its `confidence` only reaches "solid"/"strong" once 2+ independent
    signals agree (see Linear.md "Decision Making": never conclude from one signal).
    """
    script_urls = _script_urls(page)
    all_global_names = sorted({name for tool in KNOWN_TOOLS for name in tool.global_vars})
    found_globals = _check_globals(page, all_global_names)
    request_urls = [r.url for r in requests]

    detections: list[ToolDetection] = []
    for tool in KNOWN_TOOLS:
        network_patterns = tool.network_patterns or tool.script_patterns

        matched_scripts = tuple(
            url for url in script_urls if any(pattern in url for pattern in tool.script_patterns)
        )
        matched_globals = tuple(name for name in tool.global_vars if name in found_globals)
        matched_network = tuple(
            url for url in request_urls if any(pattern in url for pattern in network_patterns)
        )

        signal_count = sum(bool(m) for m in (matched_scripts, matched_globals, matched_network))
        if signal_count == 0:
            continue

        detections.append(
            ToolDetection(
                name=tool.name,
                category=tool.category,
                signal_count=signal_count,
                matched_scripts=matched_scripts,
                matched_globals=matched_globals,
                matched_network=matched_network,
            )
        )
    return detections


def collect_and_record(
    page: Page,
    store: EvidenceStore,
    run_id: str,
    label: str,
    *,
    requests: list[RequestRecord] | None = None,
    detections: list[ToolDetection] | None = None,
) -> Evidence:
    """Detect known onboarding tools (or reuse pre-computed `detections`) and
    record the findings as Evidence."""
    requests = requests or []
    detections = detections if detections is not None else detect_tools(page, requests)
    top_confidence = max((d.confidence for d in detections), default=None)

    return store.add(
        run_id,
        label,
        url=_safe_url(page),
        javascript={
            "detected_tools": [
                {
                    "name": d.name,
                    "category": d.category.value,
                    "signal_count": d.signal_count,
                    "confidence": d.confidence,
                    "matched_scripts": list(d.matched_scripts),
                    "matched_globals": list(d.matched_globals),
                    "matched_network": list(d.matched_network),
                }
                for d in detections
            ]
        },
        network={"request_count": len(requests)},
        confidence=top_confidence,
    )
