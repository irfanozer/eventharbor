#Requires -Version 7.4
# Loads only functions, replacing every Azure request and wait with offline fixtures.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$scriptPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../scripts/azure-economy/attach-restored-runtime.ps1'))
$tokens = $null
$problems = $null
$tree = [Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$problems)
if ($problems.Count) { throw 'Attach helper has syntax errors.' }
foreach ($function in $tree.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Set-Item -LiteralPath "Function:$($function.Name)" -Value $function.Body.GetScriptBlock()
}
$script:checks = 0
function Assert-Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:checks++
}
$settings = @{
    SubscriptionId = [guid]'83099284-9ad4-4140-b8fe-8388b6d98a98'; ResourceGroup = 'rg-demos-economy'
    VmName = 'vm-eh-demos-economy'; Project = 'eventharbor'
    PostgresHost = 'psql-demos-economy-fixture.postgres.database.azure.com'
    PublicHostname = 'eventharbor.example.com'; AcmeEmail = 'operator@example.com'
    AppPassword = ConvertTo-SecureString 'PRIVATE_APP_FIXTURE' -AsPlainText -Force
}
$script:calls = [System.Collections.Generic.List[string]]::new()
$script:mode = 'success'
$script:bodyPath = $null
$script:statusReads = 0
function Start-Sleep { param([int]$Seconds) }
function Invoke-AttachRuntimeAzureJson {
    param([string[]]$Arguments)
    if (($Arguments -join '|').Contains('PRIVATE_APP_FIXTURE')) { throw 'Password leaked to CLI arguments.' }
    $operation = ($Arguments | Select-Object -First 3) -join ' '
    $script:calls.Add($operation)
    $groupId = "/subscriptions/$($settings.SubscriptionId)/resourceGroups/rg-demos-economy"
    $tags = @{ application = 'EventHarbor-PulseExchange'; costProfile = 'economy'; environment = 'economy'; managedBy = 'Bicep' }
    $vmId = "$groupId/providers/Microsoft.Compute/virtualMachines/vm-eh-demos-economy"
    $nicId = "$groupId/providers/Microsoft.Network/networkInterfaces/nic-eh-demos-economy"
    $vnetId = "$groupId/providers/Microsoft.Network/virtualNetworks/vnet-demos-economy"
    switch -Wildcard ($operation) {
        'group show *' {
            if ($script:mode -eq 'foreign-tag') { $tags.application = 'Other' }
            $response = @{ id = $groupId; location = 'westus2'; tags = $tags }
        }
        'vm show *' {
            $tags.application = 'eventharbor'
            $response = @{ id = $vmId; location = 'westus2'; tags = $tags; networkProfile = @{ networkInterfaces = @(@{ id = $nicId }) } }
        }
        'postgres flexible-server show' {
            $response = @{
                id = "$groupId/providers/Microsoft.DBforPostgreSQL/flexibleServers/psql-demos-economy-fixture"
                location = 'westus2'; tags = $tags; fullyQualifiedDomainName = $settings.PostgresHost; version = '17'
                network = @{ publicNetworkAccess = $(if ($script:mode -eq 'public-database') { 'Enabled' } else { 'Disabled' }); delegatedSubnetResourceId = "$vnetId/subnets/snet-postgres" }
            }
        }
        'network nic show' {
            $tags.application = 'eventharbor'
            $subnet = if ($script:mode -eq 'wrong-vnet') { "$vnetId-other/subnets/snet-demo-vms" } else { "$vnetId/subnets/snet-demo-vms" }
            $response = @{ id = $nicId; location = 'westus2'; tags = $tags; enableIPForwarding = $false; ipConfigurations = @(@{ subnet = @{ id = $subnet } }) }
        }
        'rest --method put' {
            $script:bodyPath = $Arguments[[Array]::IndexOf($Arguments, '--body') + 1].Substring(1)
            $body = Get-Content -LiteralPath $script:bodyPath -Raw | ConvertFrom-Json -Depth 20
            Assert-Check ($body.properties.protectedParameters.Count -eq 1 -and $body.properties.protectedParameters[0].name -ceq 'ECONOMY_APP_PASSWORD' -and $body.properties.protectedParameters[0].value -ceq 'PRIVATE_APP_FIXTURE') 'App password must be a protected parameter.'
            Assert-Check (-not (($body.properties.parameters | ConvertTo-Json) -match 'PRIVATE_APP_FIXTURE')) 'Ordinary parameters must not contain credentials.'
            Assert-Check (-not $body.properties.source.script.Contains('PRIVATE_APP_FIXTURE')) 'Inline script must not contain credentials.'
            Assert-Check ($body.properties.asyncExecution -eq $true -and $body.properties.timeoutInSeconds -eq 600) 'Managed command must be bounded and asynchronously inspected.'
            if ($IsWindows) {
                $acl = Get-Acl -LiteralPath (Split-Path -Parent $script:bodyPath)
                Assert-Check $acl.AreAccessRulesProtected 'Temporary secret directory must not inherit broad access.'
            }
            $response = @{ properties = @{ provisioningState = 'Succeeded' } }
        }
        'rest --method get' {
            Assert-Check (($Arguments -join '|').Contains('$expand=instanceView')) 'Execution status must expand instanceView.'
            $script:statusReads++
            $state = if ($script:statusReads -eq 1) { 'Running' } elseif ($script:mode -eq 'failed-state') { 'Failed' } else { 'Succeeded' }
            $view = @{ executionState = $state; exitCode = $(if ($script:mode -eq 'nonzero') { 9 } else { 0 }); output = 'PRIVATE_REMOTE_OUTPUT'; error = 'PRIVATE_REMOTE_ERROR' }
            if ($script:mode -eq 'missing-exit') { $view.Remove('exitCode') }
            $response = @{ properties = @{ provisioningState = 'Succeeded'; instanceView = $view } }
        }
        'rest --method delete' { $response = @{} }
        default { throw 'Unexpected Azure call in the offline test.' }
    }
    return $response | ConvertTo-Json -Depth 20 | ConvertFrom-Json -Depth 20
}

$result = Invoke-AttachRestoredRuntime @settings -DryRun
Assert-Check ($result.Action -eq 'DryRun' -and $script:calls.Count -eq 0) 'DryRun cannot contact Azure.'
$withoutPassword = $settings.Clone()
$withoutPassword.Remove('AppPassword')
$failed = $false
try { $null = Invoke-AttachRestoredRuntime @withoutPassword } catch { $failed = $true }
Assert-Check ($failed -and $script:calls.Count -eq 0) 'Missing retained password must fail before any cloud request.'

foreach ($badInput in @(@{ ResourceGroup = 'rg-eventharbor-prod' }, @{ VmName = 'vm-px-demos-economy' }, @{ PostgresHost = 'old-prod.postgres.database.azure.com' }, @{ AcmeEmail = 'bad$@example.com' })) {
    $badSettings = $settings.Clone()
    foreach ($entry in $badInput.GetEnumerator()) { $badSettings[$entry.Key] = $entry.Value }
    $failed = $false
    try { $null = Invoke-AttachRestoredRuntime @badSettings } catch { $failed = $true }
    Assert-Check ($failed -and $script:calls.Count -eq 0) 'Wrong scope or unsafe input must stop before cloud requests.'
}

foreach ($case in @('foreign-tag', 'public-database', 'wrong-vnet')) {
    $script:mode = $case
    $script:calls.Clear()
    $failed = $false
    try { $null = Invoke-AttachRestoredRuntime @settings } catch { $failed = $true }
    Assert-Check ($failed -and $script:calls -notcontains 'rest --method put') "Unsafe target accepted: $case"
}

foreach ($case in @('success', 'nonzero', 'failed-state', 'missing-exit')) {
    $script:mode = $case
    $script:calls.Clear()
    $script:statusReads = 0
    $script:bodyPath = $null
    $failure = $null
    try { $result = Invoke-AttachRestoredRuntime @settings } catch { $failure = $_.Exception.Message }
    Assert-Check ($script:statusReads -eq 2) 'Provisioning success is not remote script success; wait for terminal instanceView.'
    Assert-Check ($null -ne $script:bodyPath -and -not (Test-Path -LiteralPath $script:bodyPath) -and -not (Test-Path -LiteralPath (Split-Path -Parent $script:bodyPath))) 'Secret request files must be cleaned up after success or failure.'
    if ($case -eq 'success') {
        Assert-Check ($null -eq $failure -and $result.Action -eq 'RestoredRuntimeAttached' -and -not $result.DatabaseModified -and -not $result.ApplicationStarted) 'Verified attach did not complete safely.'
        Assert-Check ($script:calls -contains 'rest --method delete') 'Successful run command must be removed.'
    } else {
        Assert-Check ($null -ne $failure -and $failure -notmatch 'PRIVATE_REMOTE') 'Failed execution must report failure without remote messages.'
        Assert-Check ($script:calls -notcontains 'rest --method delete') 'Retain failed run commands for controlled diagnosis.'
    }
}
Write-Output "ATTACH_RESTORED_RUNTIME_OFFLINE_CHECKS_PASS: $checks checks; no Azure requests or database changes."
