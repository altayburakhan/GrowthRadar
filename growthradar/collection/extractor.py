from __future__ import annotations

import re

from bs4 import BeautifulSoup

from growthradar.core.models import PageContent

_SIGNUP_PATTERN = re.compile(r"\bsign\s?up\b|\bcreate\s+(an?\s+)?account\b|\bget\s+started\b", re.I)
_TRIAL_PATTERN = re.compile(r"\bfree\s+trial\b|\btry\s+(it\s+)?free\b|\bstart\s+(your\s+)?trial\b", re.I)
_DEMO_PATTERN = re.compile(
    r"\bbook\s+a\s+demo\b|\brequest\s+a\s+demo\b|\bschedule\s+a\s+demo\b|\btalk\s+to\s+sales\b", re.I
)
_PRICING_TIER_PATTERN = re.compile(r"(\$|€|£)\s?\d{1,4}(\.\d{2})?\s*(/|per)\s*(mo|month|user|seat|year)", re.I)

B2B_KEYWORDS = [
    "team", "teams", "workspace", "integration", "integrations", "api", "dashboard",
    "enterprise", "workflow", "admin", "seat", "seats", "sso", "onboarding", "b2b",
    "saas", "self-serve", "self serve",
]
B2C_KEYWORDS = [
    "download the app", "app store", "google play", "shop now", "add to cart",
    "personal use", "for families", "for individuals",
]


def parse_page(raw_html: str, url: str) -> tuple[str, str, str]:
    """Returns (title, meta_description, visible_text) extracted from raw HTML."""
    soup = BeautifulSoup(raw_html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    meta_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = meta_tag.get("content", "").strip() if meta_tag else ""

    text = " ".join(soup.get_text(separator=" ").split())
    return title, meta_description, text


def enrich_page(page: PageContent) -> PageContent:
    """Fills in title/meta/text on a fetched page. No-op for failed fetches."""
    if not page.fetched_ok or not page.raw_html:
        return page
    title, meta_description, text = parse_page(page.raw_html, page.url)
    return page.model_copy(update={"title": title, "meta_description": meta_description, "text": text})


def count_keyword_hits(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    return sum(lowered.count(keyword) for keyword in keywords)


def detect_cta_signals(text: str) -> dict[str, bool]:
    return {
        "has_signup_cta": bool(_SIGNUP_PATTERN.search(text)),
        "has_free_trial_cta": bool(_TRIAL_PATTERN.search(text)),
        "has_demo_cta": bool(_DEMO_PATTERN.search(text)),
    }


def estimate_pricing_tiers(text: str) -> int:
    """Rough heuristic: counts currency-amount-per-period mentions (e.g. "$29/month")
    as a proxy for the number of pricing tiers on a page. Not exact -- a page
    mentioning the same price twice will overcount slightly -- but good enough to
    distinguish "no visible pricing" from "structured multi-tier SaaS pricing"."""
    return min(len(_PRICING_TIER_PATTERN.findall(text)), 10)
