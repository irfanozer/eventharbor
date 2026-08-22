# Architecture

## Initial topology

```text
Publisher
    |
    v
FastAPI API -----> PostgreSQL <----- Delivery worker
                       |                    |
                       |                    v
                       |             Receiver Lab / verified endpoint
                       |                    |
                       +<---- attempt ------+

React dashboard -----> FastAPI query APIs
```

The API and worker are separate process modes built from the same backend
artifact. PostgreSQL is the source of truth and initial durable scheduler.
Workers claim due delivery rows with short leases and `FOR UPDATE SKIP LOCKED`,
perform network I/O outside the claim transaction, then append an immutable
attempt and update delivery state.

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

## Planned data model

- `workspaces`
- `api_keys` (hashed; plaintext shown once)
- `sources`
- `endpoints`
- `endpoint_secrets` (encrypted and versioned)
- `subscriptions`
- `events`
- `deliveries`
- `delivery_attempts`
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
