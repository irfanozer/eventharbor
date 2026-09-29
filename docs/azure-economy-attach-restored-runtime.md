# Attach an already restored database to a new economy VM

Use `scripts/azure-economy/attach-restored-runtime.ps1` after the reviewed data
restore, role isolation, and restore verification have finished. Do not run the
original initializer against restored databases. That initializer creates roles;
this separate helper only checks an existing application role and writes the
new VM's protected runtime configuration.

The helper accepts only the reviewed subscription, `rg-demos-economy`, West US 2,
and the matching `vm-eh-demos-economy` or `vm-px-demos-economy` resource. It checks
ownership tags and verifies the VM NIC and private PostgreSQL server are on the
same economy VNet. Supply the exact new PostgreSQL hostname from the reviewed
foundation outputs. Public database access is rejected.

Supply the retained application password through `-AppPassword` as a
`SecureString`. No administrator password is used. Run with `-DryRun` first to
validate input without reading credentials or contacting Azure.

The managed command connects with the application account using `verify-full`
TLS. Its database transaction is read-only. It checks the current database and
role, database ownership, absence of elevated privileges and role memberships,
denied CONNECT access to the other project's database, one Alembic version, and
the expected restored application tables with some retained data. Both projects'
CONNECT restrictions must already be in place.

After successful checks, it creates `/etc/<project>/runtime.env` owned by root
with mode `0600`. It refuses any existing file or symlink. It never creates a
role, modifies a database, starts an application, switches DNS, or deletes an old
deployment. A successful result does not prove that the website is running.

The application password is passed as a protected managed-command parameter in
a private local request file, not as a command-line argument. Local secret files
are removed on success or failure. Success requires a terminal instance view
with exit code zero, then removes the managed command. Failed or unconfirmed
commands are retained for controlled diagnosis; output is deliberately withheld.
If execution is uncertain, inspect it before retrying because the runtime file
may already exist.

Offline tests, without Azure or PostgreSQL access:

```powershell
python tests/deployment/test_attach_restored_runtime.py
pwsh -NoProfile -File tests/deployment/test_attach_restored_runtime.ps1
```
