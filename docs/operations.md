# EventHarbor operations

This runbook covers the small Azure public-demo deployment. It deliberately favors
clarity and low cost over high availability.

## Release model

Every successful main-branch CI run publishes images tagged with the full Git SHA.
Azure deployment runs only when the repository variable
`AZURE_DEPLOYMENT_ENABLED` equals `true`.

The release order is migration, internal services, worker, public web app, then
live verification. Database migrations must remain backward compatible with the
previous application image because image rollback does not reverse a migration.

To temporarily stop automatic Azure releases while still running CI and building
images:

```powershell
gh variable set AZURE_DEPLOYMENT_ENABLED `
  --repo "YOUR_GITHUB_USER/YOUR_REPOSITORY" `
  --body "false"
```

## Quick health check

```powershell
$baseUrl = "https://eventharbor.irfanburakozer.com"
Invoke-RestMethod "$baseUrl/healthz"
Invoke-RestMethod "$baseUrl/api/health"
Invoke-RestMethod "$baseUrl/api/ready"
Invoke-RestMethod "$baseUrl/api/v1/control-room/overview"
```

Interpretation:

- `/healthz` proves Nginx can serve the frontend container;
- `/api/health` proves the API process is alive;
- `/api/ready` proves the API can execute a bounded PostgreSQL query;
- the overview proves the same public proxy path used by the React app works.

## Inspect the Azure resources

```powershell
az containerapp list `
  --resource-group rg-eventharbor-prod `
  --output table

az containerapp job execution list `
  --resource-group rg-eventharbor-prod `
  --name eventharbor-migrate-prod `
  --output table
```

The expected apps are:

- `eventharbor-web-prod`: external ingress;
- `eventharbor-api-prod`: internal ingress;
- `eventharbor-receiver-prod`: internal ingress, at most one replica;
- `eventharbor-worker-prod`: no ingress;
- `eventharbor-migrate-prod`: manual job, not a continuously running app.
- `eventharbor-cleanup-prod`: daily scheduled job that removes only expired,
  terminal synthetic event histories.

## Logs

The Container Apps environment sends console logs to Log Analytics. Use **Azure
Portal > Container Apps > app name > Log stream** for the fastest inspection.

Useful Azure CLI commands are:

```powershell
az containerapp logs show `
  --resource-group rg-eventharbor-prod `
  --name eventharbor-api-prod `
  --follow

az containerapp logs show `
  --resource-group rg-eventharbor-prod `
  --name eventharbor-worker-prod `
  --follow
```

For a failed migration, open the migration job in Azure Portal and choose the
failed execution, then inspect its console logs before rerunning the workflow.

The cleanup job runs at `04:00 UTC` with a seven-day default retention window,
500-row batches, and a 5,000-event maximum per execution. Check its execution
history periodically:

```powershell
az containerapp job execution list `
  --resource-group rg-eventharbor-prod `
  --name eventharbor-cleanup-prod `
  --output table
```

## Rollback

The workflow records the currently running frontend and backend image references
before updating the apps. If live verification fails, it redeploys those previous
images automatically.

For a manual rollback:

1. open a previous successful GitHub Actions release;
2. copy its full commit SHA;
3. identify the two GHCR image names tagged with that SHA;
4. rerun `infra/azure/apps.bicep` with those image references and the existing
   masked database URL;
5. repeat the four health checks.

Do not downgrade the database schema automatically. Fix forward or use a tested,
explicit Alembic downgrade only when the data consequences are understood.

## Cost-saving modes

### Normal public-demo mode

- web: minimum 0 replicas;
- API: minimum 0 replicas;
- Receiver Lab: minimum 0 replicas, maximum 1;
- worker: exactly 1 replica;
- PostgreSQL: running.

The Receiver Lab stays warm through a normal short demo flow. If a very long pause
causes its in-memory scenario to disappear, start a different incident and resend.
Keeping it at one permanent replica improves continuity but increases cost.

### Pause active delivery processing

Scale the worker to zero when the demo will not be used:

```powershell
az containerapp update `
  --resource-group rg-eventharbor-prod `
  --name eventharbor-worker-prod `
  --min-replicas 0 `
  --max-replicas 1
```

Restore it before sharing the demo:

```powershell
az containerapp update `
  --resource-group rg-eventharbor-prod `
  --name eventharbor-worker-prod `
  --min-replicas 1 `
  --max-replicas 1
```

Events accepted while the worker is paused remain stored and resume later.

### Stop PostgreSQL compute temporarily

Pause the worker first so it does not continuously reconnect while PostgreSQL is
stopped.

```powershell
az postgres flexible-server stop `
  --resource-group rg-eventharbor-prod `
  --name YOUR_GENERATED_POSTGRES_SERVER_NAME
```

Storage still incurs charges, the website will not work, and Azure automatically
starts a stopped Flexible Server after seven days. Start it before a demo:

```powershell
az postgres flexible-server start `
  --resource-group rg-eventharbor-prod `
  --name YOUR_GENERATED_POSTGRES_SERVER_NAME
```

Then restore the worker to one minimum replica with the command in the previous
section.

Microsoft documents this behavior in
[Stop and start a Flexible Server](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/how-to-stop-start-server).

## Security boundary

- Only the web app is public.
- Nginx enforces a small request-body ceiling and shared request-rate limits.
- The API accepts only the configured Receiver Lab routes, so visitors cannot turn
  it into a general-purpose webhook sender.
- Browser API calls are same-origin.
- PostgreSQL is private and requires TLS.
- GitHub uses OIDC and stores no Azure password.
- The database URL is a masked GitHub production-environment secret and a Container
  Apps secret reference.
- The demo accepts synthetic portfolio data only. Never enter real customer,
  payment, or personal data.

Receiver Lab scenario state is intentionally in memory and its maximum replica
count is one. This makes the failure simulator easy to understand but is not a
high-availability design. PostgreSQL also has no HA replica in this cost-focused
deployment.

## Alerts to configure in Azure Portal

At minimum create:

- Cost Management budget alerts at 50%, 80%, and 100%;
- PostgreSQL CPU, storage, and active-connections alerts;
- Container Apps restart/unhealthy-replica alerts;
- Log Analytics retention of 30 days or less for this demo.

## Permanently remove the deployment

Deleting the resource group removes the apps, private database, logs, and recovery
history. Export anything needed first. Then verify the exact resource group name:

```powershell
az group show --name rg-eventharbor-prod --output table
```

Only when permanent deletion is intended:

```powershell
az group delete `
  --name rg-eventharbor-prod `
  --yes `
  --no-wait
```

This is not recoverable through the repository. Remove the Cloudflare CNAME/TXT
records afterward, disable `AZURE_DEPLOYMENT_ENABLED`, and remove the GitHub OIDC
application if it will not be reused.
