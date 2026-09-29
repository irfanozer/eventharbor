# Combined economy migration

## Scope

The approved replacement is two small Linux VMs and one shared private PostgreSQL 17 server in `rg-demos-economy`, West US 2. Each project keeps its own database and application account. This replaces the earlier proposal to move PulseExchange into the existing EventHarbor database server first.

The user approved discarding the old demo history and starting both replacement databases with fresh fictional data. A final copy of the old databases is not part of the current route. Retain the existing verified rehearsal backups while testing the replacement. The full-history copy procedure below remains an alternative for a future migration, not the next step for this deployment.

The original `infra/azure`, `scripts/azure`, and `deploy-production.yml` files are retained. Existing production deployment flags are paused during the migration so a push cannot reactivate an old writer or undo the cost profile. The new VM workflow is separate and manually dispatched.

This runbook describes the approved procedure. It does not claim that application deployment, public cutover, or old-resource retirement has completed.

## Cost boundary

Two `Standard_B2ats_v2` VMs share one 750-hour monthly allowance when the subscription is eligible. They do not each get 750 hours. Two always-running VMs use approximately 1,460 hours in a 730-hour month, leaving 710 paid hours before other use. The two 64 GiB P6 disks and one B1ms/32 GiB database also depend on the account's remaining benefits. Public IPv4, DNS, transfer, backup overage, and temporary overlap can be billed.

Quota approval does not grant free billing. Creating a replacement database does not reset an already consumed monthly allowance. Savings are not complete while the old paid environments and load balancers remain provisioned.

## Current route: fresh demo data

1. Run the economy foundation preflight and deploy the new resource group with explicit cost acknowledgements. Preserve the new administrator credentials and SSH recovery key in a protected location outside every repository.
2. Preserve the private snapshot of old connection settings, images, revisions, and job schedules, together with the verified rehearsal backups. Keep old deployment flags disabled. Inspect existing state rather than recreating migration jobs or replaying completed operations.
3. Use `cutover.ps1` to pause old scheduled jobs, stop old APIs, then stop old workers if they have not already been stopped. Confirm the writer freeze. The approved maintenance window permits this outage; it does not authorize bypassing target checks or exposing PostgreSQL publicly.
4. Use `scripts/azure-economy/initialize-runtime.ps1` once per project with the exact new server, project VM, and preview hostname. Supply the retained `-PostgresAdministratorPassword` and `-AppPassword` as `SecureString` values. The initializer requires an untouched empty foundation database, creates its restricted isolated role, transfers public-schema and database ownership in the guarded order, revokes PUBLIC access, and writes only root-owned runtime configuration. It refuses existing roles, data, or runtime files and never resets a database. Inspect a partial failure before retrying.
5. Initialize both projects before starting either replacement application. Do not run `FinalCopy`, restore rehearsal data, or use `attach-restored-runtime.ps1` for this fresh-data route.
6. Deploy verified immutable images to each VM. Deployment runs schema migrations; PulseExchange also seeds fictional starter markets through its API. For EventHarbor, create only missing Orders, Shipping, and Inventory Receiver Lab destinations under `http://eventharbor-receiver-prod/webhooks/`, matching the frontend definitions. Do not overwrite conflicting or disabled destinations, and do not retain or expose endpoint-creation signing secrets.
7. Test readiness, EventHarbor delivery and retry cases with new sample events, PulseExchange order matching and WebSocket confirmation, resource headroom, and HTTPS on the preview hostnames. Use the [hostname-only helper](azure-economy-runtime-hostname.md) for the reviewed final hostname, then coordinate public DNS, redeployment, and final HTTPS checks. The helper itself does not change DNS or restart services. New databases become authoritative for subsequent demo writes.
8. Configure the independent `azure-economy` GitHub environment and [VM-scoped identity](azure-economy-oidc.md). Keep old workflow files and credentials separate. Do not re-enable old production or shared-Container-Apps deployments after cutover.
9. Observe shared database CPU credits, connections, memory, storage, and both VMs under demo and maintenance load. Review an exact retirement list and retained backups before deleting old resources. The approved loss of old demo history does not mean old resources have already been deleted. Never dismantle Azure-managed networking groups manually.

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

Before replacement applications accept writes, the stopped original services can be recovered from their exact saved revisions and schedules. After replacement applications accept writes, the new databases are authoritative. Pointing back to stale originals would lose those writes; freeze and reconcile data before any rollback. A failed script must be inspected before retrying rather than bypassing an existing-target guard.

Neither these scripts nor the VM workflow automatically delete original databases, original environments, or migration backups. A stopped PostgreSQL server can restart automatically after seven days and continues to incur storage charges. Record an explicit follow-up decision instead of treating a stop as permanent retirement.

## Repository ownership

EventHarbor owns the single shared foundation, migration image, private access
and copy controllers, and the guarded [runtime attachment](azure-economy-attach-restored-runtime.md),
[hostname update](azure-economy-runtime-hostname.md), and [OIDC setup](azure-economy-oidc.md)
helpers. Those helpers accept the explicit project and support both applications.
PulseExchange retains its own runtime, application images, and manual VM deploy
workflow. Do not provision the shared foundation or duplicate migration jobs
from both repositories. The [historical database-only plan](shared-postgres-plan.md)
is retained for reference, not as a second migration step.
