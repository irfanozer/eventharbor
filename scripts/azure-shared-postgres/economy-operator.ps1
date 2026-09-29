#requires -Version 7.4
[CmdletBinding()]
param(
    [ValidateSet('Status','CreateJob','Inspect','Rehearse','FinalCopy','Verify')][string]$Action='Status',
    [Parameter(Mandatory)][ValidateSet('eventharbor','pulseexchange')][string]$Project,
    [Parameter(Mandatory)][string]$StateDirectory,
    [guid]$SubscriptionId='83099284-9ad4-4140-b8fe-8388b6d98a98',
    [string]$Image,
    [switch]$Apply,
    [switch]$WritersFrozen,
    [switch]$VerifyRehearsal
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if ("$SubscriptionId" -cne '83099284-9ad4-4140-b8fe-8388b6d98a98') {throw 'Only the reviewed subscription is supported.'}
. (Join-Path $PSScriptRoot '../azure-economy/common.ps1')
$apiVersion='2026-01-01'
$targetServer='psql-demos-economy-56sjj5b3bl7ro'
$targetHost="$targetServer.postgres.database.azure.com"
$jobName="economy-copy-$Project-prod"
$jobId="/subscriptions/$SubscriptionId/resourceGroups/rg-pulseexchange-prod/providers/Microsoft.App/jobs/$jobName"
$environmentId="/subscriptions/$SubscriptionId/resourceGroups/rg-pulseexchange-prod/providers/Microsoft.App/managedEnvironments/cae-pulseexchange-prod"
$identityId="/subscriptions/$SubscriptionId/resourceGroups/rg-demos-db-migration/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-px-pgcopy-prod"
$storage='stpgcopy830992849ad4'
$container='pulseexchange-backups'

function Invoke-EconomyCopyAzure([string[]]$Arguments) {Invoke-EconomyAzureJson -Arguments $Arguments -Sensitive}
function Get-EconomyCopyUrl([string]$Id,[string]$Suffix='') {"https://management.azure.com${Id}${Suffix}?api-version=$apiVersion"}
function Assert-EconomyCopyFolder {
    if (-not (Test-Path -LiteralPath $StateDirectory -PathType Container) -or
        ((Get-Item -LiteralPath $StateDirectory).Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw 'Use the existing protected snapshot directory, not a link.'}
    if ($IsWindows) {
        $acl=Get-Acl -LiteralPath $StateDirectory
        $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        if (-not $acl.AreAccessRulesProtected) {throw 'State directory must disable inherited access.'}
        foreach ($entry in $acl.Access) {
            if ($entry.AccessControlType -eq 'Allow' -and $entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $sid) {throw 'State must be readable only by the current operator.'}
        }
    } elseif (([IO.File]::GetUnixFileMode($StateDirectory) -band [IO.UnixFileMode]63) -ne 0) {throw 'State must have no group or other access.'}
}
function Get-EconomyCopyPrivatePath([string]$Name) {
    Assert-EconomyCopyFolder
    if ($Name -notmatch '^(state|economy-credentials|economy-copy-jobs)\.json$' -and $Name -notmatch '^economy-copy-request-[a-f0-9]{32}\.json$') {throw 'Unreviewed private filename.'}
    $path=Join-Path $StateDirectory $Name
    if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw 'Private state files must not be links.'}
    return $path
}
function Read-EconomyCopyState {
    $snapshot=Read-EconomyCopyJson 'state.json'
    if ($snapshot.schema -ne 1 -or $snapshot.subscription -cne "$SubscriptionId") {throw 'Original protected snapshot identity mismatch.'}
    return $snapshot
}
function Read-EconomyCopyJson([string]$Name) {
    try {return Get-Content -LiteralPath (Get-EconomyCopyPrivatePath $Name) -Raw | ConvertFrom-Json -AsHashtable -Depth 100}
    catch {throw 'Protected migration JSON cannot be read or parsed. Raw input and diagnostics withheld.'}
}
function Save-EconomyCopyJournal {
    [IO.File]::WriteAllText((Get-EconomyCopyPrivatePath 'economy-copy-jobs.json'),($journal | ConvertTo-Json -Depth 100),[Text.UTF8Encoding]::new($false))
}
function Assert-EconomyCopyImage([string]$Value) {
    if ($Value -cnotmatch '^ghcr\.io/irfanozer/eventharbor-backend@sha256:[a-f0-9]{64}$') {throw 'Use an immutable digest of the reviewed database-tools image.'}
}
function Send-EconomyCopyBody([string]$Method,[string]$Url,$Body) {
    $path=Get-EconomyCopyPrivatePath ('economy-copy-request-'+[guid]::NewGuid().ToString('N')+'.json')
    try {
        [IO.File]::WriteAllText($path,($Body | ConvertTo-Json -Depth 100),[Text.UTF8Encoding]::new($false))
        Invoke-EconomyCopyAzure @('rest','--method',$Method,'--url',$Url,'--body',"@$path")
    } finally {if (Test-Path -LiteralPath $path) {Remove-Item -LiteralPath $path}}
}
function Get-EconomyCopyIdentity {
    $identity=Invoke-EconomyCopyAzure @('rest','--method','get','--url',"https://management.azure.com${identityId}?api-version=2023-01-31")
    if ($identity.id -ine $identityId -or $identity.tags.purpose -cne 'shared-postgres-migration' -or
        $identity.tags.application -cne 'EventHarbor-PulseExchange' -or
        $identity.properties.clientId -notmatch '^[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$') {throw 'Existing backup managed identity does not match the reviewed resource.'}
    return $identity
}
function Assert-EconomyCopyTarget {
    $server=Invoke-EconomyCopyAzure @('postgres','flexible-server','show','--subscription',"$SubscriptionId",'-g','rg-demos-economy','-n',$targetServer)
    $expectedId="/subscriptions/$SubscriptionId/resourceGroups/rg-demos-economy/providers/Microsoft.DBforPostgreSQL/flexibleServers/$targetServer"
    $network="/subscriptions/$SubscriptionId/resourceGroups/rg-demos-economy/providers/Microsoft.Network"
    if ($server.id -ine $expectedId -or $server.fullyQualifiedDomainName -cne $targetHost -or
        $server.administratorLogin -cne 'portfolio_admin' -or $server.version -cne '17' -or $server.state -cne 'Ready' -or
        $server.location.Replace(' ','').ToLowerInvariant() -cne 'westus2' -or
        $server.tags.costProfile -cne 'economy' -or $server.tags.environment -cne 'economy' -or $server.tags.application -cne 'EventHarbor-PulseExchange' -or
        $server.network.publicNetworkAccess -cne 'Disabled' -or
        $server.network.delegatedSubnetResourceId -ine "$network/virtualNetworks/vnet-demos-economy/subnets/snet-postgres" -or
        $server.network.privateDnsZoneArmResourceId -ine "$network/privateDnsZones/demos-economy.postgres.database.azure.com") {throw 'New private PostgreSQL identity, owner, region, version or readiness mismatch.'}
}
function Get-EconomyCopySourceUrl($Snapshot) {
    $name="$Project-api-prod"
    $expectedId="/subscriptions/$SubscriptionId/resourceGroups/rg-$Project-prod/providers/Microsoft.App/containerApps/$name"
    if (-not $Snapshot.resources.ContainsKey($name) -or $Snapshot.resources[$name].id -ine $expectedId) {throw 'Source API is not the exact protected production snapshot.'}
    $values=@($Snapshot.resources[$name].secrets.value | Where-Object name -CEQ 'database-url')
    if ($values.Count -ne 1 -or -not $values[0].value) {throw 'Original database-url secret is missing or ambiguous.'}
    $url=[string]$values[0].value
    $sourceHost=if ($Project -eq 'eventharbor') {'psql-eventharbor-prod-dodczqz7eaeps.postgres.database.azure.com'} else {'psql-pulseexchange-prod-kj25jhmdlk6ik.postgres.database.azure.com'}
    try {
        $parsed=[uri]$url
        if ($parsed.Scheme -cne 'postgresql+asyncpg' -or $parsed.Host -cne $sourceHost -or $parsed.AbsolutePath -cne "/$Project" -or
            $parsed.Port -notin @(-1,5432) -or -not $parsed.UserInfo.Contains(':') -or $parsed.Fragment -or
            $parsed.Query -notin @('','?ssl=require','?ssl=verify-full')) {throw 'invalid'}
    } catch {throw 'Original source URL differs from the fixed project endpoint/database. Raw input withheld.'}
    return $url
}
function Get-EconomyCopyTargetUrl {
    $credentials=Read-EconomyCopyJson 'economy-credentials.json'
    if ($credentials.subscriptionId -cne "$SubscriptionId" -or $credentials.resourceGroup -cne 'rg-demos-economy' -or
        $credentials.location -cne 'westus2' -or $credentials.postgresAdministrator -cne 'portfolio_admin' -or
        $credentials.postgresAdministratorPassword -isnot [string] -or $credentials.postgresAdministratorPassword.Length -lt 8 -or
        $credentials.postgresAdministratorPassword.Length -gt 128) {throw 'New foundation credential metadata does not match the reviewed deployment.'}
    $password=[uri]::EscapeDataString([string]$credentials.postgresAdministratorPassword)
    return "postgresql+asyncpg://portfolio_admin:$password@${targetHost}:5432/postgres?ssl=verify-full"
}
function Get-EconomyCopyJob {
    $job=Invoke-EconomyCopyAzure @('rest','--method','get','--url',(Get-EconomyCopyUrl $jobId))
    if ($job.id -ine $jobId -or $job.tags.purpose -cne 'economy-postgres-copy' -or $job.tags.application -cne $Project -or
        $job.tags.environment -cne 'prod' -or $job.location -cne 'eastus2' -or $job.properties.provisioningState -cne 'Succeeded' -or
        $job.properties.environmentId -ine $environmentId -or $job.properties.configuration.triggerType -cne 'Manual' -or
        $job.properties.configuration.replicaRetryLimit -ne 0 -or $job.properties.configuration.manualTriggerConfig.parallelism -ne 1 -or
        $job.properties.configuration.manualTriggerConfig.replicaCompletionCount -ne 1 -or @($job.properties.template.containers).Count -ne 1) {throw 'Economy copy-job ownership or shape mismatch.'}
    $ids=@($job.identity.userAssignedIdentities.PSObject.Properties.Name)
    if ($job.identity.type -cne 'UserAssigned' -or $ids.Count -ne 1 -or $ids[0] -ine $identityId) {throw 'Copy job must use only the existing container-scoped backup identity.'}
    $copy=$job.properties.template.containers[0]
    Assert-EconomyCopyImage $copy.image
    if ($copy.name -cne 'copy' -or ($copy.command -join '|') -cne 'python3|/app/economy_migrate.py') {throw 'Wrong migration helper command.'}
    $secrets=@{SOURCE_DATABASE_URL='source-url';TARGET_ADMIN_DATABASE_URL='target-url';TARGET_APP_PASSWORD='app-password';REHEARSAL_APP_PASSWORD='rehearsal-password'}
    $plain=@{MIGRATION_PROJECT=$Project;BACKUP_STORAGE_ACCOUNT=$storage;BACKUP_CONTAINER=$container;MIGRATION_IDENTITY_CLIENT_ID=$identity.properties.clientId;WRITERS_FROZEN='false';VERIFY_TARGET='final';MIGRATION_RUN_ID=$record.runId}
    if (@($copy.env).Count -ne ($secrets.Count+$plain.Count)) {throw 'Unexpected copy-job environment settings.'}
    foreach ($key in $secrets.Keys) {
        $entry=@($copy.env | Where-Object name -CEQ $key)
        if ($entry.Count -ne 1 -or $entry[0].secretRef -cne $secrets[$key]) {throw 'Copy-job secret reference mismatch.'}
    }
    foreach ($key in $plain.Keys) {
        $entry=@($copy.env | Where-Object name -CEQ $key)
        if ($entry.Count -ne 1 -or $entry[0].value -cne $plain[$key]) {throw 'Copy-job project, backup, identity or run marker mismatch.'}
    }
    if ($copy.image -cne $record.image) {throw 'Copy image differs from the protected job journal.'}
    return $job
}
function Assert-EconomyCopyIdle {
    # Serialize both projects against the small shared server and backup job.
    foreach ($name in @('economy-copy-eventharbor-prod','economy-copy-pulseexchange-prod','pulseexchange-dbcopy-prod')) {
        $found=@(Invoke-EconomyCopyAzure @('containerapp','job','list','--subscription',"$SubscriptionId",'-g','rg-pulseexchange-prod','--query',"[?name=='$name'].name"))
        if ($found.Count -gt 1) {throw 'Ambiguous copy-job inventory.'}
        if (-not $found.Count) {continue}
        $runs=@(Invoke-EconomyCopyAzure @('containerapp','job','execution','list','--subscription',"$SubscriptionId",'-g','rg-pulseexchange-prod','-n',$name))
        if (@($runs | Where-Object {(Get-EconomyValue $_.properties 'status') -notin @('Succeeded','Failed','Stopped')}).Count) {throw 'A migration execution is active or indeterminate. Inspect it before starting another.'}
    }
}

if (-not $Apply -and $Action -ne 'Status') {
    Write-Host "Plan only: $Project $Action against the fixed new economy database. No secrets read or cloud calls made."
    return
}
$snapshot=Read-EconomyCopyState
$journalPath=Get-EconomyCopyPrivatePath 'economy-copy-jobs.json'
$journal=if (Test-Path -LiteralPath $journalPath) {Read-EconomyCopyJson 'economy-copy-jobs.json'}
    else {@{schema=1;subscription="$SubscriptionId";snapshotRunId=$snapshot.runId;targetHost=$targetHost;projects=@{}}}
if ($journal.schema -ne 1 -or $journal.subscription -cne "$SubscriptionId" -or $journal.snapshotRunId -cne $snapshot.runId -or $journal.targetHost -cne $targetHost) {throw 'Economy copy journal identity mismatch.'}
$identity=Get-EconomyCopyIdentity
Assert-EconomyCopyTarget
if ($Action -eq 'CreateJob') {
    Assert-EconomyCopyImage $Image
    $existing=@(Invoke-EconomyCopyAzure @('containerapp','job','list','--subscription',"$SubscriptionId",'-g','rg-pulseexchange-prod','--query',"[?name=='$jobName'].name"))
    if ($existing.Count) {throw 'Economy copy job already exists; never overwrite its credentials.'}
    Assert-EconomyCopyIdle
    $sourceUrl=Get-EconomyCopySourceUrl $snapshot
    $targetUrl=Get-EconomyCopyTargetUrl
    $appPassword=if ($Project -eq 'eventharbor') {$snapshot.eventPassword} else {$snapshot.finalPassword}
    if ($appPassword -cnotmatch '^[a-fA-F0-9]{64}$') {throw 'Expected retained per-project application password is missing.'}
    if (-not $journal.projects.ContainsKey($Project)) {
        $short=if ($Project -eq 'eventharbor') {'eh'} else {'px'}
        $journal.projects[$Project]=@{runId=('vmcopy-'+$short+'-'+[datetime]::UtcNow.ToString('yyyyMMddHHmmss'));image=$Image;
            rehearsalPassword=[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32));jobId=$jobId}
        Save-EconomyCopyJournal
    }
    $record=$journal.projects[$Project]
    if ($record.image -cne $Image -or $record.jobId -ine $jobId -or $record.rehearsalPassword -cnotmatch '^[a-fA-F0-9]{64}$') {throw 'Existing protected job-creation intent differs. Review it before retrying.'}
    $values=@{'source-url'=$sourceUrl;'target-url'=$targetUrl;'app-password'=$appPassword;'rehearsal-password'=$record.rehearsalPassword}
    $environment=@(
        @{name='SOURCE_DATABASE_URL';secretRef='source-url'},@{name='TARGET_ADMIN_DATABASE_URL';secretRef='target-url'},
        @{name='TARGET_APP_PASSWORD';secretRef='app-password'},@{name='REHEARSAL_APP_PASSWORD';secretRef='rehearsal-password'},
        @{name='MIGRATION_PROJECT';value=$Project},@{name='MIGRATION_RUN_ID';value=$record.runId},
        @{name='BACKUP_STORAGE_ACCOUNT';value=$storage},@{name='BACKUP_CONTAINER';value=$container},
        @{name='MIGRATION_IDENTITY_CLIENT_ID';value=$identity.properties.clientId},@{name='WRITERS_FROZEN';value='false'},@{name='VERIFY_TARGET';value='final'}
    )
    $body=@{location='eastus2';tags=@{application=$Project;environment='prod';purpose='economy-postgres-copy'};
        identity=@{type='UserAssigned';userAssignedIdentities=@{$identityId=@{}}};properties=@{environmentId=$environmentId;workloadProfileName='Consumption';
            configuration=@{triggerType='Manual';replicaTimeout=2400;replicaRetryLimit=0;manualTriggerConfig=@{parallelism=1;replicaCompletionCount=1};
                secrets=@($values.GetEnumerator() | ForEach-Object {@{name=$_.Key;value=$_.Value}})};
            template=@{containers=@(@{name='copy';image=$Image;command=@('python3','/app/economy_migrate.py');args=@('Inspect');env=$environment;resources=@{cpu=0.5;memory='1Gi'}})}}}
    $null=Send-EconomyCopyBody 'put' (Get-EconomyCopyUrl $jobId) $body
    Write-Host "Created dedicated $Project copy job. No database command has started."
    return
}
if (-not $journal.projects.ContainsKey($Project)) {throw 'Create this project copy job using its protected journal first.'}
$record=$journal.projects[$Project]
if ($record.jobId -ine $jobId -or $record.runId -cnotmatch '^vmcopy-(eh|px)-[0-9]{14}$') {throw 'Project copy journal is invalid.'}
$job=Get-EconomyCopyJob
if ($Action -eq 'Status') {
    Invoke-EconomyCopyAzure @('containerapp','job','execution','list','--subscription',"$SubscriptionId",'-g','rg-pulseexchange-prod','-n',$jobName,
        '--query','[].{name:name,status:properties.status,start:properties.startTime,end:properties.endTime}')
    return
}
Assert-EconomyCopyIdle
if ($Action -eq 'FinalCopy' -and -not $WritersFrozen) {throw 'FinalCopy requires independently verified stopped source writers and -WritersFrozen.'}
if ($VerifyRehearsal -and $Action -ne 'Verify') {throw 'VerifyRehearsal is only valid for Verify.'}
$template=$job.properties.template
$template.containers[0].args=@($Action)
$phase=if ($Action -eq 'Rehearse' -or $VerifyRehearsal) {'rehearsal'} else {'final'}
foreach ($setting in $template.containers[0].env) {
    if ($setting.name -ceq 'MIGRATION_RUN_ID') {$setting.value=$record.runId+'-'+$phase}
    if ($setting.name -ceq 'WRITERS_FROZEN') {$setting.value=if ($WritersFrozen) {'true'} else {'false'}}
    if ($setting.name -ceq 'VERIFY_TARGET') {$setting.value=$phase}
}
$result=Send-EconomyCopyBody 'post' (Get-EconomyCopyUrl $jobId '/start') $template
if (-not $result.name) {throw 'Execution acceptance is unconfirmed. Inspect job status before retrying.'}
$record.lastExecution=$result.name;$record.lastAction=$Action;$record.lastPhase=$phase
Save-EconomyCopyJournal
Write-Host "Started $Project $Action execution $($result.name). Verify execution success and sanitized helper results separately; no application URL or DNS was changed."
