#requires -Version 7.0
<#
.SYNOPSIS
Offline regression checks for economy preflight and the native CLI wrapper.
.DESCRIPTION
Mocks all Azure calls. The native-wrapper checks launch this same file in a
fixture mode that emits controlled JSON and stderr; no Azure process is started.
#>
[CmdletBinding()]
param(
    [ValidateSet('tests', 'warning', 'failure', 'invalid-json')][string]$ProcessFixture = 'tests',
    [Parameter(ValueFromRemainingArguments)][string[]]$IgnoredArguments
)
$ErrorActionPreference = 'Stop'
switch ($ProcessFixture) {
    'warning' {
        [Console]::Error.WriteLine('Fixture warning belongs on stderr.')
        [Console]::Out.WriteLine('{"ok":true,"source":"fixture"}')
        exit 0
    }
    'failure' {
        [Console]::Error.WriteLine('pretend-sensitive-request-value')
        exit 23
    }
    'invalid-json' {
        [Console]::Out.WriteLine('This is not JSON.')
        exit 0
    }
}

. (Join-Path $PSScriptRoot '../../scripts/azure-economy/common.ps1')
$nativeWrapper = ${function:Invoke-EconomyAzureJson}
$script:scenario = 'blocked'
$script:seenOperations = [System.Collections.Generic.List[string]]::new()

function Invoke-EconomyAzureJson {
    param([string[]]$Arguments, [switch]$Sensitive)
    $operation = ($Arguments | Select-Object -First 2) -join ' '
    $script:seenOperations.Add($operation)
    switch ($operation) {
        'account show' {
            return [pscustomobject]@{
                id = '00000000-0000-4000-8000-000000000001'
                name = 'offline fixture'; state = 'Enabled'
            }
        }
        'group exists' { return $script:scenario -eq 'foreign-group' }
        'group show' { return [pscustomobject]@{ location = 'eastus2'; tags = [pscustomobject]@{ application = 'other' } } }
        'vm list-skus' {
            foreach ($spec in @(@('Standard_B1s', 'standardBSFamily', 1), @('Standard_B2ats_v2', 'standardBasv2Family', 2))) {
                $restrictions = @()
                if ($script:scenario -eq 'blocked') {
                    $restrictions = @([pscustomobject]@{
                        type = 'Location'; values = @('eastus2'); reasonCode = 'NotAvailableForSubscription'
                    })
                }
                if ($script:scenario -eq 'zone-only') {
                    $restrictions = @([pscustomobject]@{
                        type = 'Zone'; values = @('eastus2'); reasonCode = 'NotAvailableForSubscription'
                    })
                }
                [pscustomobject]@{
                    name = $spec[0]; family = $spec[1]; restrictions = $restrictions
                    capabilities = @(
                        [pscustomobject]@{ name = 'CpuArchitectureType'; value = 'x64' },
                        [pscustomobject]@{ name = 'PremiumIO'; value = 'True' },
                        [pscustomobject]@{ name = 'HyperVGenerations'; value = 'V1,V2' },
                        [pscustomobject]@{ name = 'vCPUs'; value = [string]$spec[2] }
                    )
                }
            }
            return
        }
        'vm list-usage' {
            foreach ($family in @('cores', 'standardBSFamily', 'standardBasv2Family')) {
                [pscustomobject]@{
                    name = [pscustomobject]@{ value = $family }; currentValue = 0
                    limit = $(if ($script:scenario -eq 'blocked' -and $family -eq 'standardBasv2Family') { 0 } else { 10 })
                }
            }
            return
        }
        'vm list' {
            if ($script:scenario -eq 'competition') {
                return [pscustomobject]@{
                    id = '/subscriptions/fixture/resourceGroups/other/providers/Microsoft.Compute/virtualMachines/other'
                    name = 'other'; resourceGroup = 'other'; location = 'westus2'
                    hardwareProfile = [pscustomobject]@{ vmSize = 'Standard_B1s' }
                }
            }
            return @()
        }
        'resource list' {
            if ($Arguments -notcontains 'Microsoft.Compute/disks') { throw 'The generic inventory must explicitly select managed disks.' }
            if ($script:scenario -eq 'disk-competition') {
                return '/subscriptions/fixture/resourceGroups/other/providers/Microsoft.Compute/disks/other'
            }
            return @()
        }
        'disk show' {
            if ($Arguments -notcontains '--ids') { throw 'A disk detail query must use its explicit resource ID.' }
            return [pscustomobject]@{
                name = 'other'; resourceGroup = 'other'; managedBy = $null; diskSizeGb = 64
                sku = [pscustomobject]@{ name = 'Premium_LRS' }
            }
        }
        'postgres flexible-server' {
            if ($Arguments[2] -eq 'list') { return @() }
            return [pscustomobject]@{
                restricted = $null
                supportedServerVersions = @([pscustomobject]@{ name = '17' })
                supportedServerEditions = @([pscustomobject]@{
                    name = 'Burstable'
                    supportedServerSkus = @([pscustomobject]@{ name = 'Standard_B1ms' })
                    supportedStorageEditions = @([pscustomobject]@{
                        name = 'ManagedDisk'
                        supportedStorageMb = @([pscustomobject]@{ storageSizeMb = 32768 })
                    })
                })
            }
        }
        default { throw "Unexpected CLI arguments in offline fixture: $operation" }
    }
}

$checks = @{
    SubscriptionId = [guid]'00000000-0000-4000-8000-000000000001'
    Location = 'eastus2'
}
foreach ($scenario in @('blocked', 'valid', 'zone-only', 'competition', 'disk-competition', 'foreign-group')) {
    $script:scenario = $scenario
    $report = Test-EconomyFoundation @checks
    $expected = $scenario -in @('valid', 'zone-only')
    if ($report.Ready -ne $expected) {
        throw "Unexpected readiness for ${scenario}: $($report.Blockers -join ', ')"
    }
    if ($scenario -eq 'blocked') {
        if (@($report.Blockers | Where-Object { $_ -match 'NotAvailableForSubscription' }).Count -ne 2) {
            throw 'Both unavailable SKU guards must fail.'
        }
        if (@($report.Blockers | Where-Object { $_ -match 'Insufficient standardBasv2Family' }).Count -ne 1) {
            throw 'Zero family quota was not caught.'
        }
        $acknowledged = Test-EconomyFoundation @checks -AcknowledgeSharedAllowances
        if ($acknowledged.Ready) { throw 'A billing acknowledgment must never bypass SKU restrictions or quota.' }
    }
    if ($scenario -in @('competition', 'disk-competition')) {
        $acknowledged = Test-EconomyFoundation @checks -AcknowledgeSharedAllowances
        if (-not $acknowledged.Ready) { throw 'The explicit shared-allowance acknowledgment did not release its own guard.' }
    }
    Write-Output "Passed: $scenario"
}

$script:scenario = 'valid'
$sharedB2ats = Test-EconomyFoundation @checks -EventHarborVmSize 'Standard_B2ats_v2'
if ($sharedB2ats.Ready) { throw 'Two B2ats VMs must require their explicit shared-hour acknowledgment.' }
$sharedB2ats = Test-EconomyFoundation @checks -EventHarborVmSize 'Standard_B2ats_v2' -AcknowledgeSharedVmHours
if (-not $sharedB2ats.Ready) { throw 'Two explicitly acknowledged B2ats VMs should pass when capacity and quota are sufficient.' }
$basQuota = @($sharedB2ats.QuotaChecks | Where-Object { $_.Family -eq 'standardBasv2Family' })
if ($basQuota.Count -ne 1 -or $basQuota[0].Required -ne 4) { throw 'Two B2ats VMs require four Basv2 family quota cores.' }
if (@($sharedB2ats.Warnings | Where-Object { $_ -match 'ONE 750-hour' }).Count -ne 1) { throw 'The shared-hour paid-compute warning is required.' }
$sharedB1s = Test-EconomyFoundation @checks -PulseExchangeVmSize 'Standard_B1s' -AcknowledgeSharedVmHours
if ($sharedB1s.Ready) { throw 'The shared-hour opt-in must not silently allow repeated B1s VMs.' }
Write-Output 'Passed: explicit B2ats shared-hour opt-in and four-core quota demand'
$expired = Test-EconomyFoundation @checks -FreeAllowanceExpiry '2000-01-01'
if ($expired.Ready) { throw 'Expired allowance passed preflight.' }
Write-Output 'Passed: expired allowance'
try {
    $null = Test-EconomyFoundation @checks -ResourceGroupName 'rg-eventharbor-prod'
    throw 'Production resource group was not blocked.'
} catch {
    if ($_ -notmatch 'Existing production resource groups are forbidden|does not match') { throw }
}
Write-Output 'Passed: production resource group protection'
if ($script:seenOperations[0] -ne 'account show' -or $script:seenOperations -contains 'System.Collections.Hashtable') {
    throw 'Azure arguments lost their explicit array shape.'
}
Write-Output 'Passed: explicit native argument arrays'

# Substitute only command discovery. The real wrapper still launches a native
# subprocess and reads its two streams, but that process is PowerShell running
# this file's fixture mode. No Azure executable or account is used.
$script:fixtureShell = (Get-Process -Id $PID).Path
function Get-Command {
    param([string]$Name, $ErrorAction)
    if ($Name -ne 'az') { throw "Unexpected command discovery in native fixture: $Name" }
    return [pscustomobject]@{ Source = $script:fixtureShell }
}
$fixtureArguments = @('-NoProfile', '-NonInteractive', '-File', $PSCommandPath, '-ProcessFixture')
$json = & $nativeWrapper -Arguments ($fixtureArguments + @('warning'))
if (-not $json.ok -or $json.source -ne 'fixture') { throw 'Stderr warning polluted the successful JSON response.' }
Write-Output 'Passed: JSON stdout is separate from warning stderr'
try {
    $null = & $nativeWrapper -Sensitive -Arguments ($fixtureArguments + @('failure'))
    throw 'A nonzero sensitive subprocess exit was not caught.'
} catch {
    if ($_ -notmatch 'exit code 23' -or $_ -match 'pretend-sensitive-request-value') {
        throw 'Sensitive native failures must preserve the exit code and suppress request diagnostics.'
    }
}
Write-Output 'Passed: sensitive native failure redaction'
try {
    $null = & $nativeWrapper -Arguments ($fixtureArguments + @('invalid-json'))
    throw 'Invalid JSON was accepted.'
} catch {
    if ($_ -notmatch 'returned invalid JSON') { throw }
}
Write-Output 'Passed: malformed JSON fails closed'
Write-Output 'ECONOMY_PREFLIGHT_OFFLINE_CHECKS_PASS'
