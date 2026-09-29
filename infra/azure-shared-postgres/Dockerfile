FROM postgres:17-bookworm

RUN apt-get update \
    && apt-get install --no-install-recommends -y python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY migrate.py /app/migrate.py
COPY isolate_eventharbor.py /app/isolate_eventharbor.py
COPY economy_migrate.py /app/economy_migrate.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# This is a short-lived client job, never a PostgreSQL server.
ENTRYPOINT ["python3", "/app/migrate.py"]
CMD ["Inspect"]
