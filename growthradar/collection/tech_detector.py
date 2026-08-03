from __future__ import annotations

import re

from growthradar.collection.tech_signatures import TECH_SIGNATURES
from growthradar.core.models import DetectedTech, PageContent


def detect_technologies(pages: list[PageContent]) -> list[DetectedTech]:
    combined_html = "\n".join(page.raw_html for page in pages if page.raw_html)
    detected: list[DetectedTech] = []

    for name, spec in TECH_SIGNATURES.items():
        for pattern in spec["patterns"]:
            if re.search(pattern, combined_html, re.I):
                detected.append(DetectedTech(name=name, category=spec["category"], matched_pattern=pattern))
                break

    return detected
