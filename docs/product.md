# Product definition

EventHarbor is a developer platform for reliably delivering outbound webhooks
when customer endpoints are slow, rate-limited, broken, or temporarily offline.

## Target user

The primary user is a backend or platform engineer at a SaaS company that must
send billing, order, account, security, CI/CD, or integration events to customer
systems.

Their questions are operational:

- Was the event durably accepted?
- Which destinations received it?
- Why did one destination fail?
- When will it retry?
- Could it have been delivered twice?
- Can it be replayed without erasing its history?

## Core concepts

- **Event:** immutable business fact, such as `invoice.paid`
- **Endpoint:** verified destination URL and versioned signing secret
- **Subscription:** relationship between event types and endpoints
- **Delivery:** one event being sent to one endpoint
- **Attempt:** one concrete HTTP request and its recorded result
- **Dead letter:** delivery that exhausted its retry policy
- **Replay:** new delivery generation linked to the original history

## Primary user journey

1. Register and verify an endpoint.
2. Subscribe it to one or more event types.
3. Publish an event with an idempotency key.
4. Receive a stable event ID after the database transaction commits.
5. Watch every delivery attempt and retry in the timeline.
6. Inspect a stopped, saved delivery, confirm its destination is online, and approve replay.

## Public demonstration

The demo opens directly to a guided Control Room without a registration wall.
Visitors publish synthetic events only to the built-in Receiver Lab. The
lab exposes named order, shipping, and inventory routes. It can return `200`,
fail a deterministic number of times with `503`, return `429` with
`Retry-After`, exceed the request timeout, or validate the selected event
contract and return an exact permanent `400`.

Arbitrary outbound URLs remain disabled in the public demo to prevent SSRF and
open-relay abuse.

## Agentic diagnostics

The optional Failure Investigator is outside the delivery path. It receives
read-only, sanitized tools for attempts, endpoint health, traces, and retry
policy. It must link every conclusion to evidence and cannot replay deliveries,
rotate secrets, disable endpoints, or change policy without explicit operator
approval through deterministic application code.
