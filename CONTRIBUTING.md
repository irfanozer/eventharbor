# Contributing

Bug reports, documentation corrections, tests, and focused code changes are
welcome.

## Before opening an issue

- Search existing issues for the same behavior.
- Include the smallest reproducible example you can provide.
- For delivery failures, include the observed state, HTTP status or error type,
  attempt number, and relevant logs. Remove credentials and payload data that
  should not be public.
- Describe the behavior you expected and what happened instead.

## Development setup

The simplest way to run the complete system is:

~~~bash
docker compose up --build
~~~

See the [local demo guide](docs/local-demo.md) for service addresses and
troubleshooting.

For backend development:

~~~bash
cd backend
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy src
pytest
~~~

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1`. PostgreSQL integration tests require a dedicated
database whose name ends in `_test`, supplied through
`EVENTHARBOR_TEST_DATABASE_URL`.

For frontend development:

~~~bash
cd frontend
npm ci
npm run typecheck
npm test
npm run test:runtime-config
npm run build
~~~

## Pull requests

Keep each pull request focused on one behavior. Before submitting it:

1. Explain the problem and the observable result of the change.
2. Add or update tests for the behavior.
3. Run the applicable formatting, linting, type-checking, test, and build
   commands.
4. Update the [architecture](docs/architecture.md) or
   [reliability contract](docs/reliability-contract.md) when a change affects a
   system boundary or delivery guarantee.
5. Add an architecture decision record under `docs/adr/` when the change
   introduces a consequential, difficult-to-reverse design decision.
6. Include migration and rollback notes when changing the database schema.

Reliability changes should cover failure paths as well as the successful path.
Tests involving retries should remain deterministic by injecting time or random
inputs rather than depending on wall-clock timing.

## Security and data handling

- Do not commit passwords, signing secrets, tokens, private keys, or populated
  `.env` files.
- Do not include real customer payloads or private endpoint addresses in tests,
  fixtures, screenshots, logs, or issues.
- Do not weaken the dedicated-test-database guard in the integration fixtures.
- Treat changes to signing, endpoint validation, replay rules, lease fencing, or
  credential handling as security-sensitive.

## Reliability expectations

Changes must preserve the documented
[at-least-once delivery contract](docs/reliability-contract.md). In particular:

- Event acceptance and initial delivery creation remain atomic.
- Outbound network I/O never occurs while a delivery row lock is held.
- Every claimed request has persisted attempt evidence before it is sent.
- Stale workers cannot finalize a newer lease.
- Replay appends a new generation instead of rewriting history.
