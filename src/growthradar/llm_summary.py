"""LLM-generated evidence summary.

After scoring.py computes its 100% rule-based, deterministic verdict, this
asks Groq's chat completion API for a short, plain-English explanation of
*why* -- grounded strictly in the same evidence the score already cites, so
a salesperson reading the report gets a human-readable "why" alongside the
numbers. The verdict itself never depends on this call: ICP fit / onboarding
opportunity / product experience stay computed by scoring.py's pure
boolean-check logic (Linear.md "never conclude from one signal" -- an LLM
paraphrase is not a signal, and must never be allowed to change the score).

Skipped -- logged, never raised -- when no Groq key is configured (i.e.
`Config.resolve_provider()` isn't "groq") or the request fails for any
reason; the skip reason is still recorded as Evidence so the run stays
reproducible (same isolation pattern the removed vision.py used).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from growthradar.config import Config
from growthradar.evidence import Evidence, EvidenceStore
from growthradar.scoring import ScoreResult

logger = logging.getLogger(__name__)

_GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
_REQUEST_TIMEOUT_SECONDS = 20
_MAX_TOKENS = 220
_CONFIDENT_THRESHOLD = 0.65

_PROMPT_TEMPLATE = """You are a sales analyst summarizing an automated evaluation of a SaaS \
product as a potential customer for UserGuiding (a user-onboarding platform). Based ONLY on \
the facts below, write a short (2-3 sentence) plain-English explanation of why the verdict is \
what it is. Do not invent any fact not listed. Do not just restate the numbers -- explain what \
they mean for a salesperson deciding whether to reach out.

Facts:
{facts}

Explanation:"""


def _facts_text(evidence: list[Evidence], score: ScoreResult) -> str:
    registration_completed = any(
        e.label == "registration attempt"
        and isinstance(e.visible_ui, dict)
        and e.visible_ui.get("submitted") is True
        for e in evidence
    )
    explored_pages = len({e.url for e in evidence if e.label.startswith("dom:") and e.url})

    tools: set[str] = set()
    for e in evidence:
        if not e.label.startswith("js/network:") or not isinstance(e.javascript, dict):
            continue
        for tool in e.javascript.get("detected_tools", []):
            if tool.get("confidence", 0) >= _CONFIDENT_THRESHOLD:
                tools.add(f"{tool.get('name')} ({tool.get('category', 'onboarding')})")

    onboarding_categories: set[str] = set()
    for e in evidence:
        if (
            e.label.startswith("onboarding heuristics:")
            and (e.confidence or 0) >= _CONFIDENT_THRESHOLD
            and isinstance(e.visible_ui, dict)
        ):
            onboarding_categories.update(e.visible_ui.get("matched_categories", []))

    lines = [
        f"Registration completed: {registration_completed}",
        f"Pages explored: {explored_pages}",
        f"Technologies detected: {', '.join(sorted(tools)) or 'none'}",
        f"Onboarding UI patterns observed: {', '.join(sorted(onboarding_categories)) or 'none'}",
        f"ICP fit score: {score.icp_fit.score}/100",
        f"Onboarding opportunity score: {score.onboarding_opportunity.score}/100",
        f"Product experience score: {score.product_experience.score}/100",
        f"Overall score: {score.overall_score}/100 ({score.verdict.upper()})",
    ]
    return "\n".join(lines)


def _call_groq(prompt: str, config: Config) -> str | None:
    payload = {
        "model": config.groq_model,
        "max_tokens": _MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        _GROQ_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.groq_api_key or ''}",
            # Groq's API sits behind Cloudflare, which blocks urllib's default
            # "Python-urllib/x.y" User-Agent outright (HTTP 403, Cloudflare
            # error 1010) -- any real-looking value clears it.
            "User-Agent": config.user_agent,
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:  # noqa: S310
            body: dict[str, Any] = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning("Groq summary request failed: %s", exc)
        return None

    choices = body.get("choices") or []
    if not choices:
        logger.warning("Groq summary: no choices in response")
        return None
    content = (choices[0].get("message") or {}).get("content")
    return str(content).strip() if content else None


def summarize_evidence(evidence: list[Evidence], score: ScoreResult, config: Config) -> str | None:
    """Ask Groq for a plain-English explanation of `score`'s verdict.

    Returns None (never raises) when Groq isn't available for this
    configuration (no key, provider resolves elsewhere) or the call fails
    for any reason.
    """
    if config.resolve_provider() != "groq" or not config.groq_api_key:
        logger.info("LLM summary skipped: resolved provider is not Groq or no API key set")
        return None

    prompt = _PROMPT_TEMPLATE.format(facts=_facts_text(evidence, score))
    return _call_groq(prompt, config)


def record_llm_summary(
    store: EvidenceStore,
    run_id: str,
    label: str,
    *,
    evidence: list[Evidence],
    score: ScoreResult,
    config: Config,
) -> Evidence:
    """Summarize a run's evidence via Groq and record the result -- or the
    skip reason -- as Evidence."""
    summary = summarize_evidence(evidence, score, config)
    if summary is None:
        return store.add(run_id, label, visible_ui={"skipped": True})

    return store.add(run_id, label, visible_ui={"summary": summary})
