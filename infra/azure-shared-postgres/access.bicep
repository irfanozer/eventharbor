targetScope = 'subscription'

metadata name = 'Existing demo database migration access'
metadata description = 'Add private network reachability and an authenticated backup container. No database, application, or existing subnet is redeployed.'

var location = 'eastus2'
var tags = {
  application: 'EventHarbor-PulseExchange'
  environment: 'prod'
  managedBy: 'Bicep'
  purpose: 'shared-postgres-migration'
}
var eventVnetId = resourceId(subscription().subscriptionId, 'rg-eventharbor-prod', 'Microsoft.Network/virtualNetworks', 'vnet-eventharbor-prod')
var pulseVnetId = resourceId(subscription().subscriptionId, 'rg-pulseexchange-prod', 'Microsoft.Network/virtualNetworks', 'vnet-pulseexchange-prod')

resource backupGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-demos-db-migration'
  location: location
  tags: tags
}

module eventPeering './peering.bicep' = {
  name: 'pg-copy-eh-peering'
  scope: resourceGroup('rg-eventharbor-prod')
  params: {
    localVnetName: 'vnet-eventharbor-prod'
    remoteVnetId: pulseVnetId
    peeringName: 'shared-postgres-to-pulseexchange'
  }
}

module pulsePeering './peering.bicep' = {
  name: 'pg-copy-px-peering'
  scope: resourceGroup('rg-pulseexchange-prod')
  params: {
    localVnetName: 'vnet-pulseexchange-prod'
    remoteVnetId: eventVnetId
    peeringName: 'shared-postgres-to-eventharbor'
  }
}

module dnsLink './dns-link.bicep' = {
  name: 'pg-copy-dns'
  scope: resourceGroup('rg-eventharbor-prod')
  params: {
    pulseVnetId: pulseVnetId
    tags: tags
  }
}

module backup './backup.bicep' = {
  name: 'pg-copy-backup'
  scope: backupGroup
  params: {
    location: location
    tags: tags
  }
}

output backupStorageAccountId string = backup.outputs.storageAccountId
output backupContainerId string = backup.outputs.containerId
output backupBlobHostname string = backup.outputs.blobHostname
output backupContainerUrl string = backup.outputs.containerUrl
output jobIdentityResourceId string = backup.outputs.identityResourceId
output jobIdentityClientId string = backup.outputs.identityClientId
output eventPeeringId string = eventPeering.outputs.peeringId
output pulsePeeringId string = pulsePeering.outputs.peeringId
output privateDnsLinkId string = dnsLink.outputs.linkId
