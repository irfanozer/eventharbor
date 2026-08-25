# Deploy EventHarbor to Azure

This guide publishes the real EventHarbor application at
`eventharbor.irfanburakozer.com`. It does not deploy the local Docker Compose
stack. Compose remains the development environment.

## What the cloud version contains

```text
Recruiter's browser
        |
        v
Cloudflare DNS (DNS only)
        |
        v
Public web Container App (Nginx + React)
        |
        +-- /api/* --> internal API Container App
                            |
                            +--> private PostgreSQL
                            |
                            +--> internal Receiver Lab Container App

Background worker Container App --> private PostgreSQL --> Receiver Lab
Manual migration Container Apps Job --> private PostgreSQL
Daily retention Container Apps Job --> private PostgreSQL
```

Only the Nginx/React app has public ingress. The API and Receiver Lab use Azure
Container Apps internal service discovery, the worker has no ingress, and the
database has no public network access. Browser requests stay same-origin, so the
application does not need a permissive CORS policy.

The release pipeline publishes two public, immutable GHCR images:

- `ghcr.io/OWNER/REPOSITORY-backend:FULL_GIT_SHA`
- `ghcr.io/OWNER/REPOSITORY-frontend:FULL_GIT_SHA`

The backend image is reused for the API, worker, Receiver Lab, and migration job.

## Cost boundary

Creating files, pushing to GitHub, publishing public GHCR images, and configuring
OIDC do not create the Azure runtime. Running `bootstrap-foundation.ps1` with
`-ConfirmCosts` is the point at which billable resources are created.

The main recurring cost is Azure Database for PostgreSQL Flexible Server. The
always-running worker is the next largest cost. The web, API, and Receiver Lab
use zero minimum replicas in the release workflow and wake on demand. Log
Analytics can also charge for ingestion.

For this deliberately small public demo, budget approximately **$20 to $40 per
month before credits**, depending on region and traffic. This is an estimate, not
a price guarantee. Confirm the selected region in the
[Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/)
before provisioning. Azure documents the Container Apps consumption free grant
on its [pricing page](https://azure.microsoft.com/en-us/pricing/details/container-apps/)
and PostgreSQL pricing on the
[Flexible Server pricing page](https://azure.microsoft.com/en-us/pricing/details/postgresql/flexible-server/).

Before provisioning, create an Azure Cost Management budget at a value you are
comfortable with, such as $25. Budget alerts warn; they do not automatically stop
resources or cap spending.

## Before you begin

You need:

- the EventHarbor GitHub repository;
- an Azure subscription with permission to create resources and role assignments;
- the domain active in Cloudflare;
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-windows);
- [GitHub CLI](https://cli.github.com/);
- PowerShell 7 recommended (Windows PowerShell also works for the supplied scripts).

Run commands from the EventHarbor repository root.

## 1. Put this exact project in the GitHub repository

This workspace copy currently has no Git remote. First copy the changed files into
the local clone that is connected to your real EventHarbor repository, or connect
this folder after confirming the correct repository URL.

```powershell
git remote -v
git status
```

If this is the folder you intend to use and `git remote -v` is empty:

```powershell
git remote add origin https://github.com/YOUR_GITHUB_USER/YOUR_REPOSITORY.git
```

Review, commit, and push the completed application before the deployment change.
Then make a separate deployment commit so the cloud work is easy to review and
roll back:

```powershell
git add .
git commit -m "Prepare EventHarbor Azure deployment"
git push -u origin main
```

Do not add `.env`, a database URL, a database password, or Azure credentials.

## 2. Let CI publish the two images

The `Publish images and deploy production` workflow runs after a successful main
branch CI run. At first it publishes images but skips Azure deployment because the
repository variable `AZURE_DEPLOYMENT_ENABLED` is not yet `true`.

If the automatic run does not start, open **GitHub > Actions > Publish images and
deploy production > Run workflow**.

After the first image build, open each package in GitHub:

1. Open the repository or profile **Packages** section.
2. Open `REPOSITORY-backend`, then **Package settings**.
3. Change visibility to **Public** and confirm.
4. Repeat for `REPOSITORY-frontend`.

Public package visibility is required because the Container Apps templates do not
store a GHCR personal access token. The images contain only application code and
are expected to match this public portfolio repository.

## 3. Sign in locally

```powershell
az login
az account list --output table
gh auth login
gh auth status
```

Copy the subscription ID you want to use. Replace the example values in every
command below.

## 4. Create the Azure foundation

First create the Azure budget in the portal. Then run:

```powershell
.\scripts\azure\bootstrap-foundation.ps1 `
  -SubscriptionId "YOUR_SUBSCRIPTION_ID" `
  -Location "eastus2" `
  -GitHubRepository "YOUR_GITHUB_USER/YOUR_REPOSITORY" `
  -ConfirmCosts
```

The script:

1. registers the required Azure resource providers;
2. creates `rg-eventharbor-prod`;
3. validates and deploys `infra/azure/foundation.bicep`;
4. creates the VNet, private DNS, Container Apps environment, Log Analytics, and
   private PostgreSQL Flexible Server;
5. generates a strong database password in memory;
6. stores the full database URL as the masked GitHub production-environment secret
   `EVENTHARBOR_DATABASE_URL`;
7. deletes the temporary parameter file containing the password.

The password and database URL are not committed or printed.

Use `eastus2` only if PostgreSQL 17 and `Standard_B1ms` are available for the
subscription there. If validation reports SKU availability, choose a nearby Azure
region and rerun with that region.

## 5. Create passwordless GitHub-to-Azure trust

```powershell
.\scripts\azure\configure-github-oidc.ps1 `
  -SubscriptionId "YOUR_SUBSCRIPTION_ID" `
  -GitHubOwner "YOUR_GITHUB_USER" `
  -GitHubRepository "YOUR_REPOSITORY" `
  -ConfigureGitHub
```

This creates a Microsoft Entra application, a service principal, and one federated
credential whose subject is exactly:

```text
repo:YOUR_GITHUB_USER/YOUR_REPOSITORY:environment:production
```

It assigns `Contributor` only on the dedicated EventHarbor resource group. This is
broad inside that one resource group but cannot modify other resource groups or
assign Azure roles. No Azure client secret is created. GitHub receives the client,
tenant, and subscription IDs as production-environment variables.

If role assignment fails, the signed-in Azure account needs `Owner` or `User Access
Administrator` on this resource group for the one-time setup.

## 6. Protect and enable the GitHub production environment

In **GitHub > Settings > Environments > production**:

1. allow deployments only from `main`;
2. optionally add yourself as a required reviewer;
3. confirm the environment secret `EVENTHARBOR_DATABASE_URL` exists;
4. confirm these environment variables exist:
   `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
   `AZURE_RESOURCE_GROUP`, `AZURE_NAME_PREFIX`, `AZURE_ENVIRONMENT_NAME`, and
   `AZURE_CONTAINER_APPS_ENVIRONMENT`.

Then create this **repository-level** Actions variable, not an environment secret:

```powershell
gh variable set AZURE_DEPLOYMENT_ENABLED `
  --repo "YOUR_GITHUB_USER/YOUR_REPOSITORY" `
  --body "true"
```

This variable is the deployment safety switch. Set it to `false` before making
infrastructure changes or intentionally pausing releases.

## 7. Run the first release

Open **GitHub > Actions > Publish images and deploy production > Run workflow**.
The workflow performs this order:

1. checks out the exact tested Git SHA;
2. rebuilds and publishes immutable backend and frontend images;
3. obtains a short-lived Azure token through OIDC;
4. validates all Bicep templates;
5. creates or updates the migration job;
6. runs Alembic and waits for success;
7. deploys Receiver Lab, API, worker, web, and the daily bounded retention job;
8. verifies `/healthz`, `/api/health`, `/api/ready`, and the control-room overview;
9. restores the previous images if a later release fails live verification.

The first release has no previous revision to restore, so a first-release failure
stops with diagnostics instead of pretending a rollback occurred.

The scheduled cleanup removes terminal synthetic histories older than seven days
in bounded batches. It never deletes pending, in-progress, or retry-wait work, and
uses a PostgreSQL advisory lock to prevent overlapping cleanup executions.

The generated Azure URL is available on the workflow deployment summary. Verify it
before adding the custom domain.

## 8. Add `eventharbor.irfanburakozer.com`

Read the exact generated values:

```powershell
.\scripts\azure\show-domain-records.ps1 `
  -SubscriptionId "YOUR_SUBSCRIPTION_ID"
```

Create the two printed records in **Cloudflare > DNS > Records**:

| Type | Name | Value | Proxy |
|---|---|---|---|
| CNAME | `eventharbor` | generated `azurecontainerapps.io` hostname | DNS only |
| TXT | `asuid.eventharbor` | Azure verification ID | not applicable |

If the zone already has any CAA records, also add:

| Type | Name | Flags | Tag | CA domain name |
|---|---|---:|---|---|
| CAA | `@` | `0` | `issue` | `digicert.com` |

Leave the CNAME gray-cloud **DNS only**. Azure's managed-certificate documentation
requires the CNAME to resolve directly to the generated Container Apps hostname;
an intermediary such as Cloudflare's proxy can prevent issuance or renewal.

After DNS resolves:

1. open **Azure Portal > Container Apps > eventharbor-web-prod**;
2. open **Custom domains**;
3. choose **Add custom domain**;
4. enter `eventharbor.irfanburakozer.com`;
5. choose a free **Azure managed certificate**;
6. wait for validation and binding to finish;
7. open `https://eventharbor.irfanburakozer.com/` in a private browser window.

Official references:

- [Container Apps custom domains and managed certificates](https://learn.microsoft.com/en-us/azure/container-apps/custom-domains-managed-certificates)
- [Container Apps service-to-service connections](https://learn.microsoft.com/en-us/azure/container-apps/connect-apps)
- [PostgreSQL private networking](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private)
- [GitHub OIDC for Azure](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-azure)

## 9. Add the demo link to the portfolio

Use this as the project's primary demo URL:

```text
https://eventharbor.irfanburakozer.com/
```

Keep the GitHub repository as a separate link. The portfolio case study should say
that the live demo uses synthetic data, real HTTP requests, durable PostgreSQL
records, a background retry worker, and an independent Receiver Lab service.

## What is intentionally not automated

- The scripts do not change Cloudflare records.
- The scripts do not bind the domain or certificate before you verify DNS.
- The pipeline does not provision the foundation on every push.
- No pipeline can approve its own protected production environment.
- Resource-group deletion is not scripted because it removes the database and all
  recoverable deployment history.

See [operations.md](operations.md) for monitoring, pausing costs, rollback, and
removal.
