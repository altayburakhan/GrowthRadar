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

If a vision model is also configured (`Config.groq_vision_model`), the facts
fed to this summary include one-sentence vision descriptions of the
post-login dashboard/onboarding screenshot and a product-updates screenshot
(see vision_fallback.describe_screenshot) -- purely additive narrative
context, same non-negotiable rule as the text facts: it can shape the
wording of the explanation, never the score.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from growthradar.config import Config
from growthradar.evidence import Evidence, EvidenceStore
from growthradar.scoring import ScoreResult
from growthradar.vision_fallback import describe_screenshot

logger = logging.getLogger(__name__)

_GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
_REQUEST_TIMEOUT_SECONDS = 20
# The default GROQ_MODEL (openai/gpt-oss-120b) is a reasoning model --
# Groq counts its internal reasoning against max_tokens before the final
# answer, so a budget sized for the answer alone (previously 220) got cut
# off mid-sentence (finish_reason="length") on this project's actual
# prompt/facts shape; 500 leaves enough room for both.
_MAX_TOKENS = 500
_CONFIDENT_THRESHOLD = 0.65
# Some Groq models (e.g. qwen3.6, used for GROQ_VISION_MODEL -- see
# vision_fallback.py) inline their reasoning as a <think>...</think> block
# ahead of the actual answer, unlike gpt-oss's separate `reasoning` field.
# Stripping defensively here means switching GROQ_MODEL to one of those
# later doesn't leak raw reasoning into the report's "AI summary" section.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

_PROMPT_TEMPLATE = """You are a sales analyst summarizing an automated evaluation of a SaaS \
product as a potential customer for UserGuiding (a user-onboarding platform). Based ONLY on \
the facts below, write a plain-English explanation of why the verdict is what it is, in AT \
MOST TWO SENTENCES total -- be concise, do not pad. Do not invent any fact not listed. Do not \
just restate the numbers -- explain what they mean for a salesperson deciding whether to reach \
out. If a competitor onboarding tool is listed, say so explicitly and note that outreach should \
be framed around switching/replacing it, not "do you need this at all". If this company is \
already a UserGuiding customer, say so explicitly instead -- this is not a \
competitive-displacement prospect. If a screenshot observation is listed, weave in whichever \
one is most relevant instead of just the raw numbers.

Facts:
{facts}

Explanation (2 sentences max):"""


def _screenshot_kind(e: Evidence) -> str | None:
    if isinstance(e.visible_ui, dict):
        kind = e.visible_ui.get("screenshot_kind")
        return kind if isinstance(kind, str) else None
    return None


def _find_screenshot_path(evidence: list[Evidence], *kinds: str) -> str | None:
    for e in evidence:
        if e.label.startswith("screenshot:") and e.screenshot and _screenshot_kind(e) in kinds:
            return e.screenshot
    return None


def _screenshot_observations(evidence: list[Evidence], config: Config) -> list[str]:
    """One-sentence vision descriptions of the two screenshots most relevant
    to onboarding experience -- the post-login dashboard/onboarding screen
    and, if the crawl found one, a product-updates/changelog page. Returns
    [] (never raises) when no vision model is configured or a description
    couldn't be produced -- this is purely additive narrative context, so
    its absence must never block or alter the rest of the summary."""
    targets = (
        ("dashboard/onboarding", _find_screenshot_path(evidence, "dashboard", "onboarding")),
        ("product updates", _find_screenshot_path(evidence, "product_updates")),
    )
    observations = []
    for label, path in targets:
        if not path:
            continue
        description = describe_screenshot(path, label, config)
        if description:
            observations.append(f"{label.capitalize()} screenshot shows: {description}")
    return observations


def _facts_text(evidence: list[Evidence], score: ScoreResult, config: Config) -> str:
    registration_completed = any(
        e.label == "registration attempt"
        and isinstance(e.visible_ui, dict)
        and e.visible_ui.get("submitted") is True
        for e in evidence
    )
    explored_pages = len({e.url for e in evidence if e.label.startswith("dom:") and e.url})

    tools: set[str] = set()
    competitor_tools: set[str] = set()
    already_userguiding = False
    for e in evidence:
        if not e.label.startswith("js/network:") or not isinstance(e.javascript, dict):
            continue
        for tool in e.javascript.get("detected_tools", []):
            if tool.get("confidence", 0) < _CONFIDENT_THRESHOLD:
                continue
            name = str(tool.get("name"))
            category = tool.get("category", "onboarding")
            tools.add(f"{name} ({category})")
            if category != "onboarding":
                continue
            if name == "UserGuiding":
                already_userguiding = True
            else:
                competitor_tools.add(name)

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
        f"Already a UserGuiding customer: {already_userguiding}",
        f"Competitor onboarding tools detected: {', '.join(sorted(competitor_tools)) or 'none'}",
        f"Onboarding UI patterns observed: {', '.join(sorted(onboarding_categories)) or 'none'}",
        *_screenshot_observations(evidence, config),
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
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            body: dict[str, Any] = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning("Groq summary request failed: %s", exc)
        return None

    choices = body.get("choices") or []
    if not choices:
        logger.warning("Groq summary: no choices in response")
        return None
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        return None
    return _THINK_BLOCK_RE.sub("", str(content)).strip() or None


def summarize_evidence(evidence: list[Evidence], score: ScoreResult, config: Config) -> str | None:
    """Ask Groq for a plain-English explanation of `score`'s verdict.

    Returns None (never raises) when Groq isn't available for this
    configuration (no key, provider resolves elsewhere) or the call fails
    for any reason.
    """
    if config.resolve_provider() != "groq" or not config.groq_api_key:
        logger.info("LLM summary skipped: resolved provider is not Groq or no API key set")
        return None

    prompt = _PROMPT_TEMPLATE.format(facts=_facts_text(evidence, score, config))
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
