#requires -Version 7.4
# Offline only. No Azure process, managed identity request or real secret is used.
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$repository=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
. (Join-Path $repository 'scripts/azure-economy/common.ps1')
$tokens=$null;$errors=$null
$tree=[Management.Automation.Language.Parser]::ParseFile((Join-Path $repository 'scripts/azure-shared-postgres/economy-operator.ps1'),[ref]$tokens,[ref]$errors)
if ($errors.Count) {throw 'Economy operator did not parse.'}
foreach ($definition in $tree.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]},$false)) {. ([scriptblock]::Create($definition.Extent.Text))}
$entryText=($tree.EndBlock.Statements | Where-Object {$_ -isnot [Management.Automation.Language.FunctionDefinitionAst] -and $_.Extent.Text -notmatch '^\. \(Join-Path \$PSScriptRoot'} | ForEach-Object {$_.Extent.Text}) -join "`n"
if ($entryText -match 'azure-economy/common\.ps1') {throw 'Mandatory CLI mock could be replaced.'}
$entry=[scriptblock]::Create($entryText)
$subscription='83099284-9ad4-4140-b8fe-8388b6d98a98'
$target='psql-demos-economy-56sjj5b3bl7ro.postgres.database.azure.com'
$imageValue='ghcr.io/irfanozer/eventharbor-backend@sha256:'+('d'*64)
$checks=0
function Assert-Check([bool]$Condition,[string]$Message) {if (-not $Condition) {throw $Message};$script:checks++}
function Invoke-EconomyOperatorFixture {
    param([string]$TestAction='Status',[string]$TestProject='pulseexchange',[switch]$NoApply,[switch]$Frozen,
          [switch]$Rehearsal,[string]$Fault='',[scriptblock]$MutateJob,[switch]$NoJournal)
    $StateDirectory=New-EconomyPrivateDirectory
    $Action=$TestAction;$Project=$TestProject;$SubscriptionId=[guid]$subscription;$Apply=-not $NoApply
    $WritersFrozen=[bool]$Frozen;$VerifyRehearsal=[bool]$Rehearsal;$Image=$imageValue
    if ($Fault -eq 'image') {$Image='ghcr.io/irfanozer/eventharbor-backend:latest'}
    $prefix="/subscriptions/$subscription/resourceGroups/"
    $identityResource=$prefix+'rg-demos-db-migration/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-px-pgcopy-prod'
    $jobResource=$prefix+"rg-pulseexchange-prod/providers/Microsoft.App/jobs/economy-copy-$Project-prod"
    $environmentResource=$prefix+'rg-pulseexchange-prod/providers/Microsoft.App/managedEnvironments/cae-pulseexchange-prod'
    $client='00000000-0000-4000-8000-000000000001'
    $resources=@{}
    foreach ($app in @('eventharbor','pulseexchange')) {
        $hostName=if ($app -eq 'eventharbor') {'psql-eventharbor-prod-dodczqz7eaeps.postgres.database.azure.com'} else {'psql-pulseexchange-prod-kj25jhmdlk6ik.postgres.database.azure.com'}
        $resources["$app-api-prod"]=@{id=$prefix+"rg-$app-prod/providers/Microsoft.App/containerApps/$app-api-prod";
            secrets=@{value=@(@{name='database-url';value="postgresql+asyncpg://admin:PRIVATE_SOURCE_VALUE@${hostName}:5432/${app}?ssl=require"})}}
    }
    if ($Fault -eq 'source') {$resources["$Project-api-prod"].secrets.value[0].value='postgresql+asyncpg://admin:PRIVATE_SOURCE_VALUE@wrong.example/db'}
    $snapshot=@{schema=1;subscription=$subscription;runId='px-20260929123045';resources=$resources;eventPassword=('A'*64);finalPassword=('B'*64)}
    $credentials=@{subscriptionId=$subscription;resourceGroup='rg-demos-economy';location='westus2';postgresAdministrator='portfolio_admin';postgresAdministratorPassword='PRIVATE_TARGET_ADMIN_VALUE'}
    if ($Fault -eq 'credentials') {$credentials.resourceGroup='rg-eventharbor-prod'}
    [IO.File]::WriteAllText((Join-Path $StateDirectory 'state.json'),($snapshot | ConvertTo-Json -Depth 50))
    [IO.File]::WriteAllText((Join-Path $StateDirectory 'economy-credentials.json'),($credentials | ConvertTo-Json -Depth 20))
    if ($Fault -eq 'malformed-json') {[IO.File]::WriteAllText((Join-Path $StateDirectory 'economy-credentials.json'),'{PRIVATE_BAD_JSON')}
    $base=if ($Project -eq 'eventharbor') {'vmcopy-eh-20260929123045'} else {'vmcopy-px-20260929123045'}
    $saved=@{schema=1;subscription=$subscription;snapshotRunId=$snapshot.runId;targetHost=$target;
        projects=@{$Project=@{runId=$base;image=$imageValue;jobId=$jobResource;rehearsalPassword=('C'*64)}}}
    if (-not $NoJournal) {[IO.File]::WriteAllText((Join-Path $StateDirectory 'economy-copy-jobs.json'),($saved | ConvertTo-Json -Depth 30))}
    $envValues=@(
        @{name='SOURCE_DATABASE_URL';secretRef='source-url'},@{name='TARGET_ADMIN_DATABASE_URL';secretRef='target-url'},
        @{name='TARGET_APP_PASSWORD';secretRef='app-password'},@{name='REHEARSAL_APP_PASSWORD';secretRef='rehearsal-password'},
        @{name='MIGRATION_PROJECT';value=$Project},@{name='MIGRATION_RUN_ID';value=$base},
        @{name='BACKUP_STORAGE_ACCOUNT';value='stpgcopy830992849ad4'},@{name='BACKUP_CONTAINER';value='pulseexchange-backups'},
        @{name='MIGRATION_IDENTITY_CLIENT_ID';value=$client},@{name='WRITERS_FROZEN';value='false'},@{name='VERIFY_TARGET';value='final'}
    )
    $job=@{id=$jobResource;location='eastus2';tags=@{purpose='economy-postgres-copy';application=$Project;environment='prod'};
        identity=@{type='UserAssigned';userAssignedIdentities=@{$identityResource=@{}}};properties=@{environmentId=$environmentResource;provisioningState='Succeeded';
            configuration=@{triggerType='Manual';replicaRetryLimit=0;manualTriggerConfig=@{parallelism=1;replicaCompletionCount=1}};
            template=@{containers=@(@{name='copy';image=$imageValue;command=@('python3','/app/economy_migrate.py');args=@('Inspect');env=$envValues})}}}
    $job=$job | ConvertTo-Json -Depth 50 | ConvertFrom-Json -Depth 50
    if ($MutateJob) {& $MutateJob $job}
    $server=@{id=$prefix+'rg-demos-economy/providers/Microsoft.DBforPostgreSQL/flexibleServers/psql-demos-economy-56sjj5b3bl7ro';fullyQualifiedDomainName=$target;
        administratorLogin='portfolio_admin';version='17';state='Ready';location='westus2';tags=@{costProfile='economy';environment='economy';application='EventHarbor-PulseExchange'};
        network=@{publicNetworkAccess='Disabled';delegatedSubnetResourceId=$prefix+'rg-demos-economy/providers/Microsoft.Network/virtualNetworks/vnet-demos-economy/subnets/snet-postgres';
            privateDnsZoneArmResourceId=$prefix+'rg-demos-economy/providers/Microsoft.Network/privateDnsZones/demos-economy.postgres.database.azure.com'}}
    if ($Fault -eq 'target') {$server.network.publicNetworkAccess='Enabled'}
    $server=$server | ConvertTo-Json -Depth 30 | ConvertFrom-Json
    $calls=[Collections.Generic.List[object]]::new();$bodies=[Collections.Generic.List[object]]::new();$paths=[Collections.Generic.List[string]]::new()
    function Get-Command {throw 'Native command discovery is forbidden in this offline fixture.'}
    function Invoke-EconomyAzureJson {
        param([string[]]$Arguments,[switch]$Sensitive)
        if (-not $Sensitive) {throw 'Sensitive invocation is mandatory.'}
        $calls.Add(@($Arguments))
        if ($Arguments -contains '--body') {
            $reference=$Arguments[[array]::IndexOf($Arguments,'--body')+1]
            if (-not $reference.StartsWith('@')) {throw 'Secret request body was passed in argv.'}
            $path=$reference.Substring(1);$paths.Add($path)
            if ([IO.Path]::GetDirectoryName($path) -ne $StateDirectory) {throw 'Private body escaped its protected directory.'}
            $body=Get-Content -LiteralPath $path -Raw | ConvertFrom-Json -Depth 100;$bodies.Add($body)
            if ($Fault -eq 'body') {throw 'Safe fixture request failure.'}
            return [pscustomobject]@{name='economy-copy-execution'}
        }
        if ($Arguments[0] -eq 'postgres') {return $server}
        if ($Arguments[0] -eq 'rest') {
            $url=$Arguments[[array]::IndexOf($Arguments,'--url')+1]
            if ($url -match 'Microsoft.ManagedIdentity') {
                return [pscustomobject]@{id=$identityResource;tags=[pscustomobject]@{purpose='shared-postgres-migration';application='EventHarbor-PulseExchange'};properties=[pscustomobject]@{clientId=$client}}
            }
            if ($url -like "https://management.azure.com${jobResource}?*") {return $job}
        }
        if (($Arguments[0..2] -join ' ') -eq 'containerapp job list') {
            if ($TestAction -ne 'CreateJob' -or $Fault -eq 'existing') {return "economy-copy-$Project-prod"}
            return
        }
        if (($Arguments[0..3] -join ' ') -eq 'containerapp job execution list') {
            return [pscustomobject]@{name='last-execution';properties=[pscustomobject]@{status=$(if ($Fault -eq 'active') {'Unknown'} else {'Succeeded'})}}
        }
        throw 'Unexpected Azure operation; no native process was invoked.'
    }
    try {
        $failure=$null;$output=@()
        try {$output=@(& $entry 6>&1)} catch {$failure=$_.Exception.Message}
        foreach ($path in $paths) {Assert-Check (-not (Test-Path -LiteralPath $path)) 'A private request file was not removed.'}
        Assert-Check ((($output | Out-String)+$failure) -notmatch 'PRIVATE_[A-Z_]+') 'A secret marker leaked in output/error text.'
        Assert-Check ((($calls | ForEach-Object {$_ -join ' '}) -join ' ') -notmatch 'PRIVATE_[A-Z_]+') 'A secret marker appeared in CLI arguments.'
        $resultPath=Join-Path $StateDirectory 'economy-copy-jobs.json'
        $journalResult=if (Test-Path -LiteralPath $resultPath) {Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json -AsHashtable -Depth 100} else {$null}
        return @{Failed=($null -ne $failure);Failure=$failure;Calls=$calls.Count;Bodies=@($bodies.ToArray());Journal=$journalResult}
    } finally {
        $resolved=[IO.Path]::GetFullPath($StateDirectory);$temporary=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar)+[IO.Path]::DirectorySeparatorChar
        if (-not $resolved.StartsWith($temporary,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notmatch '^azure-economy-[a-f0-9]{32}$') {throw 'Unsafe fixture cleanup path.'}
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
$result=Invoke-EconomyOperatorFixture -TestAction CreateJob -NoApply
Assert-Check (-not $result.Failed -and $result.Calls -eq 0) 'Plan mode made Azure requests.'
$result=Invoke-EconomyOperatorFixture
Assert-Check (-not $result.Failed -and $result.Bodies.Count -eq 0) "Status fixture failed: $($result.Failure)"
foreach ($project in @('eventharbor','pulseexchange')) {
    $result=Invoke-EconomyOperatorFixture -TestAction CreateJob -TestProject $project -NoJournal
    Assert-Check (-not $result.Failed -and $result.Bodies.Count -eq 1) "Creation fixture failed: $($result.Failure)"
    $body=$result.Bodies[0];$secrets=$body.properties.configuration.secrets
    $targetUrl=@($secrets | Where-Object name -CEQ 'target-url')[0].value
    $sourceUrl=@($secrets | Where-Object name -CEQ 'source-url')[0].value
    Assert-Check ($targetUrl.EndsWith("@${target}:5432/postgres?ssl=verify-full")) 'Wrong final target URL.'
    Assert-Check ($sourceUrl.EndsWith("/${project}?ssl=require")) 'Wrong project source URL.'
    $expected=if ($project -eq 'eventharbor') {'A'*64} else {'B'*64}
    Assert-Check (@($secrets | Where-Object name -CEQ 'app-password')[0].value -ceq $expected) 'Per-project retained final password changed.'
    Assert-Check ($result.Journal.projects[$project].rehearsalPassword -cmatch '^[a-fA-F0-9]{64}$') 'New per-project rehearsal password was not saved securely.'
    Assert-Check (($body.properties.template.containers[0].command -join '|') -ceq 'python3|/app/economy_migrate.py') 'Wrong helper module.'
}
foreach ($fault in @('image','existing','source','credentials','malformed-json','target','body')) {
    $result=Invoke-EconomyOperatorFixture -TestAction CreateJob -Fault $fault
    Assert-Check $result.Failed 'Unsafe job creation was accepted.'
    if ($fault -ne 'body') {Assert-Check ($result.Bodies.Count -eq 0) 'An invalid creation case submitted a mutation.'}
}
foreach ($mutation in @(
    {param($j) $j.id+='-foreign'}, {param($j) $j.tags.application='other'}, {param($j) $j.properties.configuration.triggerType='Schedule'},
    {param($j) $j.properties.configuration.replicaRetryLimit=1}, {param($j) $j.properties.configuration.manualTriggerConfig.parallelism=2},
    {param($j) $j.properties.template.containers[0].command=@('python3','/app/migrate.py')},
    {param($j) $j.properties.template.containers[0].env[4].value='other'},
    {param($j) $j.properties.template.containers[0].env[6].value='otheraccount'},
    {param($j) $j.properties.template.containers[0].env[0].secretRef='other'},
    {param($j) $j.properties.provisioningState='Unknown'}
)) {
    $result=Invoke-EconomyOperatorFixture -MutateJob $mutation
    Assert-Check ($result.Failed -and $result.Bodies.Count -eq 0) 'Mutated copy job passed its guards.'
}
$result=Invoke-EconomyOperatorFixture -TestAction Rehearse -Fault active
Assert-Check ($result.Failed -and $result.Bodies.Count -eq 0) 'An indeterminate competing execution was ignored.'
$result=Invoke-EconomyOperatorFixture -TestAction FinalCopy
Assert-Check ($result.Failed -and $result.Bodies.Count -eq 0) 'FinalCopy bypassed the writer-freeze gate.'
$rehearse=Invoke-EconomyOperatorFixture -TestAction Rehearse
$final=Invoke-EconomyOperatorFixture -TestAction FinalCopy -Frozen
$verifyRehearse=Invoke-EconomyOperatorFixture -TestAction Verify -Rehearsal
$verifyFinal=Invoke-EconomyOperatorFixture -TestAction Verify
foreach ($case in @($rehearse,$final,$verifyRehearse,$verifyFinal)) {Assert-Check (-not $case.Failed -and $case.Bodies.Count -eq 1) "Execution fixture failed: $($case.Failure)"}
function Get-Run($Case) {@($Case.Bodies[0].containers[0].env | Where-Object name -CEQ 'MIGRATION_RUN_ID')[0].value}
Assert-Check ((Get-Run $rehearse) -cne (Get-Run $final)) 'Rehearsal and final backup paths collide.'
Assert-Check ((Get-Run $rehearse) -ceq (Get-Run $verifyRehearse)) 'Rehearsal Verify selected the wrong manifest.'
Assert-Check ((Get-Run $final) -ceq (Get-Run $verifyFinal)) 'Final Verify selected the wrong manifest.'
Write-Host "PASS $checks economy copy operator checks; no Azure calls or real credentials used."
