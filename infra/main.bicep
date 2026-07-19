targetScope = 'subscription'

@description('The azd environment name.')
@minLength(1)
param environmentName string

@description('The Azure region for the PoC.')
@allowed([
  'eastus2'
])
param location string = 'eastus2'

@description('The object ID that receives Content Understanding data-plane access.')
param principalId string

@description('The principal type for the Content Understanding role assignment.')
@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param principalType string = 'User'

@description('PostgreSQL administrator login name.')
param postgresAdminLogin string = 'crewmealadmin'

@description('Azure region for the PostgreSQL flexible server. Defaults to centralus because eastus2 is offer-restricted for PostgreSQL flexible servers on some subscriptions.')
param postgresLocation string = 'centralus'

@description('Local hour (Korea Standard Time, 0-23) at which a Logic App restarts the PostgreSQL server each morning, so the self-recovering worker resumes after the nightly cost auto-stop.')
@minValue(0)
@maxValue(23)
param postgresAutoStartHourKst int = 7

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

@description('Tenant ID expected by ingest authentication.')
param ingestTenantId string = m365TenantId

@description('Audience expected by ingest authentication.')
param ingestAudience string

@description('Comma-separated allowed application IDs for ingest authentication.')
param ingestAllowedAppIds string = ''

@description('Comma-separated browser origins allowed to call the ingest API (CORS).')
param ingestAllowedOrigins string = ''

@description('Whether ingest authentication is required.')
param ingestRequireAuth string = 'true'

@description('Initial container image used before azd deploy updates Container Apps.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Tags applied to all Azure resources.')
param tags object = {
  'azd-env-name': environmentName
  application: 'crewmeal'
  purpose: 'content-understanding-poc'
}

var locationAbbreviation = 'eus2'
var resourceGroupName = 'rg-crewmeal-ppt-${environmentName}-${locationAbbreviation}'
var resourceToken = uniqueString(subscription().id, environmentName, location)
var foundryAccountName = toLower('aif-crewmeal-${environmentName}-${resourceToken}')

resource resourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  scope: resourceGroup
  params: {
    name: foundryAccountName
    location: location
    tags: tags
    accessPrincipalId: principalId
    accessPrincipalType: principalType
  }
}

module platform 'modules/platform.bicep' = {
  name: 'platform'
  scope: resourceGroup
  params: {
    environmentName: environmentName
    location: location
    postgresLocation: postgresLocation
    tags: tags
    resourceToken: resourceToken
    postgresAdminLogin: postgresAdminLogin
    postgresAdminPassword: postgresAdminPassword
    adminKey: adminKey
    sessionSecret: sessionSecret
    m365ClientSecret: m365ClientSecret
    m365TenantId: m365TenantId
    m365ClientId: m365ClientId
    m365SiteId: m365SiteId
    m365DriveId: m365DriveId
    m365ListId: m365ListId
    m365SiteUrl: m365SiteUrl
    m365ConnectionId: m365ConnectionId
    ingestTenantId: ingestTenantId
    ingestAudience: ingestAudience
    ingestAllowedAppIds: ingestAllowedAppIds
    ingestAllowedOrigins: ingestAllowedOrigins
    ingestRequireAuth: ingestRequireAuth
    containerImage: containerImage
    foundryAccountName: foundry.outputs.name
    contentUnderstandingEndpoint: foundry.outputs.endpoint
    contentUnderstandingGptDeployment: foundry.outputs.gptDeploymentName
    slideImageDeployment: foundry.outputs.lunaDeploymentName
    slideImageModel: 'gpt-5.6-luna'
    contentUnderstandingEmbeddingDeployment: foundry.outputs.embeddingDeploymentName
  }
}

module pgAutoStart 'modules/pg-autostart.bicep' = {
  name: 'pgAutoStart'
  scope: resourceGroup
  params: {
    postgresServerName: platform.outputs.postgresServerName
    location: location
    tags: tags
    startHourKst: postgresAutoStartHourKst
  }
}

output AZURE_RESOURCE_GROUP string = resourceGroup.name
output AZURE_AI_FOUNDRY_NAME string = foundry.outputs.name
output AZURE_AI_FOUNDRY_RESOURCE_ID string = foundry.outputs.resourceId
output CONTENTUNDERSTANDING_ENDPOINT string = foundry.outputs.endpoint
output CONTENTUNDERSTANDING_ANALYZER_ID string = 'prebuilt-documentSearch'
output CONTENTUNDERSTANDING_API_VERSION string = '2025-11-01'
output CONTENTUNDERSTANDING_GPT_DEPLOYMENT string = foundry.outputs.gptDeploymentName
output SLIDE_IMAGE_DEPLOYMENT string = foundry.outputs.lunaDeploymentName
output SLIDE_IMAGE_MODEL string = 'gpt-5.6-luna'
output CONTENTUNDERSTANDING_EMBEDDING_DEPLOYMENT string = foundry.outputs.embeddingDeploymentName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = platform.outputs.acrLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = platform.outputs.acrName
output SERVICE_WEB_URI string = 'https://${platform.outputs.webFqdn}'
output AZURE_KEY_VAULT_NAME string = platform.outputs.keyVaultName
output POSTGRES_HOST string = platform.outputs.postgresHost
output AZURE_CONTAINER_APPS_ENVIRONMENT_ID string = platform.outputs.containerAppsEnvironmentId
output AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID string = platform.outputs.managedIdentityClientId
output AZURE_USER_ASSIGNED_IDENTITY_PRINCIPAL_ID string = platform.outputs.managedIdentityPrincipalId
