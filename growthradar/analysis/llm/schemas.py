from __future__ import annotations

from pydantic import BaseModel, Field


class LLMAssessment(BaseModel):
    """Structured qualitative assessment. Shared by every provider implementation
    (Claude, the offline heuristic fallback, or any future provider) so the rest
    of the pipeline never has to know which one produced it."""

    product_experience_score: float = Field(ge=0, le=100)
    product_experience_reasoning: str
    onboarding_opportunity_score: float = Field(ge=0, le=100)
    onboarding_opportunity_reasoning: str
    recommended_pitch_angle: str
    confidence: float = Field(ge=0, le=1, default=0.7)
