targetScope = 'subscription'

metadata description = 'Temporary direct global peering and private DNS access from the existing copy-job VNet to the new economy PostgreSQL server. Does not redeploy either VNet or database.'

var pulseVnetId = resourceId(subscription().subscriptionId, 'rg-pulseexchange-prod', 'Microsoft.Network/virtualNetworks', 'vnet-pulseexchange-prod')
var economyVnetId = resourceId(subscription().subscriptionId, 'rg-demos-economy', 'Microsoft.Network/virtualNetworks', 'vnet-demos-economy')

module pulsePeering './peering.bicep' = {
  name: 'economy-copy-px-peering'
  scope: resourceGroup('rg-pulseexchange-prod')
  params: {
    localVnetName: 'vnet-pulseexchange-prod'
    remoteVnetId: economyVnetId
    peeringName: 'economy-copy-to-demos'
  }
}

module economyPeering './peering.bicep' = {
  name: 'economy-copy-new-peering'
  scope: resourceGroup('rg-demos-economy')
  params: {
    localVnetName: 'vnet-demos-economy'
    remoteVnetId: pulseVnetId
    peeringName: 'economy-copy-to-pulseexchange'
  }
}

module dnsLink './economy-dns-link.bicep' = {
  name: 'economy-copy-dns'
  scope: resourceGroup('rg-demos-economy')
}

output pulsePeeringId string = pulsePeering.outputs.peeringId
output economyPeeringId string = economyPeering.outputs.peeringId
output privateDnsLinkId string = dnsLink.outputs.linkId
