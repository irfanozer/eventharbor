resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: 'demos-economy.postgres.database.azure.com'
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'pulseexchange-economy-copy'
  location: 'global'
  tags: {
    application: 'EventHarbor-PulseExchange'
    environment: 'economy'
    costProfile: 'economy'
    purpose: 'temporary-economy-postgres-copy'
    managedBy: 'Bicep'
  }
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: resourceId(subscription().subscriptionId, 'rg-pulseexchange-prod', 'Microsoft.Network/virtualNetworks', 'vnet-pulseexchange-prod')
    }
  }
}

output linkId string = link.id
