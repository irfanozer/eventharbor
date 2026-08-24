"""Validated process configuration loaded from EventHarbor environment variables."""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Runtime environments with intentionally explicit names."""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


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
    receiver_lab_url: str = "http://receiver-lab:8100/webhooks"
    worker_poll_interval_seconds: float = Field(default=0.5, gt=0, le=60)
    worker_http_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    worker_lease_seconds: float = Field(default=30.0, gt=0, le=600)
    worker_max_attempts: int = Field(default=8, ge=1, le=100)
    worker_base_delay_seconds: float = Field(default=10.0, gt=0, le=3_600)
    worker_max_delay_seconds: float = Field(default=3_600.0, gt=0, le=86_400)
    worker_retry_after_cap_seconds: float = Field(default=3_600.0, gt=0, le=86_400)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def lease_must_outlive_http_request(self) -> "Settings":
        """Leave enough time to persist a result before another worker can reclaim it."""

        if self.worker_lease_seconds <= self.worker_http_timeout_seconds:
            raise ValueError("worker_lease_seconds must exceed worker_http_timeout_seconds")
        if self.worker_max_delay_seconds < self.worker_base_delay_seconds:
            raise ValueError("worker_max_delay_seconds must be at least worker_base_delay_seconds")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings object per process."""

    return Settings()
