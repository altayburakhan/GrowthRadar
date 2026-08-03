from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# Ordered so the most valuable pages for lead scoring are discovered first when
# a homepage links to many candidate pages and max_pages caps how many we keep.
_PAGE_KEYWORDS = {
    "pricing": ["pricing", "plans"],
    "about": ["about", "company"],
    "careers": ["careers", "jobs"],
    "docs": ["docs", "documentation", "help", "support"],
    "blog": ["blog", "news"],
    "changelog": ["changelog", "whats-new", "release-notes"],
    "customers": ["customers", "case-studies", "testimonials"],
}


def normalize_base_url(raw_input: str) -> str:
    raw_input = raw_input.strip()
    if not re.match(r"^https?://", raw_input, re.I):
        raw_input = f"https://{raw_input}"
    parsed = urlparse(raw_input)
    return f"{parsed.scheme}://{parsed.netloc}"


def discover_candidate_urls(base_url: str, homepage_html: str, max_pages: int) -> dict[str, str]:
    """Finds a small, high-signal set of same-domain pages linked from the homepage,
    keyed by page label (pricing, about, careers, ...)."""
    if max_pages <= 0:
        return {}

    soup = BeautifulSoup(homepage_html, "lxml")
    domain = urlparse(base_url).netloc
    found: dict[str, str] = {}

    for anchor in soup.find_all("a", href=True):
        if len(found) >= max_pages:
            break
        absolute = urljoin(base_url, anchor["href"])
        parsed = urlparse(absolute)
        if parsed.netloc != domain:
            continue
        path = parsed.path.lower()
        for label, keywords in _PAGE_KEYWORDS.items():
            if label in found:
                continue
            if any(keyword in path for keyword in keywords):
                found[label] = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                break

    return found
