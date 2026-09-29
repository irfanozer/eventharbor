#Requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$VmName,
    [Parameter(Mandatory)][ValidateSet('eventharbor', 'pulseexchange')][string]$Project,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9-]+\.postgres\.database\.azure\.com$')][string]$PostgresHost,
    [Parameter(Mandatory)][ValidatePattern('^[a-z][a-z0-9_]{0,62}$')][string]$PostgresAdministrator,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9][a-z0-9.-]+\.[a-z]{2,63}$')][string]$PublicHostname,
    [Parameter(Mandatory)][ValidatePattern('^[^\s@]+@[^\s@]+\.[^\s@]+$')][string]$AcmeEmail,
    [SecureString]$PostgresAdministratorPassword,
    [SecureString]$AppPassword,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$short=if ($Project -ceq 'eventharbor') {'eh'} else {'px'}
if ("$SubscriptionId" -cne '83099284-9ad4-4140-b8fe-8388b6d98a98' -or
    $ResourceGroup -cne 'rg-demos-economy' -or $VmName -cne "vm-$short-demos-economy" -or
    $PostgresHost -cne 'psql-demos-economy-56sjj5b3bl7ro.postgres.database.azure.com' -or
    $PostgresAdministrator -cne 'portfolio_admin') {
    throw 'Only the exact approved economy foundation, project VM and administrator may be initialized.'
}
if ($null -ne $AppPassword -and ($AppPassword.Length -lt 32 -or $AppPassword.Length -gt 256)) {throw 'Supply an application password of 32 to 256 characters as a SecureString.'}
if ($AcmeEmail -match '[\s"''`$#]') { throw 'Use a plain ACME contact address without interpolation characters.' }
if ($DryRun) {
    Write-Host "Would initialize a separate $Project role and root-only runtime configuration on $VmName. No credentials read and no Azure calls made."
    return
}
. (Join-Path $PSScriptRoot 'common.ps1')
function Invoke-AzJson([string[]]$Arguments) {
    # Even errors can echo secret-bearing request bodies. Suppress diagnostics.
    Invoke-EconomyAzureJson -Arguments $Arguments -Sensitive
}
$vm = Invoke-AzJson @('vm', 'show', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroup, '--name', $VmName)
$groupId="/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"
if ($vm.id -ine "$groupId/providers/Microsoft.Compute/virtualMachines/$VmName" -or
    $vm.location -cne 'westus2' -or $vm.tags.application -cne $Project -or $vm.tags.costProfile -cne 'economy' -or
    $vm.tags.environment -cne 'economy' -or $vm.tags.managedBy -cne 'Bicep') {
    throw 'VM ownership tags do not match this economy application.'
}
$server = Invoke-AzJson @('postgres', 'flexible-server', 'show', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroup, '--name', $PostgresHost.Split('.')[0])
if ($server.id -ine "$groupId/providers/Microsoft.DBforPostgreSQL/flexibleServers/$($PostgresHost.Split('.')[0])" -or
    $server.tags.costProfile -cne 'economy' -or $server.tags.environment -cne 'economy' -or
    $server.tags.application -cne 'EventHarbor-PulseExchange' -or $server.tags.managedBy -cne 'Bicep' -or
    $server.location -cnotin @('westus2', 'West US 2') -or $server.version -cne '17' -or $server.state -cne 'Ready' -or
    $server.fullyQualifiedDomainName -cne $PostgresHost -or $server.administratorLogin -cne $PostgresAdministrator) {
    throw 'PostgreSQL must be the new economy server in the same resource group, with its matching administrator.'
}
if ($server.network.publicNetworkAccess -cne 'Disabled' -or
    $server.network.delegatedSubnetResourceId -ine "$groupId/providers/Microsoft.Network/virtualNetworks/vnet-demos-economy/subnets/snet-postgres" -or
    $server.network.privateDnsZoneArmResourceId -ine "$groupId/providers/Microsoft.Network/privateDnsZones/demos-economy.postgres.database.azure.com") {
    throw 'The economy database must use private networking in the dedicated economy resource group.'
}
if (-not $PostgresAdministratorPassword) {
    $PostgresAdministratorPassword = Read-Host 'New economy PostgreSQL administrator password' -AsSecureString
}
$remoteScript = @'
#!/usr/bin/env bash
set -euo pipefail
umask 077
cloud-init status --wait >/dev/null
python3 - <<'PY'
import os, pathlib, re, subprocess, sys
from urllib.parse import quote

def fail():
    sys.exit('Fresh database initialization failed. Inspect retained state before retrying. Database diagnostics are withheld; no existing data is overwritten.')

if os.geteuid() != 0:
    fail()
project = os.environ['ECONOMY_PROJECT']
if project not in ('eventharbor', 'pulseexchange'):
    sys.exit('Invalid project.')
folder = pathlib.Path('/etc') / project
if folder.is_symlink() or not folder.is_dir() or folder.stat().st_uid != 0:
    fail()
destination = folder / 'runtime.env'
if destination.exists() or destination.is_symlink():
    sys.exit('Runtime configuration already exists. Initialization never rotates or overwrites existing credentials.')
host = os.environ['ECONOMY_PGHOST']
role = project + '_app'
password = os.environ['ECONOMY_APP_PASSWORD']
if (host != 'psql-demos-economy-56sjj5b3bl7ro.postgres.database.azure.com' or
        os.environ['ECONOMY_PGADMIN'] != 'portfolio_admin' or not 32 <= len(password) <= 256 or
        any(char in password for char in '\0\r\n')):
    fail()
values = {
    'PUBLIC_HOSTNAME': os.environ['ECONOMY_PUBLIC_HOSTNAME'],
    'ACME_EMAIL': os.environ['ECONOMY_ACME_EMAIL'],
    'CADDY_IMAGE': 'caddy:2.11.4-alpine@sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b',
    project.upper() + '_DATABASE_URL': f'postgresql+asyncpg://{role}:{quote(password, safe="")}@{host}:5432/{project}?ssl=verify-full',
}
if (not re.fullmatch(r'[a-z0-9][a-z0-9.-]+\.[a-z]{2,63}', values['PUBLIC_HOSTNAME']) or
        not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', values['ACME_EMAIL']) or
        any(not value or any(c.isspace() or c in "\"'`$#" for c in value) for value in values.values())):
    fail()
env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), 'LANG': 'C.UTF-8',
       'PGHOST': host, 'PGPORT': '5432', 'PGUSER': 'portfolio_admin',
       'PGPASSWORD': os.environ['ECONOMY_ADMIN_PASSWORD'], 'PGDATABASE': project,
       'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': '/etc/ssl/certs/ca-certificates.crt',
       'PGCONNECT_TIMEOUT': '15', 'PGCLIENTENCODING': 'UTF8',
       'PGOPTIONS': '-c statement_timeout=30000 -c lock_timeout=15000 -c client_min_messages=warning',
       'ECONOMY_APP_PASSWORD': password}
# Same ownership order and empty-database invariants as the PostgreSQL 17-tested
# EconomyPg.claim_empty_foundation path. Execute the guard and changes atomically.
sql = r'''
\set ON_ERROR_STOP on
\getenv app_password ECONOMY_APP_PASSWORD
BEGIN;
SET LOCAL createrole_self_grant = 'set';
SELECT pg_advisory_xact_lock(170929, __LOCK__);
DO $guard$ BEGIN
IF current_database()<>'__PROJECT__' OR current_user<>'portfolio_admin'
 OR current_setting('server_version_num')::integer NOT BETWEEN 170000 AND 179999
 OR NOT EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database()
               AND pg_get_userbyid(datdba)='portfolio_admin' AND pg_encoding_to_char(encoding)='UTF8' AND datlocprovider='c')
 OR EXISTS(SELECT 1 FROM pg_roles WHERE rolname='__ROLE__')
 OR (SELECT count(*) FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg\_%' ESCAPE '\' AND n.nspname<>'information_schema')<>1
 OR NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='public'
               AND pg_get_userbyid(nspowner) IN ('portfolio_admin','pg_database_owner','azure_pg_admin')
               AND pg_has_role(current_user,nspowner,'USAGE'))
 OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname NOT LIKE 'pg\_%' ESCAPE '\' AND n.nspname<>'information_schema')
 OR EXISTS(SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
           WHERE n.nspname NOT LIKE 'pg\_%' ESCAPE '\' AND n.nspname<>'information_schema')
 OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
           WHERE n.nspname NOT LIKE 'pg\_%' ESCAPE '\' AND n.nspname<>'information_schema')
 OR (SELECT count(*) FROM pg_extension WHERE extname='plpgsql')<>1
 OR EXISTS(SELECT 1 FROM pg_extension WHERE extname<>'plpgsql')
 OR EXISTS(SELECT 1 FROM pg_largeobject_metadata) OR EXISTS(SELECT 1 FROM pg_event_trigger)
 OR EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid())
THEN RAISE EXCEPTION 'Empty foundation target guard failed'; END IF;
END $guard$;
CREATE ROLE __ROLE__ LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 12;
GRANT CREATE ON DATABASE __PROJECT__ TO __ROLE__;
ALTER SCHEMA public OWNER TO __ROLE__;
ALTER DATABASE __PROJECT__ OWNER TO __ROLE__;
SET LOCAL ROLE __ROLE__;
REVOKE CONNECT, TEMPORARY ON DATABASE __PROJECT__ FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE ALL ON ROUTINES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE ALL ON TYPES FROM PUBLIC;
COMMENT ON DATABASE __PROJECT__ IS 'economy-fresh-initializer:__PROJECT__';
COMMIT;
'''.replace('__ROLE__', role).replace('__PROJECT__', project).replace('__LOCK__', '1' if project == 'eventharbor' else '2')
try:
    result = subprocess.run(['psql', '-X', '-q', '--no-password', '--set', 'VERBOSITY=sqlstate'], input=sql, text=True, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if result.returncode or result.stderr:
        fail()
except (OSError, subprocess.SubprocessError):
    fail()
os.chmod(folder, 0o700)
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        os.fchmod(output.fileno(), 0o600)
        os.fchown(output.fileno(), 0, 0)
        output.write(''.join(f'{key}={value}\n' for key,value in values.items()))
        output.flush()
        os.fsync(output.fileno())
except BaseException:
    destination.unlink(missing_ok=True)
    raise
print('Separate application role and root-only runtime configuration created. No application was started and no old database was modified.')
PY
'@
$operationName = 'initialize-' + $Project + '-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$uri = "https://management.azure.com$($vm.id)/runCommands/$operationName`?api-version=2024-07-01"
$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ('economy-init-' + [guid]::NewGuid().ToString('N'))
$terminal = $false
$succeeded = $false
try {
    $null = New-Item -ItemType Directory -Path $temporaryDirectory
    if ($IsWindows) {
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl.SetOwner($identity)
        $acl.SetAccessRuleProtection($true, $false)
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $temporaryDirectory -AclObject $acl
    } else { [IO.File]::SetUnixFileMode($temporaryDirectory, [IO.UnixFileMode]::UserRead -bor [IO.UnixFileMode]::UserWrite -bor [IO.UnixFileMode]::UserExecute) }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($PostgresAdministratorPassword)
    try { $adminPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ($null -ne $AppPassword) {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($AppPassword)
        try { $applicationPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    } else {
        $applicationPassword = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
    }
    if ($applicationPassword.Contains("`r") -or $applicationPassword.Contains("`n") -or $applicationPassword.Contains([char]0)) {throw 'Application password contains an unsupported control character.'}
    $parameters = @{
        ECONOMY_PROJECT = $Project; ECONOMY_PGHOST = $PostgresHost
        ECONOMY_PGADMIN = $PostgresAdministrator; ECONOMY_PUBLIC_HOSTNAME = $PublicHostname
        ECONOMY_ACME_EMAIL = $AcmeEmail
    }
    $body = @{
        location = $vm.location
        properties = @{
            source = @{ script = $remoteScript.Replace("`r`n", "`n") }
            parameters = @($parameters.GetEnumerator() | ForEach-Object { @{name = $_.Key; value = $_.Value} })
            protectedParameters = @(
                @{name = 'ECONOMY_ADMIN_PASSWORD'; value = $adminPassword}
                @{name = 'ECONOMY_APP_PASSWORD'; value = $applicationPassword}
            )
            timeoutInSeconds = 600; asyncExecution = $true
        }
    }
    $bodyPath = Join-Path $temporaryDirectory 'request.json'
    [IO.File]::WriteAllText($bodyPath, ($body | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
    $null = Invoke-AzJson @('rest', '--method', 'put', '--uri', $uri, '--body', "@$bodyPath")
    $deadline = [DateTimeOffset]::UtcNow.AddMinutes(12)
    do {
        Start-Sleep -Seconds 10
        $status = Invoke-AzJson @('rest', '--method', 'get', '--uri', ($uri + '&$expand=instanceView'))
        $view = $status.properties.PSObject.Properties['instanceView']
        if ($view -and $view.Value.executionState -in @('Succeeded', 'Failed', 'Canceled', 'TimedOut')) {
            $terminal = $true
            if ($view.Value.executionState -ne 'Succeeded' -or $view.Value.exitCode -ne 0) {
                throw "Initialization ended in $($view.Value.executionState). Inspect run command $operationName in Azure; output was deliberately not printed here."
            }
            $succeeded = $true
            Write-Host 'Initialized the new application database role and protected VM configuration. Existing deployments are unchanged.'
            break
        }
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    if (-not $terminal) { throw "Initialization is not confirmed. Check run command $operationName before retrying; it may still be running." }
} finally {
    $adminPassword = $null; $applicationPassword = $null; $body = $null
    if (Test-Path -LiteralPath $temporaryDirectory) {
        # Exact newly-created private directory, never a workspace or user path.
        $resolvedTemporaryDirectory = [IO.Path]::GetFullPath($temporaryDirectory)
        $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        if (-not $resolvedTemporaryDirectory.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::GetFileName($resolvedTemporaryDirectory) -notmatch '^economy-init-[0-9a-f]{32}$') {
            throw 'Refusing cleanup outside the private initialization directory.'
        }
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
    if ($succeeded) {
        $null = Invoke-AzJson @('rest', '--method', 'delete', '--uri', $uri)
    }
}
