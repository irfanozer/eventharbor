import pytest
from pydantic import ValidationError

from eventharbor.config import DatabaseSSLMode, Environment, Settings, get_settings


def test_settings_use_safe_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVENTHARBOR_ENVIRONMENT", raising=False)
    monkeypatch.delenv("EVENTHARBOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("EVENTHARBOR_DATABASE_ECHO", raising=False)
    monkeypatch.delenv("EVENTHARBOR_RECEIVER_LAB_URL", raising=False)
    monkeypatch.delenv("EVENTHARBOR_RECEIVER_LAB_CONTROL_URL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.environment is Environment.LOCAL
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.database_echo is False
    assert settings.database_ssl_mode is DatabaseSSLMode.DISABLE
    assert settings.retention_days == 7
    assert settings.retention_batch_size == 500
    assert settings.retention_max_events_per_run == 5_000
    assert settings.receiver_lab_url == "http://receiver-lab:8100/webhooks"
    assert settings.receiver_lab_control_url == "http://receiver-lab:8100"


def test_settings_read_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENTHARBOR_ENVIRONMENT", "test")
    monkeypatch.setenv("EVENTHARBOR_DATABASE_POOL_SIZE", "7")
    monkeypatch.setenv("EVENTHARBOR_RECEIVER_LAB_CONTROL_URL", "http://receiver.internal:9000")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.environment is Environment.TEST
    assert settings.database_pool_size == 7
    assert settings.receiver_lab_control_url == "http://receiver.internal:9000"
    assert get_settings() is settings
    get_settings.cache_clear()


def test_settings_reject_invalid_pool_configuration() -> None:
    with pytest.raises(ValidationError):
        Settings(database_pool_size=0, _env_file=None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retention_days", 0),
        ("retention_days", 366),
        ("retention_batch_size", 0),
        ("retention_batch_size", 5_001),
        ("retention_max_events_per_run", 0),
        ("retention_max_events_per_run", 100_001),
    ],
)
def test_settings_reject_invalid_retention_bounds(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_production_settings_accept_remote_tls_database() -> None:
    settings = Settings(
        environment="production",
        database_url=(
            "postgresql+asyncpg://eventharbor_app:strong-password@"
            "eventharbor.postgres.database.azure.com/eventharbor"
        ),
        database_ssl_mode="require",
        _env_file=None,
    )

    assert settings.environment is Environment.PRODUCTION
    assert settings.database_ssl_mode is DatabaseSSLMode.REQUIRE


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "database_url": "postgresql+asyncpg://app:password@localhost/eventharbor",
                "database_ssl_mode": "require",
            },
            "non-local database host",
        ),
        (
            {
                "database_url": (
                    "postgresql+asyncpg://app:password@"
                    "eventharbor.postgres.database.azure.com/eventharbor"
                ),
                "database_ssl_mode": "disable",
            },
            "must require TLS",
        ),
        (
            {
                "database_url": (
                    "postgresql+asyncpg://app:password@"
                    "eventharbor.postgres.database.azure.com/eventharbor"
                ),
                "database_ssl_mode": "require",
                "database_echo": True,
            },
            "database_echo must be disabled",
        ),
    ],
)
def test_production_settings_reject_unsafe_database_configuration(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(environment="production", _env_file=None, **overrides)
