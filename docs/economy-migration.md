# Combined economy migration

## Scope

The approved replacement is two small Linux VMs and one shared private PostgreSQL 17 server in `rg-demos-economy`, West US 2. Each project keeps its own database and application account. This replaces the earlier proposal to move PulseExchange into the existing EventHarbor database server first.

The user approved discarding the old demo history and starting both replacement databases with fresh fictional data. A final copy of the old databases is not part of the current route. Retain the existing verified rehearsal backups while testing the replacement. The full-history copy procedure below remains an alternative for a future migration, not the next step for this deployment.

The original `infra/azure`, `scripts/azure`, and `deploy-production.yml` files are retained. Existing production deployment flags are paused during the migration so a push cannot reactivate an old writer or undo the cost profile. The new VM workflow is separate and manually dispatched.

## Verified deployment status on 2026-09-29

- [EventHarbor](https://eventharbor.irfanburakozer.com) and [PulseExchange](https://pulseexchange.irfanburakozer.com) are live with fresh fictional data on `vm-eh-demos-economy` and `vm-px-demos-economy` in `rg-demos-economy`, West US 2. Their shared private PostgreSQL server keeps separate databases and restricted roles. TLS, database isolation, and each role's 12-connection limit were verified.
- Public EventHarbor checks passed for permanent rejection (`400`) and recovery after rate limiting (`429`, `429`, `200`), with retry gaps of approximately 5.14 seconds. PulseExchange checks passed for a fictional ORBIT trade in REST and WebSocket results, reconnect replay, and cancellation of the test order.
- At inspection, every application container was healthy, reported zero restarts, and had no current OOM flag. Maintenance timers were enabled. Available VM memory was approximately 250 MiB for EventHarbor and 280 MiB for PulseExchange; these are observations, not capacity guarantees.
- The [EventHarbor deployment run](https://github.com/irfanozer/eventharbor/actions/runs/36580517335) succeeded. Its controller revision was `43d23fd`; the reused immutable application images still identify application revision `5d50c89`. Legacy deployment files are unchanged, old deployment flags remain disabled, and the new manual economy workflow is enabled.
- **Old-resource retirement is complete.** Azure confirmed that `rg-eventharbor-prod`, `rg-pulseexchange-prod`, and both Azure-managed networking groups are absent, including their old databases, Container Apps, jobs, load balancers, and public IPs. Replacement infrastructure and the retained backup account were verified after deletion. Old history was intentionally discarded; retained rehearsal backups are earlier snapshots, not final copies. Already-accrued charges and delayed billing entries can still appear.

## Cost boundary

Two `Standard_B2ats_v2` VMs share one 750-hour monthly allowance when the subscription is eligible. They do not each get 750 hours. Two always-running VMs use approximately 1,460 hours in a 730-hour month, leaving 710 paid hours before other use. The two 64 GiB P6 disks and one B1ms/32 GiB database also depend on the account's remaining benefits. Public IPv4, DNS, transfer, backup overage, and temporary overlap can be billed.

Quota approval does not grant free billing. Creating a replacement database does not reset an already consumed monthly allowance. Savings are not complete while the old paid environments and load balancers remain provisioned.

### Estimate verified on 2026-09-29

USD retail rates for West US 2, assuming 730 running hours per VM and database each month. The benefits column assumes the subscription remains eligible and the listed monthly allowances are available, with no competing usage.

| Resource | Verified retail rate | With benefits | Without benefits |
| --- | --- | ---: | ---: |
| Two Linux B2ats_v2 VMs | [$0.0094 per VM-hour][vm-rates] | $6.67 | $13.72 |
| Two 64 GiB P6 LRS disks | [$9.2801 per disk-month][disk-rates] | $0.00 | $18.56 |
| One B1ms PostgreSQL server, 32 GB | [$0.017/hour plus $0.115/GB-month][postgres-rates] | $0.00 | $16.09 |
| Two regional Standard IPv4 addresses | [$0.005 per address-hour][ip-rates] | $7.30 | $7.30 |
| One private DNS zone | [$0.50/month][dns-rates] | $0.50 | $0.50 |
| **Monthly baseline** | Before usage extras and taxes | **$14.47** | **$56.17** |

The allowances cover [750 shared hours for eligible VM instances](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/create-free-services), [two P6 disks](https://marketplace.microsoft.com/en-us/product/microsoft.freeaccountvirtualmachine?tab=Overview), and [750 B1ms PostgreSQL hours with 32 GB of storage and backup storage](https://azure.microsoft.com/en-us/pricing/purchase-options/azure-account). These introductory benefits are time-limited, not permanent free hosting.

Plan for approximately **$15-20/month while those benefits apply**, not a spending cap. Private DNS queries add $0.40 per million; retained Hot LRS backup blobs have a [retail storage rate of $0.0184/GB-month][blob-rates] plus transactions before any storage allowance. Transfer, backup overage, other usage, and taxes are additional. The estimate does not erase already-accrued charges or this month's consumed allowances, and temporary overlap can increase the migration month's bill. After benefits end, the same configuration has an approximately **$56.17/month baseline**, plus those extras.

[vm-rates]: https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Virtual%20Machines%27%20and%20armRegionName%20eq%20%27westus2%27%20and%20armSkuName%20eq%20%27Standard_B2ats_v2%27%20and%20priceType%20eq%20%27Consumption%27
[disk-rates]: https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Storage%27%20and%20armRegionName%20eq%20%27westus2%27%20and%20meterName%20eq%20%27P6%20LRS%20Disk%27%20and%20priceType%20eq%20%27Consumption%27
[postgres-rates]: https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Azure%20Database%20for%20PostgreSQL%27%20and%20armRegionName%20eq%20%27westus2%27%20and%20priceType%20eq%20%27Consumption%27
[ip-rates]: https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Virtual%20Network%27%20and%20armRegionName%20eq%20%27westus2%27%20and%20priceType%20eq%20%27Consumption%27
[dns-rates]: https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Azure%20DNS%27%20and%20skuName%20eq%20%27Private%27%20and%20priceType%20eq%20%27Consumption%27
[blob-rates]: https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Storage%27%20and%20armRegionName%20eq%20%27westus2%27%20and%20skuName%20eq%20%27Hot%20LRS%27%20and%20priceType%20eq%20%27Consumption%27

## Current route: fresh demo data

1. Run the economy foundation preflight and deploy the new resource group with explicit cost acknowledgements. Preserve the new administrator credentials and SSH recovery key in a protected location outside every repository.
2. Preserve the private snapshot of old connection settings, images, revisions, and job schedules, together with the verified rehearsal backups. Keep old deployment flags disabled. Inspect existing state rather than recreating migration jobs or replaying completed operations.
3. Use `cutover.ps1` to pause old scheduled jobs, stop old APIs, then stop old workers if they have not already been stopped. Confirm the writer freeze. The approved maintenance window permits this outage; it does not authorize bypassing target checks or exposing PostgreSQL publicly.
4. Use `scripts/azure-economy/initialize-runtime.ps1` once per project with the exact new server, project VM, and preview hostname. Supply the retained `-PostgresAdministratorPassword` and `-AppPassword` as `SecureString` values. The initializer requires an untouched empty foundation database, creates its restricted isolated role, transfers public-schema and database ownership in the guarded order, revokes PUBLIC access, and writes only root-owned runtime configuration. It refuses existing roles, data, or runtime files and never resets a database. Inspect a partial failure before retrying.
5. Initialize both projects before starting either replacement application. Do not run `FinalCopy`, restore rehearsal data, or use `attach-restored-runtime.ps1` for this fresh-data route.
6. Deploy verified immutable images to each VM. Deployment runs schema migrations; PulseExchange also seeds fictional starter markets through its API. For EventHarbor, create only missing Orders, Shipping, and Inventory Receiver Lab destinations under `http://eventharbor-receiver-prod/webhooks/`, matching the frontend definitions. Do not overwrite conflicting or disabled destinations, and do not retain or expose endpoint-creation signing secrets.
7. Test readiness, EventHarbor delivery and retry cases with new sample events, PulseExchange order matching and WebSocket confirmation, resource headroom, and HTTPS on the preview hostnames. Use the [hostname-only helper](azure-economy-runtime-hostname.md) for the reviewed final hostname, then coordinate public DNS, redeployment, and final HTTPS checks. The helper itself does not change DNS or restart services. New databases become authoritative for subsequent demo writes.
8. Configure the independent `azure-economy` GitHub environment and [VM-scoped identity](azure-economy-oidc.md). Keep old workflow files and credentials separate. Do not re-enable old production or shared-Container-Apps deployments after cutover.
9. Observe shared database CPU credits, connections, memory, storage, and both VMs under demo and maintenance load. Review an exact retirement list and retained backups before deleting old resources. Confirm retirement independently of approval to discard history; the dated deployment status above records the result for this migration. Never dismantle Azure-managed networking groups manually.

## Alternative: preserve full history in a future migration

Do not mix this route with fresh initialization. It requires untouched final destination databases and is retained for future use when the old data must be preserved.

1. Preserve protected source settings and keep both old databases authoritative until the final write freeze. Use `scripts/azure-shared-postgres/prepare-economy-access.ps1` for reviewed temporary direct peering and private DNS access. Never expose PostgreSQL publicly for a copy.
2. Build the separately tagged tools image through `shared-postgres-tools.yml`. Require offline checks and the real PostgreSQL 17 copy test, then pin its digest. Create the two manual copy jobs with `economy-operator.ps1`.
3. Inspect each source and rehearse into separate rehearsal databases. Independently run `Verify -VerifyRehearsal` for backup upload/download, restored table hashes, row counts, and Alembic version. Live source sequences can advance during rehearsal. Test application access without starting a second live worker or processor.
4. Pause source scheduled jobs, stop APIs, then stop workers using `cutover.ps1`. Confirm no source writers remain. Make final consistent copies into the empty foundation databases. Refuse nonempty targets rather than cleaning them. Verify both final copies, including sequence state, before starting either replacement application.
5. Use `scripts/azure-economy/attach-restored-runtime.ps1` with retained application passwords. It checks role isolation and restored schema using read-only SQL and writes root-owned runtime configuration without creating databases or starting applications. Do not use the fresh initializer on restored data.
6. Continue with immutable-image deployment, preview and public functional checks, independent VM-scoped CI setup, and separately reviewed source retirement as described above.

## Recovery

For the full-history alternative, if rehearsal stopped before restoring any objects because Azure retained ownership of the `public` schema, use the explicit `RecoverRehearsal` action only after inspecting the failed execution. It downloads and checks the original saved archive, requires the exact empty rehearsal database and retained role with matching run markers, and refuses a populated or unrelated target. It repairs only that rehearsal schema and restores the verified archive in one transaction. It does not replace the backup, drop a database, or change the final application database. Any temporary administrator CONNECT or CREATE permission used for the repair is revoked afterward. Run `Verify -VerifyRehearsal` separately after recovery succeeds. Rehearsal recovery is not a prerequisite for the current fresh-data route.

Recovery from saved original revisions and schedules was available only while the original resources and databases still existed. The original database servers have now been deleted under the approved discard-history route. The replacement databases are authoritative. Returning to the old deployment profile requires recreating its infrastructure from the preserved files and planning a new data transfer. Retained rehearsal backups are earlier snapshots, not a complete rollback of current data. Freeze and reconcile writes before any future cutover. Inspect a failed script before retrying rather than bypassing an existing-target guard.

The repository migration helpers and VM workflow do not automatically delete original databases, original environments, or migration backups. This migration uses a separately reviewed and explicitly authorized retirement operation. For future migrations, a stopped PostgreSQL server can restart automatically after seven days and continues to incur storage charges. Record an explicit follow-up decision instead of treating a stop as permanent retirement.

## Repository ownership

EventHarbor owns the single shared foundation, migration image, private access
and copy controllers, and the guarded [runtime attachment](azure-economy-attach-restored-runtime.md),
[hostname update](azure-economy-runtime-hostname.md), and [OIDC setup](azure-economy-oidc.md)
helpers. Those helpers accept the explicit project and support both applications.
PulseExchange retains its own runtime, application images, and manual VM deploy
workflow. Do not provision the shared foundation or duplicate migration jobs
from both repositories. The [historical database-only plan](shared-postgres-plan.md)
is retained for reference, not as a second migration step.
