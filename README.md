# EventHarbor

**Accept an event once. Deliver it reliably. Explain every failure.**

EventHarbor is a production-minded webhook delivery platform for systems whose
destinations may be slow, rate-limited, unavailable, or broken. It durably
accepts events, signs outbound requests, records every delivery attempt,
retries transient failures, dead-letters exhausted deliveries, and preserves a
complete replay history.

> Project status: foundation. The reliability contract and initial domain code
> are implemented. Persistence, delivery workers, and the dashboard are being
> built in public; no unimplemented capability is presented as complete.

## Signature demonstration

The public demo is designed around one inspectable failure-and-recovery story:

```text
Publish event
  -> receiver returns 503
  -> attempts and retry timing appear in the timeline
  -> delivery reaches the dead-letter queue
  -> receiver is repaired
  -> an operator approves replay
  -> the signed delivery succeeds
```

The receiver laboratory makes each failure deterministic, so the same scenario
can be reproduced in a browser, locally, and in CI.

## Reliability contract

EventHarbor promises **at-least-once delivery**, not exactly-once HTTP delivery.
A worker can crash after the destination accepts a request but before success is
recorded, creating an unavoidable duplicate-delivery window. Stable event IDs
allow consumers to deduplicate safely.

The core guarantees and limitations are documented in
[`docs/reliability-contract.md`](docs/reliability-contract.md).

## Planned architecture

- Python and FastAPI for the API, delivery worker, and receiver laboratory
- PostgreSQL as the authoritative store and initial durable scheduler
- React and TypeScript for the operator dashboard
- HTTPX for outbound webhook delivery
- OpenTelemetry for correlated logs, metrics, and traces
- Docker Compose for local development
- Azure Container Apps, Azure Database for PostgreSQL, and Key Vault in production
- Terraform for reproducible cloud infrastructure

The initial system is a modular monolith with independent API and worker
processes. It deliberately avoids Kafka, Kubernetes, and premature
microservices. See [`docs/architecture.md`](docs/architecture.md).

## Local foundation

Prerequisites:

- Docker Desktop with Docker Compose, or Python 3.12+

Start PostgreSQL, the API, and the deterministic receiver laboratory:

```bash
docker compose up --build
```

Then visit:

- API health: <http://localhost:8000/health>
- API documentation: <http://localhost:8000/docs>
- Receiver Lab health: <http://localhost:8100/health>
- Receiver Lab documentation: <http://localhost:8100/docs>

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
frontend/                React dashboard (next milestone)
docs/                    Product, architecture, guarantees, and decisions
docs/adr/                Architecture decision records
infra/                   Azure and local infrastructure (later milestone)
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
