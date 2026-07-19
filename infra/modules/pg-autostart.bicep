@description('Name of the existing PostgreSQL flexible server to auto-start each morning.')
param postgresServerName string

@description('Azure region for the Logic App. The management-plane start call itself is region-agnostic.')
param location string = resourceGroup().location

@description('Tags applied to the Logic App.')
param tags object = {}

@description('Local hour (Korea Standard Time, 0-23) at which the server is started every morning. The nightly cost automation stops it ~00:08 KST; this restarts it before work hours so the self-recovering worker resumes automatically.')
@minValue(0)
@maxValue(23)
param startHourKst int = 7

@description('Name for the scheduler Logic App.')
param logicAppName string = take('logic-pgautostart-${uniqueString(resourceGroup().id, postgresServerName)}', 80)

// Contributor — the only built-in role that grants flexibleServers/start/action.
// Scoped to the single PostgreSQL server resource (least privilege for a built-in role).
var contributorRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')

resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: postgresServerName
}

resource startWorkflow 'Microsoft.Logic/workflows@2019-05-01' = {
  name: logicAppName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      parameters: {}
      triggers: {
        DailyMorningKst: {
          type: 'Recurrence'
          recurrence: {
            frequency: 'Day'
            interval: 1
            timeZone: 'Korea Standard Time'
            schedule: {
              hours: [
                startHourKst
              ]
              minutes: [
                0
              ]
            }
          }
        }
      }
      actions: {
        StartPostgres: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: '${environment().resourceManager}subscriptions/${subscription().subscriptionId}/resourceGroups/${resourceGroup().name}/providers/Microsoft.DBforPostgreSQL/flexibleServers/${postgresServerName}/start?api-version=2024-08-01'
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: environment().resourceManager
            }
          }
        }
      }
    }
  }
}

resource startRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(postgresServer.id, startWorkflow.id, 'pg-autostart-contributor')
  scope: postgresServer
  properties: {
    roleDefinitionId: contributorRoleDefinitionId
    principalId: startWorkflow.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output logicAppName string = startWorkflow.name
output logicAppPrincipalId string = startWorkflow.identity.principalId
