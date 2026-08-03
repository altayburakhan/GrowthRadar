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


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 700, temperature: float = 0.2):
        try:
            import anthropic
        except ImportError as exc:
            raise LLMProviderError(
                "The 'anthropic' package is required for AnthropicProvider. Install it with "
                "`pip install anthropic` or set GROWTHRADAR_LLM_PROVIDER=heuristic."
            ) from exc
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    def assess(self, evidence: CompanyEvidence) -> LLMAssessment:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_prompt(evidence)}],
            )
        except Exception as exc:
            raise LLMProviderError(f"Anthropic API call failed: {exc}") from exc

        text = "".join(block.text for block in response.content if hasattr(block, "text"))
        match = _JSON_BLOCK.search(text)
        if not match:
            raise LLMProviderError(f"Anthropic response did not contain JSON: {text[:200]!r}")

        try:
            payload = json.loads(match.group(0))
            return LLMAssessment.model_validate(payload)
        except Exception as exc:
            raise LLMProviderError(f"Failed to parse Anthropic response as LLMAssessment: {exc}") from exc
