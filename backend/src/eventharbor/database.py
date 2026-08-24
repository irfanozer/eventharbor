"""Asynchronous SQLAlchemy engine, sessions, and shared ORM metadata."""

from collections.abc import AsyncIterator

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from eventharbor.config import Settings, get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all EventHarbor ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build the process-wide asynchronous database engine."""

    resolved = settings or get_settings()
    return create_async_engine(
        resolved.database_url,
        echo=resolved.database_echo,
        pool_pre_ping=True,
        pool_size=resolved.database_pool_size,
        max_overflow=resolved.database_max_overflow,
        pool_timeout=resolved.database_pool_timeout_seconds,
    )


engine = build_engine()
session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a short-lived session for one API request or worker operation."""

    async with session_factory() as session:
        yield session
