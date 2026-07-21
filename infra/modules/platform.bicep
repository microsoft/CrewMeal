@description('The azd environment name.')
param environmentName string

@description('Azure region for platform resources.')
param location string

@description('Azure region for the PostgreSQL flexible server (may differ from other resources due to regional offer restrictions).')
param postgresLocation string

@description('Tags applied to all resources.')
param tags object

@description('Stable unique token from the subscription deployment.')
param resourceToken string

@description('PostgreSQL administrator login name.')
param postgresAdminLogin string

@secure()
@description('PostgreSQL administrator password.')
param postgresAdminPassword string

@secure()
@description('Admin API key for the web app.')
param adminKey string

@secure()
@description('Session secret for the web app.')
param sessionSecret string

@secure()
@description('Microsoft 365 app client secret.')
param m365ClientSecret string

@description('Microsoft 365 tenant ID.')
param m365TenantId string

@description('Microsoft 365 app client ID.')
param m365ClientId string

@description('Microsoft 365 site ID.')
param m365SiteId string

@description('Microsoft 365 drive ID.')
param m365DriveId string

@description('Microsoft 365 list ID.')
param m365ListId string

@description('Microsoft 365 site URL.')
param m365SiteUrl string

@description('Microsoft 365 connection ID.')
param m365ConnectionId string

@description('MIP File SDK CLI command for decrypting MIP-protected documents (e.g. a wrapper around the Microsoft MIP File SDK). Empty disables MIP decryption. The M365 service principal must be an Azure RMS super user for unattended decryption to work.')
param mipSdkCli string = ''

@description('Azure RMS OAuth scope requested for the MIP SDK app-only token.')
param mipRmsScope string = 'https://aadrm.com/.default'

@description('Tenant ID expected by ingest authentication.')
param ingestTenantId string

@description('Audience expected by ingest authentication.')
param ingestAudience string

@description('Comma-separated allowed application IDs for ingest authentication.')
param ingestAllowedAppIds string

@description('Comma-separated browser origins allowed to call the ingest API (CORS).')
param ingestAllowedOrigins string

@description('Whether ingest authentication is required.')
param ingestRequireAuth string

@description('Initial container image used before azd deploy updates Container Apps.')
param containerImage string

@description('Foundry account name used for Azure OpenAI data-plane RBAC.')
param foundryAccountName string

@description('Content Understanding endpoint from Foundry.')
param contentUnderstandingEndpoint string

@description('GPT deployment name from Foundry.')
param contentUnderstandingGptDeployment string

@description('Slide-image deployment name from Foundry.')
param slideImageDeployment string

@description('Model label recorded for slide-image token usage.')
param slideImageModel string = 'gpt-5.6-luna'

@description('Model label shown with token-cost estimates.')
param priceModelLabel string = 'gpt-5.6-luna'

@description('Estimated input-token price in USD per million tokens.')
param priceInputUsdPerMillion string = '1'

@description('Estimated output-token price in USD per million tokens.')
param priceOutputUsdPerMillion string = '6'

@description('Embedding deployment name from Foundry.')
param contentUnderstandingEmbeddingDeployment string

@description('Number of slides analyzed concurrently per enhancement job (per-job GPT parallelism). Bounded by the active slide-image deployment TPM capacity.')
param slideImageMaxWorkers int = 4

@description('Maximum worker replicas for cross-job parallelism. KEDA scales out toward this ceiling based on enhancement job queue depth. Keep aligned with the active slide-image deployment TPM capacity.')
@minValue(1)
@maxValue(10)
param workerMaxReplicas int = 3

@description('KEDA target: number of in-flight (queued + processing) jobs per worker replica. 1 = one replica per in-flight job.')
@minValue(1)
param workerQueueScaleTarget int = 1

var sanitizedEnvironmentName = toLower(replace(environmentName, '_', '-'))
var alphaNumericEnvironmentName = replace(replace(sanitizedEnvironmentName, '-', ''), '.', '')
var acrName = take('acrcrewmeal${alphaNumericEnvironmentName}${resourceToken}', 50)
var storageAccountName = take('stcrewmeal${alphaNumericEnvironmentName}${resourceToken}', 24)
var keyVaultName = take('kv-crewmeal-${sanitizedEnvironmentName}-${resourceToken}', 24)
var postgresServerName = take('pg-crewmeal-${sanitizedEnvironmentName}-${resourceToken}-${postgresLocation}', 63)
var logAnalyticsWorkspaceName = take('log-crewmeal-${sanitizedEnvironmentName}-${resourceToken}', 63)
var containerAppsEnvironmentName = take('cae-crewmeal-${sanitizedEnvironmentName}-${resourceToken}', 60)
var webAppName = take('ca-web-${sanitizedEnvironmentName}-${resourceToken}', 32)
var workerAppName = take('ca-worker-${sanitizedEnvironmentName}-${resourceToken}', 32)
var managedIdentityName = take('id-crewmeal-${sanitizedEnvironmentName}-${resourceToken}', 128)
var databaseUrl = 'postgresql+psycopg://${postgresAdminLogin}:${postgresAdminPassword}@${postgresServer.properties.fullyQualifiedDomainName}:5432/crewmeal?sslmode=require'
// URL form without the SQLAlchemy '+psycopg' suffix, for the KEDA postgresql scaler connection string.
var kedaPgConnection = 'postgresql://${postgresAdminLogin}:${postgresAdminPassword}@${postgresServer.properties.fullyQualifiedDomainName}:5432/crewmeal?sslmode=require'

var acrPullRoleDefinitionId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var storageBlobDataContributorRoleDefinitionId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var keyVaultSecretsUserRoleDefinitionId = '4633458b-17de-408a-b874-0445c86b69e6'
var cognitiveServicesOpenAIUserRoleDefinitionId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource mi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
  tags: tags
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource artifactsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'artifacts'
  properties: {
    publicAccess: 'None'
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    sku: {
      family: 'A'
      name: 'standard'
    }
  }
}

resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresServerName
  location: postgresLocation
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    network: {
      publicNetworkAccess: 'Enabled'
    }
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }
}

resource postgresFirewallAllowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgresServer
  name: 'AllowAllAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgresServer
  name: 'crewmeal'
}

resource m365ClientSecretSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'm365-client-secret'
  properties: {
    value: m365ClientSecret
  }
}

resource adminKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'admin-key'
  properties: {
    value: adminKey
  }
}

resource sessionSecretSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'session-secret'
  properties: {
    value: sessionSecret
  }
}

resource postgresAdminPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'postgres-admin-password'
  properties: {
    value: postgresAdminPassword
  }
}

resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'database-url'
  properties: {
    value: databaseUrl
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryAccountName
}

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, mi.id, acrPullRoleDefinitionId)
  scope: acr
  properties: {
    principalId: mi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleDefinitionId)
  }
}

resource storageBlobDataContributorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, mi.id, storageBlobDataContributorRoleDefinitionId)
  scope: storageAccount
  properties: {
    principalId: mi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleDefinitionId)
  }
}

resource keyVaultSecretsUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, mi.id, keyVaultSecretsUserRoleDefinitionId)
  scope: keyVault
  properties: {
    principalId: mi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleDefinitionId)
  }
}

resource foundryOpenAIUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, mi.id, cognitiveServicesOpenAIUserRoleDefinitionId)
  scope: foundryAccount
  properties: {
    principalId: mi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUserRoleDefinitionId)
  }
}

var commonSecrets = [
  {
    name: 'database-url'
    value: databaseUrl
  }
  {
    name: 'm365-client-secret'
    value: m365ClientSecret
  }
]

var commonEnv = [
  {
    name: 'AZURE_CLIENT_ID'
    value: mi.properties.clientId
  }
  {
    name: 'DATABASE_URL'
    secretRef: 'database-url'
  }
  {
    name: 'CREWMEAL_ARTIFACT_BACKEND'
    value: 'database'
  }
  {
    name: 'CREWMEAL_ARTIFACT_DIR'
    value: '/tmp/crewmeal-artifacts'
  }
  {
    name: 'CREWMEAL_M365_TENANT_ID'
    value: m365TenantId
  }
  {
    name: 'CREWMEAL_M365_CLIENT_ID'
    value: m365ClientId
  }
  {
    name: 'CREWMEAL_M365_CLIENT_SECRET'
    secretRef: 'm365-client-secret'
  }
  {
    name: 'CREWMEAL_M365_SITE_ID'
    value: m365SiteId
  }
  {
    name: 'CREWMEAL_M365_DRIVE_ID'
    value: m365DriveId
  }
  {
    name: 'CREWMEAL_M365_LIST_ID'
    value: m365ListId
  }
  {
    name: 'CREWMEAL_M365_SITE_URL'
    value: m365SiteUrl
  }
  {
    name: 'CREWMEAL_M365_CONNECTION_ID'
    value: m365ConnectionId
  }
  {
    // MIP decryption: unattended shell-out to the MIP SDK CLI. Requires the
    // M365 service principal above to be granted Azure RMS super-user rights so
    // it can decrypt any protected content in the tenant. Empty = disabled.
    name: 'CREWMEAL_MIP_SDK_CLI'
    value: mipSdkCli
  }
  {
    name: 'CREWMEAL_MIP_RMS_SCOPE'
    value: mipRmsScope
  }
  {
    name: 'SLIDE_IMAGE_MODEL'
    value: slideImageModel
  }
  {
    name: 'SLIDE_IMAGE_DEPLOYMENT'
    value: slideImageDeployment
  }
  {
    name: 'CREWMEAL_PRICE_MODEL_LABEL'
    value: priceModelLabel
  }
  {
    name: 'CREWMEAL_PRICE_INPUT_USD_PER_M'
    value: priceInputUsdPerMillion
  }
  {
    name: 'CREWMEAL_PRICE_OUTPUT_USD_PER_M'
    value: priceOutputUsdPerMillion
  }
]

resource webApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: webAppName
  location: location
  tags: union(tags, {
    'azd-service-name': 'web'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${mi.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: mi.id
        }
      ]
      secrets: concat(commonSecrets, [
        {
          name: 'admin-key'
          value: adminKey
        }
        {
          name: 'session-secret'
          value: sessionSecret
        }
      ])
    }
    template: {
      containers: [
        {
          name: 'web'
          image: containerImage
          env: concat(commonEnv, [
            {
              name: 'APP_ROLE'
              value: 'web'
            }
            {
              name: 'PORT'
              value: '8000'
            }
            {
              name: 'CREWMEAL_WEB_BASE_URL'
              value: 'https://${webAppName}.${containerAppsEnvironment.properties.defaultDomain}'
            }
            {
              name: 'CREWMEAL_ADMIN_KEY'
              secretRef: 'admin-key'
            }
            {
              name: 'CREWMEAL_WEB_SESSION_SECRET'
              secretRef: 'session-secret'
            }
            {
              name: 'CREWMEAL_INGEST_TENANT_ID'
              value: ingestTenantId
            }
            {
              name: 'CREWMEAL_INGEST_AUDIENCE'
              value: ingestAudience
            }
            {
              name: 'CREWMEAL_INGEST_ALLOWED_APP_IDS'
              value: ingestAllowedAppIds
            }
            {
              name: 'CREWMEAL_INGEST_ALLOWED_ORIGINS'
              value: ingestAllowedOrigins
            }
            {
              name: 'CREWMEAL_INGEST_REQUIRE_AUTH'
              value: ingestRequireAuth
            }
            {
              name: 'CREWMEAL_STATUS_REQUIRE_AUTH'
              value: 'true'
            }
          ])
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 2
      }
    }
  }
  dependsOn: [
    acrPullRoleAssignment
    keyVaultSecretsUserRoleAssignment
    storageBlobDataContributorRoleAssignment
  ]
}

resource workerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: workerAppName
  location: location
  tags: union(tags, {
    'azd-service-name': 'worker'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${mi.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: acr.properties.loginServer
          identity: mi.id
        }
      ]
      secrets: concat(commonSecrets, [
        {
          name: 'pg-keda-connection'
          value: kedaPgConnection
        }
      ])
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: containerImage
          env: concat(commonEnv, [
            {
              name: 'APP_ROLE'
              value: 'worker'
            }
            {
              name: 'CONTENTUNDERSTANDING_ENDPOINT'
              value: contentUnderstandingEndpoint
            }
            {
              name: 'CONTENTUNDERSTANDING_ANALYZER_ID'
              value: 'prebuilt-documentSearch'
            }
            {
              name: 'CONTENTUNDERSTANDING_API_VERSION'
              value: '2025-11-01'
            }
            {
              name: 'CONTENTUNDERSTANDING_GPT_DEPLOYMENT'
              value: contentUnderstandingGptDeployment
            }
            {
              name: 'CONTENTUNDERSTANDING_EMBEDDING_DEPLOYMENT'
              value: contentUnderstandingEmbeddingDeployment
            }
            {
              name: 'SLIDE_IMAGE_MAX_WORKERS'
              value: string(slideImageMaxWorkers)
            }
          ])
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: workerMaxReplicas
        rules: [
          {
            name: 'job-queue-depth'
            custom: {
              type: 'postgresql'
              metadata: {
                query: 'SELECT count(*) FROM jobs WHERE status IN (\'queued\', \'processing\')'
                targetQueryValue: string(workerQueueScaleTarget)
                activationTargetQueryValue: '0'
              }
              auth: [
                {
                  secretRef: 'pg-keda-connection'
                  triggerParameter: 'connection'
                }
              ]
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    acrPullRoleAssignment
    keyVaultSecretsUserRoleAssignment
    storageBlobDataContributorRoleAssignment
    foundryOpenAIUserRoleAssignment
  ]
}

output managedIdentityClientId string = mi.properties.clientId
output managedIdentityPrincipalId string = mi.properties.principalId
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output webFqdn string = '${webAppName}.${containerAppsEnvironment.properties.defaultDomain}'
output keyVaultName string = keyVault.name
output postgresHost string = postgresServer.properties.fullyQualifiedDomainName
output postgresServerName string = postgresServer.name
output containerAppsEnvironmentId string = containerAppsEnvironment.id
