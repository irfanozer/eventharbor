# EventHarbor Azure infrastructure

This directory contains the Bicep templates for EventHarbor's Azure deployment.
For the complete setup procedure, cost boundary, GitHub Actions configuration,
and custom domain steps, use [the deployment guide](../../docs/deployment.md).

## Topology

```text
Internet
  |
  v
eventharbor-web-prod (external Container App)
  |
  +-- /api/* --> eventharbor-api-prod (internal ingress)
                       |                 |
                       v                 v
              private PostgreSQL   eventharbor-receiver-prod
                       ^                 ^
                       |                 |
              eventharbor-worker-prod ---+

eventharbor-migrate-prod --> private PostgreSQL
eventharbor-cleanup-prod --> private PostgreSQL
```

The web app is the only public Azure service endpoint. The API and Receiver Lab
use internal Container Apps service discovery, the worker has no ingress, and
PostgreSQL is attached through a delegated subnet and private DNS.

## Template inventory and deployment order

| Order | File | Responsibility |
| ---: | --- | --- |
| 1 | `foundation.bicep` | VNet, delegated subnets, private DNS, Log Analytics, Container Apps environment, PostgreSQL server, and application database |
| 2 | `migration.bicep` | Manual Alembic migration job for the exact backend image being released |
| 3 | `apps.bicep` | Web, API, worker, Receiver Lab, and scheduled retention-cleanup job |
| - | `parameters.example.json` | Shared non-secret parameter values |

Always run and verify the migration job before deploying `apps.bicep`. This keeps
new application revisions from starting against an older database schema.

## Images and runtime contract

The templates expect public Linux AMD64 GHCR images, preferably addressed by
digest:

```text
ghcr.io/OWNER/REPOSITORY-backend@sha256:DIGEST
ghcr.io/OWNER/REPOSITORY-frontend@sha256:DIGEST
```

The backend image is reused by the API, worker, Receiver Lab, migration job, and
cleanup job. It must contain `/app/alembic.ini` and `/app/alembic`. The frontend
must listen on port 8080, expose `/healthz`, and use `API_UPSTREAM` for its Nginx
proxy target.

Because the packages are public, Container Apps pulls them anonymously. The
templates intentionally contain no GHCR username, password, or registry secret.

## Parameters

### Shared naming

| Parameter | Default | Purpose |
| --- | --- | --- |
| `location` | Resource-group location | Azure region |
| `namePrefix` | `eventharbor` | Resource-name prefix |
| `environmentName` | `prod` | Resource-name suffix |
| `containerAppsEnvironmentName` | Derived | Existing Container Apps environment |
| `tags` | `{}` | Azure resource tags |

### Foundation

| Parameter | Default | Purpose |
| --- | --- | --- |
| `postgresAdministratorLogin` | `eventharbor_admin` | PostgreSQL administrator login |
| `postgresAdministratorPassword` | Required, secure | PostgreSQL administrator password |
| `postgresDatabaseName` | `eventharbor` | Application database |
| `postgresVersion` | `17` | PostgreSQL major version |
| `postgresSkuName` | `Standard_B1ms` | Flexible Server compute SKU |
| `postgresStorageSizeGB` | `32` | Allocated storage |

### Migration and applications

| Parameter | Default | Purpose |
| --- | --- | --- |
| `backendImage` | Required | Immutable backend image |
| `frontendImage` | Required in `apps.bicep` | Immutable frontend image |
| `databaseUrl` | Required, secure | Async PostgreSQL connection URL |
| `applicationEnvironment` | `production` | Runtime safeguards |
| `logLevel` | `INFO` | Application log level |
| `webCustomDomainName` | Empty | Existing public hostname to preserve during app updates |
| `webCustomDomainCertificateId` | Empty | Existing Azure managed-certificate resource ID bound to that hostname |
| `webMinReplicas` / `webMaxReplicas` | `1` / `1` | Web scaling bounds; one warm replica avoids first-page cold starts |
| `apiMinReplicas` / `apiMaxReplicas` | `1` / `2` | API scaling bounds; one warm replica avoids a second cold start after React loads |
| `workerMinReplicas` / `workerMaxReplicas` | `1` / `1` | Delivery worker scaling bounds |
| `receiverMinReplicas` / `receiverMaxReplicas` | `0` / `1` | Receiver Lab scaling bounds |
| Retry parameters | 4 attempts, 1-second base, 2-second backoff cap, 5-second Retry-After cap | Fast outage retries with a visible five-second pause when the receiver is busy |

`parameters.example.json` contains no secrets. The deployment workflow supplies
the database URL from the masked `EVENTHARBOR_DATABASE_URL` repository secret.

## Security and reliability choices

- GitHub Actions authenticates to Azure with OIDC scoped to this repository's
  `main` branch; no Azure client secret is stored.
- The deployment identity is `Contributor` only on the dedicated EventHarbor
  resource group.
- PostgreSQL has no public network access and requires TLS.
- Only the web app has external ingress; browser API traffic stays same-origin.
- Destination URLs remain restricted to Receiver Lab routes to preserve the SSRF
  boundary.
- Image tags are converted to immutable digests before deployment.
- Migrations run before application revisions, and failed smoke tests restore the
  previous application images when available.
- Retention cleanup is a bounded scheduled job protected by a PostgreSQL advisory
  lock.

The topology omits Azure Container Registry, Key Vault, NAT Gateway, Application
Gateway, Redis, zone redundancy, and PostgreSQL high availability to keep this
deployment appropriately small. Database credentials are stored as Container
Apps secrets and must be rotated by redeploying every consumer.

For routine monitoring, rollback, scaling, and removal, see
[the operations runbook](../../docs/operations.md).
