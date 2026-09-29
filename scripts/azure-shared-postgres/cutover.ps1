#requires -Version 7.4
[CmdletBinding()]
param(
    [ValidateSet('Inspect','PauseJobs','StopApi','StopWorker','StartWorker','StartApi','ResumeJobs')]
    [string]$Action = 'Inspect',
    [Parameter(Mandatory)][ValidateSet('eventharbor','pulseexchange')][string]$Project,
    [Parameter(Mandatory)][string]$StateDirectory,
    [guid]$SubscriptionId = '83099284-9ad4-4140-b8fe-8388b6d98a98',
    [switch]$Apply,
    [switch]$ConfirmSourceStillAuthoritative
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($SubscriptionId -ne [guid]'83099284-9ad4-4140-b8fe-8388b6d98a98') { throw 'This cutover is scoped to the reviewed subscription only.' }
. (Join-Path $PSScriptRoot '../azure-economy/common.ps1')
$apiVersion = '2026-01-01'
$resourceGroup = "rg-$Project-prod"
$repository = if ($Project -eq 'eventharbor') { 'irfanozer/eventharbor' } else { 'irfanozer/pulse-exchange' }
$workerPart = if ($Project -eq 'eventharbor') { 'worker' } else { 'processor' }
$schedulePart = if ($Project -eq 'eventharbor') { 'cleanup' } else { 'maintenance' }
# Freeze or deliberately recover existing Container Apps during the VM migration.
# No database URL change, GitHub secret publication, DNS change or DB retirement.
$consumers = @(
    @{kind='containerApps';name="$Project-api-prod"},
    @{kind='containerApps';name="$Project-$workerPart-prod"},
    @{kind='jobs';name="$Project-$schedulePart-prod"},
    @{kind='jobs';name="$Project-migrate-prod"}
)

function Assert-CutoverFolder {
    if (-not (Test-Path -LiteralPath $StateDirectory -PathType Container)) { throw 'A protected operator snapshot directory is required.' }
    if ((Get-Item -LiteralPath $StateDirectory).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'State directory must not be a link.' }
    if ($IsWindows) {
        $acl = Get-Acl -LiteralPath $StateDirectory
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        if (-not $acl.AreAccessRulesProtected) { throw 'State directory must disable inherited access.' }
        foreach ($entry in $acl.Access) {
            if ($entry.AccessControlType -eq 'Allow' -and $entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $sid) {
                throw 'State directory must be readable only by the current operator.'
            }
        }
    } elseif (([IO.File]::GetUnixFileMode($StateDirectory) -band [IO.UnixFileMode]63) -ne 0) {
        throw 'State directory must have no group or other permissions.'
    }
}
function Get-CutoverPath([string]$Name) {
    Assert-CutoverFolder
    $path = Join-Path $StateDirectory $Name
    if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Private state files must not be links.'
    }
    return $path
}
function Save-CutoverJournal {
    $path = Get-CutoverPath "cutover-$Project.json"
    [IO.File]::WriteAllText($path, ($journal | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
}
function Invoke-CutoverAzure([string[]]$Arguments) {
    Invoke-EconomyAzureJson -Arguments $Arguments -Sensitive
}
function Invoke-CutoverGitHub {
    param([string[]]$Arguments)
    $command = Get-Command gh -CommandType Application -ErrorAction Stop | Select-Object -First 1
    if ($command.Source.EndsWith('.cmd')) { throw 'Use the official GitHub CLI executable, not a command-shell wrapper.' }
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $command.Source
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true; $info.RedirectStandardInput = $true
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $info
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync(); $stderr = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(120000)) { $process.Kill($true); throw 'GitHub operation timed out; inspect its status before retrying.' }
        $output = $stdout.GetAwaiter().GetResult(); [void]$stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw "GitHub operation failed with exit code $($process.ExitCode). Diagnostics withheld." }
    } finally { $process.Dispose() }
    try { return $output | ConvertFrom-Json -Depth 100 }
    catch { throw 'GitHub returned an unexpected result. Raw output withheld.' }
}
function Assert-DeploymentsPaused {
    $variable = Invoke-CutoverGitHub -Arguments @('api',"repos/$repository/actions/variables/AZURE_DEPLOYMENT_ENABLED")
    if ($variable.value -cne 'false') { throw 'AZURE_DEPLOYMENT_ENABLED must remain explicitly false throughout cutover.' }
    $runs = Invoke-CutoverGitHub -Arguments @('api',"repos/$repository/actions/workflows/deploy-production.yml/runs?per_page=100")
    if (@($runs.workflow_runs | Where-Object status -NE 'completed').Count) { throw 'A production workflow is still running or waiting. Finish or cancel it separately before cutover.' }
    if ($Project -eq 'pulseexchange') {
        $values = Invoke-CutoverGitHub -Arguments @('api',"repos/$repository/environments/production/variables?per_page=100")
        $override = @($values.variables | Where-Object name -CEQ 'AZURE_DEPLOYMENT_ENABLED')
        if ($override.Count -and $override[0].value -cne 'false') { throw 'The production environment overrides the paused deployment flag.' }
    }
}
function Get-CutoverId($Consumer) {
    "/subscriptions/$SubscriptionId/resourceGroups/$resourceGroup/providers/Microsoft.App/$($Consumer.kind)/$($Consumer.name)"
}
function Get-CutoverUrl([string]$Id, [string]$Suffix = '') {
    "https://management.azure.com${Id}${Suffix}?api-version=$apiVersion"
}
function Get-CutoverConfig($Consumer, [switch]$AllowUpdating) {
    $id = Get-CutoverId $Consumer
    $value = Invoke-CutoverAzure @('rest','--method','get','--url',(Get-CutoverUrl $id))
    if ($value.id -ine $id -or $value.tags.application -ine $Project -or $value.tags.environment -cne 'prod' -or
        $value.location.Replace(' ','').ToLowerInvariant() -cne 'eastus2' -or
        $value.properties.environmentId -ine "/subscriptions/$SubscriptionId/resourceGroups/$resourceGroup/providers/Microsoft.App/managedEnvironments/cae-$Project-prod" -or
        @($value.properties.template.containers).Count -ne 1) { throw 'Consumer identity or single-container shape mismatch.' }
    if (-not $AllowUpdating -and $value.properties.provisioningState -cne 'Succeeded') { throw 'Consumer provisioning is not ready.' }
    if ($Consumer.kind -eq 'containerApps' -and $value.properties.configuration.activeRevisionsMode -cne 'Single') { throw 'Only existing Single revision applications are supported.' }
    return $value
}
function Get-CutoverSecrets($Consumer) {
    $response = Invoke-CutoverAzure @('rest','--method','post','--url',(Get-CutoverUrl (Get-CutoverId $Consumer) '/listSecrets'))
    $values = @($response.value)
    if (@($values | Where-Object name -CEQ 'database-url').Count -ne 1 -or
        @($values.name | Sort-Object -Unique).Count -ne $values.Count) { throw 'Expected unique database-url secret was not returned.' }
    # Preserve every current secret. Reject unresolved secrets rather than wiping them.
    foreach ($secret in $values) {
        if (-not (Get-EconomyValue $secret 'value') -and -not (Get-EconomyValue $secret 'keyVaultUrl')) { throw 'An existing secret cannot be safely preserved.' }
    }
    return $values
}
function Send-CutoverPatch($Consumer, $Configuration) {
    $path = Get-CutoverPath ('cutover-request-' + [guid]::NewGuid().ToString('N') + '.json')
    try {
        # No template, image, environment variables, CPU, memory, scale or ingress.
        $body = @{properties=@{configuration=$Configuration}}
        [IO.File]::WriteAllText($path, ($body | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
        $null = Invoke-CutoverAzure @('rest','--method','patch','--url',(Get-CutoverUrl (Get-CutoverId $Consumer)),'--body',"@$path")
    } finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path } }
    $deadline = [datetime]::UtcNow.AddMinutes(3)
    do {
        $result = Get-CutoverConfig $Consumer -AllowUpdating
        $status = $result.properties.provisioningState
        if ($status -ceq 'Succeeded') { return $result }
        if ($status -in @('Failed','Canceled','Cancelled')) { throw 'Consumer update failed. Keep writers frozen and inspect the protected journal.' }
        Start-Sleep -Seconds 3
    } while ([datetime]::UtcNow -lt $deadline)
    throw 'Consumer update did not become ready. Its final state is unverified.'
}
function Save-BeforeChange([string]$Step, $Consumer, $Configuration, $Secrets) {
    $journal.steps += @{utc=[datetime]::UtcNow.ToString('o');step=$Step;name=$Consumer.name;config=$Configuration;secrets=$Secrets}
    Save-CutoverJournal
}
function Get-CutoverRevisions($Consumer) {
    @(Invoke-CutoverAzure @('containerapp','revision','list','--subscription',"$SubscriptionId",'-g',$resourceGroup,'-n',$Consumer.name,
        '--query','[].{name:name,active:properties.active,health:properties.healthState,running:properties.runningState}'))
}
function Assert-HealthyRevision($Consumer, [string]$Expected = '') {
    $active = @(Get-CutoverRevisions $Consumer | Where-Object active -EQ $true)
    if ($active.Count -ne 1 -or $active[0].health -cne 'Healthy' -or $active[0].running -cne 'Running' -or
        ($Expected -and $active[0].name -cne $Expected)) { throw 'Expected exactly one healthy current revision.' }
    return $active[0]
}
function Assert-JobsIdle {
    $jobs = @($consumers | Where-Object kind -EQ 'jobs')
    if ($Project -eq 'pulseexchange') { $jobs += @{kind='jobs';name='pulseexchange-seed-prod'} }
    foreach ($consumer in $jobs) {
        $null = Get-CutoverConfig $consumer
        $runs = @(Invoke-CutoverAzure @('containerapp','job','execution','list','--subscription',"$SubscriptionId",'-g',$resourceGroup,'-n',$consumer.name))
        if (@($runs | Where-Object { (Get-EconomyValue $_.properties 'status') -notin @('Succeeded','Failed','Stopped') }).Count) {
            throw 'A job execution is active or indeterminate. No job or application changes were authorized.'
        }
    }
}
function Assert-SchedulePaused {
    if (-not $journal.jobsPaused) { throw 'PauseJobs must complete first.' }
    $current = Get-CutoverConfig $consumers[2]
    if ($current.properties.configuration.triggerType -cne 'Manual') { throw 'Maintenance scheduling is not paused.' }
    Assert-JobsIdle
}
function Assert-SourceConnectionsUnchanged {
    foreach ($consumer in $consumers) {
        $original=@($state.resources[$consumer.name].secrets.value | Where-Object name -CEQ 'database-url')
        $current=@(Get-CutoverSecrets $consumer | Where-Object name -CEQ 'database-url')
        if ($original.Count -ne 1 -or $current.Count -ne 1 -or $original[0].value -cne $current[0].value) {
            throw 'A source connection differs from the protected snapshot. Review recovery instead of resuming old writers.'
        }
    }
}
function Stop-CutoverApp($Consumer) {
    Assert-SchedulePaused
    $current = Get-CutoverConfig $Consumer
    $revision = Assert-HealthyRevision $Consumer
    if ($journal.stopped.ContainsKey($Consumer.name)) { throw 'A stopped revision is already recorded. Inspect the journal instead of replacing recovery evidence.' }
    $secrets=@(Get-CutoverSecrets $Consumer)
    Save-BeforeChange $Action $Consumer $current $secrets
    $journal.stopped[$Consumer.name] = @{revision=$revision.name;template=($current.properties.template | ConvertTo-Json -Depth 100 -Compress);
        databaseUrl=@($secrets | Where-Object name -CEQ 'database-url')[0].value}
    Save-CutoverJournal
    $null = Invoke-CutoverAzure @('containerapp','revision','deactivate','--subscription',"$SubscriptionId",'-g',$resourceGroup,'-n',$Consumer.name,'--revision',$revision.name)
    $deadline = [datetime]::UtcNow.AddMinutes(3)
    do {
        if (-not @(Get-CutoverRevisions $Consumer | Where-Object active -EQ $true).Count) { return }
        Start-Sleep -Seconds 3
    } while ([datetime]::UtcNow -lt $deadline)
    throw 'Revision deactivation is not confirmed. Keep cutover paused.'
}
function Start-CutoverApp($Consumer) {
    Assert-SchedulePaused
    if (-not $ConfirmSourceStillAuthoritative) { throw 'Recovery requires -ConfirmSourceStillAuthoritative. Do not restart old writers after the replacement accepts writes.' }
    if (-not $journal.stopped.ContainsKey($Consumer.name)) { throw 'No exact stopped revision was recorded for this consumer.' }
    $saved = $journal.stopped[$Consumer.name]
    $current = Get-CutoverConfig $Consumer
    $secrets=@(Get-CutoverSecrets $Consumer)
    if (@($secrets | Where-Object name -CEQ 'database-url')[0].value -cne $saved.databaseUrl) { throw 'Database connection changed after freeze. Review recovery instead of restarting a different database.' }
    if (($current.properties.template | ConvertTo-Json -Depth 100 -Compress) -cne $saved.template) { throw 'Application template changed after stopping. Do not restore an outdated revision or profile.' }
    if (@(Get-CutoverRevisions $Consumer | Where-Object active -EQ $true).Count) { throw 'Application already has an active revision. Inspect before retrying.' }
    Save-BeforeChange $Action $Consumer $current $secrets
    $null = Invoke-CutoverAzure @('containerapp','revision','activate','--subscription',"$SubscriptionId",'-g',$resourceGroup,'-n',$Consumer.name,'--revision',$saved.revision)
    # Recover only the exact frozen revision with its unchanged database URL.
    # Never redeploy a saved full template or undo the current capacity profile.
    $null = Invoke-CutoverAzure @('containerapp','revision','restart','--subscription',"$SubscriptionId",'-g',$resourceGroup,'-n',$Consumer.name,'--revision',$saved.revision)
    $deadline = [datetime]::UtcNow.AddMinutes(5)
    do {
        $active = @(Get-CutoverRevisions $Consumer | Where-Object active -EQ $true)
        if ($active.Count -eq 1 -and $active[0].name -ceq $saved.revision -and $active[0].health -ceq 'Healthy' -and $active[0].running -ceq 'Running') {
            $journal.started[$Consumer.name] = $saved.revision
            Save-CutoverJournal
            return
        }
        Start-Sleep -Seconds 3
    } while ([datetime]::UtcNow -lt $deadline)
    throw 'Restart did not become healthy. Keep API or scheduling paused; no rollback was attempted.'
}

if (-not $Apply -and $Action -ne 'Inspect') {
    Write-Host "Plan only: $Project $Action. No snapshot secrets read or cloud calls made. Add -Apply for this exact step."
    return
}
$state = Get-Content -LiteralPath (Get-CutoverPath 'state.json') -Raw | ConvertFrom-Json -AsHashtable -Depth 100
if ($state.schema -ne 1 -or $state.subscription -cne "$SubscriptionId") { throw 'Protected snapshot identity mismatch.' }
foreach ($consumer in $consumers) {
    if (-not $state.resources.ContainsKey($consumer.name) -or $state.resources[$consumer.name].id -ine (Get-CutoverId $consumer)) { throw 'Protected snapshot does not contain the exact reviewed consumers.' }
}
$journalPath = Get-CutoverPath "cutover-$Project.json"
if (Test-Path -LiteralPath $journalPath) {
    $journal = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json -AsHashtable -Depth 100
    if ($journal.schema -ne 1 -or $journal.project -cne $Project -or $journal.subscription -cne "$SubscriptionId" -or $journal.snapshotRunId -cne $state.runId) { throw 'Cutover journal identity mismatch.' }
} else {
    $journal = @{schema=1;project=$Project;subscription="$SubscriptionId";snapshotRunId=$state.runId;
        steps=@();stopped=@{};started=@{};jobsPaused=$false;schedule=$null}
}
if ($Action -eq 'Inspect') {
    $summary = foreach ($consumer in $consumers) {
        $current = Get-CutoverConfig $consumer
        @{name=$consumer.name;kind=$consumer.kind;ready=($current.properties.provisioningState -ceq 'Succeeded');
          activeRevisions=$(if ($consumer.kind -eq 'containerApps') { @(Get-CutoverRevisions $consumer | Where-Object active -EQ $true).Count } else { $null })}
    }
    @{project=$Project;consumers=@($summary);journalExists=(Test-Path -LiteralPath $journalPath);jobsRecordedPaused=$journal.jobsPaused} | ConvertTo-Json -Depth 8
    return
}
Assert-DeploymentsPaused
switch ($Action) {
    'PauseJobs' {
        Assert-JobsIdle
        if ($journal.jobsPaused) { throw 'Jobs are already recorded as paused. Inspect rather than replacing the original schedule.' }
        $consumer=$consumers[2]; $current=Get-CutoverConfig $consumer; $secrets=@(Get-CutoverSecrets $consumer)
        if ($current.properties.configuration.triggerType -cne 'Schedule') { throw 'Expected the current scheduled maintenance job; no automatic trigger conversion is allowed.' }
        $journal.schedule = $current.properties.configuration.scheduleTriggerConfig
        Save-BeforeChange $Action $consumer $current $secrets
        $configuration = @{triggerType='Manual';scheduleTriggerConfig=$null;manualTriggerConfig=@{parallelism=1;replicaCompletionCount=1};secrets=$secrets}
        $updated=Send-CutoverPatch $consumer $configuration
        if ($updated.properties.configuration.triggerType -cne 'Manual') { throw 'Job schedule pause was not confirmed.' }
        $journal.jobsPaused=$true; Save-CutoverJournal
    }
    'StopApi' { Stop-CutoverApp $consumers[0] }
    'StopWorker' {
        if (-not $journal.stopped.ContainsKey($consumers[0].name) -or @(Get-CutoverRevisions $consumers[0] | Where-Object active -EQ $true).Count) { throw 'Stop the API first, then allow pending work to drain before stopping the worker.' }
        Stop-CutoverApp $consumers[1]
    }
    'StartWorker' { Start-CutoverApp $consumers[1] }
    'StartApi' {
        if (-not $journal.started.ContainsKey($consumers[1].name)) { throw 'Start and verify the worker before starting the API.' }
        $null=Assert-HealthyRevision $consumers[1] $journal.started[$consumers[1].name]
        Start-CutoverApp $consumers[0]
    }
    'ResumeJobs' {
        if (-not $ConfirmSourceStillAuthoritative) { throw 'Recovery requires -ConfirmSourceStillAuthoritative. Do not resume old jobs after the replacement accepts writes.' }
        Assert-SchedulePaused
        Assert-SourceConnectionsUnchanged
        foreach ($consumer in $consumers[0..1]) {
            if (-not $journal.started.ContainsKey($consumer.name)) { throw 'Both applications must have completed their guarded restart.' }
            $null=Assert-HealthyRevision $consumer $journal.started[$consumer.name]
        }
        $consumer=$consumers[2]; $current=Get-CutoverConfig $consumer; $secrets=@(Get-CutoverSecrets $consumer)
        Save-BeforeChange $Action $consumer $current $secrets
        if (-not $journal.schedule) { throw 'Original schedule is missing from the private journal.' }
        $updated=Send-CutoverPatch $consumer @{triggerType='Schedule';manualTriggerConfig=$null;scheduleTriggerConfig=$journal.schedule;secrets=$secrets}
        if ($updated.properties.configuration.triggerType -cne 'Schedule' -or
            ($updated.properties.configuration.scheduleTriggerConfig | ConvertTo-Json -Compress) -cne ($journal.schedule | ConvertTo-Json -Compress)) {
            throw 'Original job schedule restoration was not confirmed.'
        }
        $journal.jobsPaused=$false; Save-CutoverJournal
    }
}
Write-Host "Completed $Project $Action. Protected journal: cutover-$Project.json. Deployment automation remains disabled; no database was stopped or deleted."
