# Shared PostgreSQL consolidation plan

## Status and scope

**Historical database-only alternative, superseded by the combined VM migration.** VM quota was approved on September 28, 2026. The selected route now copies both source databases directly to the new private economy server in West US 2, avoiding an intermediate move into the old EventHarbor server. Follow [the combined migration runbook](economy-migration.md), not the target selection below. Keep this alternative and the original Container Apps templates for future use.

The observations below were collected September 29, 2026 UTC before the combined migration. Publishing this document does not change a database, secret, deployment, DNS record, or billing allowance.

The earlier proposed path was database consolidation first, with the VM migration deferred. It would keep EventHarbor's existing private PostgreSQL server and move PulseExchange into a separate database on that server. Both sites would stay on their Container Apps hosts while VM quota was pending. The remaining sections preserve that historical design, not current execution instructions. Do not delete live load balancers or managed networking to pursue database savings.

Keep the original `infra/azure`, `scripts/azure`, and production workflow files unchanged. Record operational changes separately so the previous layout remains understandable and recoverable. This plan is separate from the new-VM migration in [azure-economy.md](azure-economy.md). Its initializer is guarded for new economy resources and must not be pointed at the existing production server as a shortcut.

## What this can save

The current screenshot showed these **September month-to-date costs, not monthly rates or a forecast**:

| Service | Month-to-date cost |
| --- | ---: |
| Container Apps | $61.04 |
| Load Balancer | $25.05 |
| PostgreSQL | $22.18 |
| Virtual Network | $8.76 |
| DNS | $1.20 |

Database consolidation alone cannot establish the approximately $20 total monthly target. Container Apps and its existing networking continue to accrue charges while retained. The selected VM route now has quota approval, but still requires migration validation and separately approved retirement before its full savings can be established.

East US 2 retail prices checked September 29, 2026 UTC were $0.017 per B1ms compute-hour and $0.115 per provisioned GB-month of PostgreSQL storage. Retiring one 32 GB server corresponds to approximately **$16.09 per 730-hour month** before allowances, discounts, taxes, backup changes, and new traffic: `730 x $0.017 + 32 x $0.115`. This is a list-price comparison, not a guaranteed reduction on the next invoice. Migration overlap adds cost.

Same-region peering costs $0.01 per GB outbound and $0.01 per GB inbound. A GB crossing between these two VNets therefore incurs approximately $0.02 across the two endpoint meters. Ten GB of total cross-peering transfers would be about $0.20. Frequent polling and returned data affect usage; measure it instead of assuming it is zero. See the [database retail prices](https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Azure%20Database%20for%20PostgreSQL%27%20and%20armRegionName%20eq%20%27eastus2%27%20and%20priceType%20eq%20%27Consumption%27) and [intra-region peering prices](https://prices.azure.com/api/retail/prices?$filter=productName%20eq%20%27Virtual%20Network%20Peering%27%20and%20armRegionName%20eq%20%27Global%27%20and%20priceType%20eq%20%27Consumption%27).

The [interim cost report](interim-cost-reduction.md#postgresql-allowance-finding) records 750 free B1ms hours and approximately 32 free GB-month already consumed September 1-8. Historical usage included four servers; only the EventHarbor and PulseExchange servers remain now. A new server or a merge does not reset that shared monthly allowance. A single eligible B1ms/32 GB server could fit a later month's published allowance, but backup usage and the exact benefit expiry still need confirmation. Do not describe the rest of September as free.

## Confirmed topology and capacity

| Item | EventHarbor, proposed retained host | PulseExchange, proposed source |
| --- | --- | --- |
| Resource group | `rg-eventharbor-prod` | `rg-pulseexchange-prod` |
| Server | `psql-eventharbor-prod-dodczqz7eaeps` | `psql-pulseexchange-prod-kj25jhmdlk6ik` |
| VNet | `vnet-eventharbor-prod`, `10.42.0.0/16` | `vnet-pulseexchange-prod`, `10.43.0.0/16` |
| Container Apps subnet | `10.42.0.0/24` | `10.43.0.0/24` |
| PostgreSQL subnet | `10.42.1.0/27` | `10.43.1.0/27` |
| Private DNS zone | `eventharbor.postgres.database.azure.com` | `pulseexchange.postgres.database.azure.com` |

Both servers were Ready in East US 2, PostgreSQL 17, Burstable `Standard_B1ms`, 32 GiB P4 storage, and 120 IOPS. Public network access, high availability, storage autogrow, and geo-redundant backup were disabled. Backup retention was seven days. Neither VNet had a peering. The address spaces do not overlap.

Seven-day metrics ending approximately September 29 at 02:09 UTC used hourly Average and Maximum aggregations. Averages below are means of the populated hourly averages; peaks are the highest returned maxima, not simultaneous combined measurements.

| Metric | EventHarbor | PulseExchange |
| --- | ---: | ---: |
| CPU average / peak | 10.06% / 70.66% | 13.81% / 85.55% |
| Memory average / peak | 60.33% / 85.80% | 60.66% / 84.86% |
| Active connections average / peak | 9.26 / 14 | 11.35 / 15 |
| Storage peak | 4.12 GiB | 4.12 GiB |
| CPU credits minimum / latest | 286 / 288 | 286 / 288 |
| Configured `max_connections` | 50 | 50 |

The latest 24-hour CPU averages were 9.94% and 14.02%. Both credit banks remained at 288 in every hourly interval, with no observed downward trend. They are healthy independently, but this does not prove a shared server has enough sustained headroom.

B1ms has one vCPU, 2 GiB memory, a 20% CPU baseline, and a maximum 288-credit bank. Simply adding the CPU averages gives about 24%, above that baseline. Shared engine and system overhead may decrease after consolidation, so the sum is not a prediction. If actual shared load stayed at 24%, the credit bank would drain by roughly 2.4 credits per hour and last about five days from full. A brief successful smoke test can conceal that problem. [CPU credit specifications](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/bv1-series)

Do not add memory percentages as if they were independent application allocations; caches and platform overhead are included. Likewise, adding reported storage includes duplicate server overhead and is not a logical database-size measurement. Storage appears ample, but measure restored data, WAL growth, I/O latency, and working memory. Combined observed connection peaks of 29 are below 50, but pools, revision overlap, jobs, and reserved/admin connections need explicit headroom.

## Gates before implementation

1. Confirm the maintenance window, who can approve rollback, the exact source and destination resources, and the retention period for protected exports. Refresh metrics, server parameters, extensions, database sizes, active jobs, and backups without printing credentials.
2. Record immutable image digests, active revisions, scale settings, job schedules, and secret names. Securely retain old connection values for recovery outside source control. Do not place URLs with passwords, database exports, or unredacted environment dumps in logs or GitHub artifacts.
3. Explicitly pause deployment automation for both repositories using the existing `AZURE_DEPLOYMENT_ENABLED` gate, and check for running deployments or manually started jobs. Changing a flag does not cancel an already running workflow. Keep workflow files intact. A later deployment must not silently restore an old endpoint, old privileged account, or unreviewed capacity settings.
4. Agree on bounded connection pools across both APIs, the worker, the processor, jobs, and possible extra revisions. The shared limit is 50 including reserved capacity, not 50 per application. Measure actual available connections before setting application-role connection limits.
5. Consider a separately tested PulseExchange polling profile of 500 or 1000 ms instead of the observed live 250 ms. Verify order-processing latency, REST/WebSocket delivery, heartbeat freshness, recovery, and correctness. This is a possible load reduction, not an already applied change or a guaranteed percentage saving.

## 1. Establish private connectivity

Create reviewed, bilateral VNet peerings between the two existing nonoverlapping networks, with only the required connectivity. Link EventHarbor's PostgreSQL private DNS zone to PulseExchange's VNet with auto-registration disabled. Peering alone does not supply the DNS link. Review NSGs and routes for TCP 5432 and retain the PostgreSQL subnet's required Storage connectivity. No public PostgreSQL firewall opening is part of this plan. [Private networking requirements](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private)

From an approved private execution context in PulseExchange's network, verify that the retained server's FQDN resolves privately, connects with TLS, and authenticates only as the intended role. Keep the existing DNS zones and links until rollback and dependency checks are complete. Use the server FQDN, not a hard-coded private IP.

## 2. Isolate both applications, including EventHarbor

Use separate databases `eventharbor` and `pulseexchange` with distinct application credentials. Confirm actual existing names before making any change. Do not reuse either server administrator account in a runtime URL, and do not share one application role across both databases.

- Create/review independent non-admin login roles, such as `eventharbor_app` and `pulseexchange_app`, with no superuser, role creation, database creation, replication, elevated role memberships, or access to the other application's objects.
- Restrict database `CONNECT`, temporary-object access where unnecessary, schema usage/create privileges, and grants inherited through `PUBLIC`. Being a different database owner alone does not establish isolation. Grant only the tables, sequences, and functions each runtime actually needs, including appropriate future-object default privileges.
- Prefer separate non-login ownership/migration roles with privileges confined to one application's database. Restore object ownership and grants deliberately; do not import source server roles or administrator memberships wholesale. Keep migration credentials separate from runtime credentials, available only for approved migration jobs. If the deployment cannot yet separate these identities, resolve that limitation explicitly before claiming least-privilege runtime access.
- Review existing EventHarbor ownership and grants before changing its runtime identity. Do not alter or replace its data while importing PulseExchange. Test both allowed access and denied cross-database access using the actual application roles.
- Update EventHarbor's existing consumers as well as PulseExchange's. Sharing the server while leaving EventHarbor on an administrator credential does not satisfy the isolation gate.

The shared server introduces a common outage, resource-exhaustion, and maintenance domain. Managed point-in-time restore operates on the server; rehearse recovery of the desired database into an isolated restored server instead of overwriting both live databases. [Backup and restore behavior](https://learn.microsoft.com/en-us/azure/postgresql/backup-restore/concepts-backup-restore)

## 3. Rehearse with protected backups

Take verified logical exports and confirm managed backup health for both databases. Use PostgreSQL-compatible dump/restore tools in an explicitly approved private context. Protect export storage and transfer, keep access limited, and set retention. An exported file existing is not proof it restores.

Restore PulseExchange into a temporary, isolated rehearsal database on the destination or another explicitly approved test target. Do not start a second live processor, seed job, or maintenance reset against production during rehearsal. Verify schema migration version, extensions, ownership, representative row counts and identifiers, sequence positions, orders, trades, commands, and replayable events. Ensure the rehearsal cannot issue real outbound deliveries or mutate the live demo.

Test the retained EventHarbor database with its restricted role and the PulseExchange rehearsal with its own restricted role. Test negative cross-database access. Exercise API readiness, delivery/retry behavior, matching, persistence, WebSocket reconnect/replay, and job permissions. Record timings and errors without exposing data or secrets. Remove only explicitly identified rehearsal objects after they are no longer needed; never run a blanket server cleanup.

## 4. Drain, make the final copy, and cut over

1. Enter the agreed brief maintenance window. Block new PulseExchange mutations and pause seed/reset jobs. Drain already accepted commands with its existing processor while preventing new submissions. Record any bounded, deliberately retained queue if complete draining is not possible.
2. Stop the old processor and all remaining PulseExchange writers after draining. Verify there are no running maintenance/migration executions, old active revisions, or database sessions that can still write. A stopped API alone is not a write freeze.
3. Quiesce EventHarbor consumers briefly if required for its role/ownership change. Preserve queued deliveries and in-flight lease/retry semantics. Never run old and new workers concurrently as an accidental validation shortcut. Leave its existing data intact.
4. Take the final consistent PulseExchange export after the write freeze. Restore only its destination database, mapping ownership and grants to its isolated roles. A previous rehearsal copy is stale once further writes occur. Recheck counts, migration version, sequence values, and durable pending work. Do not reset or reseed away history to make the migration appear successful.
5. Update every database consumer and the durable deployment source of its secret before reopening writes. Restart or replace affected processes/revisions because updating a Container Apps secret alone does not refresh running processes. Review inactive revisions and prevent them from being reactivated with the old endpoint.

| Project | Direct database consumers to inventory and update | Deployment secret |
| --- | --- | --- |
| EventHarbor | API, delivery worker, scheduled cleanup job, migration job, any operator scripts | `EVENTHARBOR_DATABASE_URL` |
| PulseExchange | API and its event relay, matching processor, scheduled maintenance/reset job, migration job, any operator scripts | `PULSEEXCHANGE_DATABASE_URL` |

The source templates use the per-resource secret name `database-url`. Inspect actual live resources and manual job definitions rather than treating this list as exhaustive. PulseExchange's seed job writes through the API, so keep it paused until the API is verified on the correct database. Frontends and EventHarbor's Receiver Lab do not need a database credential merely because other components do.

The current EventHarbor production workflow reads a repository-accessible secret without an environment declaration. PulseExchange's production job uses the `production` environment. Inspect environment, repository, and organization secret precedence and update the effective scope; a shadowing environment secret can override a repository update. Preserve names, mask values, and keep administrator credentials out of runtime secrets. If migration-only credentials are introduced, make their separate job configuration explicit and reviewed.

6. Start only the intended processor/worker and APIs. Verify readiness and actual destination identity through a non-secret query, then test both public sites: EventHarbor accepted delivery, rejection and retry; PulseExchange order, persisted trade, cancellation, WebSocket event and replay/reconnect. Verify neither application can access the other's database.
7. Resume traffic only when checks pass. Resume scheduled maintenance deliberately, preferably staggered: the original cleanup/reset templates both use 04:00 UTC. Record any schedule override so an old template redeploy cannot silently synchronize them again. Preserve bounded retention, not unlimited demo growth.
8. Record cutover time and exact revisions/digests. Re-enable deployment automation only after its effective secrets and behavior are verified. The original workflow can also restore the prior API allocation, so account for the [interim resize](interim-cost-reduction.md) when reviewing a later deployment.

No public DNS cutover is required for this database-only consolidation. Public site URLs must remain unchanged.

## 5. Observe before retiring the source

Observe **24-48 hours covering quiet traffic, representative demo use, and at least one maintenance cycle**. This is a minimum evidence window, not a guarantee against later growth. Do not delete the source based only on immediate health checks.

- Require stable readiness, correct durable work, acceptable response and order-processing latency, healthy WebSocket recovery, and no unexplained errors or restarts.
- Track shared CPU and `cpu_credits_remaining`. Aim for steady average CPU below the 20% baseline with headroom and no sustained credit depletion. Short spikes can consume credits; verify recovery instead of interpreting one full-bank reading as proof.
- Check memory trend, active connections and pool waits, IOPS/latency, storage/WAL growth, backup health, and the maintenance-cycle peak. Set warning thresholds appropriate to the observed baseline and keep admin connection capacity available.
- Verify old PulseExchange endpoints receive no application connections. Retain protected exports and a tested rollback procedure. Confirm the effective GitHub secrets and every job use the new endpoint before deciding the source is unused.

After successful immediate cutover checks, the unused source may be **explicitly approved for stopping** during the observation window to reduce compute cost while retaining it for recovery. Stopping is not deletion: storage remains billable, and the server automatically starts after seven days unless restarted sooner. Record that deadline and the responsible operator. Do not treat a stopped source as a permanent cost fix. [Stop/start behavior](https://learn.microsoft.com/en-us/azure/postgresql/overview), [stopped-server storage billing](https://azure.microsoft.com/en-us/pricing/details/postgresql/flexible-server/)

After observation, obtain approval for the exact retired server and any genuinely unused private DNS artifacts. Verify exports restore and retention is satisfied before deletion. Do not delete the PulseExchange resource group, VNet, Container Apps environment, load balancer, or other live dependencies. Recheck billed resources after cost-reporting delay; the $16.09 comparison is not realized while both servers remain running.

## Rollback boundaries

Before new writes on the destination, rollback can restore the saved effective secret configuration and intended old consumers, provided no source data has been changed or removed. Coordinate EventHarbor's role rollback separately and never restore an old whole-server snapshot over the live shared server.

After new writes on the destination, the old PulseExchange source is stale. Do not simply switch back and lose those writes. Freeze writes again, drain, export or reconcile the new data back to an approved source, validate it, then restore all consumers and deployment secrets together. Start only one intended processor. A stopped source may take time to restart. Keep the maintenance notice until correctness is verified.

## Completion record, to be filled only after execution

- Private connectivity and negative isolation tests: pending.
- Both application runtime identities and all effective secrets/jobs updated: pending.
- Rehearsal restore, final write freeze/copy, and public smoke tests: pending.
- Shared capacity observation and backup recovery test: pending.
- Explicit source stop/retirement approval and billing verification: pending.

Until these records contain real evidence, this remains a proposed consolidation, not a completed migration or a claim of free hosting.
