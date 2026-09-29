# Change the verified preview runtime to its final hostname

After checking the application at its Azure preview hostname, use the separate
`scripts/azure-economy/update-runtime-hostname.ps1` helper. It supports either
project and only accepts the exact economy VM and that project's final
`irfanburakozer.com` hostname. It does not change DNS, request certificates,
restart services, deploy images, or access PostgreSQL.

Supply the observed West US 2 preview FQDN as `-ExpectedHostname`. The helper
checks it against the project's Azure public IP before changing anything.
Without `-Apply`, it only validates input locally and returns a plan.

```powershell
.\scripts\azure-economy\update-runtime-hostname.ps1 `
  -SubscriptionId "83099284-9ad4-4140-b8fe-8388b6d98a98" `
  -ResourceGroup "rg-demos-economy" `
  -VmName "vm-eh-demos-economy" `
  -Project "eventharbor" `
  -ExpectedHostname "REPLACE_WITH_OBSERVED_PREVIEW.westus2.cloudapp.azure.com" `
  -PublicHostname "eventharbor.irfanburakozer.com"
```

Replace the preview placeholder with the exact lowercase FQDN, review the plan,
then repeat with `-Apply`. PulseExchange uses `vm-px-demos-economy`, project
`pulseexchange`, and final hostname `pulseexchange.irfanburakozer.com`.

The remote operation requires a root-owned `0700` project directory and a
root-owned regular `0600` runtime file. It refuses symlinks, mismatched current
hostnames, duplicate hostname fields, or any existing backup or partial file.
It saves the exact original bytes in
`/etc/<project>/runtime.env.before-hostname-change`, also root-owned `0600`.
Only the `PUBLIC_HOSTNAME` value changes. All other bytes, including database
credentials and line endings, are preserved. The new file is installed with an
atomic replacement on Linux.

The helper never reads database credentials back to the local computer. A
successful run confirms the file update, not a working final domain. Complete
the reviewed DNS change, then run the normal deployment controller to apply
the new hostname and verify HTTPS. The old backup contains credentials and
must remain private. If a run fails or becomes uncertain, inspect the managed
command and protected files before retrying. Existing backups are not removed
or overwritten automatically.

Offline tests:

```powershell
python tests/deployment/test_update_runtime_hostname.py
pwsh -NoProfile -File tests/deployment/test_update_runtime_hostname.ps1
```
