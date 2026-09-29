#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][ValidateSet('eventharbor', 'pulseexchange')][string]$Project,
    [switch]$Apply,
    [switch]$Restore
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-ResizeAzureJson {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $command = (Get-Command az -ErrorAction Stop).Source
    $start.FileName = $command
    if ($IsWindows -and $command.EndsWith('.cmd')) {
        $python = [IO.Path]::GetFullPath((Join-Path (Split-Path $command) '../python.exe'))
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
            throw 'Use the official Windows Azure CLI installer with its bundled Python runtime.'
        }
        $start.FileName = $python
        $start.ArgumentList.Add('-IBm')
        $start.ArgumentList.Add('azure.cli')
    }
    foreach ($argument in $Arguments + @('--only-show-errors', '--output', 'json')) {
        $start.ArgumentList.Add($argument)
    }
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $content = $stdout.GetAwaiter().GetResult()
        $null = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "Azure CLI failed with exit code $($process.ExitCode). Response diagnostics were withheld."
        }
        if ($content.Trim()) {
            try { return ($content | ConvertFrom-Json -Depth 100) }
            catch { throw 'Azure CLI returned invalid JSON.' }
        }
    } finally { $process.Dispose() }
}

function Get-ResizeSnapshot {
    param([string[]]$Target)
    # Deliberately exclude secret values and plain environment values. Preserve
    # their names/references; the update itself sends only CPU and memory flags.
    $query = '{id:id,name:name,tags:tags,state:properties.provisioningState,running:properties.runningStatus,mode:properties.configuration.activeRevisionsMode,latest:properties.latestRevisionName,ready:properties.latestReadyRevisionName,containers:properties.template.containers[].{name:name,image:image,cpu:resources.cpu,memory:resources.memory},preserved:{scale:properties.template.scale,environment:properties.environmentId,profile:properties.workloadProfileName,ingress:properties.configuration.ingress,secretReferences:properties.configuration.secrets[].{name:name,keyVaultUrl:keyVaultUrl,identity:identity},containers:properties.template.containers[].{name:name,image:image,command:command,args:args,environmentReferences:env[].{name:name,secretRef:secretRef},probes:probes,volumeMounts:volumeMounts},volumes:properties.template.volumes,terminationGracePeriod:properties.template.terminationGracePeriodSeconds}}'
    return Invoke-ResizeAzureJson -Arguments (@('containerapp', 'show') + $Target + @('--query', $query))
}

function Get-ResizeActiveRevisions {
    param([string[]]$Target)
    $query = '[?properties.active].{name:name,health:properties.healthState,running:properties.runningState}'
    return @(Invoke-ResizeAzureJson -Arguments (@('containerapp', 'revision', 'list') + $Target + @('--query', $query)))
}

function Assert-ResizeTarget {
    param($Snapshot, [object[]]$Revisions, [guid]$SubscriptionId, [string]$Project)
    $expectedId = "/subscriptions/$SubscriptionId/resourceGroups/rg-$Project-prod/providers/Microsoft.App/containerApps/$Project-api-prod"
    if ($Snapshot.id -ne $expectedId -or $Snapshot.name -ne "$Project-api-prod" -or
        $Snapshot.tags.application -ne $Project -or $Snapshot.tags.environment -ne 'prod') {
        throw 'The API identity or production ownership tags do not match the exact requested target.'
    }
    if ($Snapshot.mode -ne 'Single' -or @($Snapshot.containers).Count -ne 1 -or
        $Snapshot.containers[0].name -ne 'api' -or -not $Snapshot.containers[0].image) {
        throw 'Only the existing Single-mode application with exactly one api container is supported.'
    }
    if ($Snapshot.state -ne 'Succeeded' -or $Snapshot.running -ne 'Running' -or
        $Snapshot.latest -ne $Snapshot.ready -or $Revisions.Count -ne 1 -or
        $Revisions[0].name -ne $Snapshot.ready -or $Revisions[0].health -ne 'Healthy' -or
        $Revisions[0].running -notin @('Running', 'RunningAtMaxScale')) {
        throw 'The target must have exactly one active, ready, healthy running revision before resizing.'
    }
    if ($Snapshot.preserved.scale.minReplicas -lt 1) {
        throw 'This operation requires at least one warm API replica and never changes scaling.'
    }
}

function Get-ResizeMemoryGi {
    param([string]$Memory)
    if ($Memory -notmatch '^(0\.5|1(?:\.0)?)Gi$') { throw 'Only 0.5Gi and 1Gi API allocations are supported.' }
    return [decimal]::Parse($Memory.TrimEnd('G', 'i'), [Globalization.CultureInfo]::InvariantCulture)
}

function Invoke-ResizeExistingApi {
    param([guid]$SubscriptionId, [string]$Project, [switch]$Apply, [switch]$Restore)
    $target = @('--subscription', "$SubscriptionId", '--resource-group', "rg-$Project-prod", '--name', "$Project-api-prod")
    $before = Get-ResizeSnapshot -Target $target
    $revisions = @(Get-ResizeActiveRevisions -Target $target)
    Assert-ResizeTarget -Snapshot $before -Revisions $revisions -SubscriptionId $SubscriptionId -Project $Project
    $container = $before.containers[0]
    $currentCpu = [decimal]$container.cpu
    $currentMemory = Get-ResizeMemoryGi $container.memory
    $fromCpu = if ($Restore) { [decimal]0.25 } else { [decimal]0.5 }
    $fromMemory = if ($Restore) { [decimal]0.5 } else { [decimal]1 }
    $toCpu = if ($Restore) { [decimal]0.5 } else { [decimal]0.25 }
    $toMemory = if ($Restore) { '1Gi' } else { '0.5Gi' }
    $alreadySet = $currentCpu -eq $toCpu -and $currentMemory -eq (Get-ResizeMemoryGi $toMemory)
    if (-not $alreadySet -and ($currentCpu -ne $fromCpu -or $currentMemory -ne $fromMemory)) {
        throw 'The current allocation does not match the allowed 0.5 CPU/1Gi to 0.25 CPU/0.5Gi transition or its explicit restore.'
    }
    if (-not $Apply -or $alreadySet) {
        return [pscustomobject]@{
            Project = $Project; ResourceGroup = "rg-$Project-prod"; App = $before.name
            CurrentCpu = $currentCpu; CurrentMemory = $container.memory
            ProposedCpu = $toCpu; ProposedMemory = $toMemory
            MinReplicas = $before.preserved.scale.minReplicas; ReadyRevision = $before.ready
            Action = $(if ($alreadySet) { 'AlreadyAtRequestedSize' } else { 'InspectOnly; use -Apply to change' })
        }
    }
    $preserved = $before.preserved | ConvertTo-Json -Depth 100 -Compress
    $cpuArgument = $toCpu.ToString([Globalization.CultureInfo]::InvariantCulture)
    try {
        $null = Invoke-ResizeAzureJson -Arguments (@('containerapp', 'update') + $target +
            @('--container-name', 'api', '--cpu', $cpuArgument, '--memory', $toMemory, '--no-wait', '--query', 'name'))
        $deadline = [datetime]::UtcNow.AddMinutes(3)
        do {
            $after = Get-ResizeSnapshot -Target $target
            if ($after.state -eq 'Failed') { throw 'The new revision reported provisioning failure.' }
            if ($after.state -eq 'Succeeded' -and $after.latest -ne $before.latest -and $after.ready -eq $after.latest) {
                $active = @(Get-ResizeActiveRevisions -Target $target)
                if ($active.Count -eq 1 -and $active[0].name -eq $after.ready -and $active[0].health -eq 'Healthy') {
                    Assert-ResizeTarget -Snapshot $after -Revisions $active -SubscriptionId $SubscriptionId -Project $Project
                    if ([decimal]$after.containers[0].cpu -ne $toCpu -or
                        (Get-ResizeMemoryGi $after.containers[0].memory) -ne (Get-ResizeMemoryGi $toMemory) -or
                        ($after.preserved | ConvertTo-Json -Depth 100 -Compress) -cne $preserved) {
                        throw 'Postcheck detected an unexpected allocation, image, scaling or preserved-setting change.'
                    }
                    return [pscustomobject]@{
                        Project = $Project; App = $after.name; Cpu = $after.containers[0].cpu
                        Memory = $after.containers[0].memory; MinReplicas = $after.preserved.scale.minReplicas
                        PreviousRevision = $before.ready; ReadyRevision = $after.ready; Action = 'AppliedAndVerified'
                    }
                }
            }
            Write-Host "Waiting for a single healthy revision of $Project-api-prod..."
            Start-Sleep -Seconds 5
        } while ([datetime]::UtcNow -lt $deadline)
        throw 'Timed out waiting for the new revision to become the only active healthy revision.'
    } catch {
        $recovery = if ($Restore) { '-Apply' } else { '-Apply -Restore' }
        throw "Resize did not pass verification: $($_.Exception.Message) No automatic recovery was attempted. Inspect active revisions; once healthy, the explicit reverse operation uses $recovery with the same subscription and project."
    }
}

Invoke-ResizeExistingApi -SubscriptionId $SubscriptionId -Project $Project -Apply:$Apply -Restore:$Restore
