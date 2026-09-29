# Combined economy migration

## Scope

The approved replacement is two small Linux VMs and one shared private PostgreSQL 17 server in `rg-demos-economy`, West US 2. Each project keeps its own database and application account. This replaces the earlier proposal to move PulseExchange into the existing EventHarbor database server first.

The original `infra/azure`, `scripts/azure`, and `deploy-production.yml` files are retained. Existing production deployment flags are paused during the migration so a push cannot reactivate an old writer or undo the cost profile. The new VM workflow is separate and manually dispatched.

## Cost boundary

Two `Standard_B2ats_v2` VMs share one 750-hour monthly allowance when the subscription is eligible. They do not each get 750 hours. Two always-running VMs use approximately 1,460 hours in a 730-hour month, leaving 710 paid hours before other use. The two 64 GiB P6 disks and one B1ms/32 GiB database also depend on the account's remaining benefits. Public IPv4, DNS, transfer, backup overage, and temporary overlap can be billed.

Quota approval does not grant free billing. Creating a replacement database does not reset an already consumed monthly allowance. Savings are not complete while the old paid environments and load balancers remain provisioned.

## Execution order

1. Run the economy foundation preflight and deploy the new resource group with explicit cost acknowledgements. Preserve the new administrator credentials and SSH recovery key in a protected location outside every repository.
2. Preserve a private snapshot of the old connection settings, images, active revisions, and job schedules. Keep both old databases authoritative until the final write freeze.
3. Use `scripts/azure-shared-postgres/prepare-economy-access.ps1` to create only temporary direct peering and private DNS access for the migration jobs. Do not expose PostgreSQL publicly.
4. Build the separately tagged database tools image through `shared-postgres-tools.yml`. Offline checks and a real PostgreSQL 17 copy test must pass before publication. Pin the resulting image digest.
5. Create the two manual copy jobs using `economy-operator.ps1`. Inspect the sources and rehearse into separate rehearsal databases. Independently Verify each rehearsal's backup upload/download, restored table hashes, row counts, and Alembic version. Live source sequences can advance during rehearsal; exact sequence equality is required for the frozen final copies. Test application access separately without starting a second live worker or processor.
6. Use `cutover.ps1` to pause scheduled jobs, stop old APIs, then stop old workers. Confirm there are no remaining source writers. Recovery actions require explicit confirmation that the old source is still authoritative.
7. Make final consistent copies into the new foundation's empty project databases. The helper must refuse a nonempty destination. Verify both copies before starting any replacement application.
8. Use `scripts/azure-economy/attach-restored-runtime.ps1` with the retained application passwords. It checks isolation and the restored schema using read-only SQL, writes only the root-owned runtime configuration, and does not create databases or start applications. Do not use the fresh-database initializer on restored data.
9. Deploy verified immutable images to each VM. Test readiness, EventHarbor delivery and retry cases, PulseExchange order matching and WebSocket confirmation, resource headroom, and HTTPS on the preview hostnames. Use the [hostname-only helper](azure-economy-runtime-hostname.md) for the reviewed final hostname, then coordinate public DNS, redeployment, and final HTTPS checks. The helper itself does not change DNS or restart services.
10. Configure the independent `azure-economy` GitHub environment and [VM-scoped identity](azure-economy-oidc.md). Keep old workflow files and credentials separate. Do not re-enable old production or shared-Container-Apps deployments after cutover.
11. Observe shared database CPU credits, connections, memory, storage, and both VMs under demo and maintenance load. Review an exact retirement list and verified retained backups before deleting old resources. Never dismantle Azure-managed networking groups manually.

## Recovery

If rehearsal stopped before restoring any objects because Azure retained ownership of the `public` schema, use the explicit `RecoverRehearsal` action only after inspecting the failed execution. It downloads and checks the original saved archive, requires the exact empty rehearsal database and retained role with matching run markers, and refuses a populated or unrelated target. It repairs only that rehearsal schema and restores the verified archive in one transaction. It does not replace the backup, drop a database, or change the final application database. Any temporary administrator connection permission used for the repair is revoked afterward. Run `Verify -VerifyRehearsal` separately after recovery succeeds.

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
