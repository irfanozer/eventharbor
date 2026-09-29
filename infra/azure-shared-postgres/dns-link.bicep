param pulseVnetId string
param tags object

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: 'eventharbor.postgres.database.azure.com'
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'pulseexchange-shared-postgres'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: pulseVnetId
    }
  }
}

output linkId string = link.id
