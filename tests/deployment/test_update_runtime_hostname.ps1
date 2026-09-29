#Requires -Version 7.4
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$path = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../scripts/azure-economy/update-runtime-hostname.ps1'))
$tokens = $null; $problems = $null
$tree = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$problems)
if ($problems.Count) { throw 'Hostname update script has syntax errors.' }
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
    Project = 'eventharbor'; VmName = 'vm-eh-demos-economy'
    ExpectedHostname = 'eh-fixture.westus2.cloudapp.azure.com'; PublicHostname = 'eventharbor.irfanburakozer.com'
}
$script:calls = [System.Collections.Generic.List[string]]::new()
$script:mode = 'success'; $script:statusReads = 0; $script:bodyPath = $null
function Start-Sleep { param([int]$Seconds) }
function Invoke-HostnameAzure {
    param([string[]]$Arguments)
    $operation = ($Arguments | Select-Object -First 3) -join ' '
    $script:calls.Add($operation)
    $groupId = "/subscriptions/$($settings.SubscriptionId)/resourceGroups/rg-demos-economy"
    $tags = @{ application = 'eventharbor'; environment = 'economy'; costProfile = 'economy'; managedBy = 'Bicep' }
    switch -Wildcard ($operation) {
        'vm show *' {
            if ($script:mode -eq 'foreign-vm') { $tags.application = 'pulseexchange' }
            $result = @{ id = "$groupId/providers/Microsoft.Compute/virtualMachines/vm-eh-demos-economy"; location = 'westus2'; tags = $tags }
        }
        'network public-ip show' {
            $fqdn = if ($script:mode -eq 'wrong-preview') { 'other.westus2.cloudapp.azure.com' } else { $settings.ExpectedHostname }
            $result = @{ id = "$groupId/providers/Microsoft.Network/publicIPAddresses/pip-eh-demos-economy"; location = 'westus2'; tags = $tags; dnsSettings = @{ fqdn = $fqdn } }
        }
        'rest --method put' {
            $script:bodyPath = $Arguments[[Array]::IndexOf($Arguments, '--body') + 1].Substring(1)
            $body = Get-Content -LiteralPath $script:bodyPath -Raw | ConvertFrom-Json -Depth 20
            $names = @($body.properties.parameters.name | Sort-Object)
            Assert-Check (($names -join ',') -ceq 'ECONOMY_EXPECTED_HOSTNAME,ECONOMY_PROJECT,ECONOMY_PUBLIC_HOSTNAME') 'Only non-secret hostname parameters may be sent.'
            Assert-Check ($body.properties.timeoutInSeconds -eq 120 -and $body.properties.asyncExecution -eq $true) 'Hostname update must be bounded and inspected asynchronously.'
            $result = @{ properties = @{ provisioningState = 'Succeeded' } }
        }
        'rest --method get' {
            Assert-Check (($Arguments -join '|').Contains('$expand=instanceView')) 'Real remote execution status is required.'
            $script:statusReads++
            $state = if ($script:statusReads -eq 1) { 'Running' } elseif ($script:mode -eq 'failed-state') { 'Failed' } else { 'Succeeded' }
            $view = @{ executionState = $state; exitCode = $(if ($script:mode -eq 'nonzero') { 1 } else { 0 }); output = 'PRIVATE_RUNTIME_CONTENT'; error = 'PRIVATE_RUNTIME_CONTENT' }
            if ($script:mode -eq 'missing-exit') { $view.Remove('exitCode') }
            $result = @{ properties = @{ provisioningState = 'Succeeded'; instanceView = $view } }
        }
        'rest --method delete' { $result = @{} }
        default { throw 'Unexpected Azure call in hostname update test.' }
    }
    return $result | ConvertTo-Json -Depth 20 | ConvertFrom-Json -Depth 20
}

$result = Invoke-RuntimeHostnameUpdate @settings
Assert-Check ($result.Action -ceq 'DryRun' -and $script:calls.Count -eq 0) 'Default mode must not call Azure.'
foreach ($invalid in @(@{ VmName = 'vm-px-demos-economy' }, @{ ExpectedHostname = 'not-the-preview.example.com' }, @{ PublicHostname = 'another.irfanburakozer.com' }, @{ ResourceGroup = 'rg-eventharbor-prod' })) {
    $parameters = $settings.Clone()
    foreach ($entry in $invalid.GetEnumerator()) { $parameters[$entry.Key] = $entry.Value }
    $failed = $false
    try { $null = Invoke-RuntimeHostnameUpdate @parameters -Apply } catch { $failed = $true }
    Assert-Check ($failed -and $script:calls.Count -eq 0) 'Invalid input must fail before cloud requests.'
}
foreach ($case in @('foreign-vm', 'wrong-preview')) {
    $script:mode = $case; $script:calls.Clear()
    $failed = $false
    try { $null = Invoke-RuntimeHostnameUpdate @settings -Apply } catch { $failed = $true }
    Assert-Check ($failed -and $script:calls -notcontains 'rest --method put') 'Foreign target or wrong observed preview must fail before mutation.'
}
foreach ($case in @('success', 'nonzero', 'failed-state', 'missing-exit')) {
    $script:mode = $case; $script:calls.Clear(); $script:statusReads = 0; $script:bodyPath = $null
    $failure = $null
    try { $result = Invoke-RuntimeHostnameUpdate @settings -Apply } catch { $failure = $_.Exception.Message }
    Assert-Check ($script:statusReads -eq 2) 'ARM provisioning success is not script execution success.'
    Assert-Check ($null -ne $script:bodyPath -and -not (Test-Path -LiteralPath (Split-Path -Parent $script:bodyPath))) 'Private local request directory must be removed.'
    if ($case -eq 'success') {
        Assert-Check ($null -eq $failure -and $result.Action -ceq 'RuntimeHostnameUpdated' -and -not $result.Restarted -and -not $result.DatabaseChanged -and -not $result.DnsChanged) 'Success must describe only the hostname update.'
        Assert-Check ($script:calls -contains 'rest --method delete') 'Successful managed command must be cleaned up.'
    } else {
        Assert-Check ($null -ne $failure -and $failure -notmatch 'PRIVATE_RUNTIME_CONTENT') 'Remote output must not appear in failure text.'
        Assert-Check ($script:calls -notcontains 'rest --method delete') 'Retain failed commands for private diagnosis.'
    }
}
Write-Output "RUNTIME_HOSTNAME_OFFLINE_CHECKS_PASS: $checks checks; no Azure calls or runtime changes."
