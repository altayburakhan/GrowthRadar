"""Page-type classification for pages Linear.md calls out specifically:
Product Updates, Release Notes, Changelog, Blog, Help Center, Documentation.

GRO-11's exploration engine already tags screenshots with a rough kind based
on the anchor text that led to a page (e.g. a nav link saying "Help"). That's
prone to false positives -- a footer link labeled "Help" might land on a
generic contact page. This module classifies the *visited page's own content*
instead, and only commits to a category once two independent signals agree
(URL pattern + on-page content) -- a single signal is treated as inconclusive
and returns None rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from growthradar.dom import DomSnapshot
from growthradar.evidence import Evidence, EvidenceStore

# Two independent signals agreeing is our confidence floor throughout the
# codebase (see GRO-14/15/16's "2+ signals" confidence band).
_CLASSIFICATION_CONFIDENCE = 0.85


class PageCategory(StrEnum):
    PRODUCT_UPDATES = "product_updates"
    HELP_CENTER = "help_center"
    DOCUMENTATION = "documentation"
    BLOG = "blog"


@dataclass(frozen=True)
class CategorySignature:
    category: PageCategory
    url_patterns: tuple[str, ...]
    content_keywords: tuple[str, ...]


CATEGORY_SIGNATURES: tuple[CategorySignature, ...] = (
    CategorySignature(
        PageCategory.PRODUCT_UPDATES,
        url_patterns=(
            "/changelog",
            "/release-notes",
            "/releases",
            "/whats-new",
            "/product-updates",
        ),
        content_keywords=("changelog", "release notes", "what's new", "product updates"),
    ),
    CategorySignature(
        PageCategory.HELP_CENTER,
        url_patterns=("/help", "/support", "/faq"),
        content_keywords=(
            "help center",
            "support center",
            "frequently asked questions",
            "knowledge base",
        ),
    ),
    CategorySignature(
        PageCategory.DOCUMENTATION,
        url_patterns=("/docs", "/documentation", "/developer", "/api-reference"),
        content_keywords=(
            "documentation",
            "developer docs",
            "api reference",
            "getting started guide",
        ),
    ),
    CategorySignature(
        PageCategory.BLOG,
        url_patterns=("/blog", "/news", "/articles"),
        content_keywords=("blog", "posted by", "min read", "written by"),
    ),
)


@dataclass(frozen=True)
class PageClassification:
    category: PageCategory
    matched_url_pattern: str
    matched_content_keyword: str


def classify_page(url: str, dom: DomSnapshot) -> PageClassification | None:
    """Classify a page into a Linear.md category. Requires URL + content agreement.

    Pure function: no I/O, deterministic given its inputs.
    """
    url_lower = url.lower()
    haystack = f"{dom.title} {dom.visible_text}".lower()

    for sig in CATEGORY_SIGNATURES:
        url_hit = next((p for p in sig.url_patterns if p in url_lower), None)
        content_hit = next((k for k in sig.content_keywords if k in haystack), None)
        if url_hit and content_hit:
            return PageClassification(
                category=sig.category,
                matched_url_pattern=url_hit,
                matched_content_keyword=content_hit,
            )
    return None


def classify_and_record(
    store: EvidenceStore, run_id: str, label: str, *, url: str, dom: DomSnapshot
) -> Evidence | None:
    """Classify the page and record it as Evidence -- only when confidently classified."""
    classification = classify_page(url, dom)
    if classification is None:
        return None

    return store.add(
        run_id,
        label,
        url=url,
        visible_ui={
            "category": classification.category.value,
            "matched_url_pattern": classification.matched_url_pattern,
            "matched_content_keyword": classification.matched_content_keyword,
        },
        confidence=_CLASSIFICATION_CONFIDENCE,
    )
