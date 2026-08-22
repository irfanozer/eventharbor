# Reliability contract

## Delivery semantics

EventHarbor provides **at-least-once** outbound HTTP delivery.

An event is acknowledged with `202 Accepted` only after the event and its
initial delivery records commit successfully. The platform does not claim
exactly-once delivery: if a destination accepts a request and the worker crashes
before recording success, the lease will expire and a later worker may send the
same stable event ID again.

Consumers must deduplicate by event ID.

## Idempotent ingestion

- A publisher supplies an `Idempotency-Key`.
- Reusing the key with the same canonical payload returns the existing event.
- Reusing the key with a different payload hash returns `409 Conflict`.
- The event and its initial delivery rows are created in one transaction.

## State lifecycle

```text
pending -> in_progress -> delivered
                       -> retry_wait -> in_progress
                       -> dead_lettered
```

Delivered and dead-lettered states are terminal. Replay creates a new delivery
generation linked to the original; it does not rewrite historical attempts.

## Retry classification

- Success: `2xx`
- Transient: connection failures, timeouts, `408`, `425`, `429`, and `5xx`
- Normally permanent: other `4xx`
- `Retry-After` is honored within a configured cap
- Exponential backoff uses jitter to avoid synchronized retry storms
- Attempts and total retry duration are bounded before dead-lettering

The accelerated public-demo policy will be clearly labeled and will not be
presented as the production default.

## Invariants

- No acknowledged event disappears.
- A successful delivery is never retried.
- Attempt numbers increase monotonically within a delivery generation.
- Every state transition creates evidence in the attempt or audit history.
- Every query and mutation is workspace-scoped.
- Expired leases make abandoned work eligible for recovery.
