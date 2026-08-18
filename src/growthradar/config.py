"""Typed configuration loaded from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

LlmProvider = Literal["auto", "groq", "heuristic"]

_VALID_PROVIDERS: tuple[LlmProvider, ...] = ("auto", "groq", "heuristic")


class ConfigError(ValueError):
    """Raised when environment configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    groq_api_key: str | None
    groq_model: str
    groq_vision_model: str | None
    llm_provider: LlmProvider

    request_timeout: float
    max_pages: int
    crawl_delay: float
    user_agent: str
    # Wall-clock ceiling for one whole site (exploration + registration +
    # onboarding exploration combined), not just one phase -- per-action
    # timeouts alone don't bound total run time when a slow-but-not-quite-
    # failing site pads out several phases in a row.
    session_deadline_seconds: float

    db_path: str
    log_level: str

    registrant_country: str
    registrant_company: str | None
    registrant_email: str | None
    registrant_first_name: str | None
    registrant_last_name: str | None

    # Real Gmail inbox for reading email-verification links/codes over IMAP
    # (see temp_email.py's GmailImapProvider) -- an app password, not the
    # account password (requires 2FA; generate one at
    # myaccount.google.com/apppasswords). Never touches a browser or
    # Google's own login/OAuth pages, so it's unaffected by the automation
    # detection that blocks google_profile_dir's OAuth flow.
    gmail_address: str | None
    gmail_app_password: str | None

    # Reuse a persistent, already-authenticated Google Chrome profile so
    # "Continue with Google" buttons can be clicked instead of skipped (see
    # registration.py's _is_oauth_text). The login itself is never
    # scripted here -- Google's 2FA/verification challenges need a real
    # human once, out of band (see scripts/google_profile_bootstrap.py) --
    # this only points the browser at a profile directory that's already
    # signed in. `allow_google_oauth` is a separate, explicit opt-in on top
    # of a configured profile dir: setting a profile dir alone should never
    # silently change what a run clicks.
    google_profile_dir: str | None
    allow_google_oauth: bool

    hot_threshold: float
    warm_threshold: float
    weight_icp_fit: float
    weight_onboarding_opportunity: float
    weight_product_experience: float

    def __post_init__(self) -> None:
        if self.llm_provider not in _VALID_PROVIDERS:
            raise ConfigError(
                f"GROWTHRADAR_LLM_PROVIDER must be one of {_VALID_PROVIDERS}, "
                f"got {self.llm_provider!r}"
            )
        if self.request_timeout <= 0:
            raise ConfigError("GROWTHRADAR_REQUEST_TIMEOUT must be > 0")
        if self.max_pages <= 0:
            raise ConfigError("GROWTHRADAR_MAX_PAGES must be > 0")
        if self.session_deadline_seconds <= 0:
            raise ConfigError("GROWTHRADAR_SESSION_DEADLINE_SECONDS must be > 0")
        if self.crawl_delay < 0:
            raise ConfigError("GROWTHRADAR_CRAWL_DELAY must be >= 0")
        if self.allow_google_oauth and not self.google_profile_dir:
            raise ConfigError(
                "GROWTHRADAR_ALLOW_GOOGLE_OAUTH requires GROWTHRADAR_GOOGLE_PROFILE_DIR "
                "(a profile with no signed-in Google session can't complete OAuth)"
            )
        if not (0 <= self.warm_threshold <= self.hot_threshold):
            raise ConfigError("GROWTHRADAR_WARM_THRESHOLD must be <= GROWTHRADAR_HOT_THRESHOLD")
        weight_sum = (
            self.weight_icp_fit
            + self.weight_onboarding_opportunity
            + self.weight_product_experience
        )
        if abs(weight_sum - 1.0) > 0.01:
            raise ConfigError(
                "Scoring weights (ICP fit + onboarding opportunity + product experience) "
                f"must sum to 1.0, got {weight_sum:.3f}"
            )

    def resolve_provider(self) -> Literal["groq", "heuristic"]:
        """Resolve 'auto' to a concrete provider: Groq > heuristic, first key wins."""
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.groq_api_key:
            return "groq"
        return "heuristic"

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> Config:
        load_dotenv(dotenv_path=env_path, override=False)

        def _get(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        def _get_optional(name: str) -> str | None:
            value = _get(name)
            return value or None

        def _get_app_password(name: str) -> str | None:
            # Google shows app passwords space-grouped for readability
            # ("abcd efgh ijkl mnop") -- IMAP auth needs it without spaces.
            value = _get_optional(name)
            return value.replace(" ", "") if value else None

        try:
            return cls(
                groq_api_key=_get_optional("GROQ_API_KEY"),
                groq_model=_get("GROQ_MODEL", "openai/gpt-oss-120b"),
                # No default: unlike groq_model, Groq's vision-capable lineup
                # has repeatedly churned (3.2-vision previews deprecated;
                # account access varies), and a baked-in guess would silently
                # rot. Empty/unset means the vision fallback (registration.py)
                # stays disabled -- set explicitly once a vision model is
                # confirmed available for this account (see vision_fallback.py).
                groq_vision_model=_get_optional("GROQ_VISION_MODEL"),
                llm_provider=_get("GROWTHRADAR_LLM_PROVIDER", "auto"),  # type: ignore[arg-type]
                request_timeout=float(_get("GROWTHRADAR_REQUEST_TIMEOUT", "10")),
                max_pages=int(_get("GROWTHRADAR_MAX_PAGES", "5")),
                crawl_delay=float(_get("GROWTHRADAR_CRAWL_DELAY", "0.5")),
                session_deadline_seconds=float(_get("GROWTHRADAR_SESSION_DEADLINE_SECONDS", "160")),
                user_agent=_get("GROWTHRADAR_USER_AGENT", "GrowthRadarBot/0.1"),
                db_path=_get("GROWTHRADAR_DB_PATH", "growthradar.db"),
                log_level=_get("GROWTHRADAR_LOG_LEVEL", "INFO").upper(),
                registrant_country=_get("GROWTHRADAR_COUNTRY", "United States"),
                registrant_company=_get_optional("GROWTHRADAR_COMPANY"),
                registrant_email=_get_optional("GROWTHRADAR_EMAIL"),
                registrant_first_name=_get_optional("GROWTHRADAR_FIRST_NAME"),
                registrant_last_name=_get_optional("GROWTHRADAR_LAST_NAME"),
                gmail_address=_get_optional("GROWTHRADAR_GMAIL_ADDRESS"),
                gmail_app_password=_get_app_password("GROWTHRADAR_GMAIL_APP_PASSWORD"),
                google_profile_dir=_get_optional("GROWTHRADAR_GOOGLE_PROFILE_DIR"),
                allow_google_oauth=_get("GROWTHRADAR_ALLOW_GOOGLE_OAUTH", "false").lower()
                in ("1", "true", "yes"),
                hot_threshold=float(_get("GROWTHRADAR_HOT_THRESHOLD", "70")),
                warm_threshold=float(_get("GROWTHRADAR_WARM_THRESHOLD", "40")),
                weight_icp_fit=float(_get("GROWTHRADAR_WEIGHT_ICP_FIT", "0.30")),
                weight_onboarding_opportunity=float(
                    _get("GROWTHRADAR_WEIGHT_ONBOARDING_OPPORTUNITY", "0.45")
                ),
                weight_product_experience=float(
                    _get("GROWTHRADAR_WEIGHT_PRODUCT_EXPERIENCE", "0.25")
                ),
            )
        except ValueError as exc:
            raise ConfigError(f"Invalid environment configuration: {exc}") from exc
