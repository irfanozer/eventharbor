# Roadmap

## Milestone 1: foundation

- [x] Product and reliability contract
- [x] Explicit delivery state machine
- [x] Retry classification and bounded jitter
- [x] HMAC signing primitive
- [x] Receiver Lab skeleton
- [x] CI quality gates

## Milestone 2: first vertical slice

- [x] PostgreSQL migrations for endpoint, event, delivery, and attempt
- [x] Endpoint registration restricted to Receiver Lab locally
- [x] `POST /v1/events` with idempotency key
- [x] Worker lease and signed outbound attempt
- [x] Event and attempt query APIs
- [x] End-to-end integration test against real PostgreSQL and Receiver Lab

## Milestone 3: reliability engine

- [x] Retry scheduling and bounded `Retry-After`
- [x] Dead-letter transition after terminal or exhausted attempts
- [x] Manual replay generation
- [x] Lease recovery after worker termination
- [ ] Property and concurrency tests for the full recovery contract

## Milestone 4: recruiter demo

- [x] React Control Room
- [x] Live attempt timeline
- [x] Seeded failure presets through a constrained API facade
- [x] Event, endpoint, and dead-letter pages
- [x] Ninety-second failure, repair, and replay demonstration

## Milestone 5: security and operations

- [x] Exact Receiver Lab destination allow-list and redirect blocking
- [x] Public-edge request ceiling, mutation rate limits, and security headers
- [x] Production configuration invariants, PostgreSQL TLS, and readiness probes
- [x] Scheduled, bounded synthetic-event retention cleanup
- [ ] Workspace isolation and scoped API keys
- [ ] Endpoint verification and secret rotation
- [ ] OpenTelemetry traces, metrics, dashboards, and alerts
- [ ] Reproducible load, retry-storm, and crash-recovery reports

## Milestone 6: Azure deployment

- [x] Azure Container Apps Bicep templates with private internal services
- [x] Private Azure Database for PostgreSQL Flexible Server
- [x] Manual migration job and daily cleanup job
- [x] Immutable GHCR images and passwordless GitHub OIDC release workflow
- [x] Cloudflare custom-domain, cost, rollback, and operations runbooks
- [ ] Provision the operator-controlled Azure resources
- [ ] Bind `eventharbor.irfanburakozer.com` and publish measured monthly cost

## Milestone 7: failure investigator

- Provider-neutral model adapter
- Read-only diagnostic tools
- Evidence-linked structured conclusions
- Human-approved replay request
- Labeled evaluation suite and published results
