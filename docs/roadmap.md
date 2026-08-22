# Roadmap

## Milestone 1: foundation

- Product and reliability contract
- Explicit delivery state machine
- Retry classification and bounded jitter
- HMAC signing primitive
- Receiver Lab skeleton
- CI quality gates

## Milestone 2: first vertical slice

- PostgreSQL migrations for endpoint, event, delivery, and attempt
- Endpoint registration restricted to Receiver Lab locally
- `POST /v1/events` with idempotency key
- Worker lease and one outbound attempt
- Attempt query API
- End-to-end integration test against real PostgreSQL and Receiver Lab

## Milestone 3: reliability engine

- Retry scheduling and `Retry-After`
- Dead-letter state and manual replay generation
- Lease recovery after worker termination
- Property tests for transitions, idempotency, signatures, and timing bounds

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
