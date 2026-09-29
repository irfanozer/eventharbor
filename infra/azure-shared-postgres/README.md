# Current-server PostgreSQL consolidation helper

This is a deliberately narrow, short-lived PostgreSQL 17 client image. It does not
run a database server, deploy infrastructure, stop applications, switch URLs or
GitHub secrets, change EventHarbor ownership, or delete any database or role.
Keep the existing deployment files unchanged. This helper is not the later VM
migration and is not evidence that a cloud migration has completed.

Fixed source: `psql-pulseexchange-prod-kj25jhmdlk6ik.postgres.database.azure.com`,
database `pulseexchange`. Fixed target server:
`psql-eventharbor-prod-dodczqz7eaeps.postgres.database.azure.com`.
Every database connection verifies the TLS certificate and hostname using system
CAs. Both servers and all client programs must use PostgreSQL major version 17.

## Commands

The Docker entrypoint is `python3 /app/migrate.py`. Select one argument:

| Command | Effect |
| --- | --- |
| `Inspect` | Read source counts/hashes, sequence states and Alembic version; inspect target DB existence and connections. No Azure storage calls or DB changes. |
| `Rehearse` | Back up a live consistent source snapshot, create only the new `pulseexchange_rehearsal` DB and `pulseexchange_rehearsal_app` role, restore and compare rows/schema/Alembic. |
| `FinalCopy` | Require stopped source clients and explicit freeze acknowledgment, back up, create new `pulseexchange` DB and `pulseexchange_app` role, restore, compare all manifests including sequences. |
| `Verify` | Re-read the saved private manifest and compare the selected restored DB. Does not restore, upload, alter or switch anything. |

Rehearsal and final copy both refuse an existing destination database **or role**,
including ones marked as created by this helper. There is no `--clean`, drop,
overwrite, retry-reset or automatic cleanup path. A failed creation/restore may
leave the new role or DB in place. Inspect and recover it deliberately; blindly
rerunning cannot overwrite it. Keep the supplied application passwords securely.

## Inputs

Inject secret values through the Container Apps job's secret references. Do not
put URLs/passwords in command arguments, Bicep outputs, source files or logs.

| Environment variable | Required for |
| --- | --- |
| `SOURCE_DATABASE_URL` | All modes. Existing PX `postgresql+asyncpg://.../pulseexchange?ssl=require` or `ssl=verify-full` URL. |
| `TARGET_ADMIN_DATABASE_URL` | All modes. Existing EH admin URL ending in `/eventharbor` or `/postgres`; helper selects `postgres` for administration. |
| `MIGRATION_RUN_ID` | All modes. Unique 8-64 character lowercase letter/digit/hyphen run ID. Reuse the copy's ID only for its Verify step. |
| `TARGET_APP_PASSWORD` | FinalCopy / final Verify. Operator-generated, securely retained 32-128 character password for `pulseexchange_app`. |
| `REHEARSAL_APP_PASSWORD` | Rehearse / rehearsal Verify. Separate retained password for the rehearsal role. |
| `BACKUP_STORAGE_ACCOUNT`, `BACKUP_CONTAINER` | Copy and Verify modes. Existing private Blob container, not created by this helper. |
| `WRITERS_FROZEN=true` | FinalCopy only. An acknowledgment, not a mechanism to stop writers. |
| `VERIFY_TARGET=rehearsal` | Verify the rehearsal target; default is `final`. |
| `IDENTITY_ENDPOINT`, `IDENTITY_HEADER` | Supplied by Container Apps, never manually logged. |
| `MIGRATION_IDENTITY_CLIENT_ID` | Optional UAMI client ID; omit for the system-assigned identity. |

For rehearsal verification run `python3 /app/migrate.py Verify` with the rehearsal
copy's `MIGRATION_RUN_ID`, `VERIFY_TARGET=rehearsal`, and its retained
`REHEARSAL_APP_PASSWORD`. Final verification uses the final copy's run ID and
`TARGET_APP_PASSWORD`. It must pass before application traffic starts: legitimate
subsequent writes cause the saved row/hash/sequence comparison to fail.

The job identity needs Storage Blob Data Contributor scoped to the private backup
container. Storage requests use a managed-identity token, HTTPS, a fixed Azure
Blob hostname, and no redirects. Container **properties** must show no anonymous
access. The account should also disable anonymous Blob access. The job must have
private DNS/network access to both database servers and permitted storage egress.

## Copy and verification guarantees

One read-only repeatable-read exported snapshot covers `pg_dump -Fc` and all
source row/schema manifests. Passwords are passed only in child environments;
psql role creation reads the new password from an environment variable. Raw
PostgreSQL and HTTP diagnostics are withheld, including successful operations that
emit warnings. Bounded client operations stop on error. Temporary files have an
owner-only umask and are removed when the helper exits normally or fails.

Before making any target database change, both copy modes upload and verify:

- `shared-postgres/<run-id>/pulseexchange.dump`
- `shared-postgres/<run-id>/manifest.json`

Uploads refuse existing blob names and check length, service-validated MD5 and
stored SHA256 metadata. The helper then downloads the manifest and archive,
compares the manifest with its local record, and checks the actual downloaded
archive bytes against the expected size and SHA256. Restore uses this downloaded
archive, not the original local dump. Independent Verify repeats the download
and byte checks before inspecting the target database.
Preserve the private container and the final run ID for
recovery. These archives contain application data and need the same access and
retention controls as the live database. Temporary container storage is not the
durable backup. The reviewed archive limit is 256 MiB; larger databases need a
separately reviewed resource/time budget.

The target DB preserves the source UTF8/libc locale. A new restricted LOGIN role
owns only the new DB and restored objects, with no superuser, CREATEDB,
CREATEROLE, replication or BYPASSRLS attributes. PostgreSQL 17 role self-membership
is requested only within the role-creation transaction. Database creation is a
separate operation because PostgreSQL does not allow it in a transaction.
PUBLIC CONNECT/TEMPORARY permissions are revoked on the new DB. Isolating the
legacy EventHarbor role and removing its administrator login from all consumers
is a separate mandatory cutover task; this helper does not do it implicitly.

Restore runs as the new application owner using `--no-owner --no-acl
--no-tablespaces --exit-on-error --single-transaction`, then `ANALYZE`. It never
uses `--create`, `--clean`, global-role restore or `REASSIGN OWNED`. Source DBs are
read-only to this helper. Source schema/code is trusted project code; a logical
restore can execute functions defined in that source.

For every ordinary/leaf table and materialized view in every non-system schema,
the manifest stores columns, row count and SHA256 over sorted canonical COPY
rows. JSONB text, C sort order, UTF8, UTC and PostgreSQL date/interval/float formats
are fixed. It also records all user sequences' `last_value`/`is_called` and the
Alembic version. Extensions other than plpgsql, large objects, foreign tables and
non-UTF8/libc databases stop the helper for separate review. Hashes check data
and basic column shape, not a semantic comparison of every index/function/ACL;
restore success and application smoke tests are still required.

Sequences are not MVCC. A live rehearsal therefore verifies rows/schema/Alembic
but does **not** assert sequence equality. FinalCopy checks sequences plus a fresh
source snapshot before and after restore and refuses other source sessions at
several boundaries. These checks supplement, not replace, a genuine externally
enforced writer freeze. Stop API, processor, scheduled maintenance, migration
jobs and all automatic deployment workflows before final copy and keep them
stopped through verification and the controlled URL switch.

## Operator gates and recovery

1. Inspect private reachability, PG17 versions, connection headroom and source
   size; lower runtime pools through the separately reviewed cutover procedure.
2. Rehearse, then independently Verify that rehearsal using its saved run ID.
   Test the restored schema/application compatibility before final maintenance.
3. Stop all PX writers/jobs and automatic redeployment. Check there are no old
   processes that can restart. Supply `WRITERS_FROZEN=true` and a new final run ID.
4. FinalCopy, then Verify. Keep both source servers and the durable dump intact.
   Switch all four PX DB consumers plus the effective GitHub environment secret
   in the separate controller. Start exactly one processor. Verify health, real
   order/match/cancel behavior, stored results and WebSocket/reconnect behavior.
5. Observe combined capacity for 24-48 hours before separately approving any
   source retirement. Once the new DB accepts writes, simply switching back to
   the old URL loses those new writes; recovery requires a reviewed copy-back or
   reconciliation plan. Nothing here deletes or stops the old server.

Offline checks (no Azure or PostgreSQL access):

```sh
python tests/shared_postgres/test_migrate.py
```

These mocks do not establish real PostgreSQL SQL/restore compatibility, managed
identity permissions, Blob reachability or running-site correctness. The private
cloud rehearsal remains required before FinalCopy.

References: [PostgreSQL 17 pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html),
[Container Apps managed identity](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity),
[Blob container properties](https://learn.microsoft.com/en-us/rest/api/storageservices/get-container-properties).

## Persistent application deployment profile

`apps.bicep`, `migration.bicep`, and the additive
`.github/workflows/deploy-shared-postgres.yml` keep the reviewed shared-server
settings on later application releases. The API uses 0.25 vCPU and 0.5 GiB RAM.
Every direct database consumer uses a pool of two connections with no overflow.
Existing image names, database secret names, domains, replica limits, retry
settings, worker polling, and the 04:00 UTC cleanup schedule are unchanged.
The original `infra/azure` files and production workflow remain available.

This profile is off by default. Publishing images and deployment both require
the repository variable `AZURE_SHARED_DEPLOYMENT_ENABLED=true`. Before opting in:

1. Complete and verify the controlled database migration and restricted-role
   cutover. This workflow does not copy databases or change connection strings.
2. Keep the existing `EVENTHARBOR_DATABASE_URL` secret updated with the verified
   application connection string in its original scope. Do not put it in a file.
3. Set `AZURE_DEPLOYMENT_ENABLED=false` in every scope where it is defined and
   disable the original production workflow in GitHub Actions. Its file stays
   intact. Disabling it also prevents its original image-build job from running.
4. Set the shared opt-in variable at repository scope, then run **Publish images
   and deploy shared PostgreSQL** on `main`. Keep only one deployment profile
   enabled. Both new jobs fail if the old flag is `true` in their effective scope.

The original concurrency group and OIDC configuration are retained. The new
workflow only switches the application and migration template paths, including
application rollback. Re-enabling the original profile would restore its old
resource sizes and connection pools; it is not a database rollback procedure.
These settings reduce resource use but do not guarantee free-tier eligibility
or a zero bill.

Run the offline profile checks with:

```sh
python tests/shared_postgres/test_deployment_profile.py
```
