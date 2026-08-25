# EventHarbor on Azure

This directory describes a lean public-demo deployment. It uses managed services and private networking where they materially protect the system, while avoiding infrastructure that would add cost without improving the portfolio demonstration.

Nothing in these templates creates a subscription or resource group, modifies Cloudflare, or deploys itself. You remain in control of the Azure account, billing, GitHub permissions, and DNS.

## Architecture

```text
Browser
  |
  | HTTPS: eventharbor.irfanburakozer.com
  v
eventharbor-web-prod (external Azure Container App)
  |
  | /api/* through Nginx, same-origin
  v
eventharbor-api-prod (internal ingress)
  |                         |
  | private PostgreSQL      | internal Receiver Lab control
  v                         v
Azure PostgreSQL       eventharbor-receiver-prod (internal ingress)
  ^                         ^
  |                         |
eventharbor-worker-prod ----+ real webhook requests

eventharbor-migrate-prod (manual job) ---> private PostgreSQL
eventharbor-cleanup-prod (daily scheduled job) ---> private PostgreSQL
```

Only the web app has external ingress. The API and Receiver Lab use Azure Container Apps service discovery inside one environment. The worker has no ingress. PostgreSQL has no public network access and lives in a dedicated delegated subnet.

The topology intentionally omits Azure Container Registry, Key Vault, NAT Gateway, Application Gateway, Redis, and zone redundancy. Public GHCR images need no registry password. Database credentials are stored as Container Apps secrets. This is a cost-conscious portfolio deployment, not a claim of enterprise high availability.

## Files and deployment order

Deploy the templates in this order:

1. `foundation.bicep`: virtual network, two delegated subnets, private DNS, Log Analytics, Container Apps environment, PostgreSQL Flexible Server, and the application database.
2. `migration.bicep`: creates or updates the manual Alembic migration job.
3. Start the migration job and confirm that it succeeds.
4. `apps.bicep`: deploys the public web app, internal API, internal Receiver Lab, background worker, and daily retention-cleanup job.

This order prevents a new application revision from starting against an old database schema.

`parameters.example.json` contains only common, non-secret settings and can be passed to all three templates. Passwords and database URLs must be supplied separately at deployment time.

The templates use current stable, non-preview resource API versions: Azure Container Apps `2026-01-01`, PostgreSQL Flexible Server `2025-08-01`, and current stable Network and Log Analytics versions. Pinning explicit stable versions keeps future provider changes from silently changing the deployment contract.

## Resource names

With the example parameters, the templates create:

| Purpose | Name |
| --- | --- |
| Resource group | Chosen during bootstrap, recommended `rg-eventharbor-prod` |
| Virtual network | `vnet-eventharbor-prod` |
| Container Apps environment | `cae-eventharbor-prod` |
| Log Analytics workspace | `log-eventharbor-prod` |
| Web app | `eventharbor-web-prod` |
| API app | `eventharbor-api-prod` |
| Worker app | `eventharbor-worker-prod` |
| Receiver Lab app | `eventharbor-receiver-prod` |
| Migration job | `eventharbor-migrate-prod` |
| Retention job | `eventharbor-cleanup-prod` |
| PostgreSQL server | Globally unique `psql-eventharbor-prod-<suffix>` |

Application-to-application URLs are therefore `http://eventharbor-api-prod` and `http://eventharbor-receiver-prod`. Azure resolves these short names only inside the Container Apps environment, and the traffic does not leave the environment.

## Prerequisites

- An Azure subscription with permission to create resources and register resource providers.
- Azure CLI and Bicep CLI.
- Docker and a public GitHub repository with public GHCR packages.
- A Cloudflare-managed `irfanburakozer.com` DNS zone.
- The backend runtime must expose `/health` and a database-aware `/ready` endpoint.
- The production frontend image must listen on port 8080, expose `/healthz`, and support the `API_UPSTREAM` Nginx template variable.

Sign in and select the intended subscription yourself:

```powershell
az login
az account list --output table
az account set --subscription "YOUR SUBSCRIPTION NAME OR ID"
az account show --output table
```

Register the required providers once:

```powershell
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.Network --wait
az provider register --namespace Microsoft.OperationalInsights --wait
az provider register --namespace Microsoft.DBforPostgreSQL --wait
```

Install or update Bicep:

```powershell
az bicep install
az bicep upgrade
az bicep version
```

## 1. Create the resource group

The templates deliberately do not create the resource group. This keeps the deployment identity scoped to one explicit boundary.

```powershell
$resourceGroup = "rg-eventharbor-prod"
$location = "eastus2"

az group create --name $resourceGroup --location $location --tags application=EventHarbor environment=prod workload=public-demo
```

Confirm that PostgreSQL 17 and `Standard_B1ms` are available in the selected region before deployment. If either is unavailable, choose another nearby region or override the corresponding foundation parameter.

## 2. Prepare secrets locally

Generate one strong, URL-safe PostgreSQL password. A URL-safe value avoids accidental corruption when it is placed inside the SQLAlchemy URL. Do not write it to the repository, a parameter file, shell history, screenshots, or chat.

For the commands below, place the secret only in the current PowerShell process:

```powershell
$env:EVENTHARBOR_POSTGRES_PASSWORD = Read-Host "PostgreSQL password"
```

Clear it when deployment is finished:

```powershell
Remove-Item Env:EVENTHARBOR_POSTGRES_PASSWORD
Remove-Item Env:EVENTHARBOR_DATABASE_URL
```

Container Apps stores the database URL as an application-level secret. The Bicep parameters are marked `@secure()`, so Azure deployment history does not expose their values as ordinary parameters or outputs.

## 3. Validate and deploy the foundation

Build the templates locally before deployment:

```powershell
az bicep build --file infra/azure/foundation.bicep --stdout | Out-Null
az bicep build --file infra/azure/migration.bicep --stdout | Out-Null
az bicep build --file infra/azure/apps.bicep --stdout | Out-Null
```

Preview the foundation:

```powershell
az deployment group what-if `
  --resource-group $resourceGroup `
  --template-file infra/azure/foundation.bicep `
  --parameters '@infra/azure/parameters.example.json' `
  --parameters postgresAdministratorPassword="$env:EVENTHARBOR_POSTGRES_PASSWORD"
```

Deploy it:

```powershell
az deployment group create `
  --name eventharbor-foundation `
  --resource-group $resourceGroup `
  --template-file infra/azure/foundation.bicep `
  --parameters '@infra/azure/parameters.example.json' `
  --parameters postgresAdministratorPassword="$env:EVENTHARBOR_POSTGRES_PASSWORD"
```

Read the non-secret outputs and construct the private database URL. If the password contains URI-reserved characters, URI-encode the password before constructing this value.

```powershell
$foundationOutputs = az deployment group show `
  --name eventharbor-foundation `
  --resource-group $resourceGroup `
  --query properties.outputs `
  --output json | ConvertFrom-Json

$postgresFqdn = $foundationOutputs.postgresServerFqdn.value
$postgresDatabase = $foundationOutputs.postgresDatabaseName.value
$postgresLogin = $foundationOutputs.postgresAdministratorLogin.value
$encodedPassword = [System.Uri]::EscapeDataString($env:EVENTHARBOR_POSTGRES_PASSWORD)
$env:EVENTHARBOR_DATABASE_URL = "postgresql+asyncpg://${postgresLogin}:${encodedPassword}@${postgresFqdn}:5432/${postgresDatabase}"
```

TLS is enforced separately with `EVENTHARBOR_DATABASE_SSL_MODE=require` in the API, worker, and migration job. Do not disable that production setting. A compatible `?ssl=require` query may also be used, but it is not required by these templates.

## 4. Publish immutable public GHCR images

The deployment expects two Linux AMD64 images:

```text
ghcr.io/<github-owner>/eventharbor-backend:<git-sha-or-digest>
ghcr.io/<github-owner>/eventharbor-frontend:<git-sha-or-digest>
```

The backend image is reused for the API, worker, Receiver Lab, and migration job. The image must contain `/app/alembic.ini` and `/app/alembic`.

The frontend build must use the deterministic internal receiver URL:

```text
VITE_RECEIVER_LAB_BASE_URL=http://eventharbor-receiver-prod/webhooks
```

After the first GitHub Actions image push, open the package settings on GitHub and change both packages to **Public**. A workflow `GITHUB_TOKEN` expires after the workflow; Azure Container Apps cannot use it later. If the packages remain private, image pulls fail unless a long-lived registry token is added, which this lean design intentionally avoids.

Prefer an immutable Git commit tag or, better, an image digest:

```powershell
$backendImage = "ghcr.io/YOUR_GITHUB_OWNER/eventharbor-backend@sha256:BACKEND_DIGEST"
$frontendImage = "ghcr.io/YOUR_GITHUB_OWNER/eventharbor-frontend@sha256:FRONTEND_DIGEST"
```

## 5. Create and run the migration job

Create or update the migration job with the exact backend image that will be released:

```powershell
az deployment group create `
  --name eventharbor-migration `
  --resource-group $resourceGroup `
  --template-file infra/azure/migration.bicep `
  --parameters '@infra/azure/parameters.example.json' `
  --parameters backendImage="$backendImage" databaseUrl="$env:EVENTHARBOR_DATABASE_URL"
```

Start it:

```powershell
$migrationJob = "eventharbor-migrate-prod"
az containerapp job start --name $migrationJob --resource-group $resourceGroup --output none
az containerapp job execution list --name $migrationJob --resource-group $resourceGroup --output table
```

Wait until the newest execution reports `Succeeded`. If it reports `Failed`, inspect the execution and logs and stop; do not deploy the apps:

```powershell
$executionName = az containerapp job execution list `
  --name $migrationJob `
  --resource-group $resourceGroup `
  --query "sort_by(@, &properties.startTime)[-1].name" `
  --output tsv

az containerapp job execution show `
  --name $migrationJob `
  --resource-group $resourceGroup `
  --job-execution-name $executionName `
  --output jsonc
```

## 6. Deploy the applications

Preview the change, then deploy only after the migration succeeds:

```powershell
az deployment group what-if `
  --resource-group $resourceGroup `
  --template-file infra/azure/apps.bicep `
  --parameters '@infra/azure/parameters.example.json' `
  --parameters frontendImage="$frontendImage" backendImage="$backendImage" databaseUrl="$env:EVENTHARBOR_DATABASE_URL"

az deployment group create `
  --name eventharbor-apps `
  --resource-group $resourceGroup `
  --template-file infra/azure/apps.bicep `
  --parameters '@infra/azure/parameters.example.json' `
  --parameters frontendImage="$frontendImage" backendImage="$backendImage" databaseUrl="$env:EVENTHARBOR_DATABASE_URL"
```

Get the temporary Azure URL:

```powershell
$webUrl = az deployment group show `
  --name eventharbor-apps `
  --resource-group $resourceGroup `
  --query properties.outputs.webAppUrl.value `
  --output tsv

$webUrl
Invoke-WebRequest "$webUrl/" -UseBasicParsing
Invoke-RestMethod "$webUrl/api/v1/control-room/overview"
```

Do not consider the release complete until the page, proxied API overview, and one synthetic guided run all work.

## 7. Configure `eventharbor.irfanburakozer.com`

Read the generated web hostname and domain verification value:

```powershell
$webApp = "eventharbor-web-prod"
$containerEnvironment = "cae-eventharbor-prod"
$webFqdn = az containerapp show --name $webApp --resource-group $resourceGroup --query properties.configuration.ingress.fqdn --output tsv
$verificationId = az containerapp env show --name $containerEnvironment --resource-group $resourceGroup --query properties.customDomainConfiguration.customDomainVerificationId --output tsv
```

In **Cloudflare → DNS → Records**, add:

| Type | Name | Value | Proxy status |
| --- | --- | --- | --- |
| CNAME | `eventharbor` | The exact `$webFqdn` value | **DNS only** |
| TXT | `asuid.eventharbor` | The exact `$verificationId` value | Not applicable |

The CNAME must remain DNS-only and point directly to the generated `azurecontainerapps.io` hostname for Azure's free managed certificate to issue and renew. Cloudflare's orange-cloud proxy is an intermediate target and breaks that requirement. Cloudflare still remains the authoritative DNS provider.

If the root zone has restrictive CAA records, add a CAA record allowing `digicert.com` before certificate issuance.

After DNS resolves, bind the custom hostname and free managed certificate:

```powershell
$customDomain = "eventharbor.irfanburakozer.com"

az containerapp hostname add `
  --name $webApp `
  --resource-group $resourceGroup `
  --hostname $customDomain

az containerapp hostname bind `
  --name $webApp `
  --resource-group $resourceGroup `
  --environment $containerEnvironment `
  --hostname $customDomain `
  --validation-method CNAME
```

Certificate issuance can take several minutes. Verify both the hostname and certificate:

```powershell
Invoke-WebRequest "https://eventharbor.irfanburakozer.com/" -UseBasicParsing
Invoke-RestMethod "https://eventharbor.irfanburakozer.com/api/v1/control-room/overview"
```

## GitHub Actions and Azure OIDC

Routine deployment should use GitHub's OpenID Connect federation rather than a saved Azure password or `AZURE_CREDENTIALS` secret.

Use a GitHub Environment named `production`, restrict it to `main`, and configure an Entra federated credential whose subject is bound to that environment. Grant the deployment service principal `Contributor` only on `rg-eventharbor-prod`, not on the whole subscription.

The deploy job needs:

```yaml
permissions:
  contents: read
  id-token: write
```

The separate image-build job needs `packages: write`. Store `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and `AZURE_SUBSCRIPTION_ID` as GitHub Environment variables. They identify resources but are not passwords. Do not create an `AZURE_CREDENTIALS` secret.

Use a production concurrency group so two releases cannot migrate and deploy at the same time. The workflow order must remain: tests, build immutable images, deploy/update migration job, run and verify migration, deploy apps, then smoke test.

## Scaling and reliability choices

- Web: 0 to 1 replica. It may cold-start after inactivity, minimizing cost.
- API: template default 1 to 2 replicas; the release workflow overrides the
  minimum to 0 for the lowest-cost idle posture. Set it back to 1 when cold-start
  latency matters more than cost.
- Worker: exactly 1 small 0.25 CPU/0.5 GiB replica. A non-ingress app with zero replicas and no external scaler cannot wake itself.
- Receiver Lab: 0 to 1 replica. Internal HTTP ingress wakes it for a demo, while the maximum of one prevents a guided story from being split across separate process memories. Scaling to zero intentionally discards old, bounded test state between inactive sessions.
- Retention cleanup: a scheduled job runs `python -m eventharbor.maintenance` daily at 04:00 UTC. It uses no always-running replica and retries a failed execution once.
- PostgreSQL: Burstable B1ms, 32 GiB, seven-day backups, private networking, no HA.
- Inter-app peer traffic encryption is enabled in the Container Apps environment.

The worker code is designed for safe concurrent claims, so a later load-test stage can introduce a real scaling signal. This first deployment does not pretend that arbitrary replica count is useful without that signal.

The public demo uses an intentionally accelerated retry policy: four attempts with one- to two-second delays. The application runs with `EVENTHARBOR_ENVIRONMENT=production` so deployed-runtime safeguards remain active; "public demo" describes the workload and data, not weaker runtime validation.

## Security and operating notes

- Only synthetic data belongs in the public demo.
- Arbitrary destination URLs must stay blocked. EventHarbor's server-side exact Receiver Lab allow-list is a required SSRF boundary.
- The database URL is duplicated into the API, worker, migration, and cleanup job secret stores. Rotate them together by redeploying the two templates.
- Keep request-size limits, mutation rate limits, and retention cleanup enabled before sharing the URL publicly.
- Set an Azure budget alert and PostgreSQL storage alert. PostgreSQL is always provisioned and will usually dominate the monthly cost.
- Log Analytics is capped at 1 GiB/day with 30-day retention by the foundation template. Review ingestion after the first public week.
- The PostgreSQL subnet and Container Apps subnet are dedicated. Do not place unrelated resources in them.
- Do not set the worker minimum to zero without first adding an external scaling trigger.
- Do not set Receiver Lab above one replica until its scenario state is moved out of process memory.

## Updating and rollback

For every release, publish new immutable images and repeat migration then apps deployment. Never reuse a mutable `latest` tag.

Container Apps uses single-revision mode. Azure keeps up to three inactive revisions. If application code must be rolled back, point `apps.bicep` to the previous known-good image digests and redeploy. Database rollback is not automatic; migrations should remain backward compatible with the previous application revision.

## Removing the deployment

The resource group is the cleanup boundary. Deleting it permanently deletes the public app, logs, private network, PostgreSQL server, and database. Export anything you need first. Perform removal manually in Azure only when you intentionally want to destroy the entire demo environment.
