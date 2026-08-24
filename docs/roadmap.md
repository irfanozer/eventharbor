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

- React Control Room
- Live attempt timeline
- Seeded failure presets
- Event, endpoint, and dead-letter pages
- Ninety-second scripted demonstration

## Milestone 5: security and operations

- Workspace isolation and scoped API keys
- Endpoint verification and secret rotation
- SSRF and DNS-rebinding controls
- OpenTelemetry traces, metrics, dashboards, and alerts
- Reproducible load, retry-storm, and crash-recovery reports

## Milestone 6: Azure deployment

- Azure Container Apps
- Azure Database for PostgreSQL
- Azure Key Vault
- Terraform and deployment runbook
- Synthetic monitoring and documented monthly cost

## Milestone 7: failure investigator

- Provider-neutral model adapter
- Read-only diagnostic tools
- Evidence-linked structured conclusions
- Human-approved replay request
- Labeled evaluation suite and published results
