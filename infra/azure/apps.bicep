targetScope = 'resourceGroup'

metadata name = 'EventHarbor Container Apps'
metadata description = 'Public web app, internal API and Receiver Lab, durable delivery worker, and scheduled retention cleanup.'

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
@description('Immutable public GHCR frontend image tag or digest.')
param frontendImage string

@minLength(1)
@description('Immutable public GHCR backend image tag or digest, shared by the API, worker, and Receiver Lab.')
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

@minValue(0)
param webMinReplicas int = 0

@minValue(1)
param webMaxReplicas int = 1

@minValue(0)
param apiMinReplicas int = 1

@minValue(1)
param apiMaxReplicas int = 2

@minValue(1)
param workerMinReplicas int = 1

@minValue(1)
param workerMaxReplicas int = 1

@minValue(0)
param receiverMinReplicas int = 0

@minValue(1)
param receiverMaxReplicas int = 1

@minValue(1)
@maxValue(100)
@description('Accelerated public-demo retry budget. The production application default is higher.')
param workerMaxAttempts int = 4

@minValue(1)
@description('Accelerated public-demo base retry delay in seconds.')
param workerBaseDelaySeconds int = 1

@minValue(1)
@description('Accelerated public-demo maximum retry delay in seconds.')
param workerMaxDelaySeconds int = 2

@minValue(1)
@description('Accelerated public-demo Retry-After cap in seconds.')
param workerRetryAfterCapSeconds int = 2

@description('Optional additional resource tags. These override matching standard tag keys.')
param tags object = {}

var normalizedPrefix = toLower(namePrefix)
var normalizedEnvironment = toLower(environmentName)
var webAppName = '${normalizedPrefix}-web-${normalizedEnvironment}'
var apiAppName = '${normalizedPrefix}-api-${normalizedEnvironment}'
var workerAppName = '${normalizedPrefix}-worker-${normalizedEnvironment}'
var receiverAppName = '${normalizedPrefix}-receiver-${normalizedEnvironment}'
var cleanupJobName = '${normalizedPrefix}-cleanup-${normalizedEnvironment}'
var apiInternalOrigin = 'http://${apiAppName}'
var receiverInternalOrigin = 'http://${receiverAppName}'
var receiverWebhookBaseUrl = '${receiverInternalOrigin}/webhooks'
var resourceTags = union({
  application: 'EventHarbor'
  environment: normalizedEnvironment
  managedBy: 'Bicep'
  workload: 'public-demo'
}, tags)

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: containerAppsEnvironmentName
}

resource receiverApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: receiverAppName
  location: location
  tags: resourceTags
  properties: {
    configuration: {
      activeRevisionsMode: 'Single'
      maxInactiveRevisions: 3
      ingress: {
        allowInsecure: false
        external: false
        targetPort: 8100
        transport: 'auto'
      }
    }
    environmentId: containerAppsEnvironment.id
    template: {
      containers: [
        {
          name: 'receiver'
          image: backendImage
          command: [
            'uvicorn'
          ]
          args: [
            'eventharbor.receiver_lab.api:app'
            '--host'
            '0.0.0.0'
            '--port'
            '8100'
          ]
          env: [
            {
              name: 'EVENTHARBOR_ENVIRONMENT'
              value: applicationEnvironment
            }
            {
              name: 'EVENTHARBOR_LOG_LEVEL'
              value: logLevel
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: 8100
                scheme: 'HTTP'
              }
              initialDelaySeconds: 1
              periodSeconds: 2
              timeoutSeconds: 2
              failureThreshold: 10
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8100
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 15
              timeoutSeconds: 3
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8100
                scheme: 'HTTP'
              }
              initialDelaySeconds: 3
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: receiverMinReplicas
        maxReplicas: receiverMaxReplicas
        rules: [
          {
            name: 'receiver-http'
            http: {
              metadata: {
                concurrentRequests: '25'
              }
            }
          }
        ]
      }
      terminationGracePeriodSeconds: 30
    }
    workloadProfileName: 'Consumption'
  }
}

resource apiApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: apiAppName
  location: location
  tags: resourceTags
  properties: {
    configuration: {
      activeRevisionsMode: 'Single'
      maxInactiveRevisions: 3
      ingress: {
        allowInsecure: false
        external: false
        targetPort: 8000
        transport: 'auto'
      }
      secrets: [
        {
          name: 'database-url'
          value: databaseUrl
        }
      ]
    }
    environmentId: containerAppsEnvironment.id
    template: {
      containers: [
        {
          name: 'api'
          image: backendImage
          command: [
            'uvicorn'
          ]
          args: [
            'eventharbor.api:app'
            '--host'
            '0.0.0.0'
            '--port'
            '8000'
          ]
          env: [
            {
              name: 'EVENTHARBOR_ENVIRONMENT'
              value: applicationEnvironment
            }
            {
              name: 'EVENTHARBOR_DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'EVENTHARBOR_DATABASE_SSL_MODE'
              value: 'require'
            }
            {
              name: 'EVENTHARBOR_DATABASE_POOL_SIZE'
              value: '3'
            }
            {
              name: 'EVENTHARBOR_DATABASE_MAX_OVERFLOW'
              value: '2'
            }
            {
              name: 'EVENTHARBOR_RECEIVER_LAB_URL'
              value: receiverWebhookBaseUrl
            }
            {
              name: 'EVENTHARBOR_RECEIVER_LAB_CONTROL_URL'
              value: receiverInternalOrigin
            }
            {
              name: 'EVENTHARBOR_LOG_LEVEL'
              value: logLevel
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 1
              periodSeconds: 2
              timeoutSeconds: 2
              failureThreshold: 10
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 15
              timeoutSeconds: 3
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/ready'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 3
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: apiMinReplicas
        maxReplicas: apiMaxReplicas
        rules: [
          {
            name: 'api-http'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
      terminationGracePeriodSeconds: 30
    }
    workloadProfileName: 'Consumption'
  }
  dependsOn: [
    receiverApp
  ]
}

resource workerApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: workerAppName
  location: location
  tags: resourceTags
  properties: {
    configuration: {
      activeRevisionsMode: 'Single'
      maxInactiveRevisions: 3
      secrets: [
        {
          name: 'database-url'
          value: databaseUrl
        }
      ]
    }
    environmentId: containerAppsEnvironment.id
    template: {
      containers: [
        {
          name: 'worker'
          image: backendImage
          command: [
            'python'
          ]
          args: [
            '-m'
            'eventharbor.deliveries.worker'
          ]
          env: [
            {
              name: 'EVENTHARBOR_ENVIRONMENT'
              value: applicationEnvironment
            }
            {
              name: 'EVENTHARBOR_DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'EVENTHARBOR_DATABASE_SSL_MODE'
              value: 'require'
            }
            {
              name: 'EVENTHARBOR_DATABASE_POOL_SIZE'
              value: '3'
            }
            {
              name: 'EVENTHARBOR_DATABASE_MAX_OVERFLOW'
              value: '2'
            }
            {
              name: 'EVENTHARBOR_RECEIVER_LAB_URL'
              value: receiverWebhookBaseUrl
            }
            {
              name: 'EVENTHARBOR_WORKER_MAX_ATTEMPTS'
              value: string(workerMaxAttempts)
            }
            {
              name: 'EVENTHARBOR_WORKER_BASE_DELAY_SECONDS'
              value: string(workerBaseDelaySeconds)
            }
            {
              name: 'EVENTHARBOR_WORKER_MAX_DELAY_SECONDS'
              value: string(workerMaxDelaySeconds)
            }
            {
              name: 'EVENTHARBOR_WORKER_RETRY_AFTER_CAP_SECONDS'
              value: string(workerRetryAfterCapSeconds)
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
      scale: {
        minReplicas: workerMinReplicas
        maxReplicas: workerMaxReplicas
      }
      terminationGracePeriodSeconds: 60
    }
    workloadProfileName: 'Consumption'
  }
  dependsOn: [
    receiverApp
  ]
}

resource cleanupJob 'Microsoft.App/jobs@2026-01-01' = {
  name: cleanupJobName
  location: location
  tags: resourceTags
  properties: {
    configuration: {
      replicaRetryLimit: 1
      replicaTimeout: 600
      scheduleTriggerConfig: {
        cronExpression: '0 4 * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: [
        {
          name: 'database-url'
          value: databaseUrl
        }
      ]
      triggerType: 'Schedule'
    }
    environmentId: containerAppsEnvironment.id
    template: {
      containers: [
        {
          name: 'cleanup'
          image: backendImage
          command: [
            'python'
          ]
          args: [
            '-m'
            'eventharbor.maintenance'
          ]
          env: [
            {
              name: 'EVENTHARBOR_ENVIRONMENT'
              value: applicationEnvironment
            }
            {
              name: 'EVENTHARBOR_DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'EVENTHARBOR_DATABASE_SSL_MODE'
              value: 'require'
            }
            {
              name: 'EVENTHARBOR_DATABASE_POOL_SIZE'
              value: '2'
            }
            {
              name: 'EVENTHARBOR_DATABASE_MAX_OVERFLOW'
              value: '0'
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

resource webApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: webAppName
  location: location
  tags: resourceTags
  properties: {
    configuration: {
      activeRevisionsMode: 'Single'
      maxInactiveRevisions: 3
      ingress: {
        allowInsecure: false
        external: true
        targetPort: 8080
        transport: 'auto'
      }
    }
    environmentId: containerAppsEnvironment.id
    template: {
      containers: [
        {
          name: 'web'
          image: frontendImage
          env: [
            {
              name: 'API_UPSTREAM'
              value: apiInternalOrigin
            }
            {
              name: 'NGINX_ENVSUBST_FILTER'
              value: 'API_UPSTREAM'
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 1
              periodSeconds: 2
              timeoutSeconds: 2
              failureThreshold: 10
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 15
              timeoutSeconds: 3
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 3
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: webMinReplicas
        maxReplicas: webMaxReplicas
        rules: [
          {
            name: 'web-http'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
      terminationGracePeriodSeconds: 30
    }
    workloadProfileName: 'Consumption'
  }
  dependsOn: [
    apiApp
  ]
}

output webAppName string = webApp.name
output webAppFqdn string = webApp.properties.configuration.ingress.fqdn
output webAppUrl string = 'https://${webApp.properties.configuration.ingress.fqdn}'
output apiAppName string = apiApp.name
output apiInternalOrigin string = apiInternalOrigin
output receiverAppName string = receiverApp.name
output receiverInternalOrigin string = receiverInternalOrigin
output receiverWebhookBaseUrl string = receiverWebhookBaseUrl
output workerAppName string = workerApp.name
output cleanupJobName string = cleanupJob.name
output frontendImage string = frontendImage
output backendImage string = backendImage
