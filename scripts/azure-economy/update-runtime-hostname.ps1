#Requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$VmName,
    [Parameter(Mandatory)][ValidateSet('eventharbor', 'pulseexchange')][string]$Project,
    [Parameter(Mandatory)][string]$ExpectedHostname,
    [Parameter(Mandatory)][string]$PublicHostname,
    [switch]$Apply
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-HostnameAzure {
    param([string[]]$Arguments)
    Invoke-EconomyAzureJson -Arguments $Arguments -Sensitive
}

function Get-HostnameUpdateScript {
    return @'
#!/usr/bin/env bash
set -euo pipefail
umask 077
python3 - <<'PY'
import fcntl, os, pathlib, re, stat, sys

def fail():
    sys.exit('Hostname update validation failed. Protected file contents are not displayed. Inspect the private files before retrying.')

project = os.environ['ECONOMY_PROJECT']
expected = os.environ['ECONOMY_EXPECTED_HOSTNAME']
replacement = os.environ['ECONOMY_PUBLIC_HOSTNAME']
if (os.geteuid() != 0 or project not in ('eventharbor', 'pulseexchange') or
        not re.fullmatch(r'[a-z0-9-]+\.westus2\.cloudapp\.azure\.com', expected) or
        replacement != project + '.irfanburakozer.com'):
    fail()
folder = pathlib.Path('/etc') / project
source = folder / 'runtime.env'
backup = folder / 'runtime.env.before-hostname-change'
pending = folder / '.runtime.env.hostname-next'

def protected(path, mode, directory=False):
    value = path.lstat()
    return (value.st_uid == 0 and stat.S_IMODE(value.st_mode) == mode and
            (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)))

if (not protected(folder, 0o700, directory=True) or not protected(source, 0o600) or
        os.path.lexists(backup) or os.path.lexists(pending)):
    fail()
fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    original_stat = os.fstat(fd)
    if original_stat.st_uid != 0 or stat.S_IMODE(original_stat.st_mode) != 0o600 or not stat.S_ISREG(original_stat.st_mode):
        fail()
    with os.fdopen(os.dup(fd), 'rb') as content:
        original = content.read(65537)
    if not original or len(original) > 65536 or b'\0' in original:
        fail()
    text = original.decode('utf-8')
    lines = text.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.startswith('PUBLIC_HOSTNAME=')]
    if len(matches) != 1:
        fail()
    index = matches[0]
    line = lines[index]
    ending = '\r\n' if line.endswith('\r\n') else ('\n' if line.endswith('\n') else '')
    if line != 'PUBLIC_HOSTNAME=' + expected + ending:
        fail()
    lines[index] = 'PUBLIC_HOSTNAME=' + replacement + ending
    updated = ''.join(lines).encode('utf-8')
    if updated.replace(('PUBLIC_HOSTNAME=' + replacement + ending).encode(),
                       ('PUBLIC_HOSTNAME=' + expected + ending).encode(), 1) != original:
        fail()

    def save_private(path, data):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            os.fchown(output.fileno(), 0, 0)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())

    save_private(backup, original)
    if not protected(backup, 0o600) or backup.read_bytes() != original:
        fail()
    save_private(pending, updated)
    # A changed file means another operator intervened; never overwrite that change.
    current = source.lstat()
    if current.st_ino != original_stat.st_ino or current.st_dev != original_stat.st_dev or source.read_bytes() != original:
        fail()
    os.replace(pending, source)
    directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if not protected(source, 0o600) or source.read_bytes() != updated:
        fail()
finally:
    os.close(fd)
print('Only PUBLIC_HOSTNAME was updated. The exact previous runtime file remains in its root-only backup. No application restart, database access, or DNS change was performed.')
PY
'@
}

function Invoke-RuntimeHostnameUpdate {
    param([guid]$SubscriptionId, [string]$ResourceGroup, [string]$VmName, [string]$Project,
          [string]$ExpectedHostname, [string]$PublicHostname, [switch]$Apply)
    $short = switch ($Project) { 'eventharbor' { 'eh' }; 'pulseexchange' { 'px' }; default { throw 'Invalid project.' } }
    if ("$SubscriptionId" -cne '83099284-9ad4-4140-b8fe-8388b6d98a98' -or $ResourceGroup -cne 'rg-demos-economy' -or
        $VmName -cne "vm-$short-demos-economy" -or $ExpectedHostname -cnotmatch '^[a-z0-9-]+\.westus2\.cloudapp\.azure\.com$' -or
        $PublicHostname -cne "$Project.irfanburakozer.com") { throw 'Only the reviewed project VM, Azure preview hostname, and final project hostname are allowed.' }
    if (-not $Apply) { return [pscustomobject]@{ Action = 'DryRun'; VmName = $VmName; From = $ExpectedHostname; To = $PublicHostname; AzureCalls = 0; Restarted = $false } }
    $groupId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"
    $vmId = "$groupId/providers/Microsoft.Compute/virtualMachines/$VmName"
    $pipName = "pip-$short-demos-economy"
    $pipId = "$groupId/providers/Microsoft.Network/publicIPAddresses/$pipName"
    $vm = Invoke-HostnameAzure @('vm', 'show', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroup, '--name', $VmName)
    $pip = Invoke-HostnameAzure @('network', 'public-ip', 'show', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroup, '--name', $pipName)
    foreach ($resource in @($vm, $pip)) {
        if ($resource.location -cne 'westus2' -or $resource.tags.application -cne $Project -or $resource.tags.environment -cne 'economy' -or
            $resource.tags.costProfile -cne 'economy' -or $resource.tags.managedBy -cne 'Bicep') { throw 'Resource ownership does not match the reviewed economy project.' }
    }
    if ($vm.id -ine $vmId -or $pip.id -ine $pipId -or $pip.dnsSettings.fqdn -cne $ExpectedHostname) { throw 'Expected preview hostname does not match the reviewed project public IP.' }
    $operationName = 'hostname-' + $Project + '-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
    $uri = "https://management.azure.com${vmId}/runCommands/$operationName`?api-version=2024-07-01"
    $temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ('economy-hostname-' + [guid]::NewGuid().ToString('N'))
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
        $body = @{ location = $vm.location; properties = @{
            source = @{ script = (Get-HostnameUpdateScript).Replace("`r`n", "`n") }
            parameters = @(
                @{ name = 'ECONOMY_PROJECT'; value = $Project },
                @{ name = 'ECONOMY_EXPECTED_HOSTNAME'; value = $ExpectedHostname },
                @{ name = 'ECONOMY_PUBLIC_HOSTNAME'; value = $PublicHostname }
            )
            timeoutInSeconds = 120; asyncExecution = $true
        } }
        $bodyPath = Join-Path $temporaryDirectory 'request.json'
        [IO.File]::WriteAllText($bodyPath, ($body | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
        $null = Invoke-HostnameAzure @('rest', '--method', 'put', '--uri', $uri, '--body', "@$bodyPath")
        $deadline = [DateTimeOffset]::UtcNow.AddMinutes(5)
        do {
            Start-Sleep -Seconds 5
            $status = Invoke-HostnameAzure @('rest', '--method', 'get', '--uri', ($uri + '&$expand=instanceView'))
            $view = $status.properties.PSObject.Properties['instanceView']
            if ($view -and $view.Value.executionState -in @('Succeeded', 'Failed', 'Canceled', 'TimedOut')) {
                if ($view.Value.executionState -cne 'Succeeded' -or $null -eq $view.Value.PSObject.Properties['exitCode'] -or $view.Value.exitCode -ne 0) {
                    throw "Hostname update failed. Inspect managed command $operationName and private backup files before retrying. Remote messages are withheld."
                }
                $succeeded = $true
                break
            }
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        if (-not $succeeded) { throw "Hostname update is not confirmed. Inspect managed command $operationName before retrying; it may still be running." }
        return [pscustomobject]@{ Action = 'RuntimeHostnameUpdated'; VmName = $VmName; From = $ExpectedHostname; To = $PublicHostname; Backup = "/etc/$Project/runtime.env.before-hostname-change"; Restarted = $false; DatabaseChanged = $false; DnsChanged = $false }
    } finally {
        $body = $null
        if (Test-Path -LiteralPath $temporaryDirectory) {
            $resolved = [IO.Path]::GetFullPath($temporaryDirectory)
            $root = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
            if (-not $resolved.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notmatch '^economy-hostname-[0-9a-f]{32}$') { throw 'Refusing cleanup outside the new private hostname-update directory.' }
            Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
        }
        if ($succeeded) { $null = Invoke-HostnameAzure @('rest', '--method', 'delete', '--uri', $uri) }
    }
}

if ($Apply) { . (Join-Path $PSScriptRoot 'common.ps1') }
Invoke-RuntimeHostnameUpdate @PSBoundParameters
