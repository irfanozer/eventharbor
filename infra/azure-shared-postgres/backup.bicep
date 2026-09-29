param location string
param tags object

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stpgcopy830992849ad4'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    allowCrossTenantReplication: false
    isHnsEnabled: false
    // The endpoint is public HTTPS, but every blob request needs authorized
    // identity credentials. No paid private endpoint or anonymous access.
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Allow'
    }
    encryption: {
      keySource: 'Microsoft.Storage'
      services: {
        blob: {
          enabled: true
          keyType: 'Account'
        }
      }
    }
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: {
      enabled: true
      days: 7
      allowPermanentDelete: false
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'pulseexchange-backups'
  properties: {
    publicAccess: 'None'
    metadata: {
      purpose: 'pulseexchange-shared-postgres-backup'
    }
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-px-pgcopy-prod'
  location: location
  tags: tags
}

var roleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
resource backupWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: container
  name: guid(container.id, identity.id, roleDefinitionId)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleDefinitionId
  }
}

output storageAccountId string = account.id
output containerId string = container.id
output blobHostname string = '${account.name}.blob.core.windows.net'
output containerUrl string = '${account.properties.primaryEndpoints.blob}${container.name}'
output identityResourceId string = identity.id
output identityClientId string = identity.properties.clientId
