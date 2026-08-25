# EventHarbor

**Accept an event once. Deliver it reliably. Preserve the evidence.**

EventHarbor is a webhook delivery service built around an explicit
at-least-once reliability contract. It accepts events into PostgreSQL before
responding, delivers them asynchronously, signs the exact outbound bytes, and
keeps an attempt-by-attempt record of retries, failures, and recovery.

The repository includes a React Control Room and a separate Receiver Lab
service. Together they make the delivery lifecycle observable without replacing
the underlying API calls, database transactions, worker claims, or HTTP
requests with a browser-only simulation.

## What is implemented

- Durable event acceptance and delivery creation in one PostgreSQL transaction
- Idempotent event publication with conflict detection
- PostgreSQL-backed work claiming with short, fenced leases
- HMAC-SHA256 signatures over the timestamp and exact JSON body
- Bounded retries with full jitter and capped `Retry-After` support
- Durable attempt evidence created before outbound network I/O
- Recovery of expired worker leases with an explicit `indeterminate` outcome
- Dead-lettering after permanent failure or an exhausted attempt budget
- Idempotent, operator-approved replay that preserves the original failure
- Cursor-paginated event, endpoint, attempt, and dead-letter queries
- Reproducible Receiver Lab scenarios for `503`, `429`, terminal `400`, and
  successful delivery
- Docker Compose development environment, automated tests, Azure Bicep
  templates, and GitHub Actions workflows

## See one delivery from acceptance to recovery

Start the stack and open <http://localhost:3000>. The Control Room lets you send
a business event to a Receiver Lab route and follow the same event through:

~~~text
Browser
  -> EventHarbor API accepts and stores the event
  -> PostgreSQL makes the delivery durable
  -> worker claims the delivery and sends a signed HTTP request
  -> Receiver Lab returns the selected response
  -> EventHarbor records the outcome and retries or stops
~~~

For the unavailable-receiver scenario, the failed delivery remains
dead-lettered after its attempt budget is exhausted. Bringing the Receiver Lab
route online and approving a replay creates a new delivery generation. It does
not erase or relabel the original attempts.

See [the local demo guide](docs/local-demo.md) for the complete walkthrough.

## Reliability contract

EventHarbor provides **at-least-once delivery**, not exactly-once HTTP delivery.
If a destination accepts a request and the worker exits before recording the
response, EventHarbor cannot know whether the remote side committed its work.
Once the lease expires, that attempt is marked `indeterminate` and the stable
event may be delivered again while attempts remain.

Webhook consumers must therefore deduplicate using
`X-EventHarbor-Event-Id`. The precise guarantees, retry rules, state
transitions, and replay constraints are documented in the
[reliability contract](docs/reliability-contract.md).

## Architecture

- **FastAPI** exposes endpoint registration, event publication, replay, query,
  health, and Receiver Lab control routes.
- **PostgreSQL** is both the source of truth and durable scheduler.
- **SQLAlchemy** and **Alembic** provide asynchronous persistence and versioned
  schema migrations.
- A separate **Python delivery worker** claims due rows with
  `FOR UPDATE SKIP LOCKED` and performs outbound HTTP outside the database
  transaction.
- **HTTPX** sends signed requests without following redirects.
- **React**, **TypeScript**, **Vite**, and **TanStack Query** power the Control
  Room.
- **Nginx** serves the production frontend and proxies same-origin `/api`
  traffic to FastAPI.

The backend is a modular monolith with independently runnable API and worker
processes. See [the architecture guide](docs/architecture.md) and the accepted
[architecture decisions](docs/adr/).

## Quick start

### Prerequisites

- Docker Desktop with Docker Compose

### Run the complete system

~~~bash
docker compose up --build
~~~

Open:

- Control Room: <http://localhost:3000>
- API health: <http://localhost:8000/health>
- API documentation: <http://localhost:8000/docs>
- Receiver Lab health: <http://localhost:8100/health>
- Receiver Lab documentation: <http://localhost:8100/docs>

The migration container applies the schema before the API and worker start.
PostgreSQL data is retained in a named Docker volume.

If port `5432` is already occupied, create a `.env` file in the repository
root with:

~~~dotenv
POSTGRES_PORT=55432
~~~

Container-to-container traffic continues to use `postgres:5432`.

Stop the stack with:

~~~bash
docker compose down
~~~

Use `docker compose down -v` only when you also intend to delete the local
PostgreSQL volume.

## Development and tests

The backend requires Python 3.12 or later:

~~~bash
cd backend
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy src
pytest
~~~

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1`. PostgreSQL integration tests run when
`EVENTHARBOR_TEST_DATABASE_URL` points to a dedicated database whose name ends
in `_test`; otherwise those tests are skipped.

The frontend requires the Node.js version used by CI (currently Node 24):

~~~bash
cd frontend
npm ci
npm run typecheck
npm test
npm run test:runtime-config
npm run build
~~~

For frontend hot reload, leave the backend services running, run `npm run dev`,
and open <http://localhost:5173>.

## Deployment and operations

The Azure deployment keeps only the Nginx/React application publicly reachable.
The API and Receiver Lab use internal Container Apps ingress, the worker has no
ingress, and PostgreSQL is attached through private networking.

Follow:

- [Azure deployment](docs/deployment.md)
- [Operations, health checks, rollback, and teardown](docs/operations.md)

Cloning, building, or running the local stack does not create Azure resources.

## Current limitations

- The API does not implement user authentication, tenant isolation, or
  per-publisher authorization.
- Endpoint registration is intentionally restricted to Receiver Lab routes;
  this is not a general-purpose arbitrary-URL webhook service.
- Endpoint signing secrets are stored by the application but are not
  application-encrypted or rotatable through the API.
- Receiver Lab scenario state is held in process memory and is intended for
  demonstrations and integration tests.
- PostgreSQL polling is the only work-dispatch mechanism.
- Delivery is at least once; duplicate HTTP requests are possible after an
  indeterminate outcome.

These boundaries keep the repository's claims aligned with the behavior that
can be run and tested today.

## Repository layout

~~~text
backend/                 FastAPI applications, worker, migrations, and tests
frontend/                React Control Room, tests, and Nginx configuration
docs/                    Architecture, reliability, deployment, and operations
docs/adr/                Accepted architecture decision records
infra/azure/             Azure Bicep modules
scripts/azure/           Azure setup and deployment helpers
.github/workflows/       Continuous integration and release workflows
compose.yaml             Complete local environment
~~~

## Contributing

Bug reports and focused pull requests are welcome. Read
[CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change.

## License

MIT
