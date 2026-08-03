from __future__ import annotations

import json
import logging
import re

from growthradar.analysis.llm.base import LLMProvider, LLMProviderError
from growthradar.analysis.llm.prompts import SYSTEM_PROMPT, build_user_prompt
from growthradar.analysis.llm.schemas import LLMAssessment
from growthradar.core.models import CompanyEvidence

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class GroqProvider(LLMProvider):
    """Free-tier-friendly LLM reasoning via Groq's OpenAI-compatible chat API
    (e.g. Llama 3.3 70B). Same LLMAssessment contract as AnthropicProvider, so
    the rest of the pipeline is provider-agnostic."""

    name = "groq"

    def __init__(self, api_key: str, model: str, max_tokens: int = 700, temperature: float = 0.2):
        try:
            from groq import Groq
        except ImportError as exc:
            raise LLMProviderError(
                "The 'groq' package is required for GroqProvider. Install it with "
                "`pip install groq` or set GROWTHRADAR_LLM_PROVIDER=heuristic."
            ) from exc
        self._client = Groq(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    def assess(self, evidence: CompanyEvidence) -> LLMAssessment:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(evidence)},
                ],
            )
        except Exception as exc:
            raise LLMProviderError(f"Groq API call failed: {exc}") from exc

        text = response.choices[0].message.content or ""
        match = _JSON_BLOCK.search(text)
        if not match:
            raise LLMProviderError(f"Groq response did not contain JSON: {text[:200]!r}")

        try:
            payload = json.loads(match.group(0))
            return LLMAssessment.model_validate(payload)
        except Exception as exc:
            raise LLMProviderError(f"Failed to parse Groq response as LLMAssessment: {exc}") from exc
