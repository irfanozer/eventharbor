FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN python -m pip install --no-cache-dir .

COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

RUN groupadd --gid 10001 eventharbor \
    && useradd --uid 10001 --gid eventharbor --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin eventharbor

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "eventharbor.api:app", "--host", "0.0.0.0", "--port", "8000"]
