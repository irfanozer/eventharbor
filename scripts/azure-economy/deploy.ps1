#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')]
    [string] $SubscriptionId,

    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9_.()-]{1,90}$')]
    [string] $ResourceGroup,

    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9-]{0,63}$')]
    [string] $VmName,

    [Parameter(Mandatory)]
    [string] $BackendImage,

    [Parameter(Mandatory)]
    [string] $FrontendImage,

    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string] $ExpectedSourceRevision,

    [switch] $DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

# Only public, repository-controlled files enter the Run Command payload.
# The root-only database configuration is installed separately on the VM.
$application = 'eventharbor'
$repository = 'irfanozer/eventharbor'
$runtimeDirectory = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../infra/azure-economy/runtime'))
$allowedFiles = @(
    'compose.yaml'
    'Caddyfile'
    'nginx.conf.template'
    'README.md'
    'runtime.env.example'
    'validate_env.py'
    'install.sh'
    'deploy.sh'
    'maintenance.sh'
    'eventharbor-maintenance.service'
    'eventharbor-maintenance.timer'
    'worker-health.py'
)

foreach ($entry in @(
    @{ Name = 'BackendImage'; Value = $BackendImage; Package = 'backend' }
    @{ Name = 'FrontendImage'; Value = $FrontendImage; Package = 'frontend' }
)) {
    $pattern = '^' + [regex]::Escape("ghcr.io/$repository-$($entry.Package)") + '@sha256:[0-9a-f]{64}$'
    if ($entry.Value -cnotmatch $pattern) {
        throw "$($entry.Name) must name this project's public GHCR package by exact sha256 digest."
    }
}

$runtimeItem = Get-Item -LiteralPath $runtimeDirectory
if (-not $runtimeItem.PSIsContainer -or ($runtimeItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
    throw 'The local runtime bundle must be a regular directory, not a link.'
}

if ($ExpectedSourceRevision -cnotmatch '^[0-9a-f]{40}$') {
    throw 'ExpectedSourceRevision must be a full lowercase Git commit SHA.'
}

$bundleFiles = [ordered]@{}
$totalBytes = 0
$utf8 = [System.Text.UTF8Encoding]::new($false, $true)
foreach ($name in $allowedFiles) {
    $path = Join-Path $runtimeDirectory $name
    $item = Get-Item -LiteralPath $path
    if ($item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "Runtime bundle entry '$name' must be a regular file, not a link."
    }
    # Normalize checked-out Windows newlines before transferring Linux scripts.
    $content = [System.IO.File]::ReadAllText($path, $utf8).Replace("`r`n", "`n")
    $bytes = $utf8.GetBytes($content)
    if ($bytes.Length -gt 262144) {
        throw "Runtime bundle entry '$name' exceeds the 256 KiB per-file limit."
    }
    $totalBytes += $bytes.Length
    $bundleFiles[$name] = @{
        content = [Convert]::ToBase64String($bytes)
        sha256 = [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
    }
}
if ($totalBytes -gt 1048576) {
    throw 'The public runtime bundle exceeds the 1 MiB limit.'
}

$bundle = [ordered]@{
    application = $application
    repository = $repository
    sourceRevision = $ExpectedSourceRevision
    backendImage = $BackendImage
    frontendImage = $FrontendImage
    files = $bundleFiles
}
$bundleJson = $bundle | ConvertTo-Json -Depth 8 -Compress
$bundleBase64 = [Convert]::ToBase64String($utf8.GetBytes($bundleJson))
$bundleHash = [Convert]::ToHexString(
    [System.Security.Cryptography.SHA256]::HashData($utf8.GetBytes($bundleJson))
).ToLowerInvariant()

if ($DryRun) {
    [ordered]@{
        mode = 'DryRun'
        azureCalls = 0
        application = $application
        subscriptionId = $SubscriptionId
        resourceGroup = $ResourceGroup
        vmName = $VmName
        requiredVmTags = @{ application = $application; environment = 'economy'; costProfile = 'economy' }
        expectedSourceRevision = $ExpectedSourceRevision
        backendImage = $BackendImage
        frontendImage = $FrontendImage
        runtimeDirectory = "/opt/$application/runtime"
        existingRootOnlyConfiguration = "/etc/$application/runtime.env"
        bundleFiles = $allowedFiles
        bundleBytes = $totalBytes
        bundleSha256 = $bundleHash
        scriptTimeoutSeconds = 1800
        executionPollTimeoutSeconds = 2100
        validation = 'Local bundle and input validation only; no Azure, image-registry, or VM execution checks performed.'
    } | ConvertTo-Json -Depth 5
    return
}

$azureCommand = Get-Command az -CommandType Application -ErrorAction Stop | Select-Object -First 1
function Invoke-EconomyAzureJson {
    param(
        [Parameter(Mandatory)]
        [string[]] $Arguments,
        [Parameter(Mandatory)]
        [string] $Operation,
        [ValidateRange(1, 120)]
        [int] $TimeoutSeconds = 120
    )
    # Never pass REST URLs through cmd.exe: the polling URL contains "&".
    # The official Windows CLI launcher uses its adjacent Python interpreter.
    # Invoke that interpreter directly to preserve every argument boundary.
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $azureCommand.Source
    if ($IsWindows -and $azureCommand.Source.EndsWith('.cmd')) {
        $cliPython = [System.IO.Path]::GetFullPath((Join-Path (Split-Path $azureCommand.Source) '../python.exe'))
        if (-not (Test-Path -LiteralPath $cliPython -PathType Leaf)) {
            throw 'Unsupported Azure CLI launcher. Install the official Windows Azure CLI with its bundled Python runtime.'
        }
        $startInfo.FileName = $cliPython
        $startInfo.ArgumentList.Add('-IBm')
        $startInfo.ArgumentList.Add('azure.cli')
    }
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in @($Arguments) + @('--only-show-errors', '--output', 'json')) {
        $startInfo.ArgumentList.Add($argument)
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        [void] $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill($true)
            [void] $process.WaitForExit(5000)
            throw "Azure CLI timed out during $Operation. An in-flight Azure operation may still finish; inspect its status before retrying."
        }
        $json = $stdout.GetAwaiter().GetResult()
        [void] $stderr.GetAwaiter().GetResult()
        $nativeExitCode = $process.ExitCode
    }
    finally {
        $process.Dispose()
    }
    if ($nativeExitCode -ne 0) {
        # Never echo arbitrary API/guest output: guest commands may handle secrets.
        throw "Azure CLI failed during $Operation (exit code $nativeExitCode). Raw output was not printed."
    }
    if ([string]::IsNullOrWhiteSpace($json)) {
        throw "Azure CLI returned no JSON during $Operation."
    }
    try {
        return ($json | ConvertFrom-Json -AsHashtable -Depth 100)
    }
    catch {
        throw "Azure CLI returned invalid JSON during $Operation. Raw output was not printed."
    }
}

$vmResourceId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Compute/virtualMachines/$VmName"
$vmUrl = "https://management.azure.com${vmResourceId}?api-version=2024-07-01"
$vm = Invoke-EconomyAzureJson -Arguments @(
    'rest', '--method', 'get', '--url', $vmUrl, '--subscription', $SubscriptionId
) -Operation 'VM ownership verification'

if ([string] $vm['id'] -ine $vmResourceId) {
    throw 'Azure returned a different VM resource ID than requested.'
}
$vmTags = $vm['tags']
if (-not $vmTags -or
    $vmTags['application'] -cne $application -or
    $vmTags['costProfile'] -cne 'economy' -or
    $vmTags['environment'] -cne 'economy') {
    throw "Target VM must be tagged application=$application, costProfile=economy, environment=economy."
}
if ($vm['properties']['storageProfile']['osDisk']['osType'] -cne 'Linux') {
    throw 'The economy runtime requires a Linux VM.'
}
$location = [string] $vm['location']
if ([string]::IsNullOrWhiteSpace($location)) {
    throw 'Azure did not return the VM location.'
}

$remoteScript = @'
#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ "$(id -u)" -eq 0 ]] || { echo "Run Command must run as root." >&2; exit 1; }
command -v python3 >/dev/null
command -v docker >/dev/null
command -v flock >/dev/null
# Separate transfer lock serializes controllers; runtime scripts take their own
# shared deployment/maintenance lock while installing or changing containers.
exec 8>/run/lock/eventharbor-economy-transfer.lock
flock -n 8 || { echo "Another economy deployment controller is active." >&2; exit 1; }
python3 - <<'PY_ECONOMY'
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

app = "eventharbor"
repository = "irfanozer/eventharbor"
allowed = {
    "compose.yaml", "Caddyfile", "nginx.conf.template", "README.md",
    "runtime.env.example", "validate_env.py", "install.sh", "deploy.sh",
    "maintenance.sh", "eventharbor-maintenance.service",
    "eventharbor-maintenance.timer", "worker-health.py"
}
payload = base64.b64decode("__BUNDLE_BASE64__", validate=True)
if hashlib.sha256(payload).hexdigest() != "__BUNDLE_SHA256__":
    raise SystemExit("Runtime bundle checksum does not match.")
bundle = json.loads(payload)
if bundle.get("application") != app or bundle.get("repository") != repository:
    raise SystemExit("Runtime bundle project does not match the controller.")
revision = bundle.get("sourceRevision", "")
if not re.fullmatch(r"[0-9a-f]{40}", revision):
    raise SystemExit("Invalid source revision in runtime bundle.")
files = bundle.get("files", {})
if set(files) != allowed:
    raise SystemExit("Runtime bundle does not match the fixed filename allowlist.")

decoded = {}
for name in sorted(allowed):
    # Names are a fixed flat allowlist: no archive extraction or path traversal.
    if Path(name).name != name or "/" in name or "\\" in name:
        raise SystemExit("Runtime bundle contains an unsafe path.")
    content = base64.b64decode(files[name]["content"], validate=True)
    if len(content) > 262144 or hashlib.sha256(content).hexdigest() != files[name]["sha256"]:
        raise SystemExit("Runtime file checksum or size verification failed.")
    decoded[name] = content
if sum(map(len, decoded.values())) > 1048576:
    raise SystemExit("Runtime bundle exceeds the maximum permitted size.")

config = Path(f"/etc/{app}/runtime.env")
for directory in (Path("/etc"), config.parent, Path("/opt"), Path(f"/opt/{app}")):
    if directory.is_symlink():
        raise SystemExit("A runtime or configuration directory is a symbolic link.")
    if directory.exists() and (
        not directory.is_dir() or directory.stat().st_uid != 0
        or stat.S_IMODE(directory.stat().st_mode) & 0o022
    ):
        raise SystemExit("Runtime and configuration directories must be root owned and not group/world writable.")
if config.is_symlink() or not config.is_file():
    raise SystemExit("Install the root-only runtime.env configuration separately before deployment.")
config_stat = config.stat()
if config_stat.st_uid != 0 or stat.S_IMODE(config_stat.st_mode) != 0o600:
    raise SystemExit("runtime.env must be owned by root with mode 0600.")

base = Path(f"/opt/{app}")
base.mkdir(mode=0o750, parents=False, exist_ok=True)
os.chmod(base, 0o750)
runtime = base / "runtime"
if runtime.is_symlink() or (runtime.exists() and not runtime.is_dir()):
    raise SystemExit("The runtime target must be a regular directory.")
if runtime.exists() and (
    runtime.stat().st_uid != 0 or stat.S_IMODE(runtime.stat().st_mode) & 0o022
):
    raise SystemExit("The runtime target must be root owned and not group/world writable.")
for name in allowed:
    destination = runtime / name
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise SystemExit("An installed runtime file is a link or unexpected file type.")

# Keep this public release bundle for inspection/recovery. install.sh copies the
# fixed files into /opt/<app>/runtime under the runtime maintenance lock.
stage = Path(tempfile.mkdtemp(prefix=f"release-{revision[:12]}-", dir=base))
if stage.resolve().parent != base.resolve():
    raise SystemExit("Staged release escaped the application directory.")
os.chmod(stage, 0o750)
for name, content in decoded.items():
    target = stage / name
    with target.open("xb") as handle:
        handle.write(content)
    os.chmod(target, 0o640)
# Prepare the host's burst cushion before even the provenance image pull.
# This mode must not install or replace active runtime files/maintenance units.
subprocess.run(["bash", str(stage / "install.sh"), "--prepare-swap-only"], check=True)

# Pull by digest and verify the actual image configuration on the target host.
# Neither image metadata nor root-only environment contents are printed.
images = []
for kind in ("backend", "frontend"):
    image = bundle.get(f"{kind}Image", "")
    pattern = re.escape(f"ghcr.io/{repository}-{kind}") + r"@sha256:[0-9a-f]{64}"
    if not re.fullmatch(pattern, image):
        raise SystemExit("Image is not an allowed immutable project package.")
    subprocess.run(
        ["docker", "pull", "--platform", "linux/amd64", image],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=600,
    )
    details = json.loads(subprocess.check_output(
        ["docker", "image", "inspect", image], stderr=subprocess.DEVNULL, timeout=30
    ))[0]
    labels = details.get("Config", {}).get("Labels") or {}
    if details.get("Os") != "linux" or details.get("Architecture") != "amd64":
        raise SystemExit("Image architecture is not linux/amd64.")
    if labels.get("org.opencontainers.image.revision") != revision:
        raise SystemExit("Image source revision does not match the requested release.")
    if labels.get("org.opencontainers.image.source") != f"https://github.com/{repository}":
        raise SystemExit("Image source repository does not match this project.")
    images.append(image)

subprocess.run(["bash", str(stage / "install.sh")], check=True)
subprocess.run(
    ["bash", str(runtime / "deploy.sh"), images[0], images[1], str(config)],
    check=True,
)
print("Economy runtime deployment completed for the verified source revision.")
PY_ECONOMY
'@
$remoteScript = $remoteScript.Replace('__BUNDLE_BASE64__', $bundleBase64).Replace('__BUNDLE_SHA256__', $bundleHash)
$remoteScript = $remoteScript.Replace("`r`n", "`n")

$runCommandName = "economy-deploy-$([Guid]::NewGuid().ToString('N'))"
$runCommandResourceId = "$vmResourceId/runCommands/$runCommandName"
$runCommandUrl = "https://management.azure.com${runCommandResourceId}?api-version=2024-07-01"
$instanceViewUrl = $runCommandUrl + '&$expand=instanceView'
$body = @{
    location = $location
    properties = @{
        asyncExecution = $true
        timeoutInSeconds = 1800
        treatFailureAsDeploymentFailure = $true
        source = @{ script = $remoteScript }
    }
}
$bodyPath = Join-Path ([System.IO.Path]::GetTempPath()) "economy-runcommand-$([Guid]::NewGuid().ToString('N')).json"

try {
    [System.IO.File]::WriteAllText($bodyPath, ($body | ConvertTo-Json -Depth 8), $utf8)
    [void] (Invoke-EconomyAzureJson -Arguments @(
        'rest', '--method', 'put', '--url', $runCommandUrl,
        '--subscription', $SubscriptionId, '--body', "@$bodyPath"
    ) -Operation 'managed Run Command submission')
}
finally {
    if (Test-Path -LiteralPath $bodyPath -PathType Leaf) {
        Remove-Item -LiteralPath $bodyPath -Force
    }
}

Write-Host "Submitted $runCommandName to $VmName. Waiting for guest execution, not provisioning status."
$deadline = [DateTimeOffset]::UtcNow.AddMinutes(35)
$lastState = ''
$consecutiveReadFailures = 0
while ([DateTimeOffset]::UtcNow -lt $deadline) {
    try {
        $remainingSeconds = [Math]::Max(1, [Math]::Floor(($deadline - [DateTimeOffset]::UtcNow).TotalSeconds))
        $result = Invoke-EconomyAzureJson -Arguments @(
            'rest', '--method', 'get', '--url', $instanceViewUrl, '--subscription', $SubscriptionId
        ) -Operation 'managed Run Command execution status' -TimeoutSeconds ([Math]::Min(120, $remainingSeconds))
        $consecutiveReadFailures = 0
    }
    catch {
        $consecutiveReadFailures++
        if ($consecutiveReadFailures -ge 3) {
            throw "Unable to read $runCommandName after three attempts. Deployment status is unverified; the guest command may still be running."
        }
        Start-Sleep -Seconds 15
        continue
    }
    $properties = $result['properties']
    $view = $properties['instanceView']
    $state = if ($view) { [string] $view['executionState'] } else { '' }
    if ($state -and $state -cne $lastState) {
        Write-Host "Guest execution state: $state"
        $lastState = $state
    }
    if ($state -ceq 'Succeeded' -and $view.ContainsKey('exitCode') -and $null -ne $view['exitCode']) {
        if ([int] $view['exitCode'] -ne 0) {
            throw "Guest execution reported Succeeded but returned nonzero exit code for $runCommandName."
        }
        Write-Host "Deployment verified: $application, source $ExpectedSourceRevision, guest exit code 0."
        return
    }
    if ($state -in @('Failed', 'TimedOut', 'Canceled', 'Cancelled')) {
        $exitCode = if ($view.ContainsKey('exitCode')) { [string] $view['exitCode'] } else { 'unavailable' }
        throw "Deployment failed: $runCommandName, guest state $state, exit code $exitCode. Guest output was not printed."
    }
    if (-not $view -and $properties['provisioningState'] -ceq 'Failed') {
        throw "Run Command provisioning failed before guest execution could be verified: $runCommandName."
    }
    Start-Sleep -Seconds 15
}
throw "Timed out after 35 minutes waiting for guest completion: $runCommandName. Deployment status is unverified; inspect the command before retrying."
