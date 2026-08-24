# EventHarbor

**Accept an event once. Deliver it reliably. Explain every failure.**

EventHarbor is a production-minded webhook delivery platform for systems whose
destinations may be slow, rate-limited, unavailable, or broken. It durably
accepts events, reserves attempt evidence before outbound network I/O, signs
requests, records completed or indeterminate outcomes, retries transient
failures, and dead-letters exhausted deliveries. If a worker crashes during the
indeterminate window around an HTTP request, its expired lease is resolved and
the event is redelivered while the configured attempt budget remains.

> Project status: first vertical slice complete. PostgreSQL persistence,
> idempotent ingestion, signed delivery, retry scheduling, attempt evidence,
> expired-lease crash recovery, Docker orchestration, and a real PostgreSQL
> integration test are implemented. Manual replay, workspace isolation, and the
> React Control Room remain explicit future milestones.

## Target signature demonstration

The finished public demo is designed around one inspectable
failure-and-recovery story:

```text
Publish event
  -> receiver returns 503
  -> attempts and retry timing appear in the timeline
  -> delivery reaches the dead-letter queue
  -> receiver is repaired
  -> an operator approves replay
  -> the signed delivery succeeds
```

The current vertical slice proves durable publish, signed delivery, retries,
dead-letter transitions, lease recovery, and persisted attempt evidence through
the API and automated tests. Manual replay and the browser Control Room still
need to be added before the full story above is reproducible end to end.

## Reliability contract

EventHarbor promises **at-least-once delivery**, not exactly-once HTTP delivery.
A worker can crash after the destination accepts a request but before success is
recorded, creating an unavoidable duplicate-delivery window. An expired worker
lease is reclaimed with a new fenced attempt; stable event IDs allow consumers
to deduplicate safely.

The core guarantees and limitations are documented in
[`docs/reliability-contract.md`](docs/reliability-contract.md).

## Architecture

- Python and FastAPI power the API, delivery worker, and Receiver Lab.
- PostgreSQL is the authoritative store and durable work scheduler.
- SQLAlchemy and Alembic provide typed persistence and versioned migrations.
- HTTPX sends exact, HMAC-signed webhook bytes without following redirects.
- Docker Compose starts PostgreSQL, migrations, API, worker, and Receiver Lab.
- React, OpenTelemetry, Azure, Key Vault, and Terraform are later milestones.

The initial system is a modular monolith with independent API and worker
processes. It deliberately avoids Kafka, Kubernetes, and premature
microservices. See [`docs/architecture.md`](docs/architecture.md).

## Local vertical slice

Prerequisites:

- Docker Desktop with Docker Compose, or Python 3.12+

Start PostgreSQL, apply migrations, then run the API, worker, and deterministic
Receiver Lab:

```bash
docker compose up --build
```

Then visit:

- API health: <http://localhost:8000/health>
- API documentation: <http://localhost:8000/docs>
- Receiver Lab health: <http://localhost:8100/health>
- Receiver Lab documentation: <http://localhost:8100/docs>

Follow [`docs/local-demo.md`](docs/local-demo.md) to register the local
destination, publish an idempotent event, and inspect its persisted attempt.

If port `5432` is already used by PostgreSQL on your machine, create `.env` and
set `POSTGRES_PORT=55432`. Container-to-container connections still use
`postgres:5432`.

Run the Python checks without Docker:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy src
pytest
```

## Repository map

```text
backend/                 FastAPI applications and delivery-domain code
frontend/                React Control Room placeholder (Milestone 4)
docs/                    Product, architecture, guarantees, and decisions
docs/adr/                Architecture decision records
infra/                   Azure infrastructure (later milestone)
.github/workflows/       Continuous integration
compose.yaml             Local services
```

## Delivery roadmap

1. Foundation: reliability contract, state machine, retry policy, signing, CI
2. Vertical slice: register endpoint, publish event, persist, deliver, record attempt
3. Reliability: leases, idempotency, retries, dead letters, replay, crash recovery
4. Demo: live control room, deterministic failure controls, attempt timeline
5. Security: tenant isolation, API keys, secret rotation, endpoint verification, SSRF controls
6. Operations: OpenTelemetry, dashboards, alerts, load and recovery reports
7. Cloud: Azure deployment and Terraform
8. Diagnostics: evidence-linked, read-only failure investigator with human-approved actions

Only measured results will be published. Benchmark reports will include the
commit, machine or cloud size, payload, duration, concurrency, and raw output.

## License

MIT
