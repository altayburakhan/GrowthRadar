from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field


class Tier(str, Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    EXCLUDED = "excluded"


class PageContent(BaseModel):
    url: str
    status_code: int | None = None
    title: str = ""
    meta_description: str = ""
    text: str = ""
    raw_html: str = ""
    fetch_error: str | None = None

    @property
    def fetched_ok(self) -> bool:
        return self.fetch_error is None and self.status_code is not None and self.status_code < 400


class DetectedTech(BaseModel):
    name: str
    category: str
    matched_pattern: str


class CompanySignals(BaseModel):
    has_signup_cta: bool = False
    has_free_trial_cta: bool = False
    has_demo_cta: bool = False
    pricing_tier_count: int = 0
    has_careers_page: bool = False
    has_blog: bool = False
    has_docs_or_help_center: bool = False
    b2b_keyword_hits: int = 0
    b2c_keyword_hits: int = 0


class CompanyEvidence(BaseModel):
    """Structured, evidence-grounded snapshot of a company's public web presence.

    This is the boundary between the deterministic collection layer and the
    analysis layer: nothing downstream should touch raw HTML directly, only
    facts that have already been extracted and validated here.
    """

    domain: str
    pages: dict[str, PageContent] = Field(default_factory=dict)
    detected_technologies: list[DetectedTech] = Field(default_factory=list)
    signals: CompanySignals = Field(default_factory=CompanySignals)
    robots_disallowed_paths: list[str] = Field(default_factory=list)
    fetch_errors: list[str] = Field(default_factory=list)

    def tech_names(self) -> set[str]:
        return {t.name for t in self.detected_technologies}

    def has_technology_category(self, category: str) -> bool:
        return any(t.category == category for t in self.detected_technologies)

    def evidence_bullets(self) -> list[str]:
        """Human-readable evidence lines used to ground LLM prompts and reports."""
        fetched_pages = [p for p in self.pages.values() if p.fetched_ok]
        bullets = [f"Pages successfully analyzed: {len(fetched_pages)} of {len(self.pages)} attempted."]

        for page in fetched_pages:
            snippet = page.text[:300].replace("\n", " ").strip()
            bullets.append(f'[{page.url}] title="{page.title}" excerpt="{snippet}"')

        if self.detected_technologies:
            tech_list = ", ".join(f"{t.name} ({t.category})" for t in self.detected_technologies)
            bullets.append(f"Detected technologies: {tech_list}.")
        else:
            bullets.append("No recognizable third-party tools detected in page source.")

        s = self.signals
        bullets.append(
            "Signals: "
            f"signup_cta={s.has_signup_cta}, free_trial_cta={s.has_free_trial_cta}, demo_cta={s.has_demo_cta}, "
            f"pricing_tiers={s.pricing_tier_count}, careers_page={s.has_careers_page}, blog={s.has_blog}, "
            f"help_center={s.has_docs_or_help_center}, b2b_keyword_hits={s.b2b_keyword_hits}, "
            f"b2c_keyword_hits={s.b2c_keyword_hits}."
        )
        return bullets


class DimensionScore(BaseModel):
    name: str
    score: float = Field(ge=0, le=100)
    reasoning: str
    evidence: list[str] = Field(default_factory=list)


class LeadScoreResult(BaseModel):
    domain: str
    overall_score: float
    tier: Tier
    dimensions: list[DimensionScore]
    disqualifiers: list[str] = Field(default_factory=list)
    recommended_pitch_angle: str = ""
    provider_used: str = ""
    confidence: float = 1.0
    generated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
