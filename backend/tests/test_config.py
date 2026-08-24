import pytest
from pydantic import ValidationError

from eventharbor.config import Environment, Settings, get_settings


def test_settings_use_safe_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVENTHARBOR_ENVIRONMENT", raising=False)
    monkeypatch.delenv("EVENTHARBOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("EVENTHARBOR_DATABASE_ECHO", raising=False)
    monkeypatch.delenv("EVENTHARBOR_RECEIVER_LAB_URL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.environment is Environment.LOCAL
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.database_echo is False
    assert settings.receiver_lab_url == "http://receiver-lab:8100/webhooks"


def test_settings_read_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENTHARBOR_ENVIRONMENT", "test")
    monkeypatch.setenv("EVENTHARBOR_DATABASE_POOL_SIZE", "7")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.environment is Environment.TEST
    assert settings.database_pool_size == 7
    assert get_settings() is settings
    get_settings.cache_clear()


def test_settings_reject_invalid_pool_configuration() -> None:
    with pytest.raises(ValidationError):
        Settings(database_pool_size=0, _env_file=None)
