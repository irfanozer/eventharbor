#Requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$VmName,
    [Parameter(Mandatory)][ValidateSet('eventharbor', 'pulseexchange')][string]$Project,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9-]+\.postgres\.database\.azure\.com$')][string]$PostgresHost,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9][a-z0-9.-]+\.[a-z]{2,63}$')][string]$PublicHostname,
    [Parameter(Mandatory)][ValidatePattern('^[^\s@]+@[^\s@]+\.[^\s@]+$')][string]$AcmeEmail,
    [SecureString]$AppPassword,
    [switch]$DryRun
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-AttachRuntimeValue {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object -or $null -eq $Object.PSObject.Properties[$Name]) { return $Default }
    return $Object.$Name
}

function Invoke-AttachRuntimeAzureJson {
    param([string[]]$Arguments)
    Invoke-EconomyAzureJson -Arguments $Arguments -Sensitive
}

function Assert-AttachRuntimeTargets {
    param($Plan, $Group, $Vm, $Server, $Nic)
    foreach ($item in @(
        @{ Resource = $Group; Id = $Plan.GroupId; Application = 'EventHarbor-PulseExchange' },
        @{ Resource = $Vm; Id = $Plan.VmId; Application = $Plan.Project },
        @{ Resource = $Server; Id = $Plan.ServerId; Application = 'EventHarbor-PulseExchange' },
        @{ Resource = $Nic; Id = $Plan.NicId; Application = $Plan.Project }
    )) {
        if ((Get-AttachRuntimeValue $item.Resource 'id') -ine $item.Id -or
            (Get-AttachRuntimeValue $item.Resource 'location') -cne 'westus2') { throw 'Resource identity or region differs from the reviewed economy foundation.' }
        $tags = Get-AttachRuntimeValue $item.Resource 'tags'
        foreach ($expected in @{ application = $item.Application; costProfile = 'economy'; environment = 'economy'; managedBy = 'Bicep' }.GetEnumerator()) {
            if ((Get-AttachRuntimeValue $tags $expected.Key) -cne $expected.Value) { throw 'Resource ownership tags do not match the reviewed economy foundation.' }
        }
    }
    if ($Server.fullyQualifiedDomainName -cne $Plan.PostgresHost -or $Server.version -ne '17' -or
        $Server.network.publicNetworkAccess -cne 'Disabled' -or
        $Server.network.delegatedSubnetResourceId -ine "$($Plan.VnetId)/subnets/snet-postgres") {
        throw 'The restored PostgreSQL server must be version 17 on the exact private economy subnet.'
    }
    $interfaces = @($Vm.networkProfile.networkInterfaces)
    if ($interfaces.Count -ne 1 -or $interfaces[0].id -ine $Plan.NicId) { throw 'The VM must use only its reviewed economy network interface.' }
    $configurations = @($Nic.ipConfigurations)
    if ($configurations.Count -ne 1 -or $configurations[0].subnet.id -ine "$($Plan.VnetId)/subnets/snet-demo-vms" -or $Nic.enableIPForwarding -ne $false) {
        throw 'The VM and PostgreSQL server must use their separate subnets in the same economy VNet.'
    }
}

function Get-AttachRuntimeHostScript {
    return @'
#!/usr/bin/env bash
set -euo pipefail
umask 077
cloud-init status --wait >/dev/null
python3 - <<'PY'
import json, os, pathlib, re, subprocess, sys
from urllib.parse import quote

def fail():
    sys.exit('Restored runtime validation failed. No configuration was written. Database diagnostics are withheld.')

if os.geteuid() != 0:
    fail()
project = os.environ['ECONOMY_PROJECT']
tables = {
    'eventharbor': ('endpoints', 'events', 'deliveries', 'delivery_attempts'),
    'pulseexchange': ('market_commands', 'orders', 'trades', 'market_events', 'runtime_heartbeats'),
}
if project not in tables:
    fail()
host = os.environ['ECONOMY_PGHOST']
role = project + '_app'
other = 'pulseexchange' if project == 'eventharbor' else 'eventharbor'
password = os.environ['ECONOMY_APP_PASSWORD']
if not re.fullmatch(r'[a-z0-9-]+\.postgres\.database\.azure\.com', host) or not password:
    fail()
folder = pathlib.Path('/etc') / project
if folder.is_symlink() or not folder.is_dir() or folder.stat().st_uid != 0:
    fail()
destination = folder / 'runtime.env'
if destination.exists() or destination.is_symlink():
    sys.exit('Runtime configuration already exists. Attach never overwrites or rotates credentials.')
values = {
    'PUBLIC_HOSTNAME': os.environ['ECONOMY_PUBLIC_HOSTNAME'],
    'ACME_EMAIL': os.environ['ECONOMY_ACME_EMAIL'],
    'CADDY_IMAGE': 'caddy:2.11.4-alpine@sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b',
    project.upper() + '_DATABASE_URL': f'postgresql+asyncpg://{role}:{quote(password, safe="")}@{host}:5432/{project}?ssl=verify-full',
}
if (not re.fullmatch(r'[a-z0-9][a-z0-9.-]+\.[a-z]{2,63}', values['PUBLIC_HOSTNAME']) or
        not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', values['ACME_EMAIL'])):
    fail()
if any(not value or any(c.isspace() or c in "\"'`$#" for c in value) for value in values.values()):
    fail()
env = os.environ.copy()
env.update(PGHOST=host, PGPORT='5432', PGUSER=role, PGPASSWORD=password, PGDATABASE=project,
           PGSSLMODE='verify-full', PGSSLROOTCERT='/etc/ssl/certs/ca-certificates.crt',
           PGCONNECT_TIMEOUT='15', PGCLIENTENCODING='UTF8',
           PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=30000')
counts = ', '.join(f"'{table}', (SELECT count(*) FROM public.{table})" for table in tables[project])
sql = """
BEGIN READ ONLY;
SELECT json_build_object(
 'role', current_user, 'database', current_database(),
 'superuser', r.rolsuper, 'create_database', r.rolcreatedb,
 'create_role', r.rolcreaterole, 'replication', r.rolreplication,
 'bypass_rls', r.rolbypassrls, 'login', r.rolcanlogin,
 'memberships', (SELECT count(*) FROM pg_auth_members WHERE member = r.oid),
 'owner', (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()),
 'other_connect', has_database_privilege(current_user, '__OTHER__', 'CONNECT'),
 'tls', EXISTS (SELECT 1 FROM pg_stat_ssl WHERE pid = pg_backend_pid() AND ssl),
 'versions', (SELECT json_agg(version_num) FROM public.alembic_version),
 'table_rows', json_build_object(__COUNTS__)
) FROM pg_roles r WHERE r.rolname = current_user;
COMMIT;
""".replace('__OTHER__', other).replace('__COUNTS__', counts)
try:
    result = subprocess.run(['psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1'],
                            input=sql, text=True, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=60)
    if result.returncode:
        fail()
    record = json.loads(result.stdout)
    expected = {'role': role, 'database': project, 'owner': role, 'login': True,
                'superuser': False, 'create_database': False, 'create_role': False,
                'replication': False, 'bypass_rls': False, 'memberships': 0,
                'other_connect': False, 'tls': True}
    if any(record.get(key) != value for key, value in expected.items()):
        fail()
    versions = record.get('versions')
    if not isinstance(versions, list) or len(versions) != 1 or not isinstance(versions[0], str) or not versions[0].strip():
        fail()
    rows = record.get('table_rows')
    if (not isinstance(rows, dict) or set(rows) != set(tables[project]) or
            any(type(value) is not int or value < 0 for value in rows.values()) or sum(rows.values()) == 0):
        fail()
except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
    fail()
os.chmod(folder, 0o700)
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        os.fchmod(output.fileno(), 0o600)
        os.fchown(output.fileno(), 0, 0)
        output.write(''.join(f'{key}={value}\n' for key, value in values.items()))
        output.flush()
        os.fsync(output.fileno())
except BaseException:
    destination.unlink(missing_ok=True)
    raise
print('Restored application database verified. Root-only runtime configuration created. No application started and no database modified.')
PY
'@
}

function Invoke-AttachRestoredRuntime {
    param([guid]$SubscriptionId, [string]$ResourceGroup, [string]$VmName, [string]$Project,
          [string]$PostgresHost, [string]$PublicHostname, [string]$AcmeEmail,
          [SecureString]$AppPassword, [switch]$DryRun)
    $short = switch ($Project) { 'eventharbor' { 'eh' }; 'pulseexchange' { 'px' }; default { throw 'Invalid project.' } }
    if ("$SubscriptionId" -ne '83099284-9ad4-4140-b8fe-8388b6d98a98' -or $ResourceGroup -cne 'rg-demos-economy' -or
        $VmName -cne "vm-$short-demos-economy" -or $PostgresHost -cnotmatch '^psql-demos-economy-[a-z0-9]+\.postgres\.database\.azure\.com$') {
        throw 'Only the reviewed economy subscription, group, VM, and new PostgreSQL hostname are accepted.'
    }
    if ($PublicHostname -cnotmatch '^[a-z0-9][a-z0-9.-]+\.[a-z]{2,63}$' -or
        $AcmeEmail -notmatch '^[^\s@]+@[^\s@]+\.[^\s@]+$' -or $AcmeEmail -match '[\s"''`$#]') { throw 'Invalid public hostname or ACME contact.' }
    if ($DryRun) { return [pscustomobject]@{ Action = 'DryRun'; Project = $Project; VmName = $VmName; AzureCalls = 0; DatabaseModified = $false; ApplicationStarted = $false } }
    if ($null -eq $AppPassword -or $AppPassword.Length -eq 0) { throw 'Supply the retained restored application password as a SecureString. No administrator credentials are accepted.' }
    $groupId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"
    $plan = [pscustomobject]@{
        Project = $Project; PostgresHost = $PostgresHost; GroupId = $groupId
        VmId = "$groupId/providers/Microsoft.Compute/virtualMachines/$VmName"
        NicId = "$groupId/providers/Microsoft.Network/networkInterfaces/nic-$short-demos-economy"
        VnetId = "$groupId/providers/Microsoft.Network/virtualNetworks/vnet-demos-economy"
        ServerId = "$groupId/providers/Microsoft.DBforPostgreSQL/flexibleServers/$($PostgresHost.Split('.')[0])"
    }
    $group = Invoke-AttachRuntimeAzureJson @('group', 'show', '--subscription', "$SubscriptionId", '--name', $ResourceGroup)
    $vm = Invoke-AttachRuntimeAzureJson @('vm', 'show', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroup, '--name', $VmName)
    $server = Invoke-AttachRuntimeAzureJson @('postgres', 'flexible-server', 'show', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroup, '--name', $PostgresHost.Split('.')[0])
    $nic = Invoke-AttachRuntimeAzureJson @('network', 'nic', 'show', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroup, '--name', "nic-$short-demos-economy")
    Assert-AttachRuntimeTargets $plan $group $vm $server $nic
    $operationName = 'attach-restored-' + $Project + '-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
    $uri = "https://management.azure.com$($plan.VmId)/runCommands/$operationName`?api-version=2024-07-01"
    $temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ('economy-attach-' + [guid]::NewGuid().ToString('N'))
    $succeeded = $false
    try {
        $null = New-Item -ItemType Directory -Path $temporaryDirectory
        if ($IsWindows) {
            $acl = [Security.AccessControl.DirectorySecurity]::new()
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
            $acl.SetOwner($identity)
            $acl.SetAccessRuleProtection($true, $false)
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
            Set-Acl -LiteralPath $temporaryDirectory -AclObject $acl
        } else { [IO.File]::SetUnixFileMode($temporaryDirectory, [IO.UnixFileMode]::UserRead -bor [IO.UnixFileMode]::UserWrite -bor [IO.UnixFileMode]::UserExecute) }
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($AppPassword)
        try { $applicationPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        $parameters = @{
            ECONOMY_PROJECT = $Project; ECONOMY_PGHOST = $PostgresHost
            ECONOMY_PUBLIC_HOSTNAME = $PublicHostname; ECONOMY_ACME_EMAIL = $AcmeEmail
        }
        $body = @{
            location = $vm.location
            properties = @{
                source = @{ script = (Get-AttachRuntimeHostScript).Replace("`r`n", "`n") }
                parameters = @($parameters.GetEnumerator() | ForEach-Object { @{ name = $_.Key; value = $_.Value } })
                protectedParameters = @(@{ name = 'ECONOMY_APP_PASSWORD'; value = $applicationPassword })
                timeoutInSeconds = 600; asyncExecution = $true
            }
        }
        $bodyPath = Join-Path $temporaryDirectory 'request.json'
        [IO.File]::WriteAllText($bodyPath, ($body | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
        if (-not $IsWindows) { [IO.File]::SetUnixFileMode($bodyPath, [IO.UnixFileMode]::UserRead -bor [IO.UnixFileMode]::UserWrite) }
        $null = Invoke-AttachRuntimeAzureJson @('rest', '--method', 'put', '--uri', $uri, '--body', "@$bodyPath")
        $deadline = [DateTimeOffset]::UtcNow.AddMinutes(12)
        do {
            Start-Sleep -Seconds 10
            $status = Invoke-AttachRuntimeAzureJson @('rest', '--method', 'get', '--uri', ($uri + '&$expand=instanceView'))
            $view = Get-AttachRuntimeValue (Get-AttachRuntimeValue $status 'properties') 'instanceView'
            $state = Get-AttachRuntimeValue $view 'executionState'
            if ($state -in @('Succeeded', 'Failed', 'Canceled', 'TimedOut')) {
                $exitCode = Get-AttachRuntimeValue $view 'exitCode'
                if ($state -ne 'Succeeded' -or $null -eq $exitCode -or $exitCode -ne 0) { throw "Runtime attach failed. Inspect run command $operationName separately. All remote messages are withheld." }
                $succeeded = $true
                break
            }
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        if (-not $succeeded) { throw "Runtime attach is not confirmed. Inspect run command $operationName before retrying because it may still be running." }
        return [pscustomobject]@{ Action = 'RestoredRuntimeAttached'; Project = $Project; VmName = $VmName; DatabaseModified = $false; ApplicationStarted = $false }
    } finally {
        $applicationPassword = $null; $body = $null
        if (Test-Path -LiteralPath $temporaryDirectory) {
            $resolved = [IO.Path]::GetFullPath($temporaryDirectory)
            $root = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
            if (-not $resolved.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notmatch '^economy-attach-[0-9a-f]{32}$') { throw 'Refusing cleanup outside the new private attach directory.' }
            Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
        }
        if ($succeeded) { $null = Invoke-AttachRuntimeAzureJson @('rest', '--method', 'delete', '--uri', $uri) }
    }
}

if (-not $DryRun) { . (Join-Path $PSScriptRoot 'common.ps1') }
Invoke-AttachRestoredRuntime @PSBoundParameters
