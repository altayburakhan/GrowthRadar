import pytest

from growthradar.config import Config, ConfigError


def _clear_growthradar_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("GROWTHRADAR_") or key in {"GROQ_API_KEY", "GROQ_MODEL"}:
            monkeypatch.delenv(key, raising=False)


def test_from_env_uses_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_growthradar_env(monkeypatch)

    config = Config.from_env(env_path="/nonexistent/.env")

    assert config.llm_provider == "auto"
    assert config.max_pages == 8
    assert config.resolve_provider() == "heuristic"
    assert config.registrant_country == "United States"
    assert config.registrant_company is None
    assert config.registrant_email is None


def test_from_env_respects_country_and_company_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_growthradar_env(monkeypatch)
    monkeypatch.setenv("GROWTHRADAR_COUNTRY", "Turkey")
    monkeypatch.setenv("GROWTHRADAR_COMPANY", "Acme Analytics")
    monkeypatch.setenv("GROWTHRADAR_EMAIL", "levent@userguidingnow.com")

    config = Config.from_env(env_path="/nonexistent/.env")

    assert config.registrant_country == "Turkey"
    assert config.registrant_company == "Acme Analytics"
    assert config.registrant_email == "levent@userguidingnow.com"


def test_resolve_provider_uses_groq_when_key_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_growthradar_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")

    config = Config.from_env(env_path="/nonexistent/.env")

    assert config.resolve_provider() == "groq"


def test_invalid_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_growthradar_env(monkeypatch)
    monkeypatch.setenv("GROWTHRADAR_LLM_PROVIDER", "not-a-provider")

    with pytest.raises(ConfigError):
        Config.from_env(env_path="/nonexistent/.env")


def test_weights_must_sum_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_growthradar_env(monkeypatch)
    monkeypatch.setenv("GROWTHRADAR_WEIGHT_ICP_FIT", "0.5")
    monkeypatch.setenv("GROWTHRADAR_WEIGHT_ONBOARDING_OPPORTUNITY", "0.5")
    monkeypatch.setenv("GROWTHRADAR_WEIGHT_PRODUCT_EXPERIENCE", "0.5")

    with pytest.raises(ConfigError):
        Config.from_env(env_path="/nonexistent/.env")
