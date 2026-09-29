#requires -Version 7.4
# Offline guards only. The network entry point is never invoked.
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
$repository=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
. (Join-Path $repository 'scripts/azure-economy/common.ps1')
$tokens=$null;$errors=$null
$tree=[Management.Automation.Language.Parser]::ParseFile((Join-Path $repository 'scripts/azure-shared-postgres/prepare-economy-access.ps1'),[ref]$tokens,[ref]$errors)
if ($errors.Count) {throw 'Access controller failed parsing.'}
foreach ($definition in $tree.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]},$false)) {. ([scriptblock]::Create($definition.Extent.Text))}
function Invoke-EconomyAzureJson {throw 'Azure calls are forbidden in this fixture.'}
$subscription=[guid]'83099284-9ad4-4140-b8fe-8388b6d98a98'
$plan=Get-EconomyCopyAccessPlan $subscription
$checks=0
function Assert-Check([bool]$Condition,[string]$Message) {if (-not $Condition) {throw $Message};$script:checks++}
function New-Peer([string]$Id,[string]$Remote) {@{id=$Id;properties=@{remoteVirtualNetwork=@{id=$Remote};allowVirtualNetworkAccess=$true;allowForwardedTraffic=$false;allowGatewayTransit=$false;useRemoteGateways=$false;peeringState='Connected'}}}
function New-AccessFixture([switch]$Ready) {
    $snapshot=@{
        Pulse=@{id=$plan.PulseId;location='eastus2';tags=@{application='PulseExchange';environment='prod';managedBy='Bicep'};properties=@{addressSpace=@{addressPrefixes=@('10.43.0.0/16')};virtualNetworkPeerings=@((New-Peer $plan.OldPulsePeer $plan.EventId))}}
        Event=@{id=$plan.EventId;location='eastus2';tags=@{application='EventHarbor';environment='prod';managedBy='Bicep'};properties=@{addressSpace=@{addressPrefixes=@('10.42.0.0/16')};virtualNetworkPeerings=@((New-Peer $plan.OldEventPeer $plan.PulseId))}}
        Economy=@{id=$plan.EconomyId;location='westus2';tags=@{application='EventHarbor-PulseExchange';environment='economy';managedBy='Bicep';costProfile='economy'};properties=@{addressSpace=@{addressPrefixes=@('10.63.0.0/16')};virtualNetworkPeerings=@()}}
        Zone=@{id=$plan.ZoneId;tags=@{application='EventHarbor-PulseExchange';environment='economy';costProfile='economy'}}
        Links=@();OldLinks=@(@{id=$plan.OldDnsLink;properties=@{virtualNetwork=@{id=$plan.PulseId};registrationEnabled=$false;provisioningState='Succeeded'}})
    }
    if ($Ready) {
        $snapshot.Pulse.properties.virtualNetworkPeerings+=New-Peer $plan.PulsePeer $plan.EconomyId
        $snapshot.Economy.properties.virtualNetworkPeerings+=New-Peer $plan.EconomyPeer $plan.PulseId
        $snapshot.Links=@(@{id=$plan.DnsLink;tags=@{purpose='temporary-economy-postgres-copy';environment='economy'};properties=@{virtualNetwork=@{id=$plan.PulseId};registrationEnabled=$false;provisioningState='Succeeded'}})
    }
    return $snapshot | ConvertTo-Json -Depth 30 | ConvertFrom-Json -Depth 30
}
Assert-EconomyCopyAccess $plan (New-AccessFixture)
Assert-EconomyCopyAccess $plan (New-AccessFixture -Ready) -RequireReady
Assert-Check $true 'Valid initial and connected states must pass.'
Assert-Check ($plan.Deployments.Count -eq 3) 'Expected exactly three scoped modules.'
foreach ($mutate in @(
    {param($s) $s.Economy.properties.addressSpace.addressPrefixes[0]='10.43.0.0/16'},
    {param($s) $s.Economy.location='eastus2'},
    {param($s) $s.Pulse.tags.application='Other'},
    {param($s) $s.Economy.tags.costProfile='other'},
    {param($s) $s.Zone.tags.environment='prod'},
    {param($s) $s.Economy.properties | Add-Member -NotePropertyName dhcpOptions -NotePropertyValue ([pscustomobject]@{dnsServers=@('10.63.0.4')})},
    {param($s) $s.Pulse.properties.virtualNetworkPeerings[0].properties.peeringState='Disconnected'},
    {param($s) $s.Event.properties.virtualNetworkPeerings=@()},
    {param($s) $s.OldLinks=@()},
    {param($s) $s.OldLinks[0].properties.registrationEnabled=$true},
    {param($s) $s.Economy.properties.virtualNetworkPeerings[0].properties.allowForwardedTraffic=$true},
    {param($s) $s.Economy.properties.virtualNetworkPeerings[0].properties.useRemoteGateways=$true},
    {param($s) $s.Economy.properties.virtualNetworkPeerings[0].properties.remoteVirtualNetwork.id='/foreign'},
    {param($s) $s.Economy.properties.virtualNetworkPeerings[0].id+='-other'},
    {param($s) $s.Links[0].properties.registrationEnabled=$true},
    {param($s) $s.Links[0].tags.purpose='other'},
    {param($s) $s.Links[0].id+='-other'},
    {param($s) $s.Links[0].properties.provisioningState='Failed'}
)) {
    $s=New-AccessFixture -Ready;& $mutate $s
    $blocked=$false;try {Assert-EconomyCopyAccess $plan $s -RequireReady} catch {$blocked=$true}
    Assert-Check $blocked "An unsafe network snapshot was accepted: $mutate"
}
$allowed=@($plan.PulsePeer,$plan.EconomyPeer,$plan.DnsLink)+$plan.Deployments
$preview=@{status='Succeeded';changes=@($allowed | ForEach-Object {@{resourceId=$_;changeType='Create'}})} | ConvertTo-Json -Depth 10 | ConvertFrom-Json
Assert-EconomyCopyWhatIf $plan $preview
Assert-Check $true 'Exact allowed create preview should pass.'
foreach ($change in @(@{resourceId=$plan.EconomyId;changeType='Modify'},@{resourceId=$plan.DnsLink;changeType='Delete'},@{resourceId='/foreign';changeType='Create'})) {
    $preview.changes=@([pscustomobject]$change);$blocked=$false
    try {Assert-EconomyCopyWhatIf $plan $preview} catch {$blocked=$true}
    Assert-Check $blocked 'An unapproved what-if mutation was accepted.'
}
$blocked=$false;try {Get-EconomyCopyAccessPlan ([guid]'00000000-0000-4000-8000-000000000001')} catch {$blocked=$true}
Assert-Check $blocked 'A foreign subscription was accepted.'
$blocked=$false;try {Invoke-EconomyCopyAccess $subscription -Apply} catch {$blocked=$true}
Assert-Check $blocked 'Apply without cost acknowledgment did not fail before Azure.'
Write-Host "PASS $checks economy migration network checks; no Azure calls made."
