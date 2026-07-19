@description('The globally unique Microsoft Foundry account name.')
param name string

@description('The Azure region for the account and model deployments.')
param location string

@description('Tags applied to the account.')
param tags object

@description('The object ID that receives Content Understanding data-plane access.')
param accessPrincipalId string

@description('The principal type for the role assignment.')
param accessPrincipalType string

@description('The GPT model deployment name used by Content Understanding.')
param gptDeploymentName string = 'gpt-5-2'

@description('The GPT-5.6 Luna deployment used for production slide-image analysis.')
param lunaDeploymentName string = 'gpt-5-6-luna-test'

@description('The GPT-5-mini deployment name used for slide-image comparison.')
param gptMiniDeploymentName string = 'gpt-5-mini'

@description('The embedding model deployment name used by Content Understanding.')
param embeddingDeploymentName string = 'text-embedding-3-large'

@description('TPM capacity (GlobalStandard units, 1 unit = 1K TPM) for the primary GPT deployment.')
param gptCapacity int = 500

@description('TPM capacity (GlobalStandard units, 1 unit = 1K TPM) for GPT-5.6 Luna.')
param lunaCapacity int = 500

@description('TPM capacity (GlobalStandard units, 1 unit = 1K TPM) for the embedding deployment.')
param embeddingCapacity int = 50

var contentUnderstandingContributorRoleDefinitionId = '59a2dba3-6303-4fd8-9a2e-8cbb4bdda972'
var cognitiveServicesOpenAIUserRoleDefinitionId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

module account 'br/public:avm/res/cognitive-services/account:0.15.0' = {
  name: 'foundry-account'
  params: {
    name: name
    kind: 'AIServices'
    location: location
    tags: tags
    sku: 'S0'
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    restrictOutboundNetworkAccess: false
    allowProjectManagement: true
    managedIdentities: {
      systemAssigned: true
    }
    deployments: [
      {
        name: gptDeploymentName
        model: {
          format: 'OpenAI'
          name: 'gpt-5.2'
          version: '2025-12-11'
        }
        sku: {
          name: 'GlobalStandard'
          capacity: gptCapacity
        }
        versionUpgradeOption: 'NoAutoUpgrade'
      }
      {
        name: gptMiniDeploymentName
        model: {
          format: 'OpenAI'
          name: 'gpt-5-mini'
          version: '2025-08-07'
        }
        sku: {
          name: 'GlobalStandard'
          capacity: 100
        }
        versionUpgradeOption: 'NoAutoUpgrade'
      }
      {
        name: lunaDeploymentName
        model: {
          format: 'OpenAI'
          name: 'gpt-5.6-luna'
          version: '2026-07-09'
        }
        sku: {
          name: 'GlobalStandard'
          capacity: lunaCapacity
        }
        versionUpgradeOption: 'NoAutoUpgrade'
      }
      {
        name: embeddingDeploymentName
        model: {
          format: 'OpenAI'
          name: 'text-embedding-3-large'
          version: '1'
        }
        sku: {
          name: 'GlobalStandard'
          capacity: embeddingCapacity
        }
        versionUpgradeOption: 'NoAutoUpgrade'
      }
    ]
    roleAssignments: [
      {
        principalId: accessPrincipalId
        principalType: accessPrincipalType
        roleDefinitionIdOrName: contentUnderstandingContributorRoleDefinitionId
        description: 'Allows the local PoC operator to configure and run Content Understanding.'
      }
      {
        principalId: accessPrincipalId
        principalType: accessPrincipalType
        roleDefinitionIdOrName: cognitiveServicesOpenAIUserRoleDefinitionId
        description: 'Allows the local PoC operator to analyze rendered slide images with Azure OpenAI.'
      }
    ]
  }
}

output name string = account.outputs.name
output resourceId string = account.outputs.resourceId
output endpoint string = account.outputs.endpoint
output gptDeploymentName string = gptDeploymentName
output lunaDeploymentName string = lunaDeploymentName
output gptMiniDeploymentName string = gptMiniDeploymentName
output embeddingDeploymentName string = embeddingDeploymentName
