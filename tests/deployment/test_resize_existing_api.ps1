#requires -Version 7.4
$ErrorActionPreference = 'Stop'
$path = Join-Path $PSScriptRoot '../../scripts/azure-economy/resize-existing-api.ps1'
$tokens = $null
$problems = $null
$tree = [Management.Automation.Language.Parser]::ParseFile((Resolve-Path $path), [ref]$tokens, [ref]$problems)
if ($problems.Count) { throw 'Resize script did not parse.' }
# Load the actual helper functions without executing the production entry point.
foreach ($definition in $tree.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Set-Item -LiteralPath "Function:$($definition.Name)" -Value $definition.Body.GetScriptBlock()
}
$subscription = [guid]'00000000-0000-4000-8000-000000000001'
$script:scenario = 'inspect'
$script:updates = 0
$script:targetCpu = [decimal]0.5
$script:targetMemory = '1Gi'

function Invoke-ResizeAzureJson {
    param([string[]]$Arguments)
    if ($Arguments[0] -ne 'containerapp') { throw 'Unexpected command in resize fixture.' }
    if ($Arguments[1] -eq 'update') {
        if ($Arguments -contains '--image' -or $Arguments -contains '--min-replicas' -or
            $Arguments -contains '--max-replicas' -or $Arguments -contains '--set-env-vars' -or
            $Arguments -notcontains '--no-wait') { throw 'Resize arguments exceeded CPU/memory scope.' }
        $script:updates++
        $script:targetCpu = [decimal]$Arguments[[array]::IndexOf($Arguments, '--cpu') + 1]
        $script:targetMemory = $Arguments[[array]::IndexOf($Arguments, '--memory') + 1]
        return 'eventharbor-api-prod'
    }
    $revision = if ($script:updates) { 'api--new' } else { 'api--old' }
    if ($Arguments[1] -eq 'revision') {
        return [pscustomobject]@{
            name = $revision; running = 'Running'
            health = $(if ($script:scenario -eq 'unhealthy') { 'Unhealthy' } else { 'Healthy' })
        }
    }
    if ($Arguments[1] -ne 'show') { throw 'Unexpected operation in resize fixture.' }
    $image = if ($script:scenario -eq 'image-drift' -and $script:updates) { 'changed-image' } else { 'ghcr.io/example/backend@sha256:fixture' }
    $container = [pscustomobject]@{ name = 'api'; image = $image; cpu = $script:targetCpu; memory = $script:targetMemory }
    $containers = @($container)
    if ($script:scenario -eq 'sidecar') { $containers += [pscustomobject]@{ name = 'sidecar'; image = 'other' } }
    return [pscustomobject]@{
        id = "/subscriptions/$subscription/resourceGroups/rg-eventharbor-prod/providers/Microsoft.App/containerApps/eventharbor-api-prod"
        name = 'eventharbor-api-prod'
        tags = [pscustomobject]@{
            application = $(if ($script:scenario -eq 'foreign') { 'other' } else { 'EventHarbor' })
            environment = 'prod'
        }
        mode = 'Single'; state = 'Succeeded'; running = 'Running'; latest = $revision; ready = $revision
        containers = $containers
        preserved = [pscustomobject]@{
            scale = [pscustomobject]@{ minReplicas = $(if ($script:scenario -eq 'cold') { 0 } else { 1 }); maxReplicas = 2 }
            containers = @([pscustomobject]@{ name = 'api'; image = $image })
        }
    }
}

foreach ($scenario in @('inspect', 'apply', 'restore', 'foreign', 'cold', 'sidecar', 'unhealthy', 'image-drift')) {
    $script:scenario = $scenario
    $script:updates = 0
    $script:targetCpu = if ($scenario -eq 'restore') { [decimal]0.25 } else { [decimal]0.5 }
    $script:targetMemory = if ($scenario -eq 'restore') { '0.5Gi' } else { '1Gi' }
    $failed = $false
    try {
        $result = Invoke-ResizeExistingApi -SubscriptionId $subscription -Project eventharbor -Apply:($scenario -ne 'inspect') -Restore:($scenario -eq 'restore')
    } catch { $failed = $true }
    $shouldFail = $scenario -in @('foreign', 'cold', 'sidecar', 'unhealthy', 'image-drift')
    if ($failed -ne $shouldFail) { throw "Unexpected outcome for $scenario." }
    $expectedUpdates = if ($scenario -in @('apply', 'restore', 'image-drift')) { 1 } else { 0 }
    if ($script:updates -ne $expectedUpdates) { throw "Unexpected cloud-write count in mock scenario $scenario." }
    if ($scenario -eq 'apply' -and ($result.Cpu -ne 0.25 -or $result.Memory -ne '0.5Gi')) { throw 'Downsize postcheck failed.' }
    if ($scenario -eq 'restore' -and ($result.Cpu -ne 0.5 -or $result.Memory -ne '1Gi')) { throw 'Restore postcheck failed.' }
    Write-Output "Passed: $scenario"
}
$script:scenario = 'apply'
$script:updates = 0
$script:targetCpu = [decimal]0.25
$script:targetMemory = '0.5Gi'
$result = Invoke-ResizeExistingApi -SubscriptionId $subscription -Project eventharbor -Apply
if ($script:updates -ne 0 -or $result.Action -ne 'AlreadyAtRequestedSize') { throw 'Idempotent application must not create another revision.' }
Write-Output 'Passed: idempotent requested size'
Write-Output 'ECONOMY_API_RESIZE_OFFLINE_CHECKS_PASS'
