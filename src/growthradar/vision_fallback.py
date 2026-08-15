"""Vision-LLM helpers built on `Config.groq_vision_model`, used in two
unrelated places:

1. `suggest_click_target` -- a registration-step fallback (registration.py)
   for when the DOM-based heuristics (fill/check/submit/choice-pick) can't
   handle a stuck step. Only tried as a last resort: captures the current
   viewport, lists the visible clickable element texts already found by a
   plain DOM scan, and asks the model to pick ONE of those texts verbatim --
   never a free-form answer, never pixel coordinates. The chosen text is
   then clicked through registration.py's own text-matching, so vision only
   ever narrows among candidates the DOM already found; it never concludes
   or acts alone (Linear.md "never conclude from one signal").

2. `describe_screenshot` -- a one-sentence visual observation of an
   already-saved screenshot file (llm_summary.py), purely to enrich its
   plain-English narrative. Never touches scoring: `scoring.py`'s verdict
   stays 100% rule-based regardless of what this returns, same guarantee
   `llm_summary.py` already gives its text-only facts.

Both skip -- logged, never raised -- when no vision model is configured
(`Config.groq_vision_model` unset) or the request/parse fails for any
reason. Same isolation pattern as llm_summary.py's Groq call, and the same
reason it's opt-in with no default model: Groq's vision-capable lineup has
repeatedly changed (3.2-vision previews deprecated) and account access
varies -- verify a model actually accepts image input on your key before
setting GROQ_VISION_MODEL.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import Page

from growthradar.config import Config

logger = logging.getLogger(__name__)

_GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
_REQUEST_TIMEOUT_SECONDS = 30
# Reasoning models (e.g. qwen3.6, the one verified against this project's
# Groq key) emit a <think>...</think> block before the actual answer, which
# alone can run past 300 tokens -- too small a budget truncates mid-thought
# and _parse_choice never finds the JSON at all.
_MAX_TOKENS = 700
_MAX_CANDIDATES = 25
_MAX_CANDIDATE_TEXT_LEN = 60

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_PROMPT_TEMPLATE = """You are looking at a screenshot of a signup/registration step on a \
website. Below is a list of clickable elements found on the page, by their visible text. \
Pick the ONE element a real user should click next to proceed with signing up (e.g. one of \
several choice cards, or the correct "Next"/"Continue" button). Respond with ONLY a JSON \
object: {{"choice": "<exact text from the list>"}}. Copy the text exactly as it appears in \
the list -- do not paraphrase, translate, or invent an option that isn't listed.

Candidates:
{candidates}"""

DEFAULT_CANDIDATE_SELECTOR = 'button, a, [role="button"]'

# "Book a Demo"/"Request a Demo"-style CTAs lead to scheduling a live sales
# call, not a self-serve trial signup -- never something we want to click.
# registration.py's own DOM-based choice-wizard fallback already excludes
# these via _CHOICE_EXCLUDE_KEYWORDS, but vision's candidate list is
# gathered independently (no shared exclusion list -- importing one from the
# other would be circular, since registration.py already imports from this
# module) and had no such filter, so a stuck step could still have the model
# pick a demo-booking button off the screenshot.
_EXCLUDED_CANDIDATE_KEYWORDS: tuple[str, ...] = ("demo",)

_DESCRIBE_PROMPT_TEMPLATE = """This is a screenshot of a SaaS product's {page_kind} screen, \
captured during an automated evaluation for a sales-prospecting tool. In ONE short, concrete \
sentence, describe the onboarding/product-experience signal a salesperson would notice -- e.g. \
a guided checklist, an empty/cluttered state, a progress tour, a changelog entry. Describe only \
what is actually visible in the image; do not speculate or invent anything beyond it."""


def _candidate_texts(page: Page, selector: str) -> list[str]:
    """Visible, non-empty inner text of every element matching `selector`,
    deduped and capped. Deliberately independent of registration.py's claim
    marker: an element not chosen this round should still be offerable to
    vision next round."""
    try:
        locator = page.locator(selector)
        count = locator.count()
    except PlaywrightError:
        return []

    texts: list[str] = []
    seen: set[str] = set()
    for i in range(count):
        if len(texts) >= _MAX_CANDIDATES:
            break
        element = locator.nth(i)
        try:
            if not element.is_visible():
                continue
            text = element.inner_text(timeout=500).strip()
        except PlaywrightError:
            continue
        if not text or len(text) > _MAX_CANDIDATE_TEXT_LEN or text in seen:
            continue
        lowered = text.lower()
        if any(keyword in lowered for keyword in _EXCLUDED_CANDIDATE_KEYWORDS):
            continue
        seen.add(text)
        texts.append(text)
    return texts


def _parse_choice(content: str, candidates: list[str]) -> str | None:
    cleaned = _THINK_BLOCK_RE.sub("", content).strip()
    match = _JSON_OBJECT_RE.search(cleaned)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    choice = parsed.get("choice") if isinstance(parsed, dict) else None
    if not isinstance(choice, str):
        return None
    # Never trust the model's text as a locator target beyond this check --
    # it must be one of the candidates the DOM scan already found verbatim,
    # or it's discarded (the model paraphrasing/hallucinating an option that
    # isn't on the page is exactly the failure mode this guards against).
    return choice if choice in candidates else None


def _call_groq_vision_raw(
    image_b64: str, prompt: str, config: Config, *, max_tokens: int = _MAX_TOKENS
) -> str | None:
    """Send one image + text prompt to `config.groq_vision_model`, return the
    raw response text (not yet stripped of any `<think>` block or otherwise
    parsed) -- shared by `suggest_click_target` (parses a JSON choice out of
    it) and `describe_screenshot` (uses it as free text). None on any
    failure; never raises."""
    payload = {
        "model": config.groq_vision_model,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }
        ],
    }
    request = urllib.request.Request(
        _GROQ_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.groq_api_key or ''}",
            # See llm_summary.py: Groq's API sits behind Cloudflare, which
            # blocks urllib's default User-Agent outright.
            "User-Agent": config.user_agent,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            body: dict[str, Any] = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning("Groq vision request failed: %s", exc)
        return None

    choices = body.get("choices") or []
    if not choices:
        logger.warning("Groq vision: no choices in response")
        return None
    content = (choices[0].get("message") or {}).get("content")
    return str(content) if content else None


def _call_groq_vision(image_b64: str, candidates: list[str], config: Config) -> str | None:
    prompt = _PROMPT_TEMPLATE.format(candidates="\n".join(f"- {c}" for c in candidates))
    content = _call_groq_vision_raw(image_b64, prompt, config)
    if content is None:
        return None
    return _parse_choice(content, candidates)


def suggest_click_target(
    page: Page, config: Config, *, selector: str = DEFAULT_CANDIDATE_SELECTOR
) -> str | None:
    """Ask a vision-capable Groq model which visible clickable element to
    click next. Returns the chosen element's exact visible text -- never
    coordinates, never free-form text -- so the caller can click it through
    its own normal text-matching; None if unavailable, nothing to offer, or
    the response couldn't be parsed into one of the candidates. Never raises.
    """
    if not config.groq_vision_model or not config.groq_api_key:
        logger.info("vision fallback skipped: no vision model configured")
        return None

    candidates = _candidate_texts(page, selector)
    if not candidates:
        return None

    try:
        image_bytes = page.screenshot(full_page=False)
    except PlaywrightError as exc:
        logger.warning("vision fallback: screenshot failed: %s", exc)
        return None

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    return _call_groq_vision(image_b64, candidates, config)


def describe_screenshot(image_path: str, page_kind: str, config: Config) -> str | None:
    """Ask the configured vision model for a one-sentence description of an
    already-saved screenshot file, framed for a salesperson reading the
    final report -- used only to enrich llm_summary.py's narrative, never to
    influence scoring.py's rule-based verdict. `page_kind` is a short label
    (e.g. "dashboard", "onboarding", "product updates") folded into the
    prompt so the model's answer stays specific to what that screen is for.
    None if unavailable, the file can't be read, or the request fails.
    Never raises.
    """
    if not config.groq_vision_model or not config.groq_api_key:
        return None

    try:
        image_bytes = Path(image_path).read_bytes()
    except OSError as exc:
        logger.warning("screenshot description: could not read %s: %s", image_path, exc)
        return None

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = _DESCRIBE_PROMPT_TEMPLATE.format(page_kind=page_kind)
    content = _call_groq_vision_raw(image_b64, prompt, config)
    if content is None:
        return None
    return _THINK_BLOCK_RE.sub("", content).strip()
