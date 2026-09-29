# Interim Azure cost reduction

This is a reversible change to the existing Container Apps deployment while the replacement VM quota is pending. It does not deploy the VM foundation or migrate databases. Existing `infra/azure`, `scripts/azure`, and production workflows are preserved.

## Scope

Reduce only the API container from 0.5 vCPU / 1 GiB to 0.25 vCPU / 0.5 GiB. Keep the minimum replica count at one. Do not change application images, workers, secrets, database connections, domains, scaling rules, or live networking.

Seven days of Azure Monitor data ending September 29, 2026 UTC showed:

| API | Maximum CPU cores | Maximum working-set memory | Maximum restart count |
| --- | ---: | ---: | ---: |
| EventHarbor | 0.0182 | 69.0 MiB | 0 |
| PulseExchange | 0.0560 | 80.3 MiB | 0 |

These observations support the smaller allocation at current traffic, not a guarantee of capacity for future traffic. Check memory, CPU, restarts, latency and application behavior after applying.

## Inspect, apply, restore

Run from the matching project repository in PowerShell 7.4 or later. Omit `-Apply` for a read-only inspection:

```powershell
.\scripts\azure-economy\resize-existing-api.ps1 `
  -SubscriptionId "83099284-9ad4-4140-b8fe-8388b6d98a98" `
  -Project eventharbor
```

Use `-Project pulseexchange` for PulseExchange. Add `-Apply` to resize. Add both `-Apply -Restore` to restore 0.5 vCPU / 1 GiB on a healthy app. The script requires exact production ownership, one healthy active revision, and a supported starting allocation. It checks that the new revision is healthy and other captured settings remain unchanged. No secrets are printed.

If a rollout fails, inspect the active and ready revisions first. Do not blindly repeat an update. Single revision mode normally retains the previous serving revision until the replacement is ready. The restore script deliberately refuses an unhealthy starting state; use the captured previous revision and Azure's revision controls for an individually reviewed recovery.

**The original production workflow still specifies the original allocation. Running it again can restore the larger API size.** Reapply this interim script after such a deployment if the smaller allocation is still wanted. This is intentional preservation of the previous deployment, not a permanent change to its defaults. No workflow is disabled by this script.

## Cost expectations

East US 2 Consumption prices checked September 29, 2026 UTC were $0.000024 per active vCPU-second, $0.000003 per idle vCPU-second, and $0.000003 per GiB-second. At 730 hours, reducing both APIs saves about $11.83 if entirely idle, or $39.42 if entirely active, before subscription-wide free grants. Actual savings depend on traffic and the active/idle billing mix. These are not total-bill forecasts.

The database and networking bills are unchanged by this resize. A lower total bill requires the later migration and retirement of superseded resources.

## PostgreSQL allowance finding

The Free services page reportedly showed PostgreSQL as "Not in use", but a read-only September-to-date Cost Management query found 750 hours of `B1MS Compute - Free` and approximately 32 GB-month of `Storage Data Stored - Free`, both at $0. Paid usage beyond those allowances was also recorded. Historical usage included the removed FieldMerge and RouteWise servers as well as the two current servers.

The explicit query window was September 1 through September 29 at 00:00 UTC, queried September 29 at 01:44:50 UTC. The last returned usage day was September 28, with no further result pages:

| Meter | Quantity | Recorded pre-tax cost |
| --- | ---: | ---: |
| B1MS Compute - Free | 750 hours | $0 |
| B1MS | 996 hours | $16.932 |
| Storage Data Stored - Free | 31.999999968 GB-month | $0 |
| Storage Data Stored | 45.6 GB-month | $5.244 |

The free-meter rows were reported September 1 through September 8. Paid-meter rows began September 8. This is evidence that the shared free amounts were applied and exhausted, not evidence that every current server has its own free allowance. The $22.176 recorded total is not a final invoice after credits, and delayed billing records may still arrive.

Both current servers already use B1ms with 32 GiB storage. Creating another matching server does not replenish an exhausted monthly allowance. The replacement design uses one shared server with separate databases and roles so future usage can fit the allowance while eligible. Backup allowance usage and the exact benefit expiry still require confirmation. Preserve and verify database backups before any migration or deletion.

## Verification

### Applied September 29, 2026 UTC

- EventHarbor API changed from revision `eventharbor-api-prod--0000012` to `eventharbor-api-prod--0000013`.
- PulseExchange API changed from revision `pulseexchange-api-prod--0000006` to `pulseexchange-api-prod--0000007`.
- Both ended with one active healthy revision, 0.25 CPU / 0.5 GiB, and minimum replicas one. The script verified the captured non-resource settings and immutable images were unchanged.
- Nine offline resize-safety scenarios passed, including read-only default, apply, restore, identity guards, healthy-state guards, sidecar rejection, image drift detection, and idempotence.
- All seven listed public health/readiness/summary endpoints returned HTTP 200 before the rollout.
- The initial EventHarbor receiver request returned 503 while the separately scaled-to-zero receiver started. The API readiness stayed HTTP 200. After the receiver became healthy, the isolated functional checks passed: permanent rejection HTTP 400, and busy-receiver sequence 429, 429, 200 with observed gaps 5.226471 and 5.205121 seconds.
- PulseExchange's existing live smoke test passed: fictional ORBIT trade 3 units at 48 ticks, persisted REST result, WebSocket receipt, disconnected-event recovery, and cancellation of its resting test order.
- The original deployment workflows were not run or edited. No database migration, resource deletion, DNS change, worker polling change, or new infrastructure deployment was performed.

After applying, check EventHarbor `/healthz`, `/api/health`, `/api/ready`, and `/api/v1/control-room/overview`. The following opt-in smoke check creates two small, isolated demo events using an existing receiver endpoint:

```powershell
python .\scripts\azure-economy\smoke-existing.py --run-demo
```

It checks a permanent schema rejection and a successful busy-receiver flow with two five-second retries. It does not reset other visitors' receiver scenarios or delete history.

For PulseExchange, check `/healthz`, `/health/ready`, `/api/v1/diagnostics/summary`, and run its existing `scripts/smoke.py` with `PULSEEXCHANGE_SMOKE_URL` set to the live HTTPS origin. That smoke test places fictional orders, verifies matching through REST and WebSocket, and cancels its resting test order.

Sources: [Container Apps billing](https://learn.microsoft.com/en-us/azure/container-apps/billing), [revision rollout behavior](https://learn.microsoft.com/en-us/azure/container-apps/revisions), [East US 2 pricing API](https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Azure%20Container%20Apps%27%20and%20armRegionName%20eq%20%27eastus2%27%20and%20priceType%20eq%20%27Consumption%27), [free-service reporting](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/check-free-service-usage).
