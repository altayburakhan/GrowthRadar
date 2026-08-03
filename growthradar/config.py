from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (avoids adding python-dotenv as a dependency)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


_load_dotenv()


@dataclass(frozen=True)
class ScoringWeights:
    """Relative importance of each scoring dimension. Must not be assumed to sum to 1;
    the scorer normalizes by the total weight actually present for a given result."""

    icp_fit: float = _env_float("GROWTHRADAR_WEIGHT_ICP_FIT", 0.30)
    onboarding_opportunity: float = _env_float("GROWTHRADAR_WEIGHT_ONBOARDING_OPPORTUNITY", 0.45)
    product_experience: float = _env_float("GROWTHRADAR_WEIGHT_PRODUCT_EXPERIENCE", 0.25)

    def as_dict(self) -> dict[str, float]:
        return {
            "icp_fit": self.icp_fit,
            "onboarding_opportunity": self.onboarding_opportunity,
            "product_experience": self.product_experience,
        }


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None = os.environ.get("ANTHROPIC_API_KEY") or None
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    groq_api_key: str | None = os.environ.get("GROQ_API_KEY") or None
    groq_model: str = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    llm_provider: str = os.environ.get("GROWTHRADAR_LLM_PROVIDER", "auto")  # auto | anthropic | groq | heuristic

    request_timeout_seconds: float = _env_float("GROWTHRADAR_REQUEST_TIMEOUT", 10.0)
    max_pages_per_company: int = _env_int("GROWTHRADAR_MAX_PAGES", 8)
    crawl_delay_seconds: float = _env_float("GROWTHRADAR_CRAWL_DELAY", 0.5)
    user_agent: str = os.environ.get(
        "GROWTHRADAR_USER_AGENT",
        "GrowthRadarBot/0.1 (+https://userguiding.com; growth-intelligence-research)",
    )

    db_path: str = os.environ.get("GROWTHRADAR_DB_PATH", "growthradar.db")
    log_level: str = os.environ.get("GROWTHRADAR_LOG_LEVEL", "INFO")

    hot_threshold: float = _env_float("GROWTHRADAR_HOT_THRESHOLD", 70.0)
    warm_threshold: float = _env_float("GROWTHRADAR_WARM_THRESHOLD", 40.0)

    weights: ScoringWeights = ScoringWeights()


settings = Settings()
