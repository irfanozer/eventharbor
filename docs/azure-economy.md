# Lower-cost Azure deployment

## Status: selected replacement, cutover still requires verification

This deployment is the selected replacement for the existing Container Apps deployment. Follow the [combined migration runbook](economy-migration.md): both source databases move directly to the new shared server. The new foundation has been provisioned after quota approval, but that is not proof that database copy, application cutover, or source retirement is complete. It does not replace or edit `infra/azure`, `scripts/azure`, or the production workflow.

On September 28, 2026, Azure reported both `Standard_B1s` and `Standard_B2ats_v2` as unavailable to this subscription in East US 2. The regional `standardBasv2Family` quota was zero. Do not run a paid substitute because a free-eligible size is unavailable. Resolve capacity and quota first, then confirm the actual free-service meters in this subscription.

Those pre-approval read-only checks also found both sizes restricted in East US. In West US 2, B2ats_v2 was available without a zone pin but initially had zero family quota; B1s remained restricted. These are historical capacity observations, not the current approved quota state.

The selected layout is **two B2ats_v2 VMs in West US 2**, with explicit acknowledgment that they share one allowance and some compute is paid. Four Basv2 quota cores were approved and the foundation was provisioned in `rg-demos-economy`. This was an explicit choice, not an automatic paid fallback. The templates still retain their distinct-SKU default for other reviewed deployments.

## What changes

| Workload | Replacement | Reason |
| --- | --- | --- |
| EventHarbor | One Ubuntu B2ats_v2 VM with prebuilt application containers | Selected nonzonal West US 2 host; no Container Apps environment or load balancer |
| PulseExchange | One Ubuntu B2ats_v2 VM with prebuilt application containers | Separate application host; its hours share the same allowance as the first host |
| Both databases | One private PostgreSQL B1ms server, 32 GiB, separate databases and users | Avoids paying for two continuously running database servers |
| HTTPS | Caddy on each VM, existing Nginx frontend images | Keeps same-origin API and WebSocket behavior, with no paid gateway |
| Portfolio | Its existing Cloudflare deployment | It is not an Azure resource and does not need to move |

The VMs share a private virtual network. They can communicate privately, but the applications do not need to call each other. Only the database is shared. An EventHarbor database user cannot read PulseExchange tables, and vice versa. Database capacity and a database outage are shared, so this is a low-traffic portfolio layout, not a high-availability production design.

Only ports 80 and 443 are public. SSH is closed unless an explicit administrator CIDR is supplied. Deployments use Azure Managed Run Command, not a public Docker socket or a permanently exposed SSH port. Images are built in CI, never on the small VMs. Runtime limits total 512 MiB per application host, plus up to 128 MiB for one serialized job. This is a starting allocation that still needs a real memory and CPU soak test on the chosen size.

## Cost conditions, not a promise of free hosting

Microsoft publishes 750 monthly hours **each** for eligible B1s, B2ats_v2 and B2pts_v2 VM offers. Two instances of the same eligible type share that type's allowance. This deployment supports the two x86 types; the ARM type cannot run these existing x86 images unchanged.

The template's lower-cost default remains B1s plus B2ats_v2, using distinct eligible types if capacity becomes available. Selecting two B2ats_v2 hosts requires `-AcknowledgeSharedVmHours`; the scripts never make that substitution automatically.

The two 64 GiB Premium P6 disks and one PostgreSQL B1ms server with 32 GiB storage are also chosen to match published free-account offers. Eligibility, exact meters, unused monthly allowance and the offer end date must be checked on this subscription. Creating a matching resource does not create a new allowance.

Expected paid items include two Standard public IPv4 addresses, private DNS, and usage beyond any included storage, backup, or transfer allowance. At a planning rate of $0.005 per IP-hour, the two addresses alone are about $7.30 in a 730-hour month. Verify regional and subscription pricing before provisioning; that is a planning assumption, not a live quote. Do not assume the portal's legacy free Public IP meter covers Standard IPv4.

The [Azure Retail Prices API](https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Virtual%20Machines%27%20and%20armRegionName%20eq%20%27westus2%27%20and%20armSkuName%20eq%20%27Standard_B2ats_v2%27%20and%20priceType%20eq%20%27Consumption%27) returned **$0.0094/hour** for regular Linux B2ats_v2 compute in West US 2 on September 28, 2026. Exclude Windows, Spot and Cloud Services rows when checking that result.

| Two-B2ats_v2 planning estimate, 730-hour month | Amount |
| --- | ---: |
| VM compute: `(2 x 730 - 750) x $0.0094`, if 750 eligible hours remain | $6.67 |
| Two Standard IPv4 addresses at the planning rate above | $7.30 |
| One private DNS zone, planning allowance | $0.50 |
| Subtotal, **excluding** uncovered database/disks, transfer, backup overages and tax | **$14.47** |

If the VM free allowance does not apply or has already been consumed, the same subtotal rises to about **$21.52** before those other costs. This option therefore does not meet the $20 target without the required allowances. Confirm the IP/DNS rates and the other meter eligibility before execution.

The **target is under $20 per month only while the required free allowances actually apply and traffic remains small**. If PostgreSQL or either VM is not covered, stop and re-price the layout. The preflight checks capacity and quota, but cannot certify the billing offer or the remaining free-hour balance.

Old and new resources overlap during testing and both can accrue costs. The existing PostgreSQL servers also consume the same subscription allowance. September's usage does not reset because a new server is created. Savings begin only after a successful cutover and approved removal of the old resources. Credits already used or expired do not erase new charges. Re-price before the free offer ends.

Set a subscription budget of $20 with actual-cost notifications at 50%, 80% and 100%, plus a forecast notification. A budget sends alerts; it does not cap spending or stop resources. Review Cost Analysis by resource daily during migration and again after billing data catches up. Exclude no resource groups from that review merely because their names contain `economy` or `free`.

Sources: [Azure free-account offers](https://azure.microsoft.com/en-us/pricing/purchase-options/azure-account), [shared free allowances](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/create-free-services), [checking free-service usage](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/check-free-service-usage), [public IP pricing](https://azure.microsoft.com/en-us/pricing/details/ip-addresses/).

## 1. Resolve availability before any paid operation

Use PowerShell 7.4 or later, Azure CLI, and the correct signed-in subscription. Run the read-only preflight from the EventHarbor checkout:

```powershell
.\scripts\azure-economy\bootstrap-foundation.ps1 `
  -SubscriptionId "83099284-9ad4-4140-b8fe-8388b6d98a98" `
  -Location "westus2" `
  -EventHarborVmSize "Standard_B2ats_v2" `
  -PulseExchangeVmSize "Standard_B2ats_v2" `
  -AcknowledgeSharedVmHours `
  -FreeAllowanceExpiry "2027-09-12" `
  -CheckOnly
```

This is expected to block on the current zero Basv2 quota, and to flag the old databases' shared allowance use. In Azure Portal, open **Quotas > Compute**, select the subscription and **West US 2**, and request a limit of **four vCPUs for the Standard Basv2 Family**. Each chosen VM needs two cores. A positive quota does not guarantee regional capacity; rerun preflight after approval. Do not accidentally select B2als_v2 or B2pls_v2: these are different sizes.

For the distinct-SKU alternative, first obtain B1s subscription access and sufficient B-family quota in a region that supports both VMs and PostgreSQL. Omit the two explicit B2ats size arguments and the shared-hour acknowledgment to use that default. It is currently unavailable in the checked regions.

Confirm the subscription's PostgreSQL B1ms compute, 32 GiB storage/backup and Premium P6 disk meters before asserting `-ConfirmFreeAllowances`. If the portal does not show them, ask Azure billing support rather than treating zero-dollar historical meter records as proof.

## 2. Provision only the separate foundation

After capacity, quota, free-service eligibility and the cost estimate are accepted, run the bootstrap with the same region and size parameters, replacing `-CheckOnly` with `-Deploy`, `-ConfirmCosts` and `-ConfirmFreeAllowances`. Add `-AcknowledgeSharedAllowances` only after reviewing the existing servers' remaining allowance consumption and temporary overlap cost. Review its parameter help first:

```powershell
Get-Help .\scripts\azure-economy\bootstrap-foundation.ps1 -Full
```

Supply an SSH **public** key even if public SSH remains closed. Keep the private key and the PostgreSQL administrator password outside every repository. The bootstrap accepts a SecureString and sends deployment parameters from a private temporary file. Use a new resource group, normally `rg-demos-economy`. Never point this deployment at either existing production resource group.

Record the output VM names, public DNS names, and PostgreSQL server name. These are not secrets. Do not publish administrator credentials. Test using each VM's Azure-provided DNS name before moving custom domains.

## 3. Initialize the two isolated database roles

From the EventHarbor checkout, run `scripts/azure-economy/initialize-runtime.ps1` once for each project. It accepts `-Project eventharbor` or `-Project pulseexchange`, plus `-SubscriptionId`, `-ResourceGroup`, `-VmName`, `-PostgresHost`, `-PostgresAdministrator`, `-PublicHostname` and `-AcmeEmail`.

Use the **new** economy database host and each **new** VM DNS name. The script prompts for the new database administrator password, creates a random application password, and transmits secrets as protected Run Command parameters. The application URL is initially stored in `/etc/<project>/runtime.env`, owned by root with mode 0600. Deployment also keeps root-only current, previous and failed release configuration files as documented in the runtime README. Routine CI deployment does not need database credentials.

The initializer checks VM and database ownership tags. It refuses to overwrite an existing configuration or rotate an existing application role. If a partial initialization fails, inspect the protected operation and recover deliberately; do not repeatedly generate new passwords. A `-DryRun` validates input and prints a safe plan without reading secrets or calling Azure.

This prepares empty new databases. It does **not** copy old history. Do not delete the old databases based on successful initialization.

## 4. Deploy and test without changing production DNS

Both repositories have a separate `scripts/azure-economy/deploy.ps1`. Use the image digests and full source commit from one successful existing image release, not floating tags or arbitrary mixed releases:

```powershell
.\scripts\azure-economy\deploy.ps1 `
  -SubscriptionId "83099284-9ad4-4140-b8fe-8388b6d98a98" `
  -ResourceGroup "rg-demos-economy" `
  -VmName "VM_NAME_FROM_FOUNDATION_OUTPUT" `
  -BackendImage "EXACT_BACKEND_IMAGE_WITH_SHA256_DIGEST" `
  -FrontendImage "EXACT_FRONTEND_IMAGE_WITH_SHA256_DIGEST" `
  -ExpectedSourceRevision "FULL_40_CHARACTER_SOURCE_COMMIT" `
  -DryRun
```

Replace all placeholders, then remove `-DryRun` for the actual new-VM deployment. Run it from the matching project repository. Do not print a real environment file or run unredacted `docker inspect` / `docker compose config`; those can reveal database credentials.

The controller copies only its allowlisted public runtime files. It checks image source/revision labels and waits for the remote command's actual execution status and exit code. A successful Azure provisioning status alone is not application success. Application updates on these small hosts have a brief stop/start period, not a rolling deployment.

Test both temporary HTTPS hosts, including:

- Every EventHarbor receiver case, its request evidence, the five-second busy response behavior, and a complete worker-delivered event.
- A PulseExchange order, matching, persisted trade, and the corresponding WebSocket update. Verify separate visitors do not share one proxy rate-limit identity.
- Browser security headers and TLS, private database resolution and verified database certificates.
- VM reboot recovery, bounded disk/log growth, maintenance behavior, and an overnight low-traffic soak with no OOM kills, repeated restarts or depleted CPU credits.
- A managed PostgreSQL backup restore test. A saved image digest or runtime.env file is not a database backup.

Container health checks, syntax checks and offline tests do not substitute for these tests.

## 5. Set up the separate CI/CD environment

Each repository includes a new manual-only `deploy-economy.yml`. Nothing automatically switches production to it. Create a protected GitHub environment named `azure-economy`, restrict deployments to `main`, and use a **separate** Entra application/service principal for each project's new deployment. Keep existing production identities and variables intact.

Use issuer `https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`, and the repository's actual environment subject. For the current repositories the subjects are:

```text
repo:irfanozer@56190015/eventharbor@1343057274:environment:azure-economy
repo:irfanozer@56190015/pulse-exchange@1346864837:environment:azure-economy
```

Verify repository ownership/IDs and its configured subject format before creating the federated credential. Assign the new deployment principal **Virtual Machine Contributor at only its corresponding VM resource scope**. Do not give it subscription-wide Contributor. Initialization and shared infrastructure provisioning are operator tasks, not routine deployment permissions.

Add these environment-scoped secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`. Add environment variables `ECONOMY_RESOURCE_GROUP`, `ECONOMY_VM_NAME`, and initially `ECONOMY_DEPLOYMENT_ENABLED=false`. No permanent Azure access keys or database URLs belong in this environment.

Once the temporary host has passed testing, set the new deployment flag to `true`. The workflow requires a manual main-branch run, exact successful push/main CI evidence, immutable images, and matching source/revision labels. It reuses released images; it does not build on the VM.

Before publishing migration files or cutting over, deliberately disable the old auto-deployment flag `AZURE_DEPLOYMENT_ENABLED` in each repository. This does not stop the live applications; it prevents an unrelated push from redeploying the old infrastructure. Keep the old workflow files. Re-enabling the old flag later is a deliberate return to the previous architecture, not a free operation.

## 6. Preserve data, switch, then retire the old resources

Do not change the live databases, domains, or old deployments during the initial parallel test. Before cutover, agree on a brief write freeze and a retention/backup plan. The current database endpoints are private, so a data migration needs an explicitly reviewed private access path. Do not open PostgreSQL publicly to make a dump easier.

Take and verify backups of both old databases. For a full-history move, restore into the new matching database with ownership mapped to its application role, then rerun migrations and smoke tests. Never run two processors/workers against the same production database as a shortcut. A test restore made earlier is stale once new writes occur; a final write freeze and final copy are needed to avoid losing those writes.

Only after approval, update each VM's protected hostname configuration and move the corresponding custom-domain DNS record to its new VM IP. Plan Caddy certificate issuance and DNS cache overlap. Keep the old endpoints available during verification. Check the final public URLs, not only the temporary Azure hostnames.

After the new sites are verified and data retention is satisfied, review an explicit deletion list for the old EventHarbor and PulseExchange resources. Do not delete anything in `rg-demos-economy`. Remove old Container Apps environments through their owner resources; do not manually dismantle their Azure-managed `ME_*` networking resource groups. Old PostgreSQL servers, jobs, logs, and managed networking must be accounted for before declaring the savings complete.

No automatic teardown command is included. The original deployment files remain usable, but the old running resources must eventually be removed for the target monthly cost to be realistic.

## Validation recorded so far

On September 28, 2026, local Bicep compilation, PowerShell parsing and dry runs, shell syntax, both Compose configurations, both runtime test suites, the initializer's five offline tests, and the preflight/controller regression suites passed. The Windows controller tests include exact ampersand preservation in query strings. The runtime tests contain one intentional EventHarbor skip for a PulseExchange-only TLS requirement.

Re-run the shared checks from the EventHarbor checkout:

```powershell
python .\tests\deployment\test_economy_initializer.py
.\tests\deployment\test_economy_preflight.ps1
.\tests\deployment\test_economy_controller.ps1
python .\infra\azure-economy\runtime\test_runtime.py
```

Run the runtime test in PulseExchange too. The controller suite checks the sibling PulseExchange checkout if it is present, but does not require one for EventHarbor-only CI.

The Docker engine was unavailable, so actual container execution and the 1 GiB memory limits remain unverified. These checks are separate from real cloud verification. This guide must not be read as confirmation that new Azure resources exist, that free allowances have been applied, or that the production migration is complete.
