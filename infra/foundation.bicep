targetScope = 'resourceGroup'

@minLength(2)
@maxLength(12)
param prefix string
@minLength(2)
@maxLength(8)
param environment string = 'dev'
param location string = resourceGroup().location
@description('Existing Microsoft Foundry account ARM resource ID, resolved by the deployment script from the project endpoint.')
param openAiResourceId string

var stem = '${prefix}-${environment}'
var suffix = uniqueString(resourceGroup().id, prefix, environment)
var agents = ['maf', 'langchain']
var acrPullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acr${suffix}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${stem}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acaEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${stem}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for agent in agents: {
  name: '${stem}-${agent}-identity'
  location: location
}]

resource registryRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (agent, i) in agents: {
  name: guid(registry.id, identities[i].id, acrPullRole)
  scope: registry
  properties: {
    roleDefinitionId: acrPullRole
    principalId: identities[i].properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

var modelIdParts = split(openAiResourceId, '/')
module modelAccess 'modules/model-access.bicep' = {
  name: '${stem}-model-access'
  scope: resourceGroup(modelIdParts[2], modelIdParts[4])
  params: {
    accountName: modelIdParts[8]
    principalIds: [for (agent, i) in agents: identities[i].properties.principalId]
  }
}

output registryName string = registry.name
output registryServer string = registry.properties.loginServer
output environmentName string = acaEnvironment.name
output logWorkspaceId string = logs.id
output tenantId string = tenant().tenantId
output agents array = [for (agent, i) in agents: {
  name: agent
  identityName: identities[i].name
  identityId: identities[i].id
  clientId: identities[i].properties.clientId
}]
