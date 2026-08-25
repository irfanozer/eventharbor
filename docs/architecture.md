# Architecture

EventHarbor is a modular monolith with separate API, delivery-worker, and
Receiver Lab processes. PostgreSQL is the authoritative store for events,
delivery state, retry schedules, and attempt evidence.

## Runtime topology

~~~text
Browser -----> Nginx frontend :3000
                  |       |
          React SPA       +---- /api/* ----> FastAPI API :8000
                                               |        |
Publisher -------------------------------------+        +----> Receiver Lab control
                                               |
                                               v
                                           PostgreSQL
                                               ^
                                               |
                                        delivery worker ----> Receiver Lab :8100
                                               |
                                               +---- persisted attempt evidence
~~~

Nginx serves the React single-page application and proxies relative `/api`
requests to FastAPI. Vite provides the same proxy during frontend development,
so the API does not require a permissive browser cross-origin policy.

The API and worker run from the same backend artifact but have independent
entry points. Receiver Lab runs as a separate FastAPI application and acts as a
real HTTP destination with controllable response behavior.

## Event lifecycle

### 1. Accept

`POST /v1/events` requires an `Idempotency-Key` and an existing endpoint. In
one database transaction, the API:

1. Resolves the endpoint.
2. Canonicalizes and serializes the outbound event body.
3. Inserts the immutable event.
4. Inserts its initial `pending` delivery.
5. Commits before returning `202 Accepted`.

If the same source and idempotency key already exist, the API compares the
request fingerprint. An identical request returns the existing event; a
different request returns `409 Conflict`.

### 2. Claim

The worker polls for due `pending`, `retry_wait`, or expired `in_progress`
deliveries. It selects one row with `FOR UPDATE SKIP LOCKED` so multiple workers
can claim different deliveries without waiting on one another.

In a short transaction, the worker:

1. Validates that the endpoint is enabled and the attempt budget is available.
2. Advances the delivery to `in_progress`.
3. Assigns a lease owner, token, and expiration time.
4. Increments the delivery's attempt counter.
5. Inserts a matching `in_progress` attempt row.
6. Commits before any outbound network I/O.

The worker then releases the database transaction and sends the request.

### 3. Deliver

The transport sends the persisted payload bytes with these headers:

- `X-EventHarbor-Event-Id`
- `X-EventHarbor-Delivery-Id`
- `X-EventHarbor-Event-Type`
- `X-EventHarbor-Attempt`
- `X-EventHarbor-Timestamp`
- `X-EventHarbor-Signature`

The signature is HMAC-SHA256 over `timestamp + "." + payload_bytes`. HTTP
redirects are not followed. Response-body evidence and error messages are
bounded before persistence.

### 4. Resolve

The worker opens a new transaction and locks the delivery and attempt rows. It
can finalize them only when the lease owner, lease token, and attempt number
still match its original claim.

- A `2xx` response marks the attempt completed and the delivery `delivered`.
- A retryable result records the completed attempt and moves the delivery to
  `retry_wait` with a future `next_attempt_at`.
- A terminal result or exhausted budget records the attempt and moves the
  delivery to `dead_lettered`.

The attempt result and resulting delivery state commit together.

## Crash recovery and lease fencing

The difficult failure window is after a destination may have accepted the HTTP
request but before the worker commits its result. EventHarbor handles this
without claiming certainty it does not have.

When another worker finds an expired `in_progress` lease, it locks the delivery
and matching attempt, then marks that attempt `indeterminate`. If the endpoint
is enabled and attempts remain, it creates a new fenced attempt. Otherwise it
dead-letters the delivery.

An old worker cannot commit after its lease has been replaced because
finalization requires the original owner, token, and attempt number. This
prevents stale results from overwriting newer state, but it cannot eliminate the
at-least-once duplicate window at the remote destination.

## Persistence model

The schema contains four core tables:

| Table | Responsibility |
| --- | --- |
| `endpoints` | Receiver URL, enabled state, signing secret, and secret version |
| `events` | Immutable business event, exact outbound bytes, and hashes |
| `deliveries` | Per-endpoint state, retry schedule, lease, and replay generation |
| `delivery_attempts` | Lifecycle and outcome evidence for each claimed HTTP request |

Important database constraints include:

- unique `(source, idempotency_key)` for event ingestion;
- unique `(event_id, endpoint_id, replay_generation)` for delivery history;
- unique `(delivery_id, attempt_number)` and lease token per attempt;
- one active delivery generation per event-endpoint chain;
- one `in_progress` attempt per delivery.

Each event stores:

- `request_fingerprint_sha256`, derived from the endpoint, event type, and data
  used for idempotency conflict detection; and
- `payload_sha256`, derived from the exact immutable bytes sent to the receiver.

Keeping those hashes separate prevents the transport evidence from being
confused with the ingestion identity rule.

## Replay model

Replay is an append operation, not a state reset.

`POST /v1/deliveries/{delivery_id}/replays` locks the stable event, verifies
that the requested delivery is the latest generation and is dead-lettered, and
inserts a new `pending` delivery with the next replay generation. The immutable
event and every source attempt remain unchanged.

A replay request has its own idempotency key. Database constraints and the
event lock prevent concurrent requests from creating two active generations.
Receiver evidence that proves the immutable payload itself is invalid blocks
unchanged replay; corrected data must be published as a new event.

## Backend boundaries

The implemented backend is organized by responsibility:

- `routers/v1.py`: HTTP routes and dependency wiring
- `schemas.py`: request and response contracts
- `services.py`: endpoint creation, event ingestion, replay, and query rules
- `repositories.py`: persistence operations used by application services
- `deliveries/`: state transitions, retry policy, signing, HTTP transport, and
  worker orchestration
- `control_room.py`: bounded read models for the browser
- `receiver_lab/` and `demo.py`: controlled destination behavior and its API
  facade
- `models.py` and `alembic/`: relational model and versioned migrations

Application services coordinate repository operations; the HTTP router does not
implement delivery-state transitions directly.

## Control Room read model

The browser uses bounded, cursor-paginated routes for events, endpoints,
attempts, and actionable dead letters. Event lists expose the latest delivery
generation, while event detail returns the complete generation history. A dead
letter remains actionable until a newer replay generation exists.

The Receiver Lab facade accepts named test scenarios rather than arbitrary
failure configuration. Endpoint query responses omit signing secrets, and the
browser does not retain the one-time plaintext secret returned when an endpoint
is created.

## Azure topology

The Azure Bicep deployment preserves the same-origin frontend boundary:

~~~text
Cloudflare DNS
      |
      v
public Nginx/React app :8080
      |
      +-- /api/* --> internal FastAPI app :8000 --> private PostgreSQL
                           |                         ^
                           v                         |
                  internal Receiver Lab :8100 <--- worker (no ingress)

manual migration job ----------------------------> PostgreSQL
daily bounded retention job ----------------------> PostgreSQL
~~~

Only the web application has external ingress. PostgreSQL uses a delegated
subnet and private DNS. Production database connections require TLS. The web
application, API, and Receiver Lab can scale to zero; the polling worker keeps
one replica so queued deliveries continue to move. Receiver Lab is limited to
one replica because scenario state is kept in process memory.

See the [deployment guide](deployment.md) and [operations guide](operations.md).

## Deliberate boundaries

- Endpoint registration accepts only configured Receiver Lab routes. The code
  does not expose arbitrary outbound URL registration.
- The API has no user authentication, tenant isolation, or per-publisher
  authorization.
- PostgreSQL polling is the sole dispatch mechanism.
- Receiver Lab is test infrastructure, not a durable receiver service.
- Delivery is at least once; consumers are responsible for deduplication.

The reasoning behind the modular-monolith and PostgreSQL-scheduler choices is
recorded in:

- [ADR 0001: Begin as a modular monolith](adr/0001-modular-monolith.md)
- [ADR 0002: Use PostgreSQL as the durable scheduler](adr/0002-postgresql-durable-scheduler.md)
