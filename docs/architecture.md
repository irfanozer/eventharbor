# Architecture

## Initial topology

```text
Browser -----> Nginx frontend :3000
                  |       |
          static SPA      +---- /api/* ----> FastAPI API :8000
                                               |        |
Publisher -------------------------------------+        +----> Receiver Lab control
                                               |
                                               v
                                           PostgreSQL
                                               ^
                                               |
                                        Delivery worker ----> Receiver Lab webhook
                                               |
                                               +---- persisted attempt evidence
```

The Nginx frontend serves the React single-page application and proxies its
relative `/api` requests to FastAPI. The same path is provided by Vite during
frontend development. FastAPI does not require a permissive cross-origin
policy. The API and worker are separate process modes built from the same
backend artifact. PostgreSQL is the source of truth and initial durable scheduler.
Workers claim due delivery rows with short leases and `FOR UPDATE SKIP LOCKED`,
reserve an `in_progress` attempt in the same transaction, then perform network
I/O without holding the row lock. The result resolves that reserved row and the
delivery state together. If the lease expires first, recovery marks the row
`indeterminate` before reserving another attempt or dead-lettering.

## Module boundaries

- `identity`: API keys, actors, and workspace context
- `endpoints`: destinations, subscriptions, verification, secret rotation
- `events`: idempotent ingestion and immutable payloads
- `deliveries`: state machine, leases, retries, dead letters, replay
- `transport`: signing, outbound HTTP, timeout and response classification
- `receiver_lab`: reproducible destination behavior for demos and tests
- `observability`: structured logs, metrics, traces, and audit entries
- `diagnostics`: deterministic rules and the later read-only investigator

Modules expose services and repositories rather than reaching into another
module's persistence implementation.

## Implemented vertical-slice data model

- `endpoints`
- `events`
- `deliveries`
- `delivery_attempts`

The schema enforces `(source, idempotency_key)`,
`(event_id, endpoint_id, replay_generation)`, and
`(delivery_id, attempt_number)` uniqueness. Exact canonical outbound bytes are
stored alongside queryable JSONB so retries never re-serialize a payload. Each
event stores two deliberately separate SHA-256 values:

- `request_fingerprint_sha256` hashes the endpoint, event type, and data used
  to decide whether an `Idempotency-Key` retry is the same request.
- `payload_sha256` hashes the exact `payload_bytes` sent to the destination,
  so the persisted outbound body can be verified byte for byte.

Every committed delivery claim has exactly one attempt row carrying the same
lease token and attempt number. Attempt identity is append-only; its lifecycle
moves once from `in_progress` to either `completed` or `indeterminate`.

Manual replay is an explicit API mutation, not a state reset. The API locks the
stable event row, verifies that the requested source is the latest delivery
generation and is `dead_lettered`, and appends a new `pending` delivery with the
next `replay_generation`. Its attempts begin again at one while the immutable
event, source delivery, and all source attempt evidence remain unchanged. A
caller-supplied `Idempotency-Key` makes retries of the replay request safe. The
event lock, replay metadata constraints, and a partial unique index over active
event-endpoint deliveries prevent concurrent replay requests from creating two
active generations.

## Control Room read model

The Control Room uses bounded, cursor-paginated query routes for events,
endpoints, and actionable dead letters. Event lists report the latest delivery
generation; event detail preserves and returns every generation. A dead letter
drops out of the actionable list only after a newer replay generation exists.

Receiver Lab is controlled through a narrow FastAPI facade that accepts named
presets. The browser cannot submit an arbitrary receiver URL or raw failure
configuration. Endpoint query responses omit signing secrets, and the browser
does not retain the one-time secret returned during local endpoint creation.

## Azure public-demo topology

The prepared cloud deployment keeps the existing same-origin boundary while
replacing Docker Compose service networking with Azure Container Apps service
discovery:

```text
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
```

Only the web app has external ingress. PostgreSQL uses a delegated subnet and
private DNS, API readiness includes a bounded database query, and every deployed
database process requires TLS. The web/API/Receiver Lab can scale to zero in the
cost-focused release; the polling worker remains at one replica because it has no
external scaling signal. Receiver Lab is capped at one replica because its
short-lived scenario state is deliberately in process memory.

See [`deployment.md`](deployment.md) and [`operations.md`](operations.md).

See [`control-room.md`](control-room.md) for browser routes, API routes, and the
guided failure-repair-replay sequence.

## Planned expansion

- `workspaces`
- `api_keys` (hashed; plaintext shown once)
- `sources`
- `endpoint_secrets` (encrypted and versioned)
- `subscriptions`
- `audit_entries`
- `diagnostic_runs`

Important uniqueness constraints include `(source_id, idempotency_key)` and
`(event_id, endpoint_id, replay_generation)`.

## Evolution boundary

An external queue is not required for the first credible version. If observed
load or operational requirements justify Azure Service Bus later, the worker
queue interface will send delivery identifiers only; PostgreSQL will remain
authoritative. This makes the architectural evolution measurable instead of
decorative.
