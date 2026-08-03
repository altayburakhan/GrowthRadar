from __future__ import annotations

import logging
import time

from growthradar.collection.extractor import (
    B2B_KEYWORDS,
    B2C_KEYWORDS,
    count_keyword_hits,
    detect_cta_signals,
    enrich_page,
    estimate_pricing_tiers,
)
from growthradar.collection.page_discovery import discover_candidate_urls, normalize_base_url
from growthradar.collection.playwright_fetcher import PlaywrightFetcher
from growthradar.collection.tech_detector import detect_technologies
from growthradar.core.models import CompanyEvidence, CompanySignals, PageContent
from growthradar.utils.robots import can_fetch, load_robots

logger = logging.getLogger(__name__)


def _fetched(pages: dict[str, PageContent], label: str) -> bool:
    return label in pages and pages[label].fetched_ok


def build_evidence(
    raw_input: str,
    fetcher: PlaywrightFetcher,
    max_pages: int,
    user_agent: str,
    request_timeout: float,
    crawl_delay: float = 0.0,
) -> CompanyEvidence:
    """Collects and structures public evidence for a company. Never raises on
    network failure -- partial or empty evidence is returned so downstream
    scoring can still run (with disqualifiers/low confidence noting the gap)."""
    base_url = normalize_base_url(raw_input)
    domain = base_url.split("//", 1)[1]
    robots = load_robots(base_url, user_agent, request_timeout)

    homepage_url = base_url + "/"
    if not can_fetch(robots, user_agent, homepage_url):
        logger.info("robots.txt disallows crawling %s", homepage_url)
        return CompanyEvidence(
            domain=domain,
            robots_disallowed_paths=[homepage_url],
            fetch_errors=["robots.txt disallows crawling the homepage"],
        )

    pages: dict[str, PageContent] = {}
    fetch_errors: list[str] = []
    robots_disallowed: list[str] = []

    homepage = enrich_page(fetcher.fetch(homepage_url))
    pages["home"] = homepage
    if not homepage.fetched_ok:
        fetch_errors.append(f"homepage unreachable: {homepage.fetch_error}")

    candidate_pages = (
        discover_candidate_urls(base_url, homepage.raw_html, max_pages - 1) if homepage.raw_html else {}
    )

    for label, url in candidate_pages.items():
        if not can_fetch(robots, user_agent, url):
            robots_disallowed.append(url)
            continue
        if crawl_delay:
            time.sleep(crawl_delay)
        page = enrich_page(fetcher.fetch(url))
        pages[label] = page
        if not page.fetched_ok:
            fetch_errors.append(f"{url}: {page.fetch_error}")

    all_pages = list(pages.values())
    detected_tech = detect_technologies(all_pages)
    combined_text = " ".join(page.text for page in all_pages if page.fetched_ok)
    cta = detect_cta_signals(combined_text)

    signals = CompanySignals(
        has_signup_cta=cta["has_signup_cta"],
        has_free_trial_cta=cta["has_free_trial_cta"],
        has_demo_cta=cta["has_demo_cta"],
        pricing_tier_count=estimate_pricing_tiers(combined_text),
        has_careers_page=_fetched(pages, "careers"),
        has_blog=_fetched(pages, "blog"),
        has_docs_or_help_center=_fetched(pages, "docs") or any(t.category == "help_center" for t in detected_tech),
        b2b_keyword_hits=count_keyword_hits(combined_text, B2B_KEYWORDS),
        b2c_keyword_hits=count_keyword_hits(combined_text, B2C_KEYWORDS),
    )

    return CompanyEvidence(
        domain=domain,
        pages=pages,
        detected_technologies=detected_tech,
        signals=signals,
        robots_disallowed_paths=robots_disallowed,
        fetch_errors=fetch_errors,
    )
