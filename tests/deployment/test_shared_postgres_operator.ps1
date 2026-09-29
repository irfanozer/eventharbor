#requires -Version 7.4
# Offline only: every Azure invocation is replaced by a strict local fixture.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
. (Join-Path $repository 'scripts/azure-economy/common.ps1')
$operatorPath = Join-Path $repository 'scripts/azure-shared-postgres/operator.ps1'
$tokens = $null
$problems = $null
$tree = [Management.Automation.Language.Parser]::ParseFile($operatorPath, [ref]$tokens, [ref]$problems)
if ($problems.Count) { throw 'Operator script did not parse.' }
foreach ($definition in $tree.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    # Keep inline function parameter lists, not just the function body AST.
    . ([scriptblock]::Create($definition.Extent.Text))
}
# Execute the real entry-point statements, but do not dot-source common again:
# that would replace our mandatory fail-closed Azure fixture with the real CLI.
$entryText = ($tree.EndBlock.Statements | Where-Object {
    $_ -isnot [Management.Automation.Language.FunctionDefinitionAst] -and
    $_.Extent.Text -notmatch '^\. \(Join-Path \$PSScriptRoot'
} | ForEach-Object { $_.Extent.Text }) -join "`n"
if ($entryText -match 'azure-economy/common\.ps1') {
    throw 'Fixture refused an entry point that could replace its mandatory Azure mock.'
}
$entryPoint = [scriptblock]::Create($entryText)
$subscription = '83099284-9ad4-4140-b8fe-8388b6d98a98'
$jobResource = "/subscriptions/$subscription/resourceGroups/rg-pulseexchange-prod/providers/Microsoft.App/jobs/pulseexchange-dbcopy-prod"
$identityResource = "/subscriptions/$subscription/resourceGroups/rg-demos-db-migration/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-px-pgcopy-prod"
$client = '00000000-0000-4000-8000-000000000001'
$imageDigest = 'ghcr.io/irfanozer/eventharbor-backend@sha256:' + ('a' * 64)
$privateSource = 'postgresql+asyncpg://admin:PRIVATE_SOURCE_VALUE@psql-pulseexchange-prod-kj25jhmdlk6ik.postgres.database.azure.com/pulseexchange?ssl=require'
$privateTarget = 'postgresql+asyncpg://admin:PRIVATE_TARGET_VALUE@psql-eventharbor-prod-dodczqz7eaeps.postgres.database.azure.com/eventharbor'
$checks = 0

function Assert-Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:checks++
}

function New-FixtureJob {
    $value = @{
        id = $jobResource; location = 'eastus2'
        tags = @{ purpose='shared-postgres-copy'; application='PulseExchange'; environment='prod' }
        identity = @{ type='UserAssigned'; userAssignedIdentities=@{ $identityResource=@{} } }
        properties = @{
            provisioningState='Succeeded'
            environmentId="/subscriptions/$subscription/resourceGroups/rg-pulseexchange-prod/providers/Microsoft.App/managedEnvironments/cae-pulseexchange-prod"
            configuration=@{triggerType='Manual'; replicaRetryLimit=0; manualTriggerConfig=@{parallelism=1;replicaCompletionCount=1}}
            template=@{containers=@(@{
                name='copy';image=$imageDigest;command=@('python3','/app/migrate.py');args=@('Inspect')
                env=@(
                    @{name='SOURCE_DATABASE_URL';secretRef='source-url'},@{name='TARGET_ADMIN_DATABASE_URL';secretRef='target-url'},
                    @{name='TARGET_APP_PASSWORD';secretRef='final-password'},@{name='REHEARSAL_APP_PASSWORD';secretRef='rehearsal-password'},
                    @{name='MIGRATION_RUN_ID';value='px-20260929123045'},@{name='BACKUP_STORAGE_ACCOUNT';value='stpgcopy830992849ad4'},
                    @{name='BACKUP_CONTAINER';value='pulseexchange-backups'},@{name='MIGRATION_IDENTITY_CLIENT_ID';value=$client},
                    @{name='WRITERS_FROZEN';value='false'},@{name='VERIFY_TARGET';value='final'},
                    @{name='EVENTHARBOR_APP_PASSWORD';secretRef='event-password'}
                )
            })}
        }
    }
    return ($value | ConvertTo-Json -Depth 100 | ConvertFrom-Json -Depth 100)
}

function Invoke-Fixture {
    param(
        [string]$TestAction = 'Status', [bool]$DoApply = $true, [bool]$Freeze = $false,
        [bool]$RehearsalVerification = $false, [string]$TestSubscription = $subscription,
        [scriptblock]$MutateJob, [string[]]$Statuses = @(), [switch]$FailBody,
        [switch]$GetJobOnly, [switch]$NoExecutionOnly, [switch]$PrivateBodyOnly,
        [switch]$ExistingJob, [switch]$BadState
    )
    $fixtureRoot = New-EconomyPrivateDirectory
    $script:Calls = [Collections.Generic.List[object]]::new()
    $script:Bodies = [Collections.Generic.List[object]]::new()
    $script:BodyPaths = [Collections.Generic.List[string]]::new()
    $script:FixtureJob = New-FixtureJob
    if ($MutateJob) { & $MutateJob $script:FixtureJob }
    $state = @{
        schema=1; subscription=$(if ($BadState) { 'foreign-subscription' } else { $subscription })
        runId='px-20260929123045'; jobImage=$imageDigest
        finalPassword='PRIVATE_FINAL_PASSWORD';rehearsalPassword='PRIVATE_REHEARSAL_PASSWORD';eventPassword='PRIVATE_EVENT_PASSWORD'
        resources=@{
            'pulseexchange-api-prod'=@{
                secrets=@{value=@(@{name='database-url';value=$privateSource})}
                config=@{properties=@{environmentId="/subscriptions/$subscription/resourceGroups/rg-pulseexchange-prod/providers/Microsoft.App/managedEnvironments/cae-pulseexchange-prod"}}
            }
            'eventharbor-api-prod'=@{secrets=@{value=@(@{name='database-url';value=$privateTarget})}}
        }
    }
    [IO.File]::WriteAllText((Join-Path $fixtureRoot 'state.json'), ($state | ConvertTo-Json -Depth 100))
    $Action=$TestAction; $Apply=$DoApply; $WritersFrozen=$Freeze; $VerifyRehearsal=$RehearsalVerification
    $SubscriptionId=[guid]$TestSubscription; $StateDirectory=$(if ($Action -eq 'Snapshot') { $null } else { $fixtureRoot })
    $Image=$imageDigest; $IdentityResourceId=$identityResource; $IdentityClientId=$client
    $BackupStorageAccount='stpgcopy830992849ad4'; $BackupContainer='pulseexchange-backups'
    $apiVersion='2026-01-01'; $jobId=$jobResource; $jobName='pulseexchange-dbcopy-prod'

    function New-EconomyPrivateDirectory { return $fixtureRoot }
    function Get-Command { throw 'Native command discovery is forbidden in this offline fixture.' }
    function Invoke-EconomyAzureJson {
        param([string[]]$Arguments, [switch]$Sensitive)
        if (-not $Sensitive) { throw 'Fixture rejected a non-sensitive Azure invocation.' }
        if ($Arguments.Count -lt 2 -or $Arguments[0] -eq 'System.Collections.Hashtable') { throw 'Azure argument shape mismatch.' }
        $script:Calls.Add(@($Arguments))
        if ($Arguments -contains '--body') {
            $reference = $Arguments[[array]::IndexOf($Arguments, '--body') + 1]
            if (-not $reference.StartsWith('@')) { throw 'Body must use a private file, never a literal JSON argument.' }
            $path = $reference.Substring(1)
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Private request file is missing.' }
            if ([IO.Path]::GetDirectoryName($path) -ne $fixtureRoot) { throw 'Body escaped the private state directory.' }
            $script:BodyPaths.Add($path)
            $script:Bodies.Add((Get-Content -LiteralPath $path -Raw | ConvertFrom-Json -Depth 100))
            if ($FailBody) { throw 'Safe fixture request failure.' }
            return [pscustomobject]@{name='copy-execution-fixture'}
        }
        if ($Arguments[0] -eq 'rest') {
            $url = $Arguments[[array]::IndexOf($Arguments, '--url') + 1]
            $uri = [uri]$url
            if ($uri.AbsolutePath -eq $jobResource) { return $script:FixtureJob }
            if ($uri.AbsolutePath.EndsWith('/listSecrets')) {
                return [pscustomobject]@{value=@([pscustomobject]@{name='database-url';value=$(if ($url -match 'rg-pulseexchange-prod') { $privateSource } else { $privateTarget })})}
            }
            if ($Arguments -contains 'get') {
                $project = if ($url -match 'rg-pulseexchange-prod') { 'pulseexchange' } else { 'eventharbor' }
                return [pscustomobject]@{
                    id=$uri.AbsolutePath;tags=[pscustomobject]@{application=$project;environment='prod'}
                    properties=[pscustomobject]@{environmentId="/subscriptions/$subscription/resourceGroups/rg-$project-prod/providers/Microsoft.App/managedEnvironments/cae-$project-prod"}
                }
            }
        }
        if (($Arguments[0..2] -join ' ') -eq 'containerapp revision list') { return 'active-revision-fixture' }
        if (($Arguments[0..3] -join ' ') -eq 'containerapp job execution list') {
            foreach ($status in $Statuses) { [pscustomobject]@{name='execution';properties=[pscustomobject]@{status=$status}} }
            return
        }
        if (($Arguments[0..2] -join ' ') -eq 'containerapp job list') {
            if ($ExistingJob) { return 'pulseexchange-dbcopy-prod' }
            return
        }
        throw 'Unexpected Azure operation in offline fixture. No native process was invoked.'
    }
    try {
        $failure=$null; $captured=@()
        try {
            $captured = @(
                if ($GetJobOnly) { $null = Get-Job }
                elseif ($NoExecutionOnly) { Assert-NoJobExecution }
                elseif ($PrivateBodyOnly) { $null = Send-PrivateBody 'post' 'https://management.azure.com/fake' @{password='PRIVATE_BODY_SECRET'} }
                else { & $entryPoint 6>&1 }
            )
        } catch { $failure=$_.Exception.Message }
        $saved = Get-Content -LiteralPath (Join-Path $fixtureRoot 'state.json') -Raw | ConvertFrom-Json -AsHashtable -Depth 100
        foreach ($path in $script:BodyPaths) { Assert-Check (-not (Test-Path -LiteralPath $path)) 'Temporary private request file was not removed.' }
        $outputText = ($captured | Out-String) + $failure
        Assert-Check ($outputText -notmatch 'PRIVATE_[A-Z_]+') 'A secret marker leaked through operator output or exception text.'
        Assert-Check ((@($script:Calls | ForEach-Object { $_ -join ' ' }) -join ' ') -notmatch 'PRIVATE_[A-Z_]+') 'A secret marker appeared in CLI argument text.'
        return @{Failed=($null -ne $failure); Failure=$failure; Calls=@($script:Calls.ToArray()); Bodies=@($script:Bodies.ToArray()); Saved=$saved}
    } finally {
        $resolved = [IO.Path]::GetFullPath($fixtureRoot)
        $temporary = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        if (-not $resolved.StartsWith($temporary,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notmatch '^azure-economy-[a-f0-9]{32}$') {
            throw 'Refusing fixture cleanup outside its exact protected temporary directory.'
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

$result = Invoke-Fixture -TestAction Snapshot -DoApply $false
Assert-Check (-not $result.Failed -and $result.Calls.Count -eq 0) 'Inspect-only mode accessed Azure.'
$result = Invoke-Fixture -TestSubscription '00000000-0000-4000-8000-000000000001'
Assert-Check ($result.Failed -and $result.Calls.Count -eq 0) 'Foreign subscription was not blocked before Azure access.'
$result = Invoke-Fixture -GetJobOnly
Assert-Check (-not $result.Failed) "Valid job was rejected: $($result.Failure)"

$mutations = @(
    { param($job) $job.id += '-foreign' },
    { param($job) $job.tags.purpose='other' },
    { param($job) $job.tags.application='EventHarbor' },
    { param($job) $job.tags.environment='dev' },
    { param($job) $job.location='westus2' },
    { param($job) $job.properties.provisioningState='Unknown' },
    { param($job) $job.properties.environmentId += '-foreign' },
    { param($job) $job.properties.configuration.triggerType='Schedule' },
    { param($job) $job.properties.configuration.replicaRetryLimit=1 },
    { param($job) $job.properties.configuration.manualTriggerConfig.parallelism=2 },
    { param($job) $job.properties.configuration.manualTriggerConfig.replicaCompletionCount=2 },
    { param($job) $job.identity.type='SystemAssigned' },
    { param($job) $job.identity.userAssignedIdentities=[pscustomobject]@{'foreign-id'=@{}} },
    { param($job) $job.properties.template.containers[0].image='ghcr.io/irfanozer/eventharbor-backend:latest' },
    { param($job) $job.properties.template.containers[0].command=@('sh','-c','unsafe') },
    { param($job) $job.properties.template.containers[0].env[0].secretRef='other-secret' },
    { param($job) $job.properties.template.containers[0].env[5].value='foreignaccount' },
    { param($job) $job.properties.template.containers[0].env[4].value='unreviewed-run' },
    { param($job) $job.properties.template.containers += $job.properties.template.containers[0] }
)
foreach ($mutation in $mutations) {
    $result = Invoke-Fixture -GetJobOnly -MutateJob $mutation
    Assert-Check $result.Failed 'A mutated copy job passed its ownership/shape checks.'
}
foreach ($status in @('Running','Processing','Unknown','Canceled','Pending','')) {
    $result = Invoke-Fixture -NoExecutionOnly -Statuses @($status)
    Assert-Check $result.Failed 'An active or indeterminate execution was accepted.'
}
$result = Invoke-Fixture -NoExecutionOnly -Statuses @('Succeeded','Failed','Stopped')
Assert-Check (-not $result.Failed) 'Known terminal execution statuses were rejected.'
$result = Invoke-Fixture -PrivateBodyOnly
Assert-Check (-not $result.Failed -and $result.Bodies.Count -eq 1) 'Private request success path failed.'
$result = Invoke-Fixture -PrivateBodyOnly -FailBody
Assert-Check $result.Failed 'Private request failure was swallowed.'

$result = Invoke-Fixture -TestAction Snapshot
Assert-Check (-not $result.Failed) "Snapshot fixture failed: $($result.Failure)"
Assert-Check ($result.Saved.resources.Count -eq 9) 'Snapshot did not capture the exact nine expected consumers.'
Assert-Check ($result.Saved.resources['pulseexchange-api-prod'].secrets.value[0].value -ceq $privateSource) 'Snapshot did not retain real ARM value-array secret shape.'
$result = Invoke-Fixture -TestAction CreateJob
Assert-Check (-not $result.Failed -and $result.Bodies.Count -eq 1) "Create-job fixture failed: $($result.Failure)"
$secrets = $result.Bodies[0].properties.configuration.secrets
Assert-Check (@($secrets | Where-Object name -EQ 'source-url')[0].value -ceq $privateSource) 'CreateJob extracted the wrong source secret from ARM listSecrets shape.'
Assert-Check (@($secrets | Where-Object name -EQ 'target-url')[0].value -ceq $privateTarget) 'CreateJob extracted the wrong target secret from ARM listSecrets shape.'
$result = Invoke-Fixture -TestAction CreateJob -ExistingJob
Assert-Check ($result.Failed -and $result.Bodies.Count -eq 0) 'Existing copy job was overwritten.'
$result = Invoke-Fixture -TestAction FinalCopy
Assert-Check ($result.Failed -and $result.Bodies.Count -eq 0) 'Final copy started without a writer freeze acknowledgment.'
$result = Invoke-Fixture -TestAction IsolateEventHarbor
Assert-Check ($result.Failed -and $result.Bodies.Count -eq 0) 'EventHarbor ownership change started without a writer freeze acknowledgment.'
$result = Invoke-Fixture -TestAction Inspect -BadState
Assert-Check ($result.Failed -and $result.Calls.Count -eq 0) 'A foreign protected state was accepted.'

# Rehearsal and final backups cannot share immutable blob names. Verify must
# select the same phase-specific run ID as the copy it is checking.
$rehearsal = Invoke-Fixture -TestAction Rehearse
$final = Invoke-Fixture -TestAction FinalCopy -Freeze $true
$verifyRehearsal = Invoke-Fixture -TestAction Verify -RehearsalVerification $true
$verifyFinal = Invoke-Fixture -TestAction Verify
foreach ($case in @($rehearsal,$final,$verifyRehearsal,$verifyFinal)) {
    Assert-Check (-not $case.Failed -and $case.Bodies.Count -eq 1) "Execution template fixture failed: $($case.Failure)"
}
function Get-RunId($Case) { @($Case.Bodies[0].containers[0].env | Where-Object name -CEQ 'MIGRATION_RUN_ID')[0].value }
Assert-Check ((Get-RunId $rehearsal) -cne (Get-RunId $final)) 'Rehearsal and final copy must not reuse immutable backup blob paths.'
Assert-Check ((Get-RunId $rehearsal) -ceq (Get-RunId $verifyRehearsal)) 'Rehearsal Verify selected the wrong saved manifest.'
Assert-Check ((Get-RunId $final) -ceq (Get-RunId $verifyFinal)) 'Final Verify selected the wrong saved manifest.'
foreach ($route in @(
    @{Action='InspectEventHarbor';Mode='Inspect';Freeze=$false},
    @{Action='IsolateEventHarbor';Mode='Apply';Freeze=$true},
    @{Action='VerifyIsolation';Mode='Verify';Freeze=$false}
)) {
    $result = Invoke-Fixture -TestAction $route.Action -Freeze $route.Freeze
    Assert-Check (-not $result.Failed -and $result.Bodies.Count -eq 1) "EventHarbor execution fixture failed: $($result.Failure)"
    Assert-Check (($result.Bodies[0].containers[0].command -join '|') -ceq 'python3|/app/isolate_eventharbor.py') 'EventHarbor mode selected the wrong helper.'
    Assert-Check ($result.Bodies[0].containers[0].args[0] -ceq $route.Mode) 'EventHarbor mode selected the wrong argument.'
    Assert-Check ((Get-RunId $result) -ceq 'px-20260929123045-eventharbor') 'EventHarbor mode selected the wrong backup namespace.'
}
Write-Host "PASS $checks shared PostgreSQL operator checks; no Azure calls or credentials used."
