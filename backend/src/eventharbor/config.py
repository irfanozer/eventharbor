"""Validated process configuration loaded from EventHarbor environment variables."""

from enum import StrEnum
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Runtime environments with intentionally explicit names."""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class DatabaseSSLMode(StrEnum):
    """TLS policies understood by asyncpg."""

    DISABLE = "disable"
    REQUIRE = "require"
    VERIFY_FULL = "verify-full"


class Settings(BaseSettings):
    """EventHarbor configuration sourced from ``EVENTHARBOR_*`` variables."""

    model_config = SettingsConfigDict(
        env_prefix="EVENTHARBOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.LOCAL
    database_url: str = "postgresql+asyncpg://eventharbor:eventharbor@localhost:5432/eventharbor"
    database_echo: bool = False
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    database_ssl_mode: DatabaseSSLMode = DatabaseSSLMode.DISABLE
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    retention_days: int = Field(default=7, ge=1, le=365)
    retention_batch_size: int = Field(default=500, ge=1, le=5_000)
    retention_max_events_per_run: int = Field(default=5_000, ge=1, le=100_000)
    receiver_lab_url: str = "http://receiver-lab:8100/webhooks"
    receiver_lab_control_url: str = "http://receiver-lab:8100"
    worker_poll_interval_seconds: float = Field(default=0.5, gt=0, le=60)
    worker_http_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    worker_lease_seconds: float = Field(default=30.0, gt=0, le=600)
    worker_max_attempts: int = Field(default=8, ge=1, le=100)
    worker_base_delay_seconds: float = Field(default=10.0, gt=0, le=3_600)
    worker_max_delay_seconds: float = Field(default=3_600.0, gt=0, le=86_400)
    worker_retry_after_cap_seconds: float = Field(default=3_600.0, gt=0, le=86_400)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_runtime_invariants(self) -> "Settings":
        """Validate cross-field reliability and production-safety invariants."""

        if self.worker_lease_seconds <= self.worker_http_timeout_seconds:
            raise ValueError("worker_lease_seconds must exceed worker_http_timeout_seconds")
        if self.worker_max_delay_seconds < self.worker_base_delay_seconds:
            raise ValueError("worker_max_delay_seconds must be at least worker_base_delay_seconds")
        if self.environment is Environment.PRODUCTION:
            database = urlsplit(self.database_url)
            if database.scheme != "postgresql+asyncpg":
                raise ValueError("production database_url must use postgresql+asyncpg")
            if database.hostname in {None, "localhost", "127.0.0.1", "postgres"}:
                raise ValueError("production database_url must use a non-local database host")
            if database.username == "eventharbor" and database.password == "eventharbor":
                raise ValueError("production database_url must not use the local demo credentials")
            if self.database_ssl_mode is DatabaseSSLMode.DISABLE:
                raise ValueError("production database connections must require TLS")
            if self.database_echo:
                raise ValueError("database_echo must be disabled in production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings object per process."""

    return Settings()
