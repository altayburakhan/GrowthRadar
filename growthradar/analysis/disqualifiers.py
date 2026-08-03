from __future__ import annotations

from growthradar.core.models import CompanyEvidence

EXISTING_CUSTOMER_TECH = {"UserGuiding"}


def find_disqualifiers(evidence: CompanyEvidence) -> list[str]:
    """Hard-exclusion checks that should override an otherwise high score --
    e.g. a company that's already a UserGuiding customer is not a lead."""
    disqualifiers: list[str] = []

    if evidence.tech_names() & EXISTING_CUSTOMER_TECH:
        disqualifiers.append("UserGuiding is already installed on this site -- likely an existing customer.")

    if evidence.robots_disallowed_paths and not any(page.fetched_ok for page in evidence.pages.values()):
        disqualifiers.append("robots.txt disallows crawling; no evidence could be collected.")
    elif evidence.pages and not any(page.fetched_ok for page in evidence.pages.values()):
        disqualifiers.append("No pages could be reached; unable to verify this is an active company website.")

    return disqualifiers
