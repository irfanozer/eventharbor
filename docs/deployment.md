# Deploy EventHarbor to Azure

This guide deploys EventHarbor from a public GitHub repository to Azure Container
Apps and publishes it at `eventharbor.irfanburakozer.com`. Docker Compose remains
the local development environment.

## Architecture

```text
Browser
  |
  v
Cloudflare DNS (DNS only)
  |
  v
Web Container App (public Nginx + React)
  |
  +-- /api/* --> API Container App (internal)
                      |
                      +--> PostgreSQL (private network)
                      +--> Receiver Lab (internal)

Worker Container App --> PostgreSQL --> Receiver Lab
Migration job ---------> PostgreSQL
Daily cleanup job ------> PostgreSQL
```

Only the web app has public ingress. Nginx proxies `/api/*` to the internal API,
so browser requests remain same-origin. The API and Receiver Lab use internal
Container Apps service discovery, the worker has no ingress, and PostgreSQL has
no public network access.

The release workflow builds two public, immutable GitHub Container Registry
(GHCR) images:

- `ghcr.io/OWNER/REPOSITORY-backend:FULL_GIT_SHA`
- `ghcr.io/OWNER/REPOSITORY-frontend:FULL_GIT_SHA`

The backend image is shared by the API, worker, Receiver Lab, migration job, and
cleanup job. Azure deploys images by digest; it does not use a mutable `latest`
tag.

## Cost boundary

Pushing code, running GitHub Actions within the applicable GitHub allowance,
publishing public GHCR images, and configuring OIDC do not create Azure runtime
resources. The first command that can create recurring Azure cost is
`bootstrap-foundation.ps1 -ConfirmCosts`.

The main recurring resources are PostgreSQL Flexible Server, one continuously
available delivery worker, one warm web replica, one warm API replica, and Log
Analytics ingestion. Receiver Lab still scales to zero when idle.

The selected PostgreSQL configuration is Burstable B1ms with 32 GB storage. For
an eligible new Azure account, this matches the published 12-month PostgreSQL
allowance. Container Apps also includes a monthly consumption grant, but the
warm web and API replicas can incur reduced idle charges after that grant is
used. Use the Azure pricing calculator for the selected region; traffic,
subscription benefits, and idle-versus-active billing determine the actual
bill.

Before provisioning:

1. confirm the subscription's remaining benefits in **Azure Portal >
   Subscriptions > Free services**;
2. check the selected region in the
   [Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/);
3. create a Cost Management budget and alerts, for example at $10 and $25.

Budget alerts notify you; they do not automatically stop resources.

## Prerequisites

- A **public** GitHub repository containing EventHarbor.
- An Azure subscription with permission to create resources and role
  assignments.
- `irfanburakozer.com` active in Cloudflare DNS.
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-windows).
- [GitHub CLI](https://cli.github.com/).
- PowerShell 7 recommended.

Run every command from the repository root. Sign in first:

```powershell
az login
az account list --output table
gh auth login
gh auth status
```

Never commit `.env`, database URLs, database passwords, Azure credentials, or
other secrets. GitHub repository visibility and GHCR package visibility are
separate settings.

## 1. Publish the repository and images

Confirm that the correct remote is configured, then commit and push the project:

```powershell
git remote -v
git status
git add .
git commit -m "Prepare EventHarbor production deployment"
git push -u origin main
```

If necessary, make the repository public under **GitHub repository > Settings >
General > Danger Zone > Change repository visibility**.

After CI succeeds on `main`, the **Publish images and deploy production** workflow
builds the backend and frontend images. Azure deployment remains disabled until
step 5. If the image workflow does not start automatically, open **GitHub >
Actions > Publish images and deploy production > Run workflow**.

View the images from the repository's **Packages** section or from your GitHub
profile under **Packages**. Their names are:

- `REPOSITORY-backend`
- `REPOSITORY-frontend`

## 2. Make both GHCR packages public once

New GHCR packages are private by default even when their repository is public.
For each package:

1. open the package in GitHub;
2. select **Package settings**;
3. find **Danger Zone > Change package visibility**;
4. choose **Public** and complete GitHub's confirmation.

Do this for both the backend and frontend package. This is a one-time,
irreversible visibility change. Public GHCR images can be pulled anonymously,
so Azure requires no registry credentials.

## 3. Create the Azure foundation

Run the bootstrap script after reviewing the cost boundary:

```powershell
.\scripts\azure\bootstrap-foundation.ps1 `
  -SubscriptionId "YOUR_SUBSCRIPTION_ID" `
  -Location "eastus2" `
  -GitHubRepository "YOUR_GITHUB_USER/YOUR_REPOSITORY" `
  -ConfirmCosts
```

The script:

1. registers the required Azure providers;
2. creates `rg-eventharbor-prod`;
3. validates and deploys `infra/azure/foundation.bicep`;
4. creates the private network, Container Apps environment, Log Analytics, and
   PostgreSQL Flexible Server;
5. generates a strong database password in memory;
6. saves the database URL as the masked GitHub repository secret
   `EVENTHARBOR_DATABASE_URL`;
7. creates the non-secret repository variables used by the release workflow.

The password is not committed or printed. If PostgreSQL 17 or `Standard_B1ms` is
unavailable in `eastus2`, select a supported nearby region and rerun the script.

## 4. Configure branch-scoped Azure OIDC

Create passwordless trust between the repository's `main` branch and Azure:

```powershell
.\scripts\azure\configure-github-oidc.ps1 `
  -SubscriptionId "YOUR_SUBSCRIPTION_ID" `
  -GitHubOwner "YOUR_GITHUB_USER" `
  -GitHubRepository "YOUR_REPOSITORY" `
  -ConfigureGitHub
```

The script creates a Microsoft Entra application and service principal, assigns
`Contributor` only on `rg-eventharbor-prod`, and creates this exact federated
subject using GitHub's permanent owner and repository IDs:

```text
repo:YOUR_GITHUB_USER@OWNER_ID/YOUR_REPOSITORY@REPOSITORY_ID:ref:refs/heads/main
```

No Azure password or client secret is created. The client, tenant, and
subscription IDs are saved as non-secret GitHub repository variables. Azure
accepts deployment identity only from this repository's `main` branch.

GitHub.com repositories created, renamed, or transferred after July 15, 2026
use this immutable subject format. The setup script reads the canonical names
and numeric IDs from GitHub and updates an older name-only Azure credential when
necessary.

If role assignment fails, the signed-in Azure account needs `Owner` or `User
Access Administrator` permission for the one-time setup.

## 5. Enable and run the first deployment

Confirm these values under **GitHub repository > Settings > Secrets and
variables > Actions**:

- Secret: `EVENTHARBOR_DATABASE_URL`
- Variables: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
  `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `AZURE_NAME_PREFIX`,
  `AZURE_ENVIRONMENT_NAME`, `AZURE_CONTAINER_APPS_ENVIRONMENT`, and
  `EVENTHARBOR_APPLICATION_ENVIRONMENT`
- Optional explicit hostname variable: `EVENTHARBOR_CUSTOM_DOMAIN` (the release
  workflow defaults to `eventharbor.irfanburakozer.com`)

Enable Azure deployment:

```powershell
gh variable set AZURE_DEPLOYMENT_ENABLED `
  --repo "YOUR_GITHUB_USER/YOUR_REPOSITORY" `
  --body "true"
```

Open **GitHub > Actions > Publish images and deploy production > Run workflow**.
The workflow:

1. rebuilds the tested commit and publishes immutable images;
2. signs in to Azure with a short-lived OIDC token;
3. validates the Bicep templates;
4. runs Alembic and waits for the migration to succeed;
5. deploys Receiver Lab, API, worker, web, and daily cleanup;
6. preserves the existing custom hostname and Azure managed-certificate binding
   in both the release and automatic rollback;
7. verifies the custom hostname, web, API, database readiness, and control-room
   endpoints;
8. restores the previous application images if live verification fails.

Before the first custom-domain setup, the workflow summary contains the generated
Azure URL. Open it and complete one guided event flow before configuring the
custom domain. After the certificate is bound, later summaries use the public
custom URL and also retain the generated Azure origin as a diagnostic link.

Set `AZURE_DEPLOYMENT_ENABLED` to `false` whenever you intentionally want CI to
continue without changing Azure.

## 6. Configure DNS and the custom domain

Print the exact DNS values generated by Azure:

```powershell
.\scripts\azure\show-domain-records.ps1 `
  -SubscriptionId "YOUR_SUBSCRIPTION_ID"
```

Create the printed records under **Cloudflare > DNS > Records**:

| Type | Name | Value | Proxy status |
| --- | --- | --- | --- |
| CNAME | `eventharbor` | Generated `azurecontainerapps.io` hostname | **DNS only** |
| TXT | `asuid.eventharbor` | Azure verification ID | Not applicable |

If the zone already contains CAA records, also allow DigiCert:

| Type | Name | Flags | Tag | CA domain |
| --- | --- | ---: | --- | --- |
| CAA | `@` | `0` | `issue` | `digicert.com` |

Keep the CNAME gray-cloud **DNS only**. Azure's managed certificate must validate
the generated Container Apps hostname directly.

After DNS resolves:

1. open **Azure Portal > Container Apps > eventharbor-web-prod**;
2. select **Custom domains > Add custom domain**;
3. enter `eventharbor.irfanburakozer.com`;
4. choose an **Azure managed certificate**;
5. wait for validation and binding to finish.

This portal operation is required only for the first custom-domain setup. On
later releases, GitHub Actions discovers the bound managed-certificate resource
ID before changing the Container App and passes both the hostname and certificate
into Bicep. If the certificate cannot be found, the release stops before the web
app is changed. The normal deployment, live smoke test, and automatic rollback
all use `eventharbor.irfanburakozer.com`, so a release cannot silently fall back
to only the generated Azure hostname.

## 7. Verify production

Run the public health checks:

```powershell
$baseUrl = "https://eventharbor.irfanburakozer.com"
Invoke-RestMethod "$baseUrl/healthz"
Invoke-RestMethod "$baseUrl/api/health"
Invoke-RestMethod "$baseUrl/api/ready"
Invoke-RestMethod "$baseUrl/api/v1/control-room/overview"
```

Then verify in a private browser window:

1. the control room loads over HTTPS;
2. a guided event is accepted and stored;
3. its delivery attempts update;
4. a retryable incident recovers;
5. Receiver Lab records the final request.

Use these public project links:

```text
Demo:   https://eventharbor.irfanburakozer.com/
Source: https://github.com/YOUR_GITHUB_USER/YOUR_REPOSITORY
```

See [operations.md](operations.md) for health checks, logs, cost-saving modes,
rollback, and removal. See [infra/azure/README.md](../infra/azure/README.md) for
the infrastructure template contract.
