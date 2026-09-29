#requires -Version 7.4
[CmdletBinding()]
param(
    [ValidateSet('Snapshot','CreateJob','Inspect','Rehearse','FinalCopy','Verify','InspectEventHarbor','IsolateEventHarbor','VerifyIsolation','Status')][string]$Action = 'Status',
    [guid]$SubscriptionId = '83099284-9ad4-4140-b8fe-8388b6d98a98',
    [string]$StateDirectory,
    [string]$Image,
    [string]$IdentityResourceId,
    [string]$IdentityClientId,
    [string]$BackupStorageAccount = 'stpgcopy830992849ad4',
    [string]$BackupContainer = 'pulseexchange-backups',
    [switch]$Apply,
    [switch]$WritersFrozen,
    [switch]$VerifyRehearsal
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($SubscriptionId -ne [guid]'83099284-9ad4-4140-b8fe-8388b6d98a98') { throw 'This operator is scoped to the reviewed subscription only.' }
. (Join-Path $PSScriptRoot '../azure-economy/common.ps1')
$apiVersion = '2026-01-01'
$jobName = 'pulseexchange-dbcopy-prod'
$jobId = "/subscriptions/$SubscriptionId/resourceGroups/rg-pulseexchange-prod/providers/Microsoft.App/jobs/$jobName"

function Invoke-PrivateAzure([string[]]$Arguments) {
    Invoke-EconomyAzureJson -Arguments $Arguments -Sensitive
}
function Get-ArmUrl([string]$Id, [string]$Suffix = '') {
    "https://management.azure.com${Id}${Suffix}?api-version=$apiVersion"
}
function Assert-PrivateFolder([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw 'A protected state directory is required.' }
    $item = Get-Item -LiteralPath $Path
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'State must not be a linked directory.' }
    if ($IsWindows) {
        $acl = Get-Acl -LiteralPath $Path
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        if (-not $acl.AreAccessRulesProtected) { throw 'State directory must disable inherited permissions.' }
        foreach ($entry in $acl.Access) {
            if ($entry.AccessControlType -eq 'Allow' -and $entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $sid) {
                throw 'State must be readable only by the current operator.'
            }
        }
    } else {
        $mode = [IO.File]::GetUnixFileMode($Path)
        if (($mode -band [IO.UnixFileMode]63) -ne 0) { throw 'State directory must have no group/other permissions.' }
    }
}
function Save-State($State) {
    Assert-PrivateFolder $StateDirectory
    [IO.File]::WriteAllText((Join-Path $StateDirectory 'state.json'), ($State | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
}
function Read-State {
    Assert-PrivateFolder $StateDirectory
    $state = Get-Content -LiteralPath (Join-Path $StateDirectory 'state.json') -Raw | ConvertFrom-Json -AsHashtable -Depth 100
    if ($state.subscription -cne "$SubscriptionId" -or $state.schema -ne 1) { throw 'State identity mismatch.' }
    return $state
}
function Send-PrivateBody([string]$Method, [string]$Url, $Body) {
    Assert-PrivateFolder $StateDirectory
    $file = Join-Path $StateDirectory ('request-' + [guid]::NewGuid().ToString('N') + '.json')
    try {
        [IO.File]::WriteAllText($file, ($Body | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
        Invoke-PrivateAzure @('rest','--method',$Method,'--url',$Url,'--body',"@$file")
    } finally {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
    }
}
function Assert-Image([string]$Value) {
    if ($Value -cnotmatch '^ghcr\.io/irfanozer/eventharbor-backend@sha256:[0-9a-f]{64}$') {
        throw 'Use an immutable digest of the reviewed database-tools image.'
    }
}
function Get-Job {
    $job = Invoke-PrivateAzure @('rest','--method','get','--url',(Get-ArmUrl $jobId))
    if ($job.id -ine $jobId -or $job.tags.purpose -cne 'shared-postgres-copy' -or
        $job.tags.application -cne 'PulseExchange' -or $job.tags.environment -cne 'prod' -or
        $job.location -cne 'eastus2' -or $job.properties.provisioningState -cne 'Succeeded' -or
        $job.properties.environmentId -ine "/subscriptions/$SubscriptionId/resourceGroups/rg-pulseexchange-prod/providers/Microsoft.App/managedEnvironments/cae-pulseexchange-prod" -or
        $job.properties.configuration.triggerType -cne 'Manual' -or @($job.properties.template.containers).Count -ne 1 -or
        $job.properties.configuration.replicaRetryLimit -ne 0 -or
        $job.properties.configuration.manualTriggerConfig.parallelism -ne 1 -or
        $job.properties.configuration.manualTriggerConfig.replicaCompletionCount -ne 1) {
        throw 'Copy-job ownership or shape mismatch.'
    }
    $identityIds = @($job.identity.userAssignedIdentities.PSObject.Properties.Name)
    if ($job.identity.type -cne 'UserAssigned' -or $identityIds.Count -ne 1 -or
        $identityIds[0] -ine "/subscriptions/$SubscriptionId/resourceGroups/rg-demos-db-migration/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-px-pgcopy-prod") {
        throw 'Copy-job identity mismatch.'
    }
    $container = $job.properties.template.containers[0]
    Assert-Image $container.image
    if ($container.name -cne 'copy' -or ($container.command -join '|') -cne 'python3|/app/migrate.py') {
        throw 'Copy-job command mismatch.'
    }
    $expectedSecrets = @{ SOURCE_DATABASE_URL='source-url'; TARGET_ADMIN_DATABASE_URL='target-url'; TARGET_APP_PASSWORD='final-password'; REHEARSAL_APP_PASSWORD='rehearsal-password'; EVENTHARBOR_APP_PASSWORD='event-password' }
    $plain = @('MIGRATION_RUN_ID','BACKUP_STORAGE_ACCOUNT','BACKUP_CONTAINER','MIGRATION_IDENTITY_CLIENT_ID','WRITERS_FROZEN','VERIFY_TARGET')
    if (@($container.env).Count -ne ($expectedSecrets.Count + $plain.Count)) { throw 'Unexpected copy-job environment settings.' }
    foreach ($entry in $expectedSecrets.GetEnumerator()) {
        $values = @($container.env | Where-Object name -CEQ $entry.Key)
        if ($values.Count -ne 1 -or $values[0].secretRef -cne $entry.Value) { throw 'Copy-job secret reference mismatch.' }
    }
    foreach ($name in $plain) {
        $values = @($container.env | Where-Object name -CEQ $name)
        if ($values.Count -ne 1 -or -not (Get-EconomyValue $values[0] 'value')) { throw 'Copy-job public setting is missing.' }
    }
    if (@($container.env | Where-Object name -CEQ 'BACKUP_STORAGE_ACCOUNT')[0].value -cne 'stpgcopy830992849ad4' -or
        @($container.env | Where-Object name -CEQ 'BACKUP_CONTAINER')[0].value -cne 'pulseexchange-backups' -or
        @($container.env | Where-Object name -CEQ 'MIGRATION_RUN_ID')[0].value -cnotmatch '^px-[0-9]{14}$') {
        throw 'Copy-job backup target or operation ID mismatch.'
    }
    return $job
}
function Assert-NoJobExecution {
    $runs = @(Invoke-PrivateAzure @('containerapp','job','execution','list','--subscription',"$SubscriptionId",'-g','rg-pulseexchange-prod','-n',$jobName))
    if (@($runs | Where-Object { (Get-EconomyValue $_.properties 'status') -notin @('Succeeded','Failed','Stopped') }).Count) {
        throw 'A copy execution is active or indeterminate. Inspect it before running another.'
    }
}

if (-not $Apply -and $Action -ne 'Status') {
    Write-Host "Inspect-only: would perform $Action. Add -Apply after reviewing the runbook. No secrets read or cloud changes made."
    return
}
if ($Action -eq 'Snapshot') {
    if ($StateDirectory) { throw 'Snapshot always creates a new private directory; it never overwrites an existing state.' }
    $StateDirectory = New-EconomyPrivateDirectory
    $state = @{ schema = 1; subscription = "$SubscriptionId"; createdUtc = [datetime]::UtcNow.ToString('o');
        runId = 'px-' + [datetime]::UtcNow.ToString('yyyyMMddHHmmss'); resources = @{};
        finalPassword = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32));
        rehearsalPassword = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32));
        eventPassword = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)) }
    foreach ($project in @('eventharbor','pulseexchange')) {
        $parts = if ($project -eq 'eventharbor') { @(@('containerApps','api'),@('containerApps','worker'),@('jobs','cleanup'),@('jobs','migrate')) }
            else { @(@('containerApps','api'),@('containerApps','processor'),@('jobs','maintenance'),@('jobs','migrate'),@('jobs','seed')) }
        foreach ($part in $parts) {
            $name = "$project-$($part[1])-prod"
            $id = "/subscriptions/$SubscriptionId/resourceGroups/rg-$project-prod/providers/Microsoft.App/$($part[0])/$name"
            $config = Invoke-PrivateAzure @('rest','--method','get','--url',(Get-ArmUrl $id))
            if ($config.id -ine $id -or $config.tags.application -ine $project -or $config.tags.environment -cne 'prod') { throw 'Consumer identity mismatch.' }
            $secrets = Invoke-PrivateAzure @('rest','--method','post','--url',(Get-ArmUrl $id '/listSecrets'))
            $active = @()
            if ($part[0] -eq 'containerApps') {
                $active = @(Invoke-PrivateAzure @('containerapp','revision','list','--subscription',"$SubscriptionId",'-g',"rg-$project-prod",'-n',$name,'--query','[?properties.active].name'))
                if ($active.Count -ne 1) { throw 'Snapshot requires exactly one active application revision.' }
            }
            $state.resources[$name] = @{ id = $id; kind = $part[0]; config = $config; secrets = $secrets; active = $active }
        }
    }
    Save-State $state
    Write-Host "Protected recovery snapshot created: $StateDirectory"
    Write-Host 'Contains credentials. Keep it private and out of every repository, log and artifact.'
    return
}
if ($Action -eq 'Status') {
    $job = Get-Job
    Write-Host "Copy job: $jobName; provisioning: $($job.properties.provisioningState)"
    Invoke-PrivateAzure @('containerapp','job','execution','list','--subscription',"$SubscriptionId",'-g','rg-pulseexchange-prod','-n',$jobName,'--query','[].{name:name,status:properties.status,start:properties.startTime,end:properties.endTime}')
    return
}
$state = Read-State
if ($Action -eq 'CreateJob') {
    Assert-Image $Image
    $expectedIdentity = "/subscriptions/$SubscriptionId/resourceGroups/rg-demos-db-migration/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-px-pgcopy-prod"
    if ($IdentityResourceId -ine $expectedIdentity -or $IdentityClientId -notmatch '^[a-fA-F0-9-]{36}$' -or
        $BackupStorageAccount -cne 'stpgcopy830992849ad4' -or $BackupContainer -cne 'pulseexchange-backups') { throw 'Backup/identity target mismatch.' }
    $existing = @(Invoke-PrivateAzure @('containerapp','job','list','--subscription',"$SubscriptionId",'-g','rg-pulseexchange-prod','--query',"[?name=='$jobName'].name"))
    if ($existing.Count) { throw 'Copy job already exists; inspect rather than overwriting its credentials.' }
    $sourceSecrets = $state.resources['pulseexchange-api-prod'].secrets.value
    $targetSecrets = $state.resources['eventharbor-api-prod'].secrets.value
    $sourceUrl = @($sourceSecrets | Where-Object name -EQ 'database-url')[0].value
    $targetUrl = @($targetSecrets | Where-Object name -EQ 'database-url')[0].value
    if (-not $sourceUrl -or -not $targetUrl) { throw 'Expected administrator database secrets were not captured.' }
    $secretValues = @{ 'source-url' = $sourceUrl; 'target-url' = $targetUrl; 'final-password' = $state.finalPassword; 'rehearsal-password' = $state.rehearsalPassword; 'event-password' = $state.eventPassword }
    $env = @(
        @{name='SOURCE_DATABASE_URL';secretRef='source-url'},@{name='TARGET_ADMIN_DATABASE_URL';secretRef='target-url'},
        @{name='TARGET_APP_PASSWORD';secretRef='final-password'},@{name='REHEARSAL_APP_PASSWORD';secretRef='rehearsal-password'},@{name='EVENTHARBOR_APP_PASSWORD';secretRef='event-password'},
        @{name='MIGRATION_RUN_ID';value=$state.runId},@{name='BACKUP_STORAGE_ACCOUNT';value=$BackupStorageAccount},
        @{name='BACKUP_CONTAINER';value=$BackupContainer},@{name='MIGRATION_IDENTITY_CLIENT_ID';value=$IdentityClientId},
        @{name='WRITERS_FROZEN';value='false'},@{name='VERIFY_TARGET';value='final'}
    )
    $body = @{ location = 'eastus2'; tags = @{ application='PulseExchange'; environment='prod'; purpose='shared-postgres-copy' };
        identity = @{ type='UserAssigned'; userAssignedIdentities=@{ $IdentityResourceId = @{} } };
        properties = @{ environmentId=$state.resources['pulseexchange-api-prod'].config.properties.environmentId; workloadProfileName='Consumption';
            configuration=@{ triggerType='Manual'; replicaTimeout=1800; replicaRetryLimit=0; manualTriggerConfig=@{parallelism=1;replicaCompletionCount=1};
                secrets=@($secretValues.GetEnumerator() | ForEach-Object { @{name=$_.Key;value=$_.Value} }) };
            template=@{containers=@(@{name='copy';image=$Image;command=@('python3','/app/migrate.py');args=@('Inspect');env=$env;resources=@{cpu=0.5;memory='1Gi'}})} } }
    $null = Send-PrivateBody 'put' (Get-ArmUrl $jobId) $body
    $state.jobImage = $Image
    Save-State $state
    Write-Host 'Private copy job created. No database operation has started.'
    return
}
$job = Get-Job
if ($job.properties.template.containers[0].image -cne $state.jobImage) { throw 'Copy image differs from the protected snapshot.' }
Assert-NoJobExecution
if ($Action -in @('FinalCopy','IsolateEventHarbor') -and -not $WritersFrozen) { throw 'A final database change requires an independently verified write freeze and -WritersFrozen.' }
$template = $job.properties.template
$template.containers[0].args = @($Action)
$eventMode = @{ InspectEventHarbor='Inspect'; IsolateEventHarbor='Apply'; VerifyIsolation='Verify' }
if ($eventMode.ContainsKey($Action)) {
    $template.containers[0].command = @('python3','/app/isolate_eventharbor.py')
    $template.containers[0].args = @($eventMode[$Action])
}
foreach ($setting in $template.containers[0].env) {
    if ($setting.name -eq 'WRITERS_FROZEN') { $setting.value = if ($WritersFrozen) { 'true' } else { 'false' } }
    if ($setting.name -eq 'VERIFY_TARGET') { $setting.value = if ($VerifyRehearsal) { 'rehearsal' } else { 'final' } }
    if ($setting.name -eq 'MIGRATION_RUN_ID') {
        $setting.value = $state.runId + $(if ($eventMode.ContainsKey($Action)) { '-eventharbor' } elseif ($Action -eq 'Rehearse' -or $VerifyRehearsal) { '-rehearsal' } else { '-final' })
    }
}
$run = Send-PrivateBody 'post' (Get-ArmUrl $jobId '/start') $template
if (-not $run.name) { throw 'Azure did not identify the execution. Inspect job status before retrying.' }
$state.lastExecution = $run.name
$state.lastAction = $Action
Save-State $state
Write-Host "Started $Action execution $($run.name). Check Status and its sanitized logs; start acceptance is not migration success."
