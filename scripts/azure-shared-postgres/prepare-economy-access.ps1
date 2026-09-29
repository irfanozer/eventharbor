#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [switch]$Apply,
    [switch]$ConfirmCosts
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../azure-economy/common.ps1')

function Get-EconomyCopyAccessPlan([guid]$SubscriptionId) {
    if ("$SubscriptionId" -cne '83099284-9ad4-4140-b8fe-8388b6d98a98') { throw 'Only the reviewed subscription is supported.' }
    $prefix="/subscriptions/$SubscriptionId/resourceGroups/"
    $pulse=$prefix+'rg-pulseexchange-prod/providers/Microsoft.Network/virtualNetworks/vnet-pulseexchange-prod'
    $economy=$prefix+'rg-demos-economy/providers/Microsoft.Network/virtualNetworks/vnet-demos-economy'
    $event=$prefix+'rg-eventharbor-prod/providers/Microsoft.Network/virtualNetworks/vnet-eventharbor-prod'
    $zone=$prefix+'rg-demos-economy/providers/Microsoft.Network/privateDnsZones/demos-economy.postgres.database.azure.com'
    $oldZone=$prefix+'rg-eventharbor-prod/providers/Microsoft.Network/privateDnsZones/eventharbor.postgres.database.azure.com'
    return @{
        Subscription="$SubscriptionId";PulseId=$pulse;EconomyId=$economy;EventId=$event;ZoneId=$zone;OldZoneId=$oldZone
        PulsePeer="$pulse/virtualNetworkPeerings/economy-copy-to-demos"
        EconomyPeer="$economy/virtualNetworkPeerings/economy-copy-to-pulseexchange"
        DnsLink="$zone/virtualNetworkLinks/pulseexchange-economy-copy"
        OldPulsePeer="$pulse/virtualNetworkPeerings/shared-postgres-to-eventharbor"
        OldEventPeer="$event/virtualNetworkPeerings/shared-postgres-to-pulseexchange"
        OldDnsLink="$oldZone/virtualNetworkLinks/pulseexchange-shared-postgres"
        Deployments=@(
            $prefix+'rg-pulseexchange-prod/providers/Microsoft.Resources/deployments/economy-copy-px-peering'
            $prefix+'rg-demos-economy/providers/Microsoft.Resources/deployments/economy-copy-new-peering'
            $prefix+'rg-demos-economy/providers/Microsoft.Resources/deployments/economy-copy-dns'
        )
    }
}
function Get-EconomyCopyResource([string]$Id,[string]$Version='2024-05-01') {
    Invoke-EconomyAzureJson -Sensitive -Arguments @('rest','--method','get','--url',"https://management.azure.com${Id}?api-version=$Version")
}
function Get-EconomyCopyLinks([string]$Id) {
    $result=Get-EconomyCopyResource "$Id/virtualNetworkLinks" '2024-06-01'
    if (-not $result.PSObject.Properties['value'] -or (Get-EconomyValue $result 'nextLink')) { throw 'Complete DNS-link inventory is required.' }
    return @($result.value)
}
function Get-EconomyCopyAccessSnapshot($Plan) {
    $account=Invoke-EconomyAzureJson -Sensitive -Arguments @('account','show','--subscription',$Plan.Subscription,'--query','{id:id,state:state}')
    if ($account.id -ine $Plan.Subscription -or $account.state -cne 'Enabled') { throw 'Reviewed subscription is not enabled.' }
    return @{
        Pulse=Get-EconomyCopyResource $Plan.PulseId
        Economy=Get-EconomyCopyResource $Plan.EconomyId
        Event=Get-EconomyCopyResource $Plan.EventId
        Zone=Get-EconomyCopyResource $Plan.ZoneId '2024-06-01'
        Links=@(Get-EconomyCopyLinks $Plan.ZoneId)
        OldLinks=@(Get-EconomyCopyLinks $Plan.OldZoneId)
    }
}
function Assert-EconomyCopyPeering($Vnet,[string]$PeerId,[string]$RemoteId,[switch]$RequireReady) {
    $found=@()
    foreach ($peer in @(Get-EconomyValue $Vnet.properties 'virtualNetworkPeerings' @())) {
        if ($peer.id -ine $PeerId) {
            if ($peer.properties.remoteVirtualNetwork.id -ieq $RemoteId) { throw 'Another peering already targets this network; do not duplicate it.' }
            continue
        }
        $found+=,$peer
        if ($peer.properties.remoteVirtualNetwork.id -ine $RemoteId -or
            $peer.properties.allowVirtualNetworkAccess -ne $true -or $peer.properties.allowForwardedTraffic -ne $false -or
            $peer.properties.allowGatewayTransit -ne $false -or $peer.properties.useRemoteGateways -ne $false) {
            throw 'Named peering differs from direct-only reviewed access.'
        }
        if ($RequireReady -and $peer.properties.peeringState -cne 'Connected') { throw 'Required peering is not Connected.' }
    }
    if ($found.Count -gt 1 -or ($RequireReady -and $found.Count -ne 1)) { throw 'Required peering is missing or ambiguous.' }
}
function Assert-EconomyCopyAccess($Plan,$Snapshot,[switch]$RequireReady) {
    foreach ($pair in @(
        @{Value=$Snapshot.Pulse;Id=$Plan.PulseId;Cidr='10.43.0.0/16';Region='eastus2';App='PulseExchange';Environment='prod'},
        @{Value=$Snapshot.Event;Id=$Plan.EventId;Cidr='10.42.0.0/16';Region='eastus2';App='EventHarbor';Environment='prod'},
        @{Value=$Snapshot.Economy;Id=$Plan.EconomyId;Cidr='10.63.0.0/16';Region='westus2';App='EventHarbor-PulseExchange';Environment='economy'}
    )) {
        $network=$pair.Value
        if ($network.id -ine $pair.Id -or $network.location.Replace(' ','').ToLowerInvariant() -cne $pair.Region -or
            @($network.properties.addressSpace.addressPrefixes).Count -ne 1 -or $network.properties.addressSpace.addressPrefixes[0] -cne $pair.Cidr -or
            $network.tags.application -cne $pair.App -or $network.tags.environment -cne $pair.Environment -or $network.tags.managedBy -cne 'Bicep') {
            throw 'VNet identity, tags, region or exact address range differs from the reviewed deployment.'
        }
        if (@(Get-EconomyValue (Get-EconomyValue $network.properties 'dhcpOptions') 'dnsServers' @()).Count) { throw 'Custom DNS requires a separately reviewed forwarding design.' }
    }
    if ($Snapshot.Economy.tags.costProfile -cne 'economy' -or $Snapshot.Zone.id -ine $Plan.ZoneId -or
        $Snapshot.Zone.tags.costProfile -cne 'economy' -or $Snapshot.Zone.tags.environment -cne 'economy' -or
        $Snapshot.Zone.tags.application -cne 'EventHarbor-PulseExchange') { throw 'New foundation or private DNS ownership mismatch.' }
    # Peering is not transitive. The copy job uses its direct existing EH access,
    # plus its new direct economy access; the economy VNet needs no EH peering.
    Assert-EconomyCopyPeering $Snapshot.Pulse $Plan.OldPulsePeer $Plan.EventId -RequireReady
    Assert-EconomyCopyPeering $Snapshot.Event $Plan.OldEventPeer $Plan.PulseId -RequireReady
    $oldLink=@($Snapshot.OldLinks | Where-Object id -IEQ $Plan.OldDnsLink)
    if ($oldLink.Count -ne 1 -or $oldLink[0].properties.virtualNetwork.id -ine $Plan.PulseId -or
        $oldLink[0].properties.registrationEnabled -ne $false -or $oldLink[0].properties.provisioningState -cne 'Succeeded') {
        throw 'Existing copy-job DNS access to the EventHarbor source is not ready.'
    }
    Assert-EconomyCopyPeering $Snapshot.Pulse $Plan.PulsePeer $Plan.EconomyId -RequireReady:$RequireReady
    Assert-EconomyCopyPeering $Snapshot.Economy $Plan.EconomyPeer $Plan.PulseId -RequireReady:$RequireReady
    $found=@()
    foreach ($link in $Snapshot.Links) {
        if ($link.id -ine $Plan.DnsLink) {
            if ($link.properties.virtualNetwork.id -ieq $Plan.PulseId) { throw 'A differently named DNS link already targets the copy-job VNet.' }
            continue
        }
        $found+=,$link
        if ($link.properties.virtualNetwork.id -ine $Plan.PulseId -or $link.properties.registrationEnabled -ne $false -or
            $link.tags.purpose -cne 'temporary-economy-postgres-copy' -or $link.tags.environment -cne 'economy') { throw 'Existing temporary DNS link differs from the reviewed configuration.' }
        if ($RequireReady -and $link.properties.provisioningState -cne 'Succeeded') { throw 'Temporary private DNS link is not ready.' }
    }
    if ($found.Count -gt 1 -or ($RequireReady -and $found.Count -ne 1)) { throw 'Temporary private DNS link is missing or ambiguous.' }
}
function Assert-EconomyCopyWhatIf($Plan,$Preview) {
    if ($Preview.status -cne 'Succeeded') { throw 'Network what-if did not succeed.' }
    $allowed=@($Plan.PulsePeer,$Plan.EconomyPeer,$Plan.DnsLink)+$Plan.Deployments
    foreach ($change in $Preview.changes) {
        if ($change.changeType -in @('Ignore','NoChange')) { continue }
        if ($change.resourceId -notin $allowed -or $change.changeType -notin @('Create','Modify','Deploy')) {
            throw 'What-if includes an unapproved resource change or deletion. No deployment is allowed.'
        }
    }
}
function Invoke-EconomyCopyAccess([guid]$SubscriptionId,[switch]$Apply,[switch]$ConfirmCosts) {
    if ($Apply -and -not $ConfirmCosts) { throw 'Global peering and cross-region transfer can be billed; Apply requires -ConfirmCosts.' }
    $plan=Get-EconomyCopyAccessPlan $SubscriptionId
    $snapshot=Get-EconomyCopyAccessSnapshot $plan
    Assert-EconomyCopyAccess $plan $snapshot
    if ($Apply) {
        $template=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../infra/azure-shared-postgres/economy-access.bicep'))
        $arguments=@('--subscription',"$SubscriptionId",'--location','eastus2','--name','economy-copy-access','--template-file',$template)
        $preview=Invoke-EconomyAzureJson -Sensitive -Arguments (@('deployment','sub','what-if')+$arguments+@('--no-pretty-print','--result-format','ResourceIdOnly'))
        Assert-EconomyCopyWhatIf $plan $preview
        $deployment=Invoke-EconomyAzureJson -Sensitive -Arguments (@('deployment','sub','create')+$arguments)
        if ($deployment.properties.provisioningState -cne 'Succeeded') { throw 'Network deployment is not confirmed. Inspect economy-copy-access before retrying.' }
        Assert-EconomyCopyAccess $plan (Get-EconomyCopyAccessSnapshot $plan) -RequireReady
    }
    return @{mode=$(if ($Apply) {'AccessReady'} else {'ReadOnlyChecksPassed'});pulsePeeringId=$plan.PulsePeer;
        economyPeeringId=$plan.EconomyPeer;privateDnsLinkId=$plan.DnsLink;databaseChanged=$false;
        note='Temporary direct global peering has transfer charges. Remove only after independently verified migration and separately approved cleanup.'}
}

Invoke-EconomyCopyAccess @PSBoundParameters | ConvertTo-Json -Depth 8
