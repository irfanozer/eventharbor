# Local demo guide

The local stack runs the same request path shown in the Control Room:

```text
Browser -> EventHarbor API -> PostgreSQL -> delivery worker -> Receiver Lab
```

Receiver Lab is a separate HTTP service with deterministic failure modes. It
provides a safe destination for exercising retries, rate limits, permanent
failures, dead-lettering, and replay without calling an external system.

## Prerequisites

- Docker Desktop with the Docker Compose plugin
- Node.js 24+ only when running the frontend development server separately

## Start the complete stack

From the repository root, run:

```powershell
docker compose up --build
```

The stack starts PostgreSQL, applies Alembic migrations, and then starts the API,
worker, Receiver Lab, and frontend. Leave this terminal open. In another
terminal, confirm that the long-running services are healthy and `migrate` exited
with code `0`:

```powershell
docker compose ps
```

Open the Control Room at <http://localhost:3000>.

## Run a scenario

1. Choose a receiver test case.
2. Choose an event contract: `order.paid`, `shipment.dispatched`, or
   `inventory.threshold_reached`.
3. Edit the event fields if desired.
4. Select **Send this event and watch it move**.
5. Follow the highlighted route and response history until the delivery reaches
   a terminal state.

Guided mode completes the outage-recovery sequence automatically after the
original delivery stops. Manual mode pauses at that boundary so you can bring
the isolated receiver online and explicitly approve the replay.

### Test cases

| Test case | Receiver responses | Expected result |
| --- | --- | --- |
| Receiver remains unavailable | `503` four times, then an approved replay receives `200` | Automatic retries stop at the configured limit. The original delivery and its attempts remain saved; replay creates a separate successful delivery. |
| Receiver recovers briefly | `503`, `503`, `200` | The original delivery succeeds automatically during its retry window. |
| Receiver asks us to slow down | `429`, `429`, `200` with `Retry-After: 2` | The worker honors the receiver's delay before retrying. |
| Receiver rejects invalid data | One `400` identifying the omitted required field | The permanent error is saved immediately without unnecessary retries. |

The local retry delays are intentionally short. They make each scenario finish
quickly without changing the delivery state machine.

## Inspect the evidence

The Control Room combines several independent views of the same delivery:

- The route highlights whether the event is in the API, PostgreSQL, worker, or
  Receiver Lab stage.
- The response history is built from durable PostgreSQL delivery-attempt rows.
- The latest HTTP exchange shows the destination, request number, response,
  duration, and Receiver Lab receipt.
- Matching event IDs, delivery IDs, attempt numbers, and body hashes correlate
  EventHarbor's record with what Receiver Lab observed.

Use the top navigation for additional detail:

- **Events** lists accepted events and their latest delivery state.
- **Stopped deliveries** lists deliveries that exhausted retries or encountered
  a permanent failure. The API stores this state as `dead_lettered`.
- **Endpoints** lists the configured Receiver Lab destinations without exposing
  signing secrets.

PostgreSQL data is stored in a named Docker volume, so evidence remains available
after stopping and restarting the stack.

## Service URLs

| Service | URL |
| --- | --- |
| Control Room | <http://localhost:3000> |
| EventHarbor API health | <http://localhost:8000/health> |
| EventHarbor API documentation | <http://localhost:8000/docs> |
| Receiver Lab health | <http://localhost:8100/health> |
| Receiver Lab API documentation | <http://localhost:8100/docs> |

The browser uses relative `/api` requests. In the complete stack, Nginx forwards
them to the API container, so no separate browser-facing API configuration is
required.

## Frontend development with hot reload

Start the backend services in one terminal:

```powershell
docker compose up postgres migrate api worker receiver-lab
```

Start Vite in a second terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api/*` to
`http://localhost:8000/*`, matching the Nginx route used by the complete stack.

The Compose API and Receiver Lab services also reload when files under
`backend/src` change. Restart the worker after editing worker code:

```powershell
docker compose restart worker
```

## Troubleshooting

### Docker engine is unavailable

If the error mentions `dockerDesktopLinuxEngine` or a missing named pipe, start
Docker Desktop and wait until its engine reports that it is running.

### A local port is already in use

Copy `.env.example` to `.env` and override the conflicting host port:

```dotenv
POSTGRES_PORT=55432
FRONTEND_PORT=3001
```

Container-to-container database traffic still uses `postgres:5432`. If you
change `FRONTEND_PORT`, open the corresponding localhost port in the browser.

### A service is unhealthy or an event does not progress

Inspect service state and recent output:

```powershell
docker compose ps
docker compose logs --tail 200 migrate postgres api worker receiver-lab frontend
```

Follow the API, worker, and Receiver Lab while running another scenario:

```powershell
docker compose logs -f api worker receiver-lab
```

If source or dependency changes are not reflected, rebuild and recreate the
containers:

```powershell
docker compose up --build --force-recreate
```

## Stop or reset

Stop the stack while preserving PostgreSQL data:

```powershell
docker compose down
```

To start again, run `docker compose up --build`.

To remove all local EventHarbor database data and start from an empty database,
delete the named volumes as well:

```powershell
docker compose down --volumes
```

The final command permanently deletes local events, deliveries, attempts, and
endpoint registrations stored in the Compose volume.
