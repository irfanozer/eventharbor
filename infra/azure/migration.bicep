targetScope = 'resourceGroup'

metadata name = 'EventHarbor database migration job'
metadata description = 'A manually triggered Container Apps job that applies Alembic migrations before application revisions are released.'

@description('Azure region used by the existing Container Apps environment.')
param location string = resourceGroup().location

@minLength(3)
@maxLength(15)
@description('Short lowercase prefix used in resource names.')
param namePrefix string = 'eventharbor'

@minLength(2)
@maxLength(7)
@description('Deployment environment suffix, for example prod or staging.')
param environmentName string = 'prod'

@description('Name of the Container Apps environment created by foundation.bicep.')
param containerAppsEnvironmentName string = 'cae-${toLower(namePrefix)}-${toLower(environmentName)}'

@minLength(1)
@description('Immutable public GHCR backend image tag or digest.')
param backendImage string

@secure()
@minLength(1)
@description('Complete SQLAlchemy asyncpg database URL. TLS is independently enforced by EVENTHARBOR_DATABASE_SSL_MODE=require.')
param databaseUrl string

@allowed([
  'local'
  'test'
  'staging'
  'production'
])
@description('EventHarbor application environment label. Production activates deployed-runtime safeguards.')
param applicationEnvironment string = 'production'

@description('Backend logging level.')
param logLevel string = 'INFO'

@description('Optional additional resource tags. These override matching standard tag keys.')
param tags object = {}

var normalizedPrefix = toLower(namePrefix)
var normalizedEnvironment = toLower(environmentName)
var migrationJobName = '${normalizedPrefix}-migrate-${normalizedEnvironment}'
var resourceTags = union({
  application: 'EventHarbor'
  environment: normalizedEnvironment
  managedBy: 'Bicep'
  workload: 'public-demo'
}, tags)

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: containerAppsEnvironmentName
}

resource migrationJob 'Microsoft.App/jobs@2026-01-01' = {
  name: migrationJobName
  location: location
  tags: resourceTags
  properties: {
    configuration: {
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaRetryLimit: 0
      replicaTimeout: 600
      secrets: [
        {
          name: 'database-url'
          value: databaseUrl
        }
      ]
      triggerType: 'Manual'
    }
    environmentId: containerAppsEnvironment.id
    template: {
      containers: [
        {
          name: 'migrate'
          image: backendImage
          command: [
            'alembic'
          ]
          args: [
            '-c'
            '/app/alembic.ini'
            'upgrade'
            'head'
          ]
          env: [
            {
              name: 'EVENTHARBOR_DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'EVENTHARBOR_ENVIRONMENT'
              value: applicationEnvironment
            }
            {
              name: 'EVENTHARBOR_DATABASE_SSL_MODE'
              value: 'require'
            }
            {
              name: 'EVENTHARBOR_LOG_LEVEL'
              value: logLevel
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
    }
    workloadProfileName: 'Consumption'
  }
}

output migrationJobName string = migrationJob.name
output migrationJobId string = migrationJob.id
output backendImage string = backendImage
