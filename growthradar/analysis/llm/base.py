from __future__ import annotations

from abc import ABC, abstractmethod

from growthradar.analysis.llm.schemas import LLMAssessment
from growthradar.core.models import CompanyEvidence


class LLMProviderError(RuntimeError):
    """Raised when a provider fails to produce a usable assessment. The pipeline
    catches this and falls back to the offline heuristic provider."""


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def assess(self, evidence: CompanyEvidence) -> LLMAssessment:
        """Produce a structured qualitative assessment grounded in the given evidence."""
        raise NotImplementedError
