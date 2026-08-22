# ADR 0001: Begin as a modular monolith

- Status: accepted
- Date: 2026-08-22

## Context

EventHarbor needs independently scalable API and delivery-worker processes, but
its initial traffic does not justify distributed ownership, network contracts,
or deployment overhead between many services.

## Decision

Use one backend codebase with explicit domain modules and two process entry
points: API and worker. Module boundaries are enforced in code and persistence
access. Processes may scale independently from the same artifact.

## Consequences

- Local development and deployment remain understandable.
- Transactions across event acceptance and delivery creation remain simple.
- The project demonstrates module design rather than decorative microservices.
- A module can be extracted later only when measured scaling or ownership needs
  justify the operational cost.
