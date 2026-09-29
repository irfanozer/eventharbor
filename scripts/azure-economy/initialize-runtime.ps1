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
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($ResourceGroup -notmatch '^rg-[a-z0-9-]+-economy$' -or $VmName -notmatch '^vm-(eh|px)-[a-z0-9-]+$') {
    throw 'Only explicitly named economy resources may be initialized.'
}
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
if ($vm.tags.application -cne $Project -or $vm.tags.costProfile -cne 'economy') {
    throw 'VM ownership tags do not match this economy application.'
}
$server = Invoke-AzJson @('postgres', 'flexible-server', 'show', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroup, '--name', $PostgresHost.Split('.')[0])
if ($server.tags.costProfile -cne 'economy' -or $server.fullyQualifiedDomainName -cne $PostgresHost -or
    $server.administratorLogin -cne $PostgresAdministrator) {
    throw 'PostgreSQL must be the new economy server in the same resource group, with its matching administrator.'
}
$subnetPrefix = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Network/virtualNetworks/"
if ($server.network.publicNetworkAccess -cne 'Disabled' -or
    -not ([string]$server.network.delegatedSubnetResourceId).StartsWith($subnetPrefix, [StringComparison]::OrdinalIgnoreCase)) {
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
import json, os, pathlib, re, subprocess, sys, tempfile
from urllib.parse import quote

project = os.environ['ECONOMY_PROJECT']
if project not in ('eventharbor', 'pulseexchange'):
    sys.exit('Invalid project.')
folder = pathlib.Path('/etc') / project
folder.mkdir(mode=0o700, exist_ok=True)
os.chmod(folder, 0o700)
destination = folder / 'runtime.env'
if destination.exists() or destination.is_symlink():
    sys.exit('Runtime configuration already exists. Initialization never rotates or overwrites existing credentials.')
host = os.environ['ECONOMY_PGHOST']
role = project + '_app'
password = os.environ['ECONOMY_APP_PASSWORD']
if not re.fullmatch(r'[a-z0-9-]+\.postgres\.database\.azure\.com', host):
    sys.exit('Invalid PostgreSQL host.')
env = os.environ.copy()
env.update(PGHOST=host, PGPORT='5432', PGUSER=os.environ['ECONOMY_PGADMIN'],
           PGPASSWORD=os.environ['ECONOMY_ADMIN_PASSWORD'], PGDATABASE='postgres',
           PGSSLMODE='verify-full', PGSSLROOTCERT='/etc/ssl/certs/ca-certificates.crt',
           PGCONNECT_TIMEOUT='15')
sql = r'''
\set ON_ERROR_STOP on
\getenv app_password ECONOMY_APP_PASSWORD
BEGIN;
SET LOCAL createrole_self_grant = 'set';
CREATE ROLE __ROLE__ LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER DATABASE __PROJECT__ OWNER TO __ROLE__;
REVOKE CONNECT, TEMPORARY ON DATABASE __PROJECT__ FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE __PROJECT__ TO __ROLE__;
COMMIT;
'''.replace('__ROLE__', role).replace('__PROJECT__', project)
result = subprocess.run(['psql', '-X', '-q'], input=sql, text=True, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
if result.returncode:
    sys.exit('Database initialization failed. No runtime file was written. Check connectivity, admin credentials, and whether the application role already exists. Database diagnostics are withheld to protect credentials.')
values = {
    'PUBLIC_HOSTNAME': os.environ['ECONOMY_PUBLIC_HOSTNAME'],
    'ACME_EMAIL': os.environ['ECONOMY_ACME_EMAIL'],
    'CADDY_IMAGE': 'caddy:2.11.4-alpine@sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b',
    project.upper() + '_DATABASE_URL': f'postgresql+asyncpg://{role}:{quote(password, safe="")}@{host}:5432/{project}?ssl=verify-full',
}
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w') as output:
    for key, value in values.items():
        if any(c.isspace() or c in "\"'`$#" for c in value):
            sys.exit('Invalid environment value.')
        output.write(f'{key}={value}\n')
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
    $applicationPassword = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
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
