# Reliability contract

> Implementation status: durable ingestion, normal delivery, retries, attempt
> evidence, and recovery of expired `in_progress` leases are implemented.
> Manual replay and broader concurrency testing remain Milestone 3 work.

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
- Reusing the key with the same canonical endpoint, type, and data fingerprint
  returns the existing event.
- Reusing the key with a different request fingerprint returns `409 Conflict`.
- The separate payload digest always hashes the exact immutable outbound bytes;
  it is evidence for delivery integrity, not the idempotency comparison.
- The event and its initial delivery rows are created in one transaction.

## State lifecycle

```text
pending -> in_progress -> delivered
                       -> retry_wait -> in_progress
                       -> dead_lettered
pending/retry_wait -----> dead_lettered (disabled endpoint or exhausted budget)
```

Delivered and dead-lettered states are terminal. Replay creates a new delivery
generation linked to the original; it does not rewrite historical attempts.

## Retry classification

- Success: `2xx`
- Transient: connection failures, timeouts, `408`, `425`, `429`, and `5xx`
- Normally permanent: other `4xx`
- `Retry-After` is honored within a configured cap
- Exponential backoff uses jitter to avoid synchronized retry storms
- Attempts are bounded before dead-lettering. Reserved attempts whose outcome
  becomes indeterminate consume the same budget as completed requests.

The accelerated public-demo policy will be clearly labeled and will not be
presented as the production default.

## Invariants

- No acknowledged event disappears.
- A successful delivery is never retried.
- Attempt numbers increase monotonically within a delivery generation.
- Every committed claim creates an `in_progress` attempt row before network I/O.
  The lease and evidence row share one fenced token and attempt number.
- A completed transport result resolves that row to `completed` in the same
  transaction as its resulting delivery status.
- An expired lease resolves its row to `indeterminate`. This means the endpoint
  may have received the request; it does not mean delivery failed.
- Recovery never reserves an attempt beyond the configured maximum. When the
  final lease expires, its indeterminate evidence and the dead-letter transition
  commit together without another HTTP request.
- Due work is also dead-lettered without creating attempt evidence when its
  endpoint is already disabled or a reduced runtime policy says its existing
  attempt count has exhausted the budget. No HTTP request occurs in either case.
- Every query and mutation will be workspace-scoped after identity is added.
- Expired leases make abandoned work eligible for recovery by another worker.
