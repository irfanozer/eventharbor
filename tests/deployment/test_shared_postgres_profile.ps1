#requires -Version 7.4
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../../scripts/azure-economy/common.ps1')
$path = Join-Path $PSScriptRoot '../../scripts/azure-shared-postgres/profile.ps1'
$tokens = $null
$problems = $null
$tree = [Management.Automation.Language.Parser]::ParseFile((Resolve-Path $path), [ref]$tokens, [ref]$problems)
if ($problems.Count) { throw ($problems | Out-String) }
foreach ($definition in $tree.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Set-Item -LiteralPath "Function:$($definition.Name)" -Value $definition.Body.GetScriptBlock()
}
$saveRecoveryImplementation = (Get-Command Save-SharedProfileRecovery).ScriptBlock
$subscription = [guid]'83099284-9ad4-4140-b8fe-8388b6d98a98'
$script:scenario = 'inspect'
$script:writes = 0
$script:states = @{}
$script:records = @{}
$script:allTargets = @(Get-SharedProfileTargets $subscription 'all')

function Reset-ProfileFixture {
    param([string]$Scenario)
    $script:scenario = $Scenario
    $script:writes = 0
    $script:states = @{}
    foreach ($target in $script:allTargets) {
        $settings = @()
        foreach ($key in $target.Keys) {
            if ($target.Kind -eq 'jobs') { continue }
            $value = if ($key.EndsWith('_DATABASE_POOL_SIZE')) { '2' }
                elseif ($key.EndsWith('_DATABASE_MAX_OVERFLOW')) { '2' } else { '250' }
            if ($Scenario -eq 'lower-cap' -and $key.EndsWith('_DATABASE_POOL_SIZE')) { $value = '1' }
            if ($Scenario -eq 'already' -and $key.EndsWith('_DATABASE_MAX_OVERFLOW')) { $value = '0' }
            if ($Scenario -eq 'already' -and $key.EndsWith('_POLL_INTERVAL_MS')) { $value = '1000' }
            $settings += [pscustomobject]@{ name = $key; value = $value; secretRef = $null }
        }
        if ($Scenario -eq 'already' -and $target.Kind -eq 'jobs') {
            $settings = @($target.Keys | ForEach-Object { [pscustomobject]@{ name = $_; value = $(if ($_.EndsWith('_DATABASE_POOL_SIZE')) { '2' } else { '0' }); secretRef = $null } })
        }
        $script:states[$target.Id] = [pscustomobject]@{ Settings = $settings; Updated = $false }
    }
}

function Save-SharedProfileRecovery {
    param($Record, [string]$Path)
    $script:records[$Path] = ($Record | ConvertTo-Json -Depth 20 | ConvertFrom-Json -Depth 20)
    if (($Record | ConvertTo-Json -Depth 20) -match 'DATABASE_URL|password|secretRef') { throw 'Manifest exposed unapproved fields.' }
    $Path
}

function Invoke-EconomyAzureJson {
    param([string[]]$Arguments, [switch]$Sensitive)
    if (-not $Sensitive) { throw 'All calls must suppress sensitive diagnostics.' }
    foreach ($flag in @('--name', '--resource-group', '--subscription')) {
        if ($Arguments -notcontains $flag) { throw 'Every call must have explicit resource scope.' }
    }
    $name = $Arguments[[array]::IndexOf($Arguments, '--name') + 1]
    $target = @($script:allTargets | Where-Object Name -eq $name)[0]
    if ($Arguments[[array]::IndexOf($Arguments, '--resource-group') + 1] -ne $target.ResourceGroup -or
        $Arguments[[array]::IndexOf($Arguments, '--subscription') + 1] -ne "$subscription") { throw 'Incorrect resource scope.' }
    $id = $target.Id
    $state = $script:states[$id]
    if ($script:scenario -eq 'cli-failure') { throw 'Sensitive CLI failure suppressed.' }
    if ($Arguments -contains 'update') {
        foreach ($forbidden in @('--image', '--cpu', '--memory', '--min-replicas', '--max-replicas', '--replace-env-vars', '--remove-all-env-vars', '--yaml', '--cron-expression')) {
            if ($Arguments -contains $forbidden) { throw "Forbidden update flag: $forbidden" }
        }
        if ($Arguments -notcontains '--no-wait' -or $Arguments[[array]::IndexOf($Arguments, '--query') + 1] -ne 'name') { throw 'Update output or waiting is unsafe.' }
        foreach ($flag in @('--set-env-vars', '--remove-env-vars')) {
            $index = [array]::IndexOf($Arguments, $flag)
            if ($index -lt 0) { continue }
            for ($i = $index + 1; $i -lt $Arguments.Count -and -not $Arguments[$i].StartsWith('--'); $i++) {
                $parts = $Arguments[$i].Split('=', 2)
                if ($parts[0] -cnotin $target.Keys) { throw 'Unapproved env key in update.' }
                $state.Settings = @($state.Settings | Where-Object name -cne $parts[0])
                if ($flag -eq '--set-env-vars') {
                    Assert-SharedProfileNumber $parts[0] $parts[1]
                    $state.Settings += [pscustomobject]@{ name = $parts[0]; value = $parts[1]; secretRef = $null }
                }
            }
        }
        $state.Updated = $true
        $script:writes++
        return $target.Name
    }
    if ($Arguments -contains 'execution') {
        if ($script:scenario -eq 'running-job') { return [pscustomobject]@{ name = 'execution'; status = 'Running' } }
        return @()
    }
    $revision = if ($state.Updated) { 'revision--new' } else { 'revision--old' }
    if ($Arguments -contains 'revision') {
        return [pscustomobject]@{ name = $revision; health = $(if ($script:scenario -eq 'unhealthy') { 'Unhealthy' } else { 'Healthy' }); running = 'Running' }
    }
    if ($Arguments -notcontains 'show') { throw 'Unexpected fixture command.' }
    $query = $Arguments[[array]::IndexOf($Arguments, '--query') + 1]
    if ($query.Contains('configuration.secrets') -or $query.Contains('env[].{name:name,value:value}')) { throw 'Snapshot exposes all secret or env values.' }
    $settings = $state.Settings
    if ($script:scenario -eq 'secret-setting') { $settings[0].secretRef = 'forbidden' }
    if ($script:scenario -eq 'bad-number') { $settings[0].value = '2;unsafe' }
    if ($script:scenario -eq 'duplicate-setting') { $settings += $settings[0] }
    $containers = @([pscustomobject]@{ name = $target.Container; image = 'digest-pinned-image'; settings = $settings })
    if ($script:scenario -eq 'sidecar') { $containers += [pscustomobject]@{ name = 'other' } }
    [pscustomobject]@{
        id = $(if ($script:scenario -eq 'wrong-id') { $id + '-foreign' } else { $id }); name = $target.Name; state = 'Succeeded'; running = 'Running'; mode = 'Single'
        latest = $revision; ready = $revision; trigger = $target.Trigger
        tags = [pscustomobject]@{ application = $(if ($script:scenario -eq 'foreign') { 'other' } else { $target.Project }); environment = 'prod'; managedBy = 'Bicep'; workload = 'public-demo' }
        containers = $containers
        preserved = [pscustomobject]@{
            scale = [pscustomobject]@{ minReplicas = $(if ($script:scenario -eq 'cold') { 0 } else { 1 }); maxReplicas = 1 }
            image = $(if ($script:scenario -eq 'image-drift' -and $state.Updated) { 'different-image' } else { 'same-image' })
            cpu = $(if ($script:scenario -eq 'cpu-drift' -and $state.Updated) { 0.5 } else { 0.25 }); memory = '0.5Gi'; trigger = $target.Trigger
            manual = [pscustomobject]@{ parallelism = 1; replicaCompletionCount = 1 }
            schedule = [pscustomobject]@{ parallelism = $(if ($script:scenario -eq 'parallel-job') { 2 } else { 1 }); replicaCompletionCount = 1; cronExpression = '0 4 * * *' }
        }
    }
}

foreach ($scenario in @('inspect', 'apply', 'foreign', 'wrong-id', 'unhealthy', 'running-job', 'parallel-job', 'sidecar', 'secret-setting', 'bad-number', 'duplicate-setting', 'cli-failure', 'cold', 'image-drift', 'cpu-drift', 'already', 'lower-cap')) {
    Reset-ProfileFixture $scenario
    $failure = $null
    try { $result = @(Invoke-SharedPostgresProfile $subscription -Apply:($scenario -ne 'inspect') -RecoveryPath 'fixture-recovery.json') }
    catch { $failure = $_ }
    $expectedFailure = $scenario -in @('foreign', 'wrong-id', 'unhealthy', 'running-job', 'parallel-job', 'sidecar', 'secret-setting', 'bad-number', 'duplicate-setting', 'cli-failure', 'cold', 'image-drift', 'cpu-drift')
    if ([bool]$failure -ne $expectedFailure) { throw "Unexpected result for ${scenario}: $failure" }
    $expectedWrites = if ($scenario -in @('apply', 'lower-cap')) { 8 } elseif ($scenario -in @('image-drift', 'cpu-drift')) { 1 } else { 0 }
    if ($script:writes -ne $expectedWrites) { throw "Unexpected mutation count for ${scenario}: $script:writes" }
    if ($scenario -eq 'inspect' -and $result.Count -ne 8) { throw 'Inspection did not cover every resource.' }
    if ($scenario -eq 'apply') {
        $script:applyRecord = $script:records['fixture-recovery.json']
        $processor = $script:states[($script:allTargets | Where-Object Container -eq 'processor').Id]
        if (@($processor.Settings | Where-Object name -match 'POLL').value -ne '500') { throw 'Processor target must be500 ms.' }
    }
    if ($scenario -eq 'lower-cap') {
        $api = $script:states[$script:allTargets[0].Id]
        if (@($api.Settings | Where-Object name -match 'POOL_SIZE').value -ne '1') { throw 'Existing tighter pool cap must be preserved.' }
    }
    Write-Output "Passed: $scenario"
}
$blocked = $false
try { $null = Get-SharedProfileTargets ([guid]'00000000-0000-0000-0000-000000000000') 'all' } catch { $blocked = $true }
if (-not $blocked) { throw 'Foreign subscription was accepted.' }
Write-Output 'Passed: exact subscription guard'

# Use real file parsing for restoration validation; no Azure command is executed.
$manifestPath = Join-Path ([IO.Path]::GetTempPath()) ('profile-offline-test-' + [guid]::NewGuid().ToString('N') + '.json')
try {
    $null = & $saveRecoveryImplementation $script:applyRecord $manifestPath
    $blocked = $false
    try { $null = & $saveRecoveryImplementation $script:applyRecord $manifestPath } catch { $blocked = $true }
    if (-not $blocked) { throw 'Recovery file was overwritten.' }
    Write-Output 'Passed: recovery manifest refuses overwrite'
    $null = Read-SharedProfileRecovery $manifestPath $script:allTargets $subscription 'all'
    Reset-ProfileFixture 'apply'
    $null = Invoke-SharedPostgresProfile $subscription -Apply -RecoveryPath 'restore-fixture.json'
    foreach ($state in $script:states.Values) { $state.Updated = $false }
    $script:writes = 0
    $null = Invoke-SharedPostgresProfile $subscription -Apply -RestoreFrom $manifestPath
    if ($script:writes -ne 8) { throw 'Restore did not restore all changed resources.' }
    if ($script:states[$script:allTargets[2].Id].Settings.Count -ne 0) { throw 'Restore must remove originally absent job overrides.' }
    Write-Output 'Passed: validated restore including originally absent settings'
    $script:applyRecord.Resources[0].Before[0].Name = 'EVENTHARBOR_DATABASE_URL'
    [IO.File]::WriteAllText($manifestPath, ($script:applyRecord | ConvertTo-Json -Depth 20))
    $blocked = $false
    try { $null = Read-SharedProfileRecovery $manifestPath $script:allTargets $subscription 'all' } catch { $blocked = $true }
    if (-not $blocked) { throw 'Tampered manifest key was accepted.' }
    Write-Output 'Passed: tampered recovery manifest rejected'
} finally { if (Test-Path -LiteralPath $manifestPath) { Remove-Item -LiteralPath $manifestPath } }
Write-Output 'SHARED_POSTGRES_PROFILE_OFFLINE_CHECKS_PASS'
