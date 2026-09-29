targetScope = 'resourceGroup'

metadata name = 'Two-demo Azure economy foundation'
metadata description = 'Two independent demo VMs and one shared private PostgreSQL server. Deploy into a separate economy resource group.'

@description('Azure region. Verify both VM SKUs, PostgreSQL 17 and subscription quota before deployment.')
param location string = resourceGroup().location

@minLength(3)
@maxLength(15)
@description('Lowercase resource-name prefix. Use a dedicated resource group, for example rg-demos-economy.')
param namePrefix string = 'demos'

@minLength(2)
@maxLength(10)
param environmentName string = 'economy'

@minLength(1)
@maxLength(32)
@description('Linux administrator username; authentication uses the supplied SSH public key only.')
param adminUsername string = 'demoops'

@minLength(1)
@description('SSH public key, never a private key. Required even when public SSH ingress is disabled.')
param sshPublicKey string

@description('Optional trusted administrator IPv4 CIDR, for example 203.0.113.10/32. Empty omits SSH ingress; use Azure Run Command for deployment.')
param sshAllowedCidr string = ''

@description('EventHarbor x86 VM size. B1s allowance is conditional on subscription eligibility, remaining monthly hours and availability; this template does not guarantee free usage.')
param eventharborVmSize string = 'Standard_B1s'

@description('PulseExchange x86 VM size. B2ats_v2 uses a different allowance from B1s when the subscription offer permits it. Confirm eligibility and remaining hours; do not substitute an ARM SKU.')
param pulseexchangeVmSize string = 'Standard_B2ats_v2'

@description('Enable Secure Boot and vTPM on supported Gen2 VM sizes. Verify Trusted Launch support before overriding the default B-family sizes.')
param trustedLaunchEnabled bool = true

@minLength(1)
@maxLength(63)
@description('PostgreSQL administrator login, reserved for bootstrap and maintenance. Application roles are created separately with separate credentials.')
param postgresAdministratorLogin string = 'portfolio_admin'

@secure()
@minLength(8)
@maxLength(128)
@description('PostgreSQL administrator password supplied at deployment. Never put it in cloud-init, source control or deployment outputs.')
param postgresAdministratorPassword string

@description('Additional tags override matching standard tags.')
param tags object = {}

var suffix = '${toLower(namePrefix)}-${toLower(environmentName)}'
var resourceTags = union({
  application: 'EventHarbor-PulseExchange'
  environment: toLower(environmentName)
  managedBy: 'Bicep'
  workload: 'public-demo'
  costProfile: 'economy'
}, tags)
var virtualNetworkName = 'vnet-${suffix}'
var postgresServerName = take('psql-${suffix}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}', 63)
var postgresPrivateDnsZoneName = '${suffix}.postgres.database.azure.com'
var applications = [
  {
    name: 'eventharbor'
    shortName: 'eh'
    size: eventharborVmSize
    privateIp: '10.63.0.4'
  }
  {
    name: 'pulseexchange'
    shortName: 'px'
    size: pulseexchangeVmSize
    privateIp: '10.63.0.5'
  }
]

// Only non-secret host setup belongs here. The controller deploys application
// scripts into /opt/<application>/runtime and root-only configuration under
// /etc/<application> after bootstrap. No application credentials enter customData.
// Ubuntu's signed archive supplies the Docker engine and Compose V2 plugin.
var cloudInit = '''
#cloud-config
package_update: true
package_upgrade: true
packages:
  - ca-certificates
  - curl
  - jq
  - python3
  - util-linux
  - postgresql-client
  - docker.io
  - docker-compose-v2
write_files:
  - path: /etc/docker/daemon.json
    owner: root:root
    permissions: '0644'
    content: |
      {"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}
runcmd:
  - [install, -d, -o, root, -g, root, -m, '0755', /opt/demo]
  - [install, -d, -o, root, -g, root, -m, '0700', /opt/demo/config]
  - [install, -d, -o, root, -g, root, -m, '0755', /opt/__APPLICATION__/runtime]
  - [install, -d, -o, root, -g, root, -m, '0700', /etc/__APPLICATION__]
  - [systemctl, enable, --now, docker]
  - [systemctl, restart, docker]
  - [docker, compose, version]
'''

var sshRules = empty(sshAllowedCidr) ? [] : [
  {
    name: 'AllowSshFromAdministrator'
    properties: {
      priority: 120
      access: 'Allow'
      direction: 'Inbound'
      protocol: 'Tcp'
      sourcePortRange: '*'
      destinationPortRange: '22'
      sourceAddressPrefix: sshAllowedCidr
      destinationAddressPrefix: '*'
    }
  }
]

resource networkSecurityGroup 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${suffix}'
  location: location
  tags: resourceTags
  properties: {
    securityRules: concat([
      {
        name: 'AllowPublicWeb'
        properties: {
          priority: 100
          access: 'Allow'
          direction: 'Inbound'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRanges: [
            '80'
            '443'
          ]
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
        }
      }
    ], sshRules)
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: virtualNetworkName
  location: location
  tags: resourceTags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.63.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'snet-demo-vms'
        properties: {
          addressPrefix: '10.63.0.0/24'
          defaultOutboundAccess: false
          networkSecurityGroup: {
            id: networkSecurityGroup.id
          }
        }
      }
      {
        name: 'snet-postgres'
        properties: {
          addressPrefix: '10.63.1.0/27'
          delegations: [
            {
              name: 'postgres-flexible-server'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

// Standard static IPv4 addresses are paid resources. The VM allowances do not
// make these addresses free. Two addresses at $0.005/hour are about $7.30 per
// 730-hour month before tax; verify current regional pricing before deployment.
resource publicIps 'Microsoft.Network/publicIPAddresses@2024-05-01' = [for app in applications: {
  name: 'pip-${app.shortName}-${suffix}'
  location: location
  tags: union(resourceTags, { application: app.name })
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    idleTimeoutInMinutes: 4
    dnsSettings: {
      domainNameLabel: '${app.shortName}-${suffix}-${uniqueString(resourceGroup().id, app.name)}'
    }
  }
}]

resource networkInterfaces 'Microsoft.Network/networkInterfaces@2024-05-01' = [for (app, index) in applications: {
  name: 'nic-${app.shortName}-${suffix}'
  location: location
  tags: union(resourceTags, { application: app.name })
  properties: {
    enableAcceleratedNetworking: false
    enableIPForwarding: false
    ipConfigurations: [
      {
        name: 'primary'
        properties: {
          primary: true
          privateIPAllocationMethod: 'Static'
          privateIPAddress: app.privateIp
          privateIPAddressVersion: 'IPv4'
          subnet: {
            id: '${virtualNetwork.id}/subnets/snet-demo-vms'
          }
          publicIPAddress: {
            id: publicIps[index].id
          }
        }
      }
    ]
  }
}]

resource virtualMachines 'Microsoft.Compute/virtualMachines@2024-07-01' = [for (app, index) in applications: {
  name: 'vm-${app.shortName}-${suffix}'
  location: location
  tags: union(resourceTags, { application: app.name })
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    hardwareProfile: {
      vmSize: app.size
    }
    securityProfile: trustedLaunchEnabled ? {
      securityType: 'TrustedLaunch'
      uefiSettings: {
        secureBootEnabled: true
        vTpmEnabled: true
      }
    } : null
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        name: 'osdisk-${app.shortName}-${suffix}'
        createOption: 'FromImage'
        caching: 'ReadWrite'
        deleteOption: 'Delete'
        // A 64 GiB Premium_LRS OS disk occupies the P6 tier. These two disks
        // consume the two-disk allowance only on an eligible subscription.
        diskSizeGB: 64
        managedDisk: {
          storageAccountType: 'Premium_LRS'
        }
      }
    }
    osProfile: {
      computerName: '${app.shortName}-${suffix}'
      adminUsername: adminUsername
      customData: base64(replace(cloudInit, '__APPLICATION__', app.name))
      linuxConfiguration: {
        disablePasswordAuthentication: true
        provisionVMAgent: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
        patchSettings: {
          patchMode: 'ImageDefault'
          assessmentMode: 'ImageDefault'
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: networkInterfaces[index].id
          properties: {
            primary: true
            deleteOption: 'Delete'
          }
        }
      ]
    }
    diagnosticsProfile: {
      bootDiagnostics: {
        enabled: false
      }
    }
  }
}]

resource postgresPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: postgresPrivateDnsZoneName
  location: 'global'
  tags: resourceTags
}

resource postgresPrivateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: postgresPrivateDnsZone
  name: 'link-${virtualNetworkName}'
  location: 'global'
  tags: resourceTags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: postgresServerName
  location: location
  tags: resourceTags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    administratorLogin: postgresAdministratorLogin
    administratorLoginPassword: postgresAdministratorPassword
    version: '17'
    createMode: 'Create'
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    dataEncryption: {
      type: 'SystemManaged'
    }
    network: {
      delegatedSubnetResourceId: '${virtualNetwork.id}/subnets/snet-postgres'
      privateDnsZoneArmResourceId: postgresPrivateDnsZone.id
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      autoGrow: 'Disabled'
      storageSizeGB: 32
    }
  }
  dependsOn: [
    postgresPrivateDnsVnetLink
  ]
}

resource databases 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = [for app in applications: {
  parent: postgresServer
  name: app.name
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}]

output location string = location
output virtualNetworkName string = virtualNetwork.name
output virtualNetworkId string = virtualNetwork.id
output vmSubnetId string = '${virtualNetwork.id}/subnets/snet-demo-vms'
output postgresSubnetId string = '${virtualNetwork.id}/subnets/snet-postgres'
output eventharborVmName string = virtualMachines[0].name
output eventharborVmId string = virtualMachines[0].id
output eventharborVmPrincipalId string = virtualMachines[0].identity.principalId
output eventharborVmPrivateIp string = applications[0].privateIp
output eventharborVmPublicIp string = publicIps[0].properties.ipAddress
output eventharborVmPublicFqdn string = publicIps[0].properties.dnsSettings.fqdn
output pulseexchangeVmName string = virtualMachines[1].name
output pulseexchangeVmId string = virtualMachines[1].id
output pulseexchangeVmPrincipalId string = virtualMachines[1].identity.principalId
output pulseexchangeVmPrivateIp string = applications[1].privateIp
output pulseexchangeVmPublicIp string = publicIps[1].properties.ipAddress
output pulseexchangeVmPublicFqdn string = publicIps[1].properties.dnsSettings.fqdn
output postgresServerName string = postgresServer.name
output postgresServerFqdn string = postgresServer.properties.fullyQualifiedDomainName
output postgresPrivateDnsZoneName string = postgresPrivateDnsZone.name
output eventharborDatabaseName string = databases[0].name
output pulseexchangeDatabaseName string = databases[1].name
output postgresAdministratorLogin string = postgresAdministratorLogin
