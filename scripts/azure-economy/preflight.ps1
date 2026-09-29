#requires -Version 7.4
<#
.SYNOPSIS
Read-only subscription, regional SKU, quota and allowance-competition checks.
.DESCRIPTION
Does not create or modify Azure resources. A nonzero exit means deployment must
stop. Availability is not a billing guarantee or a capacity reservation.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]+$')][string]$Location,
    [ValidatePattern('^rg-[a-z0-9-]+-economy$')][string]$ResourceGroupName = 'rg-demos-economy',
    [string]$NamePrefix = 'demos',
    [ValidateSet('economy')][string]$EnvironmentName = 'economy',
    [ValidateSet('Standard_B1s', 'Standard_B2ats_v2')][string]$EventHarborVmSize = 'Standard_B1s',
    [ValidateSet('Standard_B1s', 'Standard_B2ats_v2')][string]$PulseExchangeVmSize = 'Standard_B2ats_v2',
    [Nullable[datetime]]$FreeAllowanceExpiry,
    [switch]$AcknowledgeSharedAllowances,
    [switch]$AcknowledgeSharedVmHours,
    [switch]$CheckOnly,
    [switch]$AsJson
)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    $arguments = @{
        SubscriptionId = $SubscriptionId; Location = $Location; ResourceGroupName = $ResourceGroupName
        NamePrefix = $NamePrefix; EnvironmentName = $EnvironmentName
        EventHarborVmSize = $EventHarborVmSize; PulseExchangeVmSize = $PulseExchangeVmSize
        FreeAllowanceExpiry = $FreeAllowanceExpiry; AcknowledgeSharedAllowances = $AcknowledgeSharedAllowances
        AcknowledgeSharedVmHours = $AcknowledgeSharedVmHours
    }
    $report = Test-EconomyFoundation @arguments
    if ($AsJson) { $report | ConvertTo-Json -Depth 12 } else { Show-EconomyPreflight $report }
    if (-not $report.Ready) { exit 2 }
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
