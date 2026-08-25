# EventHarbor

**Accept an event once. Deliver it reliably. Explain every failure.**

EventHarbor is a production-minded webhook delivery platform for systems whose
destinations may be slow, rate-limited, unavailable, or broken. It durably
accepts events, reserves attempt evidence before outbound network I/O, signs
requests, records completed or indeterminate outcomes, retries transient
failures, and dead-letters exhausted deliveries. If a worker crashes during the
indeterminate window around an HTTP request, its expired lease is resolved and
the event is redelivered while the configured attempt budget remains.

> Project status: reliability engine and recruiter demo complete. PostgreSQL
> persistence, idempotent ingestion, signed delivery, retry scheduling, attempt
> evidence, expired-lease crash recovery, and operator-approved replay are
> visible through the React Control Room. Workspace isolation, production
> security hardening, observability, and cloud delivery remain future milestones.

## Target signature demonstration

The Control Room is designed around one inspectable failure-and-recovery story:

```text
Publish event
  -> receiver returns 503
  -> attempts and retry timing appear in the timeline
  -> automatic sending stops and the event remains saved (dead-lettered)
  -> the test receiver is brought online
  -> an operator approves replay
  -> the signed delivery succeeds
```

The demo uses real API mutations, worker attempts, and PostgreSQL state. The
failed generation remains visible after replay, so the success does not erase
the evidence that preceded it.

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
- React, TypeScript, and Vite power the browser Control Room.
- Nginx serves the production frontend and proxies same-origin `/api` requests.
- Docker Compose starts PostgreSQL, migrations, API, worker, Receiver Lab, and
  the frontend.
- OpenTelemetry, Azure, Key Vault, and Terraform are later milestones.

The initial system is a modular monolith with independent API and worker
processes. It deliberately avoids Kafka, Kubernetes, and premature
microservices. See [`docs/architecture.md`](docs/architecture.md).

## Run the complete local demo

Prerequisites:

- Docker Desktop with Docker Compose, or Python 3.12+

Start the complete stack:

```bash
docker compose up --build
```

Then visit:

- Control Room: <http://localhost:3000>
- API health: <http://localhost:8000/health>
- API documentation: <http://localhost:8000/docs>
- Receiver Lab health: <http://localhost:8100/health>
- Receiver Lab documentation: <http://localhost:8100/docs>

In the Control Room, choose a business event and receiver behavior, then start a
guided or manual run. In manual mode, watch the real attempts stop and remain
saved, choose **Restore test receiver health**, then choose **Approve and replay**.
The original evidence remains failed while the recovery delivery succeeds. The
full path normally takes less than 90 seconds with the intentionally accelerated
local retry policy.

Follow [`docs/local-demo.md`](docs/local-demo.md) for the walkthrough and
troubleshooting. [`docs/control-room.md`](docs/control-room.md) explains the UI,
routes, API contract, retry policy, and delivery shape.

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

Run the frontend checks:

```bash
cd frontend
npm ci
npm run typecheck
npm test
npm run build
```

For Vite development with hot reload, leave the backend services running and
use `npm run dev`, then open <http://localhost:5173>. Both Vite and Nginx proxy
relative `/api` requests to FastAPI.

## Repository map

```text
backend/                 FastAPI applications and delivery-domain code
frontend/                React Control Room and Nginx container
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
4. Demo: live control room, deterministic failure controls, attempt timeline (complete)
5. Security: tenant isolation, API keys, secret rotation, endpoint verification, SSRF controls
6. Operations: OpenTelemetry, dashboards, alerts, load and recovery reports
7. Cloud: Azure deployment and Terraform
8. Diagnostics: evidence-linked, read-only failure investigator with human-approved actions

Only measured results will be published. Benchmark reports will include the
commit, machine or cloud size, payload, duration, concurrency, and raw output.

## License

MIT
