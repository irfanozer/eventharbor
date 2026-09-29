#requires -Version 7.4
<#
.SYNOPSIS
Inspect or explicitly apply the fixed low-load profile to eight existing resources.
.DESCRIPTION
Changes only allowlisted numeric environment settings. Does not change databases,
credentials, images, allocations, scaling, domains, job schedules, or job execution.
Apply writes a non-secret recovery manifest before the first update. RestoreFrom
inspects by default too; use Apply to restore the recorded values. Keep deployment
automation paused and run both applications' functional smoke tests afterwards.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [ValidateSet('all', 'eventharbor', 'pulseexchange')][string]$Project = 'all',
    [switch]$Apply,
    [string]$RecoveryPath,
    [string]$RestoreFrom
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../azure-economy/common.ps1')

function Get-SharedProfileTargets {
    param([guid]$SubscriptionId, [string]$Project)
    if ("$SubscriptionId" -ne '83099284-9ad4-4140-b8fe-8388b6d98a98') {
        throw 'Only the explicitly reviewed production subscription is supported.'
    }
    $definitions = @(
        @('eventharbor', 'api', 'containerApps', 'api', ''),
        @('eventharbor', 'worker', 'containerApps', 'worker', ''),
        @('eventharbor', 'cleanup', 'jobs', 'cleanup', 'Schedule'),
        @('eventharbor', 'migrate', 'jobs', 'migrate', 'Manual'),
        @('pulseexchange', 'api', 'containerApps', 'api', ''),
        @('pulseexchange', 'processor', 'containerApps', 'processor', ''),
        @('pulseexchange', 'maintenance', 'jobs', 'maintenance', 'Schedule'),
        @('pulseexchange', 'migrate', 'jobs', 'migrate', 'Manual')
    )
    foreach ($item in $definitions) {
        if ($Project -ne 'all' -and $item[0] -ne $Project) { continue }
        $prefix = $item[0].ToUpperInvariant()
        $keys = @("${prefix}_DATABASE_POOL_SIZE", "${prefix}_DATABASE_MAX_OVERFLOW")
        if ($item[1] -eq 'processor') { $keys += 'PULSEEXCHANGE_PROCESSOR_POLL_INTERVAL_MS' }
        $name = "$($item[0])-$($item[1])-prod"
        [pscustomobject]@{
            Project = $item[0]; Name = $name; Kind = $item[2]; Container = $item[3]
            Trigger = $item[4]; Keys = $keys; SubscriptionId = "$SubscriptionId"; ResourceGroup = "rg-$($item[0])-prod"
            Id = "/subscriptions/$SubscriptionId/resourceGroups/rg-$($item[0])-prod/providers/Microsoft.App/$($item[2])/$name"
        }
    }
}

function Invoke-SharedProfileAzure {
    param([string[]]$Arguments)
    Invoke-EconomyAzureJson -Arguments $Arguments -Sensitive
}

function Get-SharedProfileCommand {
    param($Target, [string]$Operation)
    $scope = @('--subscription', $Target.SubscriptionId, '--resource-group', $Target.ResourceGroup, '--name', $Target.Name)
    if ($Target.Kind -eq 'jobs') { return @('containerapp', 'job', $Operation) + $scope }
    return @('containerapp', $Operation) + $scope
}

function Get-SharedProfileSnapshot {
    param($Target)
    # The only returned literal env values are these public numeric settings.
    # Never request configuration.secrets, all env values, or a database URL.
    $filter = ($Target.Keys | ForEach-Object { "name=='$_'" }) -join ' || '
    $query = '{id:id,name:name,tags:tags,state:properties.provisioningState,running:properties.runningStatus,mode:properties.configuration.activeRevisionsMode,latest:properties.latestRevisionName,ready:properties.latestReadyRevisionName,trigger:properties.configuration.triggerType,containers:properties.template.containers[].{name:name,image:image,settings:env[?' + $filter + '].{name:name,value:value,secretRef:secretRef}},preserved:{location:location,tags:tags,identity:identity,environment:properties.environmentId,profile:properties.workloadProfileName,scale:properties.template.scale,ingress:properties.configuration.ingress,mode:properties.configuration.activeRevisionsMode,trigger:properties.configuration.triggerType,schedule:properties.configuration.scheduleTriggerConfig,manual:properties.configuration.manualTriggerConfig,event:properties.configuration.eventTriggerConfig,retry:properties.configuration.replicaRetryLimit,timeout:properties.configuration.replicaTimeout,containers:properties.template.containers[].{name:name,image:image,resources:resources,probes:probes,volumeMounts:volumeMounts,otherEnvironmentReferences:env[?!(' + $filter + ')].{name:name,secretRef:secretRef}},volumes:properties.template.volumes,terminationGracePeriod:properties.template.terminationGracePeriodSeconds}}'
    Invoke-SharedProfileAzure -Arguments ((Get-SharedProfileCommand $Target 'show') + @('--query', $query))
}

function Get-SharedProfileActivity {
    param($Target)
    $scope = @('--subscription', $Target.SubscriptionId, '--resource-group', $Target.ResourceGroup, '--name', $Target.Name)
    if ($Target.Kind -eq 'jobs') {
        return @(Invoke-SharedProfileAzure -Arguments (@('containerapp', 'job', 'execution', 'list') + $scope +
            @('--query', '[].{name:name,status:properties.status}')))
    }
    @(Invoke-SharedProfileAzure -Arguments (@('containerapp', 'revision', 'list') + $scope +
        @('--query', '[?properties.active].{name:name,health:properties.healthState,running:properties.runningState}')))
}

function Assert-SharedProfileTarget {
    param($Target, $Snapshot, [object[]]$Activity)
    if ($Snapshot.id -ne $Target.Id -or $Snapshot.name -ne $Target.Name -or
        (Get-EconomyValue $Snapshot.tags 'application') -ne $Target.Project -or
        (Get-EconomyValue $Snapshot.tags 'environment') -ne 'prod' -or
        (Get-EconomyValue $Snapshot.tags 'managedBy') -ne 'Bicep' -or
        (Get-EconomyValue $Snapshot.tags 'workload') -ne 'public-demo') {
        throw 'Exact production identity and all ownership tags must match.'
    }
    if ($Snapshot.state -ne 'Succeeded' -or @($Snapshot.containers).Count -ne 1 -or
        $Snapshot.containers[0].name -ne $Target.Container -or -not $Snapshot.containers[0].image) {
        throw 'A successfully provisioned resource with the single expected container is required.'
    }
    if ($Target.Kind -eq 'jobs') {
        $jobConfig = if ($Target.Trigger -eq 'Manual') { $Snapshot.preserved.manual } else { $Snapshot.preserved.schedule }
        if ($Snapshot.trigger -ne $Target.Trigger -or
            $jobConfig.parallelism -ne 1 -or $jobConfig.replicaCompletionCount -ne 1 -or
            @($Activity | Where-Object { $_.status -notin @('Succeeded', 'Failed', 'Stopped') }).Count) {
            throw 'The exact job trigger and no in-flight or unknown-status job execution are required.'
        }
    } else {
        if ($Snapshot.mode -ne 'Single' -or $Snapshot.running -ne 'Running' -or
            $Snapshot.latest -ne $Snapshot.ready -or $Activity.Count -ne 1 -or
            $Activity[0].name -ne $Snapshot.ready -or $Activity[0].health -ne 'Healthy' -or
            $Activity[0].running -notin @('Running', 'RunningAtMaxScale') -or
            $Snapshot.preserved.scale.minReplicas -lt 1 -or
            $Snapshot.preserved.scale.maxReplicas -lt $Snapshot.preserved.scale.minReplicas -or
            $Snapshot.preserved.scale.maxReplicas -gt $(if ($Target.Container -eq 'api') { 2 } else { 1 })) {
            throw 'Expected warm scaling and one active healthy Single-mode revision are required.'
        }
    }
}

function Assert-SharedProfileNumber {
    param([string]$Name, [string]$Value)
    if ($Value -cnotmatch '^(0|[1-9][0-9]{0,4})$') { throw 'Profile values must be canonical nonnegative integers.' }
    $number = [int]$Value
    if (($Name.EndsWith('_DATABASE_POOL_SIZE') -and ($number -lt 1 -or $number -gt 50)) -or
        ($Name.EndsWith('_DATABASE_MAX_OVERFLOW') -and $number -gt 100) -or
        ($Name.EndsWith('_POLL_INTERVAL_MS') -and ($number -lt 10 -or $number -gt 10000))) {
        throw 'A numeric profile value is outside the reviewed configuration bounds.'
    }
}

function Get-SharedProfileSettings {
    param($Target, $Snapshot)
    foreach ($key in $Target.Keys) {
        $matches = @($Snapshot.containers[0].settings | Where-Object { $_.name -ceq $key })
        if ($matches.Count -gt 1) { throw 'Duplicate profile environment settings are unsupported.' }
        $present = $matches.Count -eq 1
        $value = $null
        if ($present) {
            if ($matches[0].secretRef) { throw 'Numeric profile settings must not reference secrets.' }
            $value = [string]$matches[0].value
            Assert-SharedProfileNumber $key $value
        }
        [pscustomobject]@{ Name = $key; Present = $present; Value = $value }
    }
}

function Get-SharedProfileDesired {
    param([object[]]$Current)
    foreach ($setting in $Current) {
        $value = if ($setting.Name.EndsWith('_DATABASE_POOL_SIZE')) {
            if ($setting.Present) { [string][Math]::Min(2, [int]$setting.Value) } else { '2' }
        } elseif ($setting.Name.EndsWith('_DATABASE_MAX_OVERFLOW')) { '0' }
        else { if ($setting.Present) { [string][Math]::Max(500, [int]$setting.Value) } else { '500' } }
        [pscustomobject]@{ Name = $setting.Name; Present = $true; Value = $value }
    }
}

function ConvertTo-SharedProfileJson {
    param($Value)
    ConvertTo-Json -InputObject $Value -Depth 50 -Compress
}

function Read-SharedProfileRecovery {
    param([string]$Path, [object[]]$Targets, [guid]$SubscriptionId, [string]$Project)
    if ((Get-Item -LiteralPath $Path).Length -gt 65536) { throw 'Recovery manifest is unexpectedly large.' }
    $record = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -Depth 20
    if ($record.Schema -ne 1 -or $record.SubscriptionId -ne "$SubscriptionId" -or
        $record.Project -ne $Project -or @($record.Resources).Count -ne $Targets.Count) {
        throw 'Recovery manifest scope does not match the exact selected profile.'
    }
    foreach ($target in $Targets) {
        $entries = @($record.Resources | Where-Object { $_.Id -eq $target.Id })
        if ($entries.Count -ne 1 -or $entries[0].Container -ne $target.Container) { throw 'Recovery resource allowlist mismatch.' }
        foreach ($field in @('Before', 'Requested')) {
            $settings = @($entries[0].$field)
            if ($settings.Count -ne $target.Keys.Count) { throw 'Recovery setting count mismatch.' }
            foreach ($key in $target.Keys) {
                $setting = @($settings | Where-Object { $_.Name -ceq $key })
                if ($setting.Count -ne 1 -or $setting[0].Present -isnot [bool]) { throw 'Recovery setting allowlist mismatch.' }
                if ($setting[0].Present) { Assert-SharedProfileNumber $key ([string]$setting[0].Value) }
                elseif ($null -ne $setting[0].Value) { throw 'Absent recovery settings must have null values.' }
            }
        }
        if ((ConvertTo-SharedProfileJson @(Get-SharedProfileDesired $entries[0].Before)) -cne
            (ConvertTo-SharedProfileJson @($entries[0].Requested))) { throw 'Recovery manifest does not describe the fixed low-load profile.' }
    }
    $record
}

function Save-SharedProfileRecovery {
    param($Record, [string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $resolved) -PathType Container)) { throw 'Recovery directory must already exist.' }
    # CreateNew never overwrites a previous recovery manifest.
    $stream = [IO.File]::Open($resolved, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($Record | ConvertTo-Json -Depth 20))
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
    $resolved
}

function Invoke-SharedPostgresProfile {
    param([guid]$SubscriptionId, [string]$Project = 'all', [switch]$Apply, [string]$RecoveryPath, [string]$RestoreFrom)
    $targets = @(Get-SharedProfileTargets $SubscriptionId $Project)
    if ($RestoreFrom -and $RecoveryPath) { throw 'RecoveryPath cannot be combined with RestoreFrom.' }
    $restore = if ($RestoreFrom) { Read-SharedProfileRecovery $RestoreFrom $targets $SubscriptionId $Project } else { $null }
    $plans = [Collections.Generic.List[object]]::new()
    # Validate every selected resource before issuing any update.
    foreach ($target in $targets) {
        $snapshot = Get-SharedProfileSnapshot $target
        Assert-SharedProfileTarget $target $snapshot @(Get-SharedProfileActivity $target)
        $current = @(Get-SharedProfileSettings $target $snapshot)
        $desired = @(Get-SharedProfileDesired $current)
        if ($restore) {
            $entry = @($restore.Resources | Where-Object { $_.Id -eq $target.Id })[0]
            $currentJson = ConvertTo-SharedProfileJson $current
            if ($currentJson -cne (ConvertTo-SharedProfileJson @($entry.Before)) -and
                $currentJson -cne (ConvertTo-SharedProfileJson @($entry.Requested))) {
                throw 'Current settings drifted from both the recorded old and applied profile. Review manually.'
            }
            $desired = @($entry.Before)
        }
        $plans.Add([pscustomobject]@{
            Target = $target; Snapshot = $snapshot; Before = $current; Requested = $desired
            Changed = (ConvertTo-SharedProfileJson $current) -cne (ConvertTo-SharedProfileJson $desired)
        })
    }
    if (-not $Apply) {
        foreach ($plan in $plans) {
            [pscustomobject]@{ Resource = $plan.Target.Name; Current = $plan.Before; Proposed = $plan.Requested; Action = 'InspectOnly' }
        }
        return
    }
    $changed = @($plans | Where-Object Changed)
    if (-not $changed.Count) { return [pscustomobject]@{ Action = 'AlreadyAtRequestedProfile'; Resources = $targets.Count } }
    if ($restore) { $manifest = [IO.Path]::GetFullPath($RestoreFrom) }
    else {
        if (-not $RecoveryPath) { $RecoveryPath = Join-Path ([IO.Path]::GetTempPath()) ('shared-postgres-profile-' + [guid]::NewGuid().ToString('N') + '.json') }
        $record = [pscustomobject]@{
            Schema = 1; CreatedUtc = [datetime]::UtcNow.ToString('o'); SubscriptionId = "$SubscriptionId"; Project = $Project
            Resources = @($plans | ForEach-Object {
                [pscustomobject]@{ Id = $_.Target.Id; Container = $_.Target.Container; Before = $_.Before; Requested = $_.Requested }
            })
        }
        $manifest = Save-SharedProfileRecovery $record $RecoveryPath
    }
    Write-Host "Non-secret recovery manifest: $manifest"
    try {
        foreach ($plan in $changed) {
            $target = $plan.Target
            $fresh = Get-SharedProfileSnapshot $target
            Assert-SharedProfileTarget $target $fresh @(Get-SharedProfileActivity $target)
            if ((ConvertTo-SharedProfileJson $fresh.preserved) -cne (ConvertTo-SharedProfileJson $plan.Snapshot.preserved) -or
                (ConvertTo-SharedProfileJson @(Get-SharedProfileSettings $target $fresh)) -cne (ConvertTo-SharedProfileJson $plan.Before)) {
                throw 'Concurrent resource drift detected before an update.'
            }
            $arguments = (Get-SharedProfileCommand $target 'update') + @('--container-name', $target.Container, '--no-wait', '--query', 'name')
            $set = @($plan.Requested | Where-Object Present | ForEach-Object { "$($_.Name)=$($_.Value)" })
            $remove = @($plan.Requested | Where-Object { -not $_.Present } | ForEach-Object Name)
            if ($set.Count) { $arguments += @('--set-env-vars') + $set }
            if ($remove.Count) { $arguments += @('--remove-env-vars') + $remove }
            $null = Invoke-SharedProfileAzure -Arguments $arguments
            $verified = $false
            $deadline = [datetime]::UtcNow.AddMinutes(6)
            do {
                $after = Get-SharedProfileSnapshot $target
                if ($after.state -eq 'Failed') { throw 'Profile update provisioning failed.' }
                if ((ConvertTo-SharedProfileJson $after.preserved) -cne (ConvertTo-SharedProfileJson $plan.Snapshot.preserved)) {
                    throw 'Postcheck detected image, allocation, scaling, networking, job, or other preserved-setting drift.'
                }
                if ($after.state -eq 'Succeeded' -and
                    (ConvertTo-SharedProfileJson @(Get-SharedProfileSettings $target $after)) -ceq (ConvertTo-SharedProfileJson $plan.Requested)) {
                    $activity = @(Get-SharedProfileActivity $target)
                    $ready = $target.Kind -eq 'jobs' -or ($after.latest -eq $after.ready -and $after.latest -ne $plan.Snapshot.latest -and
                        $activity.Count -eq 1 -and $activity[0].name -eq $after.ready -and $activity[0].health -eq 'Healthy')
                    if ($ready) {
                        Assert-SharedProfileTarget $target $after $activity
                        $verified = $true
                        break
                    }
                }
                Start-Sleep -Seconds 5
            } while ([datetime]::UtcNow -lt $deadline)
            if (-not $verified) { throw 'Timed out waiting for the exact profile and healthy revision.' }
            [pscustomobject]@{ Resource = $target.Name; Action = 'AppliedAndVerified'; RecoveryManifest = $manifest }
        }
    } catch {
        throw "Profile stopped after a failed safety check. Earlier resources may already be updated. No automatic rollback was attempted. Recovery manifest: $manifest. $($_.Exception.Message)"
    }
}

Invoke-SharedPostgresProfile -SubscriptionId $SubscriptionId -Project $Project -Apply:$Apply -RecoveryPath $RecoveryPath -RestoreFrom $RestoreFrom
