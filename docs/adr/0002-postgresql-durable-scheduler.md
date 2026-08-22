# ADR 0002: Use PostgreSQL as the initial durable scheduler

- Status: accepted
- Date: 2026-08-22

## Context

The first version requires durable acceptance, concurrent worker claims, retry
scheduling, crash recovery, and an inspectable source of truth. Introducing a
broker immediately would create another failure boundary before the workload
requires it.

## Decision

Store delivery state and `next_attempt_at` in PostgreSQL. Workers claim due rows
with `FOR UPDATE SKIP LOCKED`, write a short lease, perform HTTP outside the
transaction, and record the result. Expired leases become recoverable.

## Consequences

- Acceptance and initial scheduling occur in one database transaction.
- Delivery state and operational evidence remain directly reconcilable.
- Database contention and polling efficiency must be benchmarked honestly.
- A future Azure Service Bus adapter may carry delivery IDs only, while
  PostgreSQL remains authoritative.
