# Reliability contract

This document defines the delivery behavior implemented by EventHarbor. It
describes what a publisher can rely on, what a webhook consumer must handle, and
where uncertainty remains.

## Delivery semantics

EventHarbor provides **at-least-once outbound HTTP delivery**.

`POST /v1/events` returns `202 Accepted` only after the immutable event and its
initial delivery row have committed to PostgreSQL. Returning `202` means the
event is durably scheduled; it does not mean that the destination has already
received it.

EventHarbor does not claim exactly-once delivery. A destination may accept a
request immediately before the worker loses its database connection or exits.
If the worker cannot record the result, a later worker marks the expired
attempt indeterminate and may send the event again.

Consumers must deduplicate using the stable
`X-EventHarbor-Event-Id` header.

## Idempotent ingestion

A publisher supplies an `Idempotency-Key` with every event publication.

- The event and initial delivery are inserted in one transaction.
- Reusing the key with the same source, endpoint, event type, and data returns
  the existing event.
- Reusing the key for a different request fingerprint returns `409 Conflict`.
- The persisted payload bytes are immutable after acceptance.
- `request_fingerprint_sha256` is used for the idempotency comparison.
- `payload_sha256` verifies the exact body used for outbound delivery.

The request fingerprint and payload digest have separate purposes and are not
interchangeable.

## Delivery states

~~~text
pending -------> in_progress -------> delivered
                      |
                      +-------------> retry_wait -------> in_progress
                      |
                      +-------------> dead_lettered

pending/retry_wait -----------------> dead_lettered
~~~

- `pending`: durably scheduled and eligible when `next_attempt_at` is due.
- `in_progress`: claimed by a worker under a time-limited lease.
- `retry_wait`: a retryable attempt failed and a later attempt is scheduled.
- `delivered`: the receiver returned a `2xx` response.
- `dead_lettered`: automatic delivery stopped after a terminal response,
  exhausted attempt budget, or disabled endpoint.

`delivered` and `dead_lettered` are terminal for that delivery generation.
Replay creates a new generation rather than changing a terminal source.

## Attempt evidence

Every outbound request has a matching `delivery_attempts` row committed before
network I/O begins. The claim and attempt share the same lease token and attempt
number.

An attempt has one of three states:

- `in_progress`: reserved and not yet resolved;
- `completed`: the worker recorded an HTTP response or transport error; or
- `indeterminate`: the lease expired before the worker could record a result,
  so the destination may have received the request.

Completed evidence includes the disposition, HTTP status or transport error,
bounded response-body excerpt, request timestamp, duration, and any scheduled
retry time.

## Claiming and lease recovery

Workers claim due rows using `FOR UPDATE SKIP LOCKED`. The claim transaction:

1. locks one eligible delivery;
2. verifies the endpoint and attempt budget;
3. assigns a lease owner, token, and expiration;
4. increments the attempt counter;
5. inserts the attempt evidence; and
6. commits before HTTP begins.

Finalization succeeds only if the delivery still has the same owner, lease
token, and attempt number. A late result from an obsolete worker is ignored.

When a lease expires:

- the matching attempt becomes `indeterminate`;
- another attempt is reserved only when the endpoint remains enabled and the
  configured budget has not been exhausted; and
- the delivery becomes `dead_lettered` when no attempt remains.

An indeterminate attempt consumes the same bounded budget as a completed
attempt because a real HTTP request may already have occurred.

## Retry classification

| Result | Disposition |
| --- | --- |
| `2xx` | delivered |
| Connection failure or timeout | retry |
| `408`, `425`, or `429` | retry |
| `5xx` | retry |
| Other `4xx` | terminal failure |

Retries use bounded exponential backoff with full jitter. A valid
`Retry-After` delta or HTTP date is honored for retryable responses after being
clamped to the configured cap. The attempt count, base delay, maximum delay,
and `Retry-After` cap are runtime settings.

The Docker Compose environment intentionally uses a smaller attempt budget and
shorter delays so the failure lifecycle is observable without a long wait.

## Signed requests

Each request includes a Unix timestamp and an HMAC-SHA256 signature:

~~~text
v1=HMAC_SHA256(secret, timestamp + "." + exact_payload_bytes)
~~~

The destination receives the signature in
`X-EventHarbor-Signature` and the timestamp in
`X-EventHarbor-Timestamp`. A consumer should:

1. reject timestamps outside its accepted clock-skew window;
2. recompute the signature over the raw request bytes;
3. compare signatures in constant time; and
4. deduplicate by event ID before applying business side effects.

Signing authenticates the body for a holder of the endpoint secret. It does not
provide exactly-once processing.

## Manual replay

`POST /v1/deliveries/{delivery_id}/replays` follows these rules:

- The source must be the latest delivery generation in its event-endpoint chain.
- The source must be `dead_lettered`.
- The endpoint must be enabled.
- The caller must provide an `Idempotency-Key`.
- Repeating the same replay request returns the existing replay generation.
- The replay reuses the immutable event and endpoint.
- The replay starts as a new `pending` delivery with
  `replay_generation + 1` and its own attempt timeline.
- The source delivery and all source attempts remain unchanged.
- Only one nonterminal generation may exist in a chain.
- Replaying an older generation or creating a second active generation returns
  `409 Conflict`.
- The response is `202 Accepted` because delivery remains asynchronous.

Receiver Lab can return structured evidence that proves the immutable payload
violates its route contract, such as a missing required field or unsupported
event type. EventHarbor blocks unchanged replay in that case because retrying
identical bytes cannot repair the data. The publisher must submit a corrected
event. Other terminal failures may remain replayable after an external repair.

## Invariants

- An acknowledged event and its initial delivery commit together.
- A successful delivery is never retried.
- Attempt numbers increase monotonically within a delivery generation.
- Every claimed HTTP request has durable evidence before it is sent.
- At most one attempt is `in_progress` for a delivery.
- A transport result and its resulting delivery state commit together.
- Expired leases resolve outstanding evidence as `indeterminate`.
- A stale worker cannot finalize a replacement lease.
- No attempt is reserved beyond the configured maximum.
- Replay generations increase monotonically within an event-endpoint chain.
- Replay never rewrites the source delivery or its attempt evidence.
- Retrying a replay request with the same idempotency key returns the same
  generation.

## Non-guarantees

EventHarbor does not guarantee:

- exactly-once delivery or exactly-once processing at the destination;
- global event ordering;
- ordering between different endpoints;
- transactional coupling between PostgreSQL and a remote receiver;
- automatic correction of an invalid immutable payload; or
- continued automatic sending after a delivery is dead-lettered.

Operational recovery from a dead letter requires repairing the receiver or
publishing corrected data, then explicitly approving replay when replay is
valid.
