#requires -Version 7.4
# Offline freeze/recovery regression checks. Native command discovery is blocked.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$repository=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
. (Join-Path $repository 'scripts/azure-economy/common.ps1')
$tokens=$null; $problems=$null
$tree=[Management.Automation.Language.Parser]::ParseFile((Join-Path $repository 'scripts/azure-shared-postgres/cutover.ps1'),[ref]$tokens,[ref]$problems)
if ($problems.Count) { throw 'Cutover controller did not parse.' }
foreach ($definition in $tree.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]},$false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$entryText=($tree.EndBlock.Statements | Where-Object {
    $_ -isnot [Management.Automation.Language.FunctionDefinitionAst] -and $_.Extent.Text -notmatch '^\. \(Join-Path \$PSScriptRoot'
} | ForEach-Object {$_.Extent.Text}) -join "`n"
if ($entryText -match 'azure-economy/common\.ps1') { throw 'Fixture refused an entry point that could replace mandatory mocks.' }
$entryPoint=[scriptblock]::Create($entryText)
$subscription='83099284-9ad4-4140-b8fe-8388b6d98a98'
$checks=0
function Assert-Check([bool]$Condition,[string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:checks++
}
function Invoke-FreezeFixture {
    param([string]$TestProject='pulseexchange',[string[]]$Actions=@('Inspect'),[switch]$NoApply,
          [switch]$Recover,[string]$Fault='',[string]$TestLocation='eastus2',[string]$TestRunning='Running')
    $StateDirectory=New-EconomyPrivateDirectory
    $Project=$TestProject; $SubscriptionId=[guid]$subscription; $Apply=-not $NoApply
    $ConfirmSourceStillAuthoritative=[bool]$Recover
    $worker=if ($Project -eq 'eventharbor') {'worker'} else {'processor'}
    $scheduled=if ($Project -eq 'eventharbor') {'cleanup'} else {'maintenance'}
    $configs=@{}; $secretValues=@{}; $revisions=@{}; $resources=@{}
    foreach ($part in @('api',$worker,$scheduled,'migrate','seed')) {
        $name="$Project-$part-prod"; $kind=if ($part -in @('api',$worker)) {'containerApps'} else {'jobs'}
        $id="/subscriptions/$subscription/resourceGroups/rg-$Project-prod/providers/Microsoft.App/$kind/$name"
        $configuration=if ($kind -eq 'containerApps') {@{activeRevisionsMode='Single'}} else {
            @{triggerType=$(if ($part -eq $scheduled) {'Schedule'} else {'Manual'});replicaTimeout=900;replicaRetryLimit=0;
              scheduleTriggerConfig=@{cronExpression='0 4 * * *';parallelism=1;replicaCompletionCount=1}}
        }
        $configs[$name]=@{id=$id;location=$TestLocation;tags=@{application=$Project;environment='prod'};
            properties=@{environmentId="/subscriptions/$subscription/resourceGroups/rg-$Project-prod/providers/Microsoft.App/managedEnvironments/cae-$Project-prod";
                provisioningState='Succeeded';configuration=$configuration;
                template=@{containers=@(@{name=$part;image='immutable-image';resources=@{cpu=0.25;memory='0.5Gi'};env=@(@{name='DATABASE_URL';secretRef='database-url'})});scale=@{minReplicas=1;maxReplicas=1}}}}
        $configs[$name]=$configs[$name] | ConvertTo-Json -Depth 100 | ConvertFrom-Json -Depth 100
        $secretValues[$name]=@([pscustomobject]@{name='database-url';value="postgresql+asyncpg://admin:PRIVATE_DATABASE_VALUE@source/$Project"},[pscustomobject]@{name='other';value='PRIVATE_OTHER_VALUE'})
        $resources[$name]=@{id=$id;config=$configs[$name];secrets=@{value=$secretValues[$name]}}
        $revisions[$name]=[pscustomobject]@{name="$name--profiled";active=$true;health='Healthy';running=$TestRunning}
    }
    if ($Fault -eq 'foreign') {$configs["$Project-api-prod"].tags.application='other'}
    if ($Fault -eq 'unhealthy') {$revisions["$Project-api-prod"].health='Unhealthy'}
    $snapshot=@{schema=1;subscription=$subscription;runId='px-20260929123045';resources=$resources}
    [IO.File]::WriteAllText((Join-Path $StateDirectory 'state.json'),($snapshot | ConvertTo-Json -Depth 100))
    $calls=[Collections.Generic.List[object]]::new(); $bodies=[Collections.Generic.List[object]]::new()
    $files=[Collections.Generic.List[string]]::new(); $operations=[Collections.Generic.List[string]]::new()
    function Get-Command { throw 'Native command discovery is forbidden in this offline fixture.' }
    function Start-Sleep { throw 'Fixture hit an unexpected polling loop.' }
    function Invoke-CutoverGitHub {
        param([string[]]$Arguments)
        if ($Arguments[0] -cne 'api') {throw 'GitHub mutations are forbidden.'}
        if ($Arguments[1] -match '/actions/variables/') {return [pscustomobject]@{value=$(if ($Fault -eq 'enabled') {'true'} else {'false'})}}
        if ($Arguments[1] -match '/workflows/') {return [pscustomobject]@{workflow_runs=@([pscustomobject]@{status=$(if ($Fault -eq 'workflow') {'in_progress'} else {'completed'})})}}
        if ($Arguments[1] -match '/environments/') {return [pscustomobject]@{variables=@()}}
        throw 'Unexpected GitHub request.'
    }
    function Invoke-EconomyAzureJson {
        param([string[]]$Arguments,[switch]$Sensitive)
        if (-not $Sensitive) {throw 'Fixture requires sensitive invocation.'}
        $calls.Add(@($Arguments))
        if ($Arguments[0] -eq 'rest') {
            $url=[uri]$Arguments[[array]::IndexOf($Arguments,'--url')+1]
            $path=$url.AbsolutePath
            if ($path.EndsWith('/listSecrets')) {
                $name=$path.Split('/')[-2]
                return [pscustomobject]@{value=$secretValues[$name]}
            }
            $name=$path.Split('/')[-1]
            if (-not $configs.ContainsKey($name)) {throw 'Unreviewed resource in fixture.'}
            if ($Arguments -contains 'patch') {
                $reference=$Arguments[[array]::IndexOf($Arguments,'--body')+1]
                if (-not $reference.StartsWith('@')) {throw 'A literal secret-bearing body was passed in argv.'}
                $file=$reference.Substring(1);$files.Add($file)
                if ([IO.Path]::GetDirectoryName($file) -ne $StateDirectory) {throw 'Private body escaped protected state.'}
                $body=Get-Content -LiteralPath $file -Raw | ConvertFrom-Json -Depth 100
                $bodies.Add($body)
                if ($Fault -eq 'patch-failure') {throw 'Safe fixture patch failure.'}
                foreach ($property in $body.properties.configuration.PSObject.Properties) {
                    if ($property.Name -eq 'secrets') {$secretValues[$name]=@($property.Value)}
                    else {$configs[$name].properties.configuration | Add-Member -NotePropertyName $property.Name -NotePropertyValue $property.Value -Force}
                }
            }
            return $configs[$name]
        }
        if (($Arguments[0..3] -join ' ') -eq 'containerapp job execution list') {
            return [pscustomobject]@{properties=[pscustomobject]@{status=$(if ($Fault -eq 'unknown-job') {'Unknown'} else {'Succeeded'})}}
        }
        if (($Arguments[0..1] -join ' ') -eq 'containerapp revision') {
            $name=$Arguments[[array]::IndexOf($Arguments,'-n')+1]
            if ($Arguments[2] -eq 'list') {return $revisions[$name]}
            $revision=$Arguments[[array]::IndexOf($Arguments,'--revision')+1]
            if ($revision -cne $revisions[$name].name) {throw 'A different revision was targeted.'}
            $operations.Add($Arguments[2]+':'+$name)
            switch ($Arguments[2]) {
                deactivate {$revisions[$name].active=$false}
                activate {$revisions[$name].active=$true}
                restart {$revisions[$name].health='Healthy';$revisions[$name].running=$TestRunning}
                default {throw 'Unreviewed revision mutation.'}
            }
            return [pscustomobject]@{name=$revision}
        }
        throw 'Unexpected Azure operation; no native process was invoked.'
    }
    try {
        $failure=$null; $output=@()
        foreach ($step in $Actions) {
            $Action=$step
            if ($step -eq 'StartWorker' -and $Fault -eq 'profile-drift') {$configs["$Project-$worker-prod"].properties.template.containers[0].resources.cpu=0.5}
            if ($step -eq 'StartWorker' -and $Fault -eq 'secret-drift') {$secretValues["$Project-$worker-prod"][0].value='PRIVATE_CHANGED_DATABASE'}
            try {$output+=@(& $entryPoint 6>&1)} catch {$failure=$_.Exception.Message;break}
        }
        foreach ($file in $files) {Assert-Check (-not (Test-Path -LiteralPath $file)) 'Private request body was not cleaned up.'}
        Assert-Check ((($output | Out-String)+$failure) -notmatch 'PRIVATE_[A-Z_]+') 'Private values leaked through output.'
        Assert-Check ((($calls | ForEach-Object {$_ -join ' '}) -join ' ') -notmatch 'PRIVATE_[A-Z_]+') 'Private values entered CLI arguments.'
        foreach ($body in $bodies) {
            Assert-Check (@($body.properties.PSObject.Properties.Name).Count -eq 1 -and $body.properties.PSObject.Properties.Name -ceq 'configuration') 'A patch can overwrite application profiles.'
            Assert-Check (@($body.properties.configuration.PSObject.Properties.Name | Where-Object {$_ -notin @('triggerType','scheduleTriggerConfig','manualTriggerConfig','secrets')}).Count -eq 0) 'Patch exceeded schedule/secret-preservation scope.'
            Assert-Check (@($body.properties.configuration.secrets | Where-Object name -CEQ 'database-url')[0].value -ceq "postgresql+asyncpg://admin:PRIVATE_DATABASE_VALUE@source/$Project") 'Controller changed the existing database URL.'
        }
        $journalFile=Join-Path $StateDirectory "cutover-$Project.json"
        $journalResult=if (Test-Path -LiteralPath $journalFile) {Get-Content -LiteralPath $journalFile -Raw | ConvertFrom-Json -AsHashtable -Depth 100} else {$null}
        return @{Failed=($null -ne $failure);Failure=$failure;Calls=$calls.Count;Bodies=$bodies.Count;Operations=@($operations.ToArray());Journal=$journalResult}
    } finally {
        $resolved=[IO.Path]::GetFullPath($StateDirectory)
        $temporary=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar)+[IO.Path]::DirectorySeparatorChar
        if (-not $resolved.StartsWith($temporary,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notmatch '^azure-economy-[a-f0-9]{32}$') {throw 'Unsafe fixture cleanup path.'}
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

$result=Invoke-FreezeFixture -Actions PauseJobs -NoApply
Assert-Check (-not $result.Failed -and $result.Calls -eq 0) 'Plan mode accessed Azure.'
$result=Invoke-FreezeFixture
Assert-Check (-not $result.Failed -and $result.Bodies -eq 0 -and $result.Operations.Count -eq 0) "Inspect failed or mutated state: $($result.Failure)"
$result=Invoke-FreezeFixture -TestLocation 'East US 2'
Assert-Check (-not $result.Failed -and $result.Bodies -eq 0 -and $result.Operations.Count -eq 0) "Azure display-name region was not normalized: $($result.Failure)"
foreach ($wrongRegion in @('East US','West US 2')) {
    $result=Invoke-FreezeFixture -Actions PauseJobs -TestLocation $wrongRegion
    Assert-Check ($result.Failed -and $result.Bodies -eq 0 -and $result.Operations.Count -eq 0) 'An unapproved consumer region was accepted.'
}
foreach ($fault in @('enabled','workflow','unknown-job','patch-failure')) {
    $result=Invoke-FreezeFixture -Actions PauseJobs -Fault $fault
    Assert-Check $result.Failed 'A deployment/job/patch blocker was ignored.'
    Assert-Check ($result.Operations.Count -eq 0) 'Applications changed despite a pause blocker.'
}
$result=Invoke-FreezeFixture -Fault foreign
Assert-Check $result.Failed 'Foreign consumer ownership passed inspection.'
$result=Invoke-FreezeFixture -Actions @('PauseJobs','StopApi') -Fault unhealthy
Assert-Check ($result.Failed -and $result.Operations.Count -eq 0) 'An unhealthy revision was selected for freeze.'
$result=Invoke-FreezeFixture -Actions @('PauseJobs','StopWorker')
Assert-Check ($result.Failed -and $result.Operations.Count -eq 0) 'Worker stopped before API freeze.'
$freeze=@('PauseJobs','StopApi','StopWorker')
foreach ($badRunning in @('Unknown','Stopped','Processing','Degraded','Failed','ScaleTo0')) {
    $result=Invoke-FreezeFixture -Actions $freeze -TestRunning $badRunning
    Assert-Check ($result.Failed -and $result.Operations.Count -eq 0) 'An unrecognized or non-running revision was accepted.'
}
$result=Invoke-FreezeFixture -Actions $freeze
Assert-Check (-not $result.Failed -and $result.Operations.Count -eq 2 -and $result.Journal.stopped.Count -eq 2) "Freeze sequence failed: $($result.Failure)"
$result=Invoke-FreezeFixture -Actions ($freeze+@('StartWorker'))
Assert-Check ($result.Failed -and $result.Operations.Count -eq 2) 'Recovery bypassed source-authority acknowledgment.'
foreach ($fault in @('profile-drift','secret-drift')) {
    $result=Invoke-FreezeFixture -Actions ($freeze+@('StartWorker')) -Recover -Fault $fault
    Assert-Check ($result.Failed -and $result.Operations.Count -eq 2) 'Recovery ignored profile/database drift.'
}
foreach ($project in @('eventharbor','pulseexchange')) {
    $result=Invoke-FreezeFixture -TestProject $project -Actions ($freeze+@('StartWorker','StartApi','ResumeJobs')) -Recover
    Assert-Check (-not $result.Failed) "Full freeze/recovery fixture failed: $($result.Failure)"
    Assert-Check ($result.Bodies -eq 2 -and $result.Operations.Count -eq 6 -and -not $result.Journal.jobsPaused) 'Recovery changed unexpected resources or failed to restore scheduling.'
    Assert-Check ($result.Journal.schedule.cronExpression -ceq '0 4 * * *') 'Original schedule was lost.'
    $result=Invoke-FreezeFixture -TestProject $project -TestRunning RunningAtMaxScale -Actions ($freeze+@('StartWorker','StartApi','ResumeJobs')) -Recover
    Assert-Check (-not $result.Failed -and $result.Operations.Count -eq 6 -and -not $result.Journal.jobsPaused) 'A healthy max-scale revision failed guarded freeze/recovery.'
}
Assert-Check ($tree.ParamBlock.Extent.Text -notmatch 'SwitchSecrets|PublishGitHubSecret') 'Obsolete CA database switching remains callable.'
Write-Host "PASS $checks source freeze/recovery checks; no Azure or GitHub requests made."
